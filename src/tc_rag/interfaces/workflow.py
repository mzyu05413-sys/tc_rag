"""CLI：一次执行 RAG，联合输出测试点 XMind 和详细用例 Excel。"""

import asyncio
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, NoReturn

import typer
import yaml
from pydantic import ValidationError

from tc_rag.application.orchestration.pipeline import Pipeline
from tc_rag.application.ports.errors import (
    ContextBudgetError,
    EmbeddingProviderError,
    OutputTruncatedError,
    StartupConfigurationError,
    WorkflowError,
)
from tc_rag.application.requests import CasesRequest
from tc_rag.application.services.plan import run_plan
from tc_rag.application.services.quality import inspect_quality
from tc_rag.bootstrap import build_pipeline, validate_runtime_config
from tc_rag.core.config import AppConfig, load_config
from tc_rag.domain.identity import new_id
from tc_rag.domain.models import Run
from tc_rag.domain.pipeline import Extraction, StageSnapshot
from tc_rag.infrastructure.exporters.quality_report import render_quality_markdown
from tc_rag.infrastructure.exporters.reports import ReportExporter
from tc_rag.infrastructure.providers.openai_sdk import OpenAIStructuredLLM, ProviderError
from tc_rag.infrastructure.storage.sqlite import SqliteRepository
from tc_rag.interfaces.progress import TerminalProgress, format_duration

DEFAULT_INSTRUCTION = "根据需求文档生成功能测试点和测试用例，保留原文依据"
DEFAULT_OUTPUT_DIR = Path("workdir/output")

