"""只读历史快照的分块统计；不初始化模型、不写业务事实。"""

import json
from pathlib import Path
from typing import Any

import typer

from tc_rag.application.services.chunk_coverage import coverage_diagnostics
from tc_rag.application.services.chunk_experiments import budget_experiment, budget_pairs
from tc_rag.application.services.chunk_gold import (
    ChunkGold,
    candidate_review,
    evaluate_boundaries,
    validate_gold,
)
from tc_rag.application.services.chunk_stats import chunk_statistics
from tc_rag.application.services.chunking import structural_chunks
from tc_rag.application.services.extraction_context import CONTEXT_POLICY, neighboring_context
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
from tc_rag.interfaces.chunk_evaluation import write_context_review, write_evaluation_materials


def chunk_stats(
    run_id: str = typer.Argument(...),
    config: Path = typer.Option(Path("workdir/local.yaml"), "--config", exists=True),
    output_dir: Path = typer.Option(Path("workdir/diagnostics/chunking"), "--output-dir"),
    docx: Path | None = typer.Option(None, "--docx", exists=True, dir_okay=False),
    compare: bool = typer.Option(False, "--compare", help="用历史原文离线试分块，不改运行记录"),
    strategy: str | None = typer.Option(
        None, "--strategy", help="structural / structural_v2；未指定继承历史策略"
    ),
    attach_headings: bool | None = typer.Option(
        None, "--attach-headings/--keep-headings", help="未指定时继承历史运行配置"
    ),
    min_tokens: int | None = typer.Option(
        None, "--min-tokens", min=0, help="未指定时继承历史运行配置；0 显式关闭"
    ),
    split_long_paragraphs: bool | None = typer.Option(
        None, "--split-long-paragraphs/--keep-long-paragraphs",
        help="未指定时继承历史运行配置",
    ),
    context_blocks: int | None = typer.Option(
        None, "--context-blocks", min=0, max=4,
        help="前/后/内部空隙各最多 N 个背景片段；0 关闭；需 --compare"
    ),
    context_chars: int | None = typer.Option(
        None, "--context-chars", min=0, max=12000, help="全部邻文的字符预算；未指定继承历史配置"
    ),
    target_tokens: int | None = typer.Option(
        None, "--target-tokens", min=1, help="仅覆盖离线试分块的目标预算，需 --compare"
    ),
    overlap_tokens: int | None = typer.Option(
        None, "--overlap-tokens", min=0, help="仅覆盖离线试分块的重叠预算，需 --compare"
    ),
    targets: list[int] | None = typer.Option(
        None, "--targets", min=1, help="预算网格 target，可重复；需 --compare"
    ),
    overlaps: list[int] | None = typer.Option(
        None, "--overlaps", min=0, help="预算网格 overlap，可重复；最多 32 组组合"
    ),
    gold: Path | None = typer.Option(
        None, "--gold", exists=True, dir_okay=False, help="同运行原文的抽样需求边界标注"
    ),
    prepare_gold: bool = typer.Option(
        False, "--prepare-gold", help="生成最多 12 项待确认候选及阅读版，不自动计为金标"
    ),
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
        historical_overlap = settings.get("overlap_tokens", 100)
        if not compare and any(x is not None for x in (
            target_tokens, overlap_tokens, targets, overlaps, context_blocks, context_chars,
        )):
            raise ValueError("预算覆盖、网格与邻文预览选项需要 --compare")
        if gold is not None and prepare_gold:
            raise ValueError("载入已有标注与准备新候选不能同时指定")
        labels = (ChunkGold.model_validate_json(gold.read_text(encoding="utf-8-sig"))
                  if gold else candidate_review(run_id, blocks) if prepare_gold else None)
        if labels is not None:
            validate_gold(labels, run_id, blocks)
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
            "overlap_tokens": historical_overlap,
            "token_counter": name,
            "counter_error": counter_error,
            "boundary_policy": "chapter_guard_v1",
            "historical_options": {
                "strategy": settings.get("strategy", "structural"),
                "attach_headings": settings.get("attach_headings", False),
                "min_tokens": settings.get("min_tokens", 0),
                "split_long_paragraphs": settings.get("split_long_paragraphs", False),
            },
            "statistics": chunk_statistics(blocks, chunks, issues, target, counter),
            "coverage": coverage_diagnostics(blocks, chunks, issues, target),
            "gold": evaluate_boundaries(labels, run_id, blocks, chunks),
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
        matrix = None
        if compare:
            if counter is None:
                raise ValueError("试分块需要历史计数器的本地编码缓存")
            overrides = {key: value for key, value in {
                "strategy": strategy,
                "attach_headings": attach_headings, "min_tokens": min_tokens,
                "split_long_paragraphs": split_long_paragraphs,
            }.items() if value is not None}
            options = {**report["historical_options"], **overrides}
            trial_target = target_tokens if target_tokens is not None else target
            trial_overlap = overlap_tokens if overlap_tokens is not None else historical_overlap
            budget_pairs([trial_target], [trial_overlap], options["min_tokens"])
            pairs = budget_pairs(
                targets or [trial_target], overlaps or [trial_overlap], options["min_tokens"]
            ) if targets is not None or overlaps is not None else None
            result = structural_chunks(
                blocks,
                trial_target,
                trial_overlap,
                counter,
                attach_headings=options["attach_headings"],
                min_tokens=options["min_tokens"],
                split_long_paragraphs=options["split_long_paragraphs"],
                strategy=options["strategy"],
            )
            report["comparison"] = {
                "options": options,
                "overrides": overrides,
                "target_tokens": trial_target,
                "overlap_tokens": trial_overlap,
                "statistics": chunk_statistics(
                    blocks, result.chunks, result.issues, trial_target, counter
                ),
                "coverage": coverage_diagnostics(
                    blocks, result.chunks, result.issues, trial_target
                ),
                "gold": evaluate_boundaries(labels, run_id, blocks, result.chunks),
            }
            if pairs is not None:
                matrix = budget_experiment(run_id, blocks, counter, pairs, options, labels)
            context_options = {
                "blocks_per_side": context_blocks if context_blocks is not None else
                settings.get("extraction_context_blocks", 0),
                "total_chars": context_chars if context_chars is not None else
                settings.get("extraction_context_chars", 2000),
            }
            report["comparison"]["context_options"] = context_options
            directory.mkdir(parents=True, exist_ok=True)
            # 仅预览原文背景，不加载模型；正式提炼还会按完整请求预算移除较远背景。
            context_preview = {
                "model_calls": 0,
                "policy": CONTEXT_POLICY,
                "scope": "邻文仅作背景，不能扩展 sources；正式请求仍受模型上下文预算约束",
                "options": context_options,
                "chunks": [{
                    "chunk_id": chunk.chunk_id,
                    "allowed_sources": [s.model_dump(mode="json") for s in chunk.sources],
                    "neighbor_context": neighboring_context(
                        chunk, blocks, context_options["blocks_per_side"],
                        context_options["total_chars"],
                    ),
                } for chunk in result.chunks],
            }
            (directory / "extraction_context.json").write_text(
                json.dumps(context_preview, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            write_context_review(directory, blocks, context_preview)
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
        directory.mkdir(parents=True, exist_ok=True)
        write_evaluation_materials(directory, blocks, labels, matrix)
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
        _print_boundaries(stats)
        typer.echo(
            f"原文字符覆盖           {report['coverage']['source_character_coverage']}"
            f"；结构检查 {report['coverage']['passed']}"
        )
        typer.echo(
            f"抽样需求边界完整率     {report['gold']['boundary_completeness']}"
            f"；已确认 {report['gold']['confirmed_units']} 项"
        )
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
            typer.echo(f"实际试分块选项：{report['comparison']['options']}")
            _print_boundaries(report["comparison"]["statistics"])
            typer.echo(f"逐块审核：{directory / 'review.md'}")
            typer.echo(f"提炼邻文预览：{directory / 'extraction_context.json'}")
            typer.echo(f"提炼邻文阅读版：{directory / 'extraction_context.md'}")
        if labels is not None:
            typer.echo(f"需求单元审核：{directory / 'gold_review.md'}")
        if matrix is not None:
            typer.echo(
                f"离线预算对照 {len(matrix['variants'])} 组：{directory / 'budget_matrix.md'}"
            )
    except (ValueError, OSError, KeyError, TypeError):
        typer.echo("分块统计未完成；请检查配置、历史快照和原件。未调用模型。", err=True)
        raise typer.Exit(2) from None


def _print_boundaries(stats: dict[str, Any]) -> None:
    """同时显示分子与分母，避免把无可比较样本误当作零重叠。"""
    overlap, chapters = stats["overlap"], stats["chapter_boundaries"]
    typer.echo(
        f"同章节相邻重叠        {overlap['overlapping_pairs']}/{overlap['eligible_pairs']}"
        f"；比例 {overlap['ratio']}；排除原因 {overlap['excluded_reasons']}"
    )
    typer.echo(
        f"跨一级章节/跨文档块    {chapters['mixed_top_level_count']} / "
        f"{chapters['mixed_document_count']}；章节未知 {chapters['unknown_heading_count']}"
    )


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
