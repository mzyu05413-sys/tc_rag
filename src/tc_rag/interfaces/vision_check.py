"""只检查图片转文字，默认离线准备；只有 --live 才创建视觉模型客户端。"""

import asyncio
import json
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Any

import typer

from tc_rag.application.ports.errors import VisionImageSkipped
from tc_rag.application.progress import ProgressEvent
from tc_rag.application.services.vision_content import image_context, vision_image_id
from tc_rag.application.services.vision_scoring import VisionGold, score_images
from tc_rag.bootstrap import build_vision_service, load_prompts, resolve_prompt_root
from tc_rag.core.config import AppConfig, load_config
from tc_rag.domain.identity import new_id
from tc_rag.domain.models import Asset, DocumentBlock, ModelInvocation, Run
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.domain.rag import RagRecord
from tc_rag.infrastructure.exporters.vision_review import export_vision_review
from tc_rag.infrastructure.providers.vision import OpenAICompatVision
from tc_rag.infrastructure.providers.vision_images import prepare_views
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository
from tc_rag.interfaces.progress import TerminalProgress


def prepare(
    config: AppConfig, run_id: str, destination: Path, limit: int | None
) -> tuple[list[DocumentBlock], list[dict[str, Any]]]:
    """从历史快照复用原图与正文，只读主数据库，保留旧结果用于对照。"""
    repository = SqliteRepository(config.storage.database, read_only=True)
    snapshot = repository.get("stage_snapshots", f"{run_id}:chunk", StageSnapshot)
    if snapshot is None:
        raise ValueError("缺少 chunk 快照")
    original = [DocumentBlock.model_validate(item) for item in snapshot.payload["blocks"]]
    blocks = [block for block in original if block.origin != "vision"]
    store = LocalArtifactStore(config.project.workdir)
    rows: list[dict[str, Any]] = []
    occurrences = Counter(
        Asset.model_validate_json(block.content).checksum
        for block in blocks if block.kind == "image"
    )
    selected: set[str] = set()
    for position, block in enumerate(blocks):
        if block.kind != "image" or (limit is not None and len(rows) >= limit):
            continue
        selected.add(block.block_id)
        asset = Asset.model_validate_json(block.content)
        content = store.read(asset)
        image_dir = destination / "images" / f"{len(rows) + 1:03}"
        image_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(asset.relative_path).suffix or ".bin"
        original_path = image_dir / f"original{suffix}"
        original_path.write_bytes(content)
        row: dict[str, Any] = {
            "number": len(rows) + 1, "block_id": block.block_id,
            "heading": " / ".join(block.heading_path),
            "original": original_path.relative_to(destination).as_posix(),
            "checksum": asset.checksum, "locator": block.locator.model_dump(mode="json"),
            "checksum_occurrences": occurrences[asset.checksum],
            "context": json.loads(image_context(
                blocks, position, config.parsing.vision.context_blocks,
                config.parsing.vision.context_chars,
            )),
            "baseline": "\n".join(
                item.content for item in original
                if item.origin == "vision" and item.block_id.startswith(block.block_id + "_")
            ),
            "views": [], "result": None, "status": "prepared", "request_stats": None,
        }
        try:
            for view in prepare_views(content, config.parsing.vision):
                path = image_dir / f"{view.view_id}.png"
                path.write_bytes(view.png)
                row["views"].append({
                    **view.metadata(), "file": path.relative_to(destination).as_posix()
                })
        except VisionImageSkipped:
            row["status"] = "skipped"
            row["skip_reason"] = "图片过小，已跳过"
        except Exception as exc:
            row["plan_error"] = getattr(exc, "code", type(exc).__name__)
        row["view_count"] = len(row["views"])
        row["total_pixels"] = sum(view["size"][0] * view["size"][1] for view in row["views"])
        row["warnings"] = (
            ["多图输入可能超出兼容服务的数量/总像素限制，请核对服务商文档；不是已确认的限额。"]
            if len(row["views"]) > 1 else []
        )
        rows.append(row)
    if not rows:
        raise ValueError("该运行没有图片")
    # 保留周围正文，去掉未选中的图片，防止 --limit 后仍意外调用剩余图片。
    return [b for b in blocks if b.kind != "image" or b.block_id in selected], rows