app = typer.Typer(
    help="tc-rag：通过 RAG 一次生成测试点 XMind、用例 Excel 及报告。"
    "不带子命令时进入交互式生成。",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """不带子命令时进入交互式生成；非交互环境改为显示帮助。"""
    if ctx.invoked_subcommand is not None:
        return
    # Windows 下 stdin 重定向仍可能报告 isatty=True，必须同时检查输出端。
    if sys.stdin.isatty() and sys.stdout.isatty():
        _interactive_start(
            config=None,
            output_dir=DEFAULT_OUTPUT_DIR,
            preview=False,
            quiet=False,
            skill_file=None,
        )
    else:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


def _config_path(path: Path | None) -> Path | None:
    if path is not None:
        return path
    for candidate in (Path("workdir/local.yaml"), Path("config/defaults.yaml")):
        if candidate.is_file():
            return candidate
    return None


def _config(path: Path | None) -> AppConfig:
    return load_config(_config_path(path))


def _progress(
    pipeline: Pipeline, quiet: bool, stage_total: int | None = None
) -> TerminalProgress | None:
    reporter = None if quiet else TerminalProgress(stage_total=stage_total)
    pipeline.progress = reporter
    if isinstance(pipeline.llm, OpenAIStructuredLLM):
        pipeline.llm.progress = reporter
    if pipeline.vision is not None:
        from tc_rag.infrastructure.providers.vision import OpenAICompatVision

        pipeline.vision.progress = reporter
        if isinstance(pipeline.vision.reader, OpenAICompatVision):
            pipeline.vision.reader.progress = reporter
    return reporter


def _snapshot(repo: SqliteRepository, run_id: str, stage: str) -> dict[str, Any]:
    value = repo.get("stage_snapshots", f"{run_id}:{stage}", StageSnapshot)
    if value is None:
        raise WorkflowError("运行或所需阶段不存在")
    return value.payload


def _config_changes(saved: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """只回显发生变化的配置段名，不输出配置值。"""
    keys = sorted(set(saved) | set(current))
    return [key for key in keys if saved.get(key) != current.get(key)]


def _configuration_error(exc: ValidationError) -> None:
    """只展示配置契约中的键名，未知键和输入值都不能成为终端内容。"""
    errors = exc.errors(include_input=False, include_url=False, include_context=False)
    if any(e["loc"] == ("sources",) for e in errors):
        typer.echo(
            "代码读取分支已移除；请删除配置中的 sources 段，"
            "并清除当前终端的 TC_RAG_SOURCES__* 环境变量。",
            err=True,
        )
    elif any(e["loc"] == ("retrieval", "mode") for e in errors):
        typer.echo("配置已统一为 RAG，请删除 retrieval.mode；CLI 不再使用 --mode。", err=True)
        typer.echo("若设置过环境变量 TC_RAG_RETRIEVAL__MODE，也请在当前终端清除。", err=True)
    elif any(e["loc"] == ("planning", "llm_supplement") for e in errors):
        typer.echo("RAG 规划已固定启用，请删除 planning.llm_supplement。", err=True)
    else:
        names: set[str] = set()

        def collect(node: Any) -> None:
            if isinstance(node, dict):
                names.update(node.get("properties", {}))
                for value in node.values():
                    collect(value)
            elif isinstance(node, list):
                for value in node:
                    collect(value)

        collect(AppConfig.model_json_schema())
        typer.echo("配置校验失败（不显示配置值或未知键名）：", err=True)
        for error in errors[:20]:
            field = ".".join(
                str(part) if isinstance(part, int) or part in names else "<未知字段>"
                for part in error["loc"]
            ) or "<配置根>"
            reason = (
                "当前版本不支持此字段，请删除"
                if error["type"] == "extra_forbidden"
                else "类型或取值不符合配置约束"
            )
            typer.echo(f"  {field}：{reason}", err=True)


def _error(exc: Exception) -> None:
    # 只有根配置模型的校验才叫配置错误；领域数据错误不混用这套提示。
    # 不回显第三方异常正文、Pydantic 输入值或任意用户字段名。
    if isinstance(exc, ValidationError) and exc.title == AppConfig.__name__:
        _configuration_error(exc)
    elif isinstance(exc, ValidationError):
        typer.echo("数据契约校验失败；请检查阶段快照及引用，不显示原始数据。", err=True)
    elif isinstance(exc, StartupConfigurationError):
        typer.echo(f"启动配置错误：{exc}", err=True)
    elif isinstance(exc, EmbeddingProviderError):
        typer.echo(str(exc), err=True)
        if exc.code == "HTTP_429_BIGMODEL_1113":
            typer.echo(
                "智谱 1113：向量服务对应账户已欠费，请在智谱控制台核对余额并充值后重试。"
                "RAG 索引未完成，本次不会继续生成测试点和用例。",
                err=True,
            )
        elif exc.code.startswith("HTTP_429"):
            typer.echo("向量服务限流或额度受限，请核对服务商控制台及错误码。", err=True)
    elif isinstance(exc, ContextBudgetError):
        typer.echo(f"上下文预算不足：{exc}", err=True)
    elif isinstance(exc, WorkflowError):
        typer.echo(f"流程检查未通过：{exc}", err=True)
    elif isinstance(exc, OutputTruncatedError):
        typer.echo(
            "模型输出被截断；请配置 models.llm.max_output_tokens，或减小分块/生成批次。"
            "runtime.max_context_tokens 只控制输入预算，不会扩大模型输出上限。",
            err=True,
        )
    elif isinstance(exc, ProviderError):
        # ProviderError 仅携带适配器生成的状态码或固定说明，不包含服务商原始正文。
        typer.echo(f"模型调用失败：{exc}", err=True)
        hints = {
            "HTTP_400": "服务商拒绝请求参数，请核对模型、api_mode 和 response_format。",
            "HTTP_401": "请检查密钥是否有效，以及是否属于当前模型服务商。",
            "HTTP_402": "生成模型账户余额不足，请充值或更换有余额的密钥后重试。",
            "HTTP_403": "请检查当前密钥的模型访问权限。",
            "HTTP_404": "请检查模型名称和 base_url 是否正确。",
            "HTTP_429": "服务商限流或额度受限，请查看服务商控制台。",
        }
        if str(exc) in hints:
            typer.echo(hints[str(exc)], err=True)
    else:
        typer.echo(f"执行失败（{type(exc).__name__}）；检查配置和运行阶段状态。", err=True)
    raise typer.Exit(2) from None


def _cancelled() -> NoReturn:
    typer.echo("已取消执行。若已显示 run_id，可用 status 查询该运行的阶段状态。", err=True)
    raise typer.Exit(130) from None


async def _close(pipeline: Pipeline) -> None:
    from tc_rag.infrastructure.providers.embedding import OpenAIEmbedder
    from tc_rag.infrastructure.providers.rerank import HttpReranker
    from tc_rag.infrastructure.providers.vision import OpenAICompatVision
    from tc_rag.infrastructure.retrieval.context import RagContext

    clients: list[Any] = []
    if isinstance(pipeline.retriever, RagContext):
        if isinstance(pipeline.retriever.embedder, OpenAIEmbedder):
            clients.append(pipeline.retriever.embedder)
        if isinstance(pipeline.retriever.reranker, HttpReranker):
            clients.append(pipeline.retriever.reranker)
    if pipeline.vision and isinstance(pipeline.vision.reader, OpenAICompatVision):
        clients.append(pipeline.vision.reader)
    if isinstance(pipeline.llm, OpenAIStructuredLLM):
        clients.append(pipeline.llm)
    # 一个客户端关闭失败也要尝试释放其余连接。
    results = await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)
    if any(isinstance(r, BaseException) for r in results):
        raise RuntimeError("部分模型客户端关闭失败")


@app.command()
def run(
    doc: Path = typer.Argument(..., exists=True, dir_okay=False),
    instruction: str | None = typer.Option(
        None,
        "--instruction",
        "-i",
        help="生成目标；省略时只展示执行计划，不调用模型。",
    ),
    output_dir: Path = typer.Option(DEFAULT_OUTPUT_DIR, "--output-dir", help="产物根目录。"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    preview: bool = typer.Option(False, "--preview", help="只展示执行计划，不调用模型。"),
    resume: str | None = typer.Option(None, "--resume", help="从已有运行继续。"),
    from_stage: str | None = typer.Option(
        None, "--from-stage", help="配合 --resume，支持 extract / plan_points。"
    ),
    quiet: bool = typer.Option(False, "--quiet", help="隐藏执行进度，保留结果与错误。"),
    skill_file: Path | None = typer.Option(
        None, "--skill-file", exists=True, dir_okay=False, help="追加用例表达规范（UTF-8）。"
    ),
    metric: Literal["cosine", "euclidean", "dot"] | None = typer.Option(
        None, "--metric", help="检索相似度：cosine / euclidean / dot。"
    ),
    normalize: bool | None = typer.Option(
        None, "--normalize/--no-normalize", help="是否对向量做 L2 归一化。"
    ),
) -> None:
    """一次生成测试点 XMind 和用例 Excel；省略 -i 或使用 --preview 时仅展示计划。"""
    try:
        config = _config_path(config)
        cfg = load_config(config)
        if skill_file is not None:
            cfg.generation.skill_file = str(skill_file)
        if metric is not None:
            cfg.retrieval.metric = metric
        if normalize is not None:
            cfg.retrieval.normalize = normalize
        stages = run_plan(doc, cfg)
        # 兼容原有只给文件路径的预览行为，绝不隐式发起付费调用。
        if preview or instruction is None:
            typer.echo("计划预览（离线）：RAG 唯一流程")
            typer.echo(" → ".join(stages))
            return
        run_id = new_id("run")
        pipeline = build_pipeline(cfg, run_id)
        typer.echo(f"run_id={run_id}")
        # 无图主线有 10 个阶段；图片解释按开关增加 1 个，恢复运行不虚构总数。
        stage_total = None if resume else 10 + int(cfg.parsing.vision_enabled)
        reporter = _progress(pipeline, quiet, stage_total)
        if not quiet:
            typer.echo(
                f"[开始] 文档：{doc.name}｜生成模型：{cfg.models.llm.model}"
                f"｜并发：{cfg.models.llm.concurrency}｜输出：{output_dir}",
                err=True,
            )
            if resume:
                typer.echo(
                    f"[恢复] 从运行 {resume} 继续"
                    + (f"，阶段 {from_stage}" if from_stage else ""),
                    err=True,
                )
        started = perf_counter()

        async def execute() -> dict[str, Any]:
            try:
                async with reporter or nullcontext():
                    return await pipeline.generate(
                        doc,
                        instruction or "",
                        output_dir,
                        run_id=run_id,
                        resume=resume,
                        from_stage=from_stage,
                    )
            finally:
                await _close(pipeline)

        result = asyncio.run(execute())
        total_points = result.get("points_total", 0)
        ready_points = result.get("points_ready", 0)
        covered_points = result.get("points_with_cases", 0)
        typer.echo(
            f"[完成] 总用时 {format_duration(perf_counter() - started)}\n"
            f"XMind 测试点目录：{Path(result['xmind']).resolve()}\n"
            f"Excel 测试用例：{Path(result['excel']).resolve()}\n"
            f"测试点 {total_points}（ready {ready_points} / 待澄清 "
            f"{total_points - ready_points}），覆盖 {covered_points} 个 ready 点；"
            f"生成用例 {result['cases']} 条，问题清单 {result['issues']} 项。"
        )
    except KeyboardInterrupt:
        _cancelled()
    except (ValueError, OSError, RuntimeError, yaml.YAMLError) as exc:
        active_run = locals().get("run_id")
        if active_run:
            typer.echo(f"【排查】查看运行进度：tc-rag status {active_run}", err=True)
        _error(exc)


def _interactive_start(
    *,
    config: Path | None,
    output_dir: Path,
    preview: bool,
    quiet: bool,
    skill_file: Path | None,
    metric: Literal["cosine", "euclidean", "dot"] | None = None,
    normalize: bool | None = None,
) -> None:
    """交互输入需求路径和提示词，连续生成测试点 XMind 与用例 Excel。"""
    try:
        config = _config_path(config)
        cfg = load_config(config)
        if metric is not None:
            cfg.retrieval.metric = metric
        if normalize is not None:
            cfg.retrieval.normalize = normalize
        # 不创建客户端、不调用模型；缺少密钥等问题应在用户输入长提示词之前说明。
        if not preview:
            validate_runtime_config(cfg)
        typer.echo(
            f"配置：{config or '内置默认'} | RAG | 生成模型：{cfg.models.llm.model}"
            f" | 并发：{cfg.models.llm.concurrency}"
        )
        if cfg.parsing.vision_enabled and cfg.models.vision.enabled:
            typer.echo(
                f"视觉模型：{cfg.models.vision.model} | 并发上限："
                f"{cfg.models.vision.concurrency} | 请求最小间隔："
                f"{cfg.runtime.vision_min_interval_seconds:g} 秒 | 限流后自动冷却"
            )
        typer.echo(
            f"向量模型：{cfg.models.embedding.model} | 重排："
            + (cfg.models.rerank.model if cfg.models.rerank.enabled else "未启用")
        )
        typer.echo(
            "仅预览执行计划，不调用模型；Ctrl+C 可取消。"
            if preview
            else "文档确认后将自动通过 RAG 生成测试点 XMind 和用例 Excel；Ctrl+C 可取消。"
        )
        while True:
            text = typer.prompt("需求文档路径（PDF/DOCX）").strip()
            # 兼容从资源管理器“复制文件地址”得到的成对引号和带空格的路径。
            if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
                text = text[1:-1]
            doc = Path(text).expanduser()
            try:
                run_plan(doc, cfg)
            except ValueError:
                typer.echo("文件不存在或格式未启用，请重新输入 PDF/DOCX 文件路径。")
                continue
            break
        # 文档确认后直接按默认目标生成，不再要求输入提示词。
        instruction = DEFAULT_INSTRUCTION
        typer.echo(f"生成目标：{instruction}")
    except typer.Abort:
        typer.echo("已取消输入，未开始生成。")
        raise typer.Exit(130) from None
    except (ValueError, OSError, RuntimeError, yaml.YAMLError) as exc:
        _error(exc)

    # 复用正式入口，保持 RAG、预算、来源和 Skill 规范一致。
    run(
        doc=doc,
        instruction=instruction,
        output_dir=output_dir,
        config=config,
        preview=preview,
        resume=None,
        from_stage=None,
        quiet=quiet,
        skill_file=skill_file,
        metric=metric,
        normalize=normalize,
    )


@app.command()
def start(
    config: Path | None = typer.Option(
        None,
        "--config",
        exists=True,
        dir_okay=False,
        help="默认优先读取 workdir/local.yaml，否则读取 config/defaults.yaml。",
    ),
    output_dir: Path = typer.Option(DEFAULT_OUTPUT_DIR, "--output-dir", help="产物根目录。"),
    preview: bool = typer.Option(False, "--preview", help="只展示执行计划，不调用模型。"),
    quiet: bool = typer.Option(False, "--quiet", help="隐藏执行进度，保留结果与错误。"),
    skill_file: Path | None = typer.Option(
        None, "--skill-file", exists=True, dir_okay=False, help="追加用例表达规范（UTF-8）。"
    ),
    metric: Literal["cosine", "euclidean", "dot"] | None = typer.Option(
        None, "--metric", help="检索相似度：cosine / euclidean / dot。"
    ),
    normalize: bool | None = typer.Option(
        None, "--normalize/--no-normalize", help="是否对向量做 L2 归一化。"
    ),
) -> None:
    """交互输入需求文档路径；无子命令且终端可交互时进入同一流程。"""
    _interactive_start(
        config=config,
        output_dir=output_dir,
        preview=preview,
        quiet=quiet,
        skill_file=skill_file,
        metric=metric,
        normalize=normalize,
    )


@app.command()
def doctor(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    live: bool = typer.Option(
        False, "--live", help="用虚构需求检查 RAG 到 XMind，最多 8 次模型请求。"
    ),
    quiet: bool = typer.Option(False, "--quiet"),
) -> None:
    """检查当前进程配置；--live 检查 RAG 首轮，最多 8 次模型请求。"""
    try:
        path = _config_path(config)
        cfg = load_config(path)
        typer.echo(f"配置：{path or '内置默认'}")
        if not live:
            validate_runtime_config(cfg)
            typer.echo("离线配置检查通过：已从配置或环境变量取得所需密钥，未调用模型。")
            typer.echo(
                "检查 RAG 与 XMind 导出：执行 doctor --live（最多 8 次请求，含生成和 embedding）。"
            )
            return

        # 使用独立数据库和虚构材料，仅验证到测试点，不执行完整用例生成。
        run_id = new_id("diagnostic")
        folder = Path(cfg.project.workdir).resolve() / "diagnostics" / run_id
        cfg = cfg.model_copy(deep=True)
        cfg.project.workdir = str(folder / "work")
        cfg.storage.database = str(folder / "data.sqlite")
        cfg.models.rerank.enabled = cfg.parsing.vision_enabled = False
        cfg.chunking.token_counter = "character_approx"
        cfg.planning.methods = ["boundary"]
        cfg.runtime.cache_enabled = False
        cfg.runtime.max_attempts = 1
        cfg.runtime.budget.max_calls, cfg.runtime.budget.max_cost = 8, None
        cfg.generation.schema_repair_attempts = 0
        validate_runtime_config(cfg)
        folder.mkdir(parents=True, exist_ok=False)
        from docx import Document

        document = Document()
        document.add_paragraph(
            "虚构需求：批量操作每次最多选择300个对象，数量为整数；超过300个时拒绝执行。"
        )
        source = folder / "fiction.docx"
        document.save(str(source))
        (folder / "config.json").write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
        pipeline = build_pipeline(cfg, run_id)
        reporter = _progress(pipeline, quiet, 7)
        typer.echo("开始 RAG 检查：虚构需求，最多 8 次模型请求，不重试，仅检查到测试点。")
        typer.echo(f"诊断材料：{folder}")

        async def execute() -> None:
            try:
                async with reporter or nullcontext():
                    await pipeline.points(
                        source,
                        "仅按原文提炼数量上限300和整数精度0，生成边界测试点，不编造界面文案。",
                        folder / "out",
                        run_id=run_id,
                    )
            finally:
                await _close(pipeline)

        asyncio.run(execute())
        extracted = Extraction.model_validate(pipeline.read(run_id, "extract"))
        objectives = [p["objective"] for p in pipeline.read(run_id, "plan_points")["points"]]
        if not extracted.rules or not all(
            any(str(value) in objective for objective in objectives) for value in (299, 300, 301)
        ):
            typer.echo(
                "检查未通过：模型响应已处理，但未形成完整的 299/300/301 边界测试点。", err=True
            )
            typer.echo(f"诊断材料：{folder}", err=True)
            raise typer.Exit(2)
        bundles = pipeline.read(run_id, "retrieval")["bundles"]
        if not bundles or not all(b.get("index_version") for b in bundles):
            raise WorkflowError("诊断未得到有效索引和检索记录")
        typer.echo(
            "RAG 检查通过：规则提炼、向量索引、检索增强、299/300/301 测试点及 XMind 导出可用。"
        )
        typer.echo("此检查不覆盖用例生成和真实需求的业务质量。")
    except KeyboardInterrupt:
        _cancelled()
    except (ValueError, OSError, RuntimeError, yaml.YAMLError) as exc:
        _error(exc)


@app.command()
def cases(
    run_id: str,
    instruction: str | None = typer.Option(
        None, "--instruction", "-i", help="生成目标；省略时沿用原运行的生成指令。"
    ),
    output_dir: Path = typer.Option(DEFAULT_OUTPUT_DIR, "--output-dir", help="产物根目录。"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    skill_file: Path | None = typer.Option(
        None, "--skill-file", exists=True, dir_okay=False, help="追加用例表达规范（UTF-8）。"
    ),
    quiet: bool = typer.Option(False, "--quiet", help="隐藏执行进度，保留结果与错误。"),
) -> None:
    """复用已保存的测试点重做用例阶段，联合导出 XMind 与 Excel。"""
    try:
        cfg = _config(config)
        if skill_file is not None:
            cfg.generation.skill_file = str(skill_file)
        repo = SqliteRepository(cfg.storage.database)
        original = repo.get("runs", run_id, Run)
        if original is None:
            raise WorkflowError("运行不存在；请确认 run_id 后重试")
        changed = _config_changes(original.config_snapshot, cfg.model_dump(mode="json"))
        if changed:
            raise WorkflowError(
                "配置已变化（" + "、".join(changed) + "）；请使用生成测试点时的原配置"
            )
        if instruction is None:
            saved = repo.get("stage_snapshots", f"{run_id}:generation_request", StageSnapshot)
            if saved is None:
                raise WorkflowError("未找到原运行的生成指令；请使用 --instruction 指定")
            instruction = str(saved.payload["instruction"])
        request = CasesRequest(
            run_id=run_id,
            instruction=instruction,
            output_dir=output_dir,
        )
        pipeline = build_pipeline(cfg, run_id)
        # cases 只执行：索引准备 → 生成 → 校验 → 联合导出。
        reporter = _progress(pipeline, quiet, 4)

        async def execute() -> dict[str, Any]:
            try:
                async with reporter or nullcontext():
                    return await pipeline.cases(request)
            finally:
                await _close(pipeline)

        typer.echo(json.dumps(asyncio.run(execute()), ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        _cancelled()
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        _error(exc)


@app.command()
def status(
    run_id: str | None = typer.Argument(None),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """查看运行状态；省略 run_id 时列出最近 20 次运行。"""
    try:
        repo = SqliteRepository(_config(config).storage.database)
        if run_id is None:
            runs = repo.list_recent("runs", Run, limit=20)
            if not runs:
                typer.echo("尚无运行记录。")
                return
            typer.echo("RUN_ID\tSTATUS")
            for entry in runs:
                typer.echo(f"{entry.run_id}\t{entry.status}")
            return
        value = repo.get("runs", run_id, Run)
        if value is None:
            raise WorkflowError("运行不存在")
        progress = _snapshot(repo, run_id, "progress")
        typer.echo(
            json.dumps(
                {"run": value.model_dump(mode="json"), "progress": progress},
                ensure_ascii=False,
                indent=2,
            )
        )
    except (ValueError, OSError) as exc:
        _error(exc)


@app.command()
def inspect(
    run_id: str,
    stage: str = typer.Option(
        "extract",
        "--stage",
        help="阶段名：extract（别名 rules）/ index / retrieval / plan_points / cases / "
        "issues / failure / manifest / progress / invocations。",
    ),
    format: Literal["json", "markdown", "xlsx"] = typer.Option(
        "json", "--format", help="输出格式；非 json 时写入 --output 或默认产物目录。"
    ),
    output: Path | None = typer.Option(
        None, "--output", help="输出文件路径；省略时 json 打印到终端。"
    ),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """查看 SQLite 阶段事实，或从 cases 快照重建用例文件。"""
    try:
        cfg = _config(config)
        repo = SqliteRepository(cfg.storage.database)
        actual = "extract" if stage == "rules" else stage
        payload = _snapshot(repo, run_id, actual)
        value = payload["rules"] if stage == "rules" else payload
        if output is None and format == "json":
            typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
            return
        if format == "xlsx" and actual != "cases":
            raise WorkflowError("xlsx 只适用于 cases 阶段；其他阶段请使用 json 或 markdown")
        if format != "json" and not isinstance(value, dict):
            raise WorkflowError("该阶段快照不是对象结构，无法写成文件；请使用 json 输出")
        suffix = "md" if format == "markdown" else format
        target = output or DEFAULT_OUTPUT_DIR / run_id / f"reexport.{suffix}"
        columns: list[tuple[str, str]] | None = (
            [(str(column.field), column.header) for column in cfg.export.case_columns]
            if format == "xlsx"
            else None
        )
        ReportExporter(columns).export(value, target)
        typer.echo(str(target))
    except (ValueError, OSError) as exc:
        _error(exc)


@app.command()
def evaluate(
    run_id: str,
    gold: Path = typer.Option(..., "--gold", exists=True, dir_okay=False),
    output: Path = typer.Option(Path("workdir/evaluation.json"), "--output"),
    k: int = typer.Option(10, "--k", min=1),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """按人工规则对应和相关性标注评估，不调用模型、不猜测语义匹配。"""
    from tc_rag.application.services.evaluation import evaluate as compute

    try:
        repo = SqliteRepository(_config(config).storage.database)
        payload = _snapshot(repo, run_id, "cases")
        saved = repo.get("stage_snapshots", f"{run_id}:retrieval", StageSnapshot)
        result = compute(
            payload, saved.payload if saved else {}, json.loads(gold.read_text(encoding="utf-8")), k
        )
        result["run_id"] = run_id
        value = repo.get("runs", run_id, Run)
        result["config_snapshot"] = value.config_snapshot if value else None
        manifests = _snapshot(repo, run_id, "manifest")
        input_data = _snapshot(repo, run_id, "input")
        generation = _snapshot(repo, run_id, "generation_request")
        from tc_rag.domain.identity import fingerprint

        labels = json.loads(gold.read_text(encoding="utf-8"))
        result["audit"] = {
            "config": value.config_snapshot if value else {},
            "input": input_data,
            "prompts": manifests["prompts"],
            "generation_instruction": generation["instruction"],
            "metrics": manifests["metrics"],
            # 规则对应和相关性 ID 随运行变化；原始金标规则内容必须相同。
            "gold_fingerprint": fingerprint(labels["rules"]),
        }
        ReportExporter().export(result, output)
        typer.echo(str(output))
    except (ValueError, OSError, KeyError) as exc:
        _error(exc)


@app.command()
def quality(
    run_id: str,
    output: Path | None = typer.Option(
        None, "--output", help="输出目录；默认写入运行产物目录的 reports/。"
    ),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    strict: bool = typer.Option(False, "--strict", help="warn 也返回退出码 2。"),
) -> None:
    """只读检查已完成运行的阶段快照，不调用模型。"""
    try:
        cfg = _config(config)
        repo = SqliteRepository(cfg.storage.database, read_only=True)
        run = repo.get("runs", run_id, Run)
        if run is None:
            raise WorkflowError("运行不存在")
        stages = (
            "input",
            "chunk",
            "extract",
            "index",
            "plan_points",
            "cases",
            "issues",
            "retrieval",
            "invocations",
            "manifest",
            "cases_output",
            "failure",
            "progress",
        )
        snapshots: dict[str, dict[str, Any]] = {}
        for stage in stages:
            value = repo.get("stage_snapshots", f"{run_id}:{stage}", StageSnapshot)
            if value is not None:
                snapshots[stage] = value.payload
        planned = snapshots.get("plan_points") or {}
        tree_id = planned.get("tree_id")
        if tree_id:
            from tc_rag.domain.review import PointTree

            tree = repo.get("point_trees", str(tree_id), PointTree)
            if tree is not None:
                snapshots["point_tree"] = tree.model_dump(mode="json")
        report = inspect_quality(run.model_dump(mode="json"), snapshots)
        cases_output = snapshots.get("cases_output") or {}
        base = output if output is not None else (
            Path(str(cases_output["directory"]))
            if cases_output.get("directory")
            else DEFAULT_OUTPUT_DIR / run_id
        ) / "reports"
        base.mkdir(parents=True, exist_ok=True)
        json_path = base / "quality_check.json"
        md_path = base / "quality_check.md"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        md_path.write_text(render_quality_markdown(report), encoding="utf-8")
        overall = report["overall"]
        counts = overall["counts"]
        typer.echo(
            f"质量检查：{overall['status']}（pass {counts['pass']} / warn {counts['warn']} / "
            f"fail {counts['fail']} / not_applicable {counts['not_applicable']}）"
        )
        typer.echo(f"JSON：{json_path}")
        typer.echo(f"Markdown：{md_path}")
        if overall["status"] == "fail" or (strict and overall["status"] == "warn"):
            raise typer.Exit(2)
    except KeyboardInterrupt:
        _cancelled()
    except typer.Exit:
        raise
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        _error(exc)


@app.command()
def estimate(
    doc: Path = typer.Argument(..., exists=True, dir_okay=False),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """本地解析与分块，估算抽取调用量；不估算费用或最终用例数量。"""
    try:
        import tempfile

        from tc_rag.application.services.chunking import structural_chunks
        from tc_rag.application.services.ingest import ingest_document
        from tc_rag.infrastructure.parsing.docx import DocxParser
        from tc_rag.infrastructure.parsing.pdf import PdfParser
        from tc_rag.infrastructure.runtime.tokens import token_counter
        from tc_rag.infrastructure.storage.files import LocalArtifactStore

        cfg = _config(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SqliteRepository(root / "estimate.sqlite")
            store = LocalArtifactStore(root)
            ingest = ingest_document(doc, store, repo)
            parser = (
                DocxParser(store, infer_headings=cfg.parsing.infer_docx_headings)
                if doc.suffix.lower() == ".docx" else PdfParser(store)
            )
            parsed = parser.parse(ingest.asset)
            chunked = structural_chunks(
                parsed.blocks,
                cfg.chunking.target_tokens,
                cfg.chunking.overlap_tokens,
                token_counter(cfg.chunking.token_counter),
            )
            typer.echo(
                json.dumps(
                    {
                        "extraction_calls_lower_bound": len(chunked.chunks),
                        "chunk_tokens_estimate": sum(c.token_estimate for c in chunked.chunks),
                        "unparsed_or_budget_issues": len(parsed.issues) + len(chunked.issues),
                        "generation_calls": None,
                        "cost": None,
                        "note": "粗估，不含提示词、重试、修复及输出 token",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
    except (ValueError, OSError, RuntimeError) as exc:
        _error(exc)
