"""需求文档入库与分块流程：解析、逐页检查点、视觉转写、分块。"""

from pathlib import Path
from typing import Any, Callable

from tc_rag.application.ports.contracts import (
    DocumentParser,
    PageOutcome,
    ParseState,
    Repository,
)
from tc_rag.application.services.chunking import TokenCounter, structural_chunks
from tc_rag.application.services.vision import VisionEvidenceService
from tc_rag.core.config import AppConfig
from tc_rag.domain import models as m
from tc_rag.domain.pipeline import StageSnapshot


class DocumentIngestHandler:
    """负责解析、逐页检查点、视觉转写与分块。

    阶段事件（stage 计时）与页面检查点（snapshot）通过构造时注入的
    回调交给编排层记录，本类只做入库相关的具体工作，不接管运行生命周期。
    """

    def __init__(
        self,
        config: AppConfig,
        repository: Repository,
        parser: Callable[[str], DocumentParser],
        counter: TokenCounter,
        *,
        vision: VisionEvidenceService | None = None,
        stage: Callable[[str, str], None] | None = None,
        snapshot: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.parser = parser
        self.counter = counter
        self.vision = vision
        self._stage = stage or (lambda run_id, name: None)
        self.snapshot = snapshot or (lambda run_id, name, payload: None)

    def _chunk(self, blocks: list[m.DocumentBlock]) -> tuple[list[m.Chunk], list[m.Issue]]:
        chunked = structural_chunks(
            blocks,
            self.config.chunking.target_tokens,
            self.config.chunking.overlap_tokens,
            self.counter,
            attach_headings=self.config.chunking.attach_headings,
            min_tokens=self.config.chunking.min_tokens,
            split_long_paragraphs=self.config.chunking.split_long_paragraphs,
            strategy=self.config.chunking.strategy,
        )
        return chunked.chunks, chunked.issues

    @staticmethod
    def _chunk_payload(
        doc: m.Document,
        blocks: list[m.DocumentBlock],
        chunks: list[m.Chunk],
        issues: list[m.Issue],
    ) -> dict[str, Any]:
        return {
            "document": doc.model_dump(mode="json"),
            "blocks": [b.model_dump(mode="json") for b in blocks],
            "chunks": [c.model_dump(mode="json") for c in chunks],
            "issues": [i.model_dump(mode="json") for i in issues],
        }

    async def _parse_document(
        self,
        run_id: str,
        document: Path,
        asset: m.Asset,
        resume_cache_run_id: str | None,
    ) -> tuple[list[m.DocumentBlock], list[m.Asset], list[m.Issue]]:
        parser = self.parser(document.suffix.lower())
        session_factory = getattr(parser, "session", None)
        if document.suffix.lower() == ".pdf" and callable(session_factory):
            return await self._parse_pdf_pages(run_id, session_factory, asset, resume_cache_run_id)
        parsed = parser.parse(asset)
        # 图片解释由 _apply_vision 在独立阶段执行，解析阶段只做抽取与定位。
        return list(parsed.blocks), list(parsed.assets), list(parsed.issues)

    async def _apply_vision(
        self, run_id: str, blocks: list[m.DocumentBlock]
    ) -> tuple[list[m.DocumentBlock], list[m.Issue]]:
        """图片解释单独计时，避免视觉耗时被算进解析阶段。"""
        if not self.config.parsing.vision_enabled or not blocks:
            return blocks, []
        if not any(block.kind == "image" for block in blocks):
            return blocks, []
        if self.vision is None:
            raise ValueError("启用图片解释但缺少视觉适配器")
        self._stage(run_id, "vision")
        return await self.vision.process(blocks)

    async def _parse_pdf_pages(
        self,
        run_id: str,
        session_factory: Callable[[m.Asset], Any],
        asset: m.Asset,
        resume_cache_run_id: str | None,
    ) -> tuple[list[m.DocumentBlock], list[m.Asset], list[m.Issue]]:
        """逐页解析并写检查点；恢复时从已完成页继续，已转写图片命中视觉缓存。

        视觉处理放在所有页解析完成之后统一执行，避免每页图片数少时跨页并发失效。
        """
        blocks: list[m.DocumentBlock] = []
        assets: list[m.Asset] = []
        parse_issues: list[m.Issue] = []
        with session_factory(asset) as session:
            state = ParseState(heading_sizes=list(session.heading_sizes))
            for page_index in range(session.page_count):
                page_no = page_index + 1
                current = self.repository.get(
                    "stage_snapshots", f"{run_id}:pdf_page:{page_no}", StageSnapshot
                )
                parent = None
                if current is None and resume_cache_run_id:
                    parent = self.repository.get(
                        "stage_snapshots",
                        f"{resume_cache_run_id}:pdf_page:{page_no}",
                        StageSnapshot,
                    )
                restored = current if current is not None else parent
                if restored is not None:
                    outcome = PageOutcome.from_payload(restored.payload)
                    if current is None:
                        # 复用父运行的页结果时同步写入本次运行，便于再次恢复。
                        self.snapshot(run_id, f"pdf_page:{page_no}", outcome.to_payload())
                else:
                    outcome = session.parse_page(page_index, state)
                    self.snapshot(run_id, f"pdf_page:{page_no}", outcome.to_payload())
                blocks.extend(outcome.blocks)
                assets.extend(outcome.assets)
                parse_issues.extend(outcome.issues)
                state = outcome.state
            # 在完整页面序列上后处理，恢复页也参与重复判断；不修改原页检查点。
            blocks, running_issues = session.finalize(blocks)
            parse_issues.extend(running_issues)
        if state.font_warnings:
            parse_issues.append(
                m.Issue(
                    issue_id=f"doc_{asset.checksum}_font_bbox",
                    kind="unparsed",
                    severity="warning",
                    object_ids=["doc_" + asset.checksum],
                    description=(
                        f"PDF字体边界信息缺失警告共{state.font_warnings}次，已合并重复终端提示；"
                        "正文解析继续，表格及文字位置需人工核对"
                    ),
                )
            )
        # 图片解释由 _apply_vision 在独立阶段执行；这里只恢复全局阅读顺序。
        ordered = [block.model_copy(update={"order": i}) for i, block in enumerate(blocks)]
        return ordered, assets, parse_issues