async def execute(
    config: AppConfig, blocks: list[DocumentBlock], rows: list[dict[str, Any]],
    directory: Path, review_id: str,
) -> list[ModelInvocation]:
    repository = SqliteRepository(directory / "vision.sqlite")
    repository.save("runs", review_id, Run(
        run_id=review_id, status="running", config_snapshot={"vision_only": True}
    ))
    records: list[ModelInvocation] = []
    image_records: dict[str, list[ModelInvocation]] = {}

    def record(invocation: ModelInvocation) -> None:
        repository.save("model_invocations", invocation.invocation_id, invocation)
        records.append(invocation)
        image_records.setdefault(vision_image_id.get() or "unassigned", []).append(invocation)

    prompts, _ = load_prompts(resolve_prompt_root())
    service = build_vision_service(
        config, repository, LocalArtifactStore(config.project.workdir), review_id, record, prompts
    )
    try:
        async with TerminalProgress(
            stage_total=1, flow_label="[流程] 仅图片转文字：视图识别 → 视觉复核 → 对照报告"
        ) as progress:
            service.progress = progress
            if isinstance(service.reader, OpenAICompatVision):
                service.reader.progress = progress
            progress(ProgressEvent("stage", "vision"))
            processed, issues = await service.process(blocks)
            # 请求正常返回不等于图片解释有效；失败图也要导出报告，但不能提示成功。
            succeeded = all(
                any(block.origin == "vision" and block.block_id.startswith(row["block_id"] + "_")
                    for block in processed)
                or any(item.description == "图片过小，已跳过"
                       and row["block_id"] in item.object_ids for item in issues)
                for row in rows
            )
            progress(ProgressEvent("stage", "completed" if succeeded else "failed"))
    finally:
        if isinstance(service.reader, OpenAICompatVision):
            await service.reader.close()
    for row in rows:
        derived = next((
            block for block in processed
            if block.origin == "vision" and block.block_id.startswith(row["block_id"] + "_")
        ), None)
        if derived is not None:
            row["result"] = derived.model_dump(mode="json")
            cache_key = derived.block_id[len(row["block_id"]) + 1:]
            cached = repository.get("rag_records", cache_key, RagRecord)
            row["structured"] = cached.payload.get("details") if cached else None
        row["issues"] = [
            item.model_dump(mode="json") for item in issues if row["block_id"] in item.object_ids
        ]
        skipped = any(item["description"] == "图片过小，已跳过" for item in row["issues"])
        row["status"] = "skipped" if skipped else "completed" if derived is not None else "failed"
        calls = image_records.get(row["block_id"], [])
        row["request_stats"] = {
            "requests": len(calls),
            "request_duration_ms": (
                round(sum(call.timing.duration_ms or 0 for call in calls), 2)
                if all(call.timing.duration_ms is not None for call in calls) else None
            ),
            "input_tokens": sum(call.usage.input_tokens or 0 for call in calls)
            if all(call.usage.input_tokens is not None for call in calls) else None,
            "output_tokens": sum(call.usage.output_tokens or 0 for call in calls)
            if all(call.usage.output_tokens is not None for call in calls) else None,
            "calls": [{"id": call.invocation_id, "stage": call.stage, "model": call.model,
                       "error": call.error} for call in calls],
        }
    succeeded = all(row["result"] is not None or row["status"] == "skipped" for row in rows)
    repository.save("runs", review_id, Run(
        run_id=review_id, status="succeeded" if succeeded else "partial_failed",
        config_snapshot={"vision_only": True},
    ))
    return records


