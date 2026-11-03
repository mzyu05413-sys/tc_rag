"""只读历史快照的分块统计；不初始化模型、不写业务事实。"""

import json
from pathlib import Path

import typer

from tc_rag.application.services.chunk_stats import chunk_statistics
from tc_rag.application.services.chunking import structural_chunks
from tc_rag.core.config import load_config
from tc_rag.domain.identity import new_id
from tc_rag.domain.models import Asset, Chunk, DocumentBlock, Issue, Run
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.infrastructure.parsing.layout_diagnostics import (
    docx_structure_statistics,
    pdf_layout_statistics,
)
from tc_rag.infrastructure.runtime.tokens import offline_token_counter
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository


def chunk_stats(
    run_id: str = typer.Argument(...),
    config: Path = typer.Option(Path("workdir/local.yaml"), "--config", exists=True),
    output_dir: Path = typer.Option(Path("workdir/diagnostics/chunking"), "--output-dir"),
    docx: Path | None = typer.Option(None, "--docx", exists=True, dir_okay=False),
    compare: bool = typer.Option(False, "--compare", help="用历史原文离线试分块，不改运行记录"),
    attach_headings: bool = typer.Option(True, "--attach-headings/--keep-headings"),
    min_tokens: int = typer.Option(0, "--min-tokens", min=0),
    split_long_paragraphs: bool = typer.Option(False, "--split-long-paragraphs"),
) -> None:
    """只读统计已保存的 Chunk/原文与可选 DOCX/PDF 布局，零模型调用。"""
    try:
        cfg = load_config(config)
        repository = SqliteRepository(cfg.storage.database, read_only=True)
        snapshot = repository.get("stage_snapshots", f"{run_id}:chunk", StageSnapshot)
        run = repository.get("runs", run_id, Run)
        if snapshot is None or run is None:
            raise ValueError("没有历史分块快照")
        blocks = [DocumentBlock.model_validate(item) for item in snapshot.payload["blocks"]]
        chunks = [Chunk.model_validate(item) for item in snapshot.payload["chunks"]]
        issues = [Issue.model_validate(item) for item in snapshot.payload.get("issues", [])]
        settings = run.config_snapshot.get("chunking", {})
        target = settings.get("target_tokens", 900)
        name = settings.get("token_counter", "tiktoken_approx")
        counter_error = None
        try:
            counter = offline_token_counter(name)
        except (ValueError, OSError, ImportError):
            counter = None
            counter_error = "本地编码缓存不可用；Chunk 沿用历史估算，视觉 token 留空，不联网下载"
        report = {
            "run_id": run_id,
            "model_calls": 0,
            "target_tokens": target,
            "token_counter": name,
            "counter_error": counter_error,
            "statistics": chunk_statistics(blocks, chunks, issues, target, counter),
        }
        asset_id = snapshot.payload.get("document", {}).get("asset_id")
        asset = repository.get("assets", asset_id, Asset) if asset_id else None
        if asset and asset.relative_path.lower().endswith(".pdf"):
            report["pdf_layout"] = pdf_layout_statistics(
                LocalArtifactStore(cfg.project.workdir).read(asset)
            )
        if docx:
            report["docx_structure"] = docx_structure_statistics(docx.read_bytes())
        directory = output_dir / new_id("chunk_stats")
        directory.mkdir(parents=True, exist_ok=True)
        if compare:
            if counter is None:
                raise ValueError("试分块需要历史计数器的本地编码缓存")
            options = {
                "attach_headings": attach_headings,
                "min_tokens": min_tokens,
                "split_long_paragraphs": split_long_paragraphs,
            }
            result = structural_chunks(
                blocks,
                target,
                settings.get("overlap_tokens", 100),
                counter,
                attach_headings=attach_headings,
                min_tokens=min_tokens,
                split_long_paragraphs=split_long_paragraphs,
            )
            report["comparison"] = {
                "options": options,
                "statistics": chunk_statistics(
                    blocks, result.chunks, result.issues, target, counter
                ),
            }
            (directory / "comparison.json").write_text(
                json.dumps(
                    {
                        "blocks": [block.model_dump(mode="json") for block in blocks],
                        "before": [chunk.model_dump(mode="json") for chunk in chunks],
                        "after": [chunk.model_dump(mode="json") for chunk in result.chunks],
                        "issues": [item.model_dump(mode="json") for item in result.issues],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (directory / "review.md").write_text(
                _review_text(run_id, blocks, chunks, result.chunks, result.issues), encoding="utf-8"
            )
        path = directory / "statistics.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        stats = report["statistics"]
        typer.echo("项目                  数量/分布")
        typer.echo(f"分块总数              {stats['chunks']}")
        typer.echo(f"token 最小/中位/P90/最大 {stats['tokens']}")
        typer.echo(f"不足100/200 token      {stats['small_chunks']}")
        typer.echo(
            f"纯标题/超预算分块       {stats['heading_only_chunks']} / {len(stats['oversize'])}"
        )
        typer.echo(
            f"标题块/空章节占比       {stats['heading_blocks']} / {stats['empty_heading_ratio']}"
        )
        typer.echo(f"视觉字符分布           {stats['visual_characters']}")
        typer.echo(f"视觉 token 分布        {stats['visual_tokens']}")
        typer.echo("一级章节              分块数（跨章节块可重复计入）")
        for chapter, count in stats["per_top_chapter"].items():
            typer.echo(f"  {chapter}：{count}")
        for item in stats["oversize"]:
            typer.echo(
                f"超预算：{item['chunk_id']}｜{item['tokens']} token｜{', '.join(item['reasons'])}"
            )
        if "pdf_layout" in report:
            layout = report["pdf_layout"]
            typer.echo(
                f"PDF 页数/多栏候选页    {layout['pages']} / {layout['possible_multicolumn_pages']}"
            )
            typer.echo("PDF 边缘重复 Top10（仅诊断，正文不删除）")
            for item in layout["running_text_top10"]:
                typer.echo(
                    f"  {item['position']}｜{item['count']} 页｜{item['normalized_text'][:80]}"
                )
        if "docx_structure" in report:
            typer.echo(f"DOCX 结构统计          {report['docx_structure']}")
        typer.echo(f"只读分块统计：{path}")
        if compare:
            typer.echo(f"离线试分块：{report['comparison']['statistics']['chunks']} 块")
            typer.echo(f"逐块审核：{directory / 'review.md'}")
    except (ValueError, OSError, KeyError, TypeError):
        typer.echo("分块统计未完成；请检查配置、历史快照和原件。未调用模型。", err=True)
        raise typer.Exit(2) from None


def _review_text(
    run_id: str,
    blocks: list[DocumentBlock],
    before: list[Chunk],
    after: list[Chunk],
    issues: list[Issue],
) -> str:
    """派生审核材料列出全部文字和来源，不覆盖 SQLite 历史事实。"""
    by_id = {block.block_id: block for block in blocks}
    lines = [
        f"# 分块对照：{run_id}",
        "",
        "零模型调用；字符区间为左闭右开。",
        "",
        "此对照使用已保存的 Block，不重新解析文档，也不重新解释图片。",
        "",
    ]
    for label, chunks in (("原分块", before), ("试分块", after)):
        lines.extend([f"## {label}（{len(chunks)} 块）", ""])
        for index, chunk in enumerate(chunks, 1):
            lines.extend([f"### {index}. {chunk.chunk_id}（{chunk.token_estimate} token）", ""])
            for source in chunk.sources:
                block = by_id[source.block_id]
                lines.append(
                    f"- {source.block_id} [{source.start_char}, {source.end_char})；"
                    f"章节：{' / '.join(block.heading_path) or '未归类'}；来源：{block.origin}"
                )
            # 选取长于原文所有反引号连续串的围栏，防止需求文字破坏审核排版。
            import re

            fence = "`" * max(
                3, max((len(s) + 1 for s in re.findall(r"`+", chunk.text)), default=3)
            )
            lines.extend(["", fence + "text", chunk.text, fence, ""])
    lines.extend(["## 本次试分块告警", ""])
    lines.extend(f"- {issue.description}；对象：{', '.join(issue.object_ids)}" for issue in issues)
    return "\n".join(lines) + "\n"
