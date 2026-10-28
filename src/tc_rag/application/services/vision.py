"""把视觉解释作为独立来源的派生 Block，原始图片定位始终保留。"""

import asyncio
from typing import Any

from tc_rag.application.ports.contracts import ArtifactStore, Repository, VisionReader
from tc_rag.application.ports.errors import VisionReadError
from tc_rag.application.progress import ProgressEvent, ProgressReporter, report
from tc_rag.application.services.design import issue
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import Asset, DocumentBlock, Issue
from tc_rag.domain.rag import RagRecord


class VisionEvidenceService:
    def __init__(
        self,
        repository: Repository,
        store: ArtifactStore,
        reader: VisionReader,
        identity: dict[str, Any],
        cache_enabled: bool = True,
        *,
        concurrency: int = 4,
    ):
        self.repository, self.store, self.reader, self.identity = (
            repository,
            store,
            reader,
            identity,
        )
        self.cache_enabled = cache_enabled
        if concurrency <= 0:
            raise ValueError("图片工作协程数必须为正数")
        self.concurrency = concurrency
        self.progress: ProgressReporter | None = None

    async def process(self, blocks: list[DocumentBlock]) -> tuple[list[DocumentBlock], list[Issue]]:
        """并发处理图片；输出仍按原始 block 顺序，单张失败不影响其余。"""
        image_blocks = [block for block in blocks if block.kind == "image"]
        total = len(image_blocks)
        if total:
            report(self.progress, ProgressEvent("stage", "vision"))

        completed = failed = cached_count = 0

        async def describe(block: DocumentBlock) -> tuple[DocumentBlock | None, Issue | None]:
            nonlocal completed, failed, cached_count
            error = None
            derived: DocumentBlock | None = None
            problem: Issue | None = None
            try:
                asset = Asset.model_validate_json(block.content)
                content = self.store.read(asset)
                context = " / ".join(block.heading_path) or "需求文档图片"
                key = "vision_" + fingerprint([asset.checksum, self.identity, context])
                cached = (
                    self.repository.get("rag_records", key, RagRecord)
                    if self.cache_enabled
                    else None
                )
                if cached:
                    data = cached.payload
                    cached_count += 1
                else:
                    description = await self.reader.read(content, context)
                    if not description.text.strip():
                        raise ValueError("图片描述为空")
                    data = {
                        "text": description.text,
                        "model": description.model,
                        "version": description.version,
                    }
                    if self.cache_enabled:
                        self.repository.save(
                            "rag_records",
                            key,
                            RagRecord(record_id=key, kind="vision", payload=data),
                        )
                derived = DocumentBlock(
                    block_id=block.block_id + "_" + key,
                    document_id=block.document_id,
                    kind="paragraph",
                    order=0,
                    heading_path=block.heading_path,
                    content=data["text"],
                    locator=block.locator,
                    origin="vision",
                    processing_model=data["model"],
                    processing_version=data["version"],
                )
                problem = issue("validation", "图片解释存在不确定性，需人工核实", [block.block_id])
            except Exception as exc:
                failed += 1
                # 只展开适配器保证安全的错误码，其他异常只保留类型。
                error = exc.code if isinstance(exc, VisionReadError) else type(exc).__name__
                problem = issue("unparsed", "图片解释失败：" + error, [block.block_id])
            completed += 1
            report(
                self.progress,
                ProgressEvent(
                    "image_completed", "vision", completed=completed, total=total, error=error
                ),
            )
            return derived, problem

        # 在应用层限制整个图片任务，而不只限制适配器里的 HTTP。
        # 避免一次读入、解码全部图片，挤占内存并阻塞其他请求的响应处理。
        outcomes: list[tuple[DocumentBlock | None, Issue | None]] = [(None, None)] * total
        pending = iter(enumerate(image_blocks))

        async def worker() -> None:
            for index, block in pending:
                outcomes[index] = await describe(block)

        workers = [asyncio.create_task(worker()) for _ in range(min(self.concurrency, total))]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        by_id = {
            block.block_id: outcome for block, outcome in zip(image_blocks, outcomes, strict=True)
        }

        result: list[DocumentBlock] = []
        issues: list[Issue] = []
        for block in blocks:
            result.append(block)
            if block.kind != "image":
                continue
            derived, problem = by_id[block.block_id]
            if derived is not None:
                result.append(derived)
            if problem is not None:
                issues.append(problem)

        if total:
            report(
                self.progress,
                ProgressEvent(
                    "vision_summary",
                    "vision",
                    total=total,
                    completed=total - failed,
                    failed=failed,
                    cached=cached_count,
                ),
            )
        # 派生 Block 紧随图片，重新编号保持确定阅读顺序。
        return [b.model_copy(update={"order": i}) for i, b in enumerate(result)], issues