def vision(
    run_id: str | None = typer.Option(None, "--run-id", help="已有运行 ID；从其快照读取原图。"),
    config: Path = typer.Option(Path("workdir/local.yaml"), "--config", exists=True),
    output_dir: Path = typer.Option(Path("workdir/diagnostics/vision"), "--output-dir"),
    limit: int | None = typer.Option(None, "--limit", min=1, help="仅检查前 N 张图片。"),
    live: bool = typer.Option(False, "--live", help="真实调用视觉模型，会发送图文并产生费用。"),
    verify: bool | None = typer.Option(None, "--verify/--no-verify", help="仅本轮覆盖复核开关。"),
    gold: Path | None = typer.Option(None, "--gold", exists=True, dir_okay=False),
    results: Path | None = typer.Option(
        None, "--results", exists=True, dir_okay=False,
        help="只离线评分已有 results.json，不调用模型。"
    ),
) -> None:
    """仅验收图片转文字；默认离线生成原图、视图计划和历史结果对照页。"""
    started = perf_counter()
    review_id = new_id("vision_check")
    directory = output_dir / review_id
    try:
        annotation = (
            VisionGold.model_validate_json(gold.read_text(encoding="utf-8-sig")) if gold else None
        )
        if results is not None:
            if live or run_id or annotation is None:
                raise ValueError("已有结果评分只允许搭配 --gold")
            saved = json.loads(results.read_text(encoding="utf-8-sig"))
            directory.mkdir(parents=True, exist_ok=True)
            score_path = directory / "score.json"
            score_path.write_text(json.dumps(
                score_images(saved["images"], annotation), ensure_ascii=False, indent=2
            ), encoding="utf-8")
            typer.echo(f"视觉离线评分：{score_path}")
            return
        if not run_id:
            raise ValueError("需要 --run-id 或 --results 与 --gold")
        overrides = {"parsing": {"vision": {"verify": verify}}} if verify is not None else None
        cfg = load_config(config, cli_overrides=overrides)
        blocks, rows = prepare(cfg, run_id, directory, limit)
        records = asyncio.run(execute(cfg, blocks, rows, directory, review_id)) if live else []
        summary = {
            "review_id": review_id, "source_run_id": run_id, "live": live,
            "images": len(rows), "model": cfg.models.vision.model,
            "processing": cfg.parsing.vision.model_dump(),
            "processing_effective": {
                name: getattr(cfg.parsing.vision, name)
                for name in type(cfg.parsing.vision).model_fields
            },
            "warnings": [
                f"models.{role}.max_output_tokens 未设置；复杂 JSON 可能被服务商默认上限截断。"
                for role in ("vision", "vision_review")
                if (role == "vision" or cfg.models.vision_review.enabled)
                and getattr(cfg.models, role).max_output_tokens is None
            ],
            "completed": sum(row["result"] is not None for row in rows),
            "skipped_images": sum(row["status"] == "skipped" for row in rows),
            "duplicate_checksums": {key: count for key, count in Counter(
                row["checksum"] for row in rows
            ).items() if count > 1},
            "review_model": (
                cfg.models.vision_review.model if cfg.models.vision_review.enabled else None
            ),
            "plan_failed_images": sum(bool(row.get("plan_error")) for row in rows),
            "failed_images": sum(row["status"] == "failed" for row in rows) if live else None,
            "model_calls": len(records), "failed_calls": sum(r.error is not None for r in records),
            "output_tokens": (
                sum(r.usage.output_tokens or 0 for r in records)
                if records and all(r.usage.output_tokens is not None for r in records) else None
            ),
            "input_tokens": (
                sum(r.usage.input_tokens or 0 for r in records)
                if records and all(r.usage.input_tokens is not None for r in records) else None
            ),
            "elapsed_seconds": round(perf_counter() - started, 2),
            "business_accuracy": None, "human_review": "not_performed", "cost": None,
        }
        path = export_vision_review(directory, rows, summary)
        if annotation is not None:
            (directory / "score.json").write_text(json.dumps(
                score_images(rows, annotation), ensure_ascii=False, indent=2
            ), encoding="utf-8")
        typer.echo(f"仅视觉{'实测' if live else '离线准备'}：{path}")
        if any(row.get("plan_error") for row in rows) or (
            live and summary["completed"] + summary["skipped_images"] != len(rows)
        ):
            typer.echo(
                "视觉验收失败：存在解释失败的图片；报告已生成，请查看本轮失败原因。",
                err=True,
            )
            raise typer.Exit(2)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        typer.echo(
            f"视觉验收未完成：{type(exc).__name__}；请检查配置、运行ID和图片资产。", err=True
        )
        raise typer.Exit(2) from None
    except KeyboardInterrupt:
        typer.echo("已取消视觉验收。", err=True)
        raise typer.Exit(130) from None
