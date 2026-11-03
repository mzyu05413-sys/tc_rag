"""离线生成分块/解析策略审核包；只读主库，所有预览资产写入独立目录。"""

import argparse
import json
from pathlib import Path

from tc_rag.application.services.chunk_stats import chunk_statistics
from tc_rag.application.services.chunking import structural_chunks
from tc_rag.core.config import load_config
from tc_rag.domain.models import Asset, Chunk, DocumentBlock, Issue, Run
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.infrastructure.parsing.docx import DocxParser
from tc_rag.infrastructure.parsing.layout_diagnostics import (
    docx_structure_statistics,
    pdf_layout_statistics,
)
from tc_rag.infrastructure.parsing.pdf import PdfParser
from tc_rag.infrastructure.runtime.tokens import offline_token_counter
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository
from tc_rag.interfaces.chunk_stats import _review_text


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare(args):
    config = load_config(args.config)
    repo = SqliteRepository(config.storage.database, read_only=True)
    original_store = LocalArtifactStore(config.project.workdir)
    summary = {"model_calls": 0, "runs": []}
    lines = [
        "# 分块与解析统一验收包",
        "",
        "只读主库；零模型调用。先看此页，再点逐块对照。",
        "",
        "试分块复用历史图文 Block；解析对照重新读取原件但不解释图片，二者不能混作端到端结果。",
        "",
    ]
    for run_id in args.run_ids:
        snapshot = repo.get("stage_snapshots", run_id + ":chunk", StageSnapshot)
        run = repo.get("runs", run_id, Run)
        if snapshot is None or run is None:
            raise ValueError("缺少运行或分块快照")
        blocks = [DocumentBlock.model_validate(b) for b in snapshot.payload["blocks"]]
        before = [Chunk.model_validate(c) for c in snapshot.payload["chunks"]]
        before_issues = [Issue.model_validate(i) for i in snapshot.payload.get("issues", [])]
        settings = run.config_snapshot.get("chunking", {})
        target, overlap = settings.get("target_tokens", 900), settings.get("overlap_tokens", 100)
        counter = offline_token_counter(settings.get("token_counter", "tiktoken_approx"))
        directory = args.output_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        measured = {
            "run_id": run_id,
            "before": chunk_statistics(blocks, before, before_issues, target, counter),
            "variants": {},
        }
        lines.extend(
            [
                f"## {run_id}",
                "",
                "| 策略 | 分块 | 纯标题块 | 超预算块 | 对照 |",
                "|---|---:|---:|---:|---|",
                f"| 原快照 | {len(before)} | {measured['before']['heading_only_chunks']} | "
                f"{len(measured['before']['oversize'])} | — |",
            ]
        )
        variants = {
            "defaults": {},
            "headings": {"attach_headings": True},
            "minimum200": {"attach_headings": True, "min_tokens": 200},
            "sentences": {"attach_headings": True, "split_long_paragraphs": True},
            "combined": {"attach_headings": True, "min_tokens": 200, "split_long_paragraphs": True},
        }
        for label, options in variants.items():
            result = structural_chunks(blocks, target, overlap, counter, **options)
            stats = chunk_statistics(blocks, result.chunks, result.issues, target, counter)
            stats["same_as_saved_chunks"] = before == result.chunks
            measured["variants"][label] = stats
            stem = directory / label
            stem.mkdir(exist_ok=True)
            write_json(
                stem / "comparison.json",
                {
                    "options": options,
                    "blocks": [b.model_dump(mode="json") for b in blocks],
                    "before": [c.model_dump(mode="json") for c in before],
                    "after": [c.model_dump(mode="json") for c in result.chunks],
                    "issues": [i.model_dump(mode="json") for i in result.issues],
                },
            )
            (stem / "review.md").write_text(
                _review_text(run_id, blocks, before, result.chunks, result.issues), encoding="utf-8"
            )
            link = f"{run_id}/{label}/review.md"
            lines.append(
                f"| {label} | {stats['chunks']} | {stats['heading_only_chunks']} | "
                f"{len(stats['oversize'])} | [逐块核对]({link}) |"
            )
        asset_id = snapshot.payload.get("document", {}).get("asset_id")
        asset = repo.get("assets", asset_id, Asset) if asset_id else None
        if asset:
            content = original_store.read(asset)
            store = LocalArtifactStore(directory / "preview_assets")
            asset = store.put(asset.relative_path, content)
            if asset.relative_path.endswith(".docx"):
                inferred = run.config_snapshot.get("parsing", {}).get("infer_docx_headings", False)
                old = DocxParser(store, infer_headings=inferred).parse(asset)
                new = DocxParser(store, infer_headings=inferred, extended=True).parse(asset)
                measured["layout"] = docx_structure_statistics(content)
                strategy = "docx_extended=true"
            else:
                old, new = (
                    PdfParser(store).parse(asset),
                    PdfParser(store, running_text="isolate").parse(asset),
                )
                measured["layout"] = pdf_layout_statistics(content)
                strategy = "pdf_running_text=isolate"
            measured["parse_comparison"] = {
                "strategy": strategy,
                "before_blocks": len(old.blocks),
                "after_blocks": len(new.blocks),
                "isolated_blocks": sum(b.kind == "other" for b in new.blocks),
                "before_headings": sum(b.kind == "heading" for b in old.blocks),
                "after_headings": sum(b.kind == "heading" for b in new.blocks),
            }
            write_json(
                directory / "parse_comparison.json",
                {
                    "strategy": strategy,
                    "before": [b.model_dump(mode="json") for b in old.blocks],
                    "after": [b.model_dump(mode="json") for b in new.blocks],
                    "issues": [i.model_dump(mode="json") for i in new.issues],
                },
            )
            parse_lines = [
                f"# 解析对照：{strategy}",
                "",
                "原件副本和提取图片仅写入本预览目录。",
                "",
            ]
            for label, values in (("改前", old.blocks), ("启用后", new.blocks)):
                parse_lines.extend([f"## {label}", ""])
                for value in values:
                    if value.kind == "image":
                        continue
                    parse_lines.extend(
                        [
                            f"### {value.order}. {value.kind} · {' / '.join(value.heading_path)}",
                            "",
                            value.locator.model_dump_json(),
                            "",
                            "    " + value.content.replace("\n", "\n    "),
                            "",
                        ]
                    )
            (directory / "parse_review.md").write_text("\n".join(parse_lines), encoding="utf-8")
            lines.extend(["", f"[解析开关对照]({run_id}/parse_review.md)：{strategy}。", ""])
        summary["runs"].append(measured)
    write_json(args.output_dir / "summary.json", summary)
    (args.output_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.output_dir / "README.md")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_ids", nargs="+")
    parser.add_argument("--config", type=Path, default=Path("workdir/local.yaml"))
    parser.add_argument("--output-dir", type=Path, required=True)
    prepare(parser.parse_args())
