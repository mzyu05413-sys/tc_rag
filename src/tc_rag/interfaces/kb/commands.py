"""知识库入库先预览，显式 --live 才调用 embedding。"""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Literal

import typer

from tc_rag.core.config import load_config
from tc_rag.infrastructure.storage.kb.sqlite import KnowledgeSqliteRepository
from tc_rag.interfaces.kb.presentation import (
    show_answer,
    show_config,
    show_metrics,
    show_result,
    show_sources,
    terminal_task,
)

app = typer.Typer(
    help="知识库：文档入库、检索问答与评测。", invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def enter(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        start(config=Path("workdir/local.yaml"), root=Path("workdir/kb/runtime"),
              live=False, quiet=False)
    else:
        typer.echo(ctx.get_help())


@app.command()
def start(
    config: Path = typer.Option(Path("workdir/local.yaml"), exists=True, dir_okay=False),
    root: Path = typer.Option(Path("workdir/kb/runtime")),
    live: bool = typer.Option(False, "--live"),
    quiet: bool = typer.Option(False, "--quiet"),
) -> None:
    """交互选择入库、检索或问答；默认预览，--live 启动实际模型调用。"""
    typer.echo("\n[知识库工作台]")
    typer.echo("1 入库需求文档\n2 检索证据\n3 单次问答\n4 连续问答\n5 查看文档"
               "\n6 问答助手\n0 退出")
    typer.echo("当前为实际执行模式。" if live else "当前为预览模式；实际执行用 kb start --live。")
    try:
        choice = typer.prompt("选择操作", default="2").strip()
        while choice not in {"0", "1", "2", "3", "4", "5", "6"}:
            typer.echo("请输入 0～6。")
            choice = typer.prompt("选择操作", default="2").strip()
        if choice == "0":
            return
        kb_id = typer.prompt("知识库名称", default="cordys").strip()
        if not kb_id:
            raise typer.BadParameter("知识库名称不能为空")
        if not live and not quiet:
            show_config(load_config(config), kb_id, config)
        if choice == "1":
            while True:
                text = typer.prompt("需求文档目录", default="workdir/kb/sources/cordys").strip()
                if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
                    text = text[1:-1]
                source = Path(text).expanduser()
                if source.is_dir():
                    break
                typer.echo("目录不存在，请重新输入。")
            ingest(kb_id=kb_id, source_dir=source, config=config, root=root,
                   versions=None, live=live, quiet=quiet, cached_vision_only=False)
        elif choice in {"2", "3"}:
            query = typer.prompt("请输入问题").strip()
            if not query:
                raise typer.BadParameter("问题不能为空")
            if choice == "2":
                search(kb_id=kb_id, query=query, config=config, root=root, top_k=5,
                       document=None, snapshot=None, live=live, quiet=quiet, json_output=False)
            else:
                ask(kb_id=kb_id, query=query, config=config, root=root, snapshot=None,
                    live=live, quiet=quiet, json_output=False)
        elif choice == "4":
            chat(kb_id=kb_id, config=config, root=root, live=live, quiet=quiet)
        elif choice == "6":
            assistant(kb_id=kb_id, config=config, root=root, live=live)
        else:
            list_documents(kb_id=kb_id, root=root, json_output=False)
    except (typer.Abort, EOFError, KeyboardInterrupt):
        typer.echo("已退出知识库工作台。")
        raise typer.Exit(130) from None


@app.command()
def batch(
    kb_id: str,
    questions: Path = typer.Option(..., exists=True, dir_okay=False),
    output: Path = typer.Option(...),
    snapshot: str = typer.Option(..., "--snapshot"),
    mode: str = typer.Option("retrieval"),
    config: Path = typer.Option(Path("workdir/local.yaml"), exists=True, dir_okay=False),
    root: Path = typer.Option(Path("workdir/kb/runtime")),
    live: bool = typer.Option(False, "--live"),
    quiet: bool = typer.Option(False, "--quiet"),
) -> None:
    """固定快照逐题执行，单题失败留档后继续；金标可以另行审核。"""
    if mode not in {"retrieval", "qa"}:
        raise typer.BadParameter("mode 仅支持 retrieval 或 qa")
    labels = [json.loads(line) for line in questions.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    ids = [label["query_id"] for label in labels]
    if not ids or len(set(ids)) != len(ids):
        raise typer.BadParameter("题号不能为空或重复")
    if any(not isinstance(label.get("question"), str) or not label["question"].strip()
           for label in labels):
        raise typer.BadParameter("问题须为非空文本")
    if any("document_ids" not in label for label in labels):
        raise typer.BadParameter("每题须明确 document_ids：文档 ID 列表，或 null 表示全库")
    if not live:
        typer.echo(f"批量预览 {len(ids)} 题，模式 {mode}，真实运行需 --live。")
        return
    if output.exists():
        raise typer.BadParameter("输出目录已存在")

    async def execute() -> None:
        from tc_rag.application.kb.answer import AnswerService
        from tc_rag.bootstrap import kb_session, resolve_prompt_root

        settings = load_config(config)
        if not quiet:
            show_config(settings, kb_id, config)
        async with kb_session(settings, root, with_answer=mode == "qa") as (service, model):
            answer_service = None
            if model is not None:
                answer_service = AnswerService(
                    service, model, (resolve_prompt_root() / "kb_qa/v1.md").read_text("utf-8"),
                    counter=service.components.counter,
                    max_input_tokens=settings.runtime.max_context_tokens,
                )
            output.mkdir(parents=True, exist_ok=False)
            failures = 0
            links = []
            from tc_rag.domain.identity import new_id
            for number, label in enumerate(labels):
                try:
                    scope = label["document_ids"]
                    if scope is not None and not isinstance(scope, list):
                        raise ValueError("document_ids 必须为列表或 null")
                    selected = set(scope) if scope is not None else None
                    async with terminal_task(
                        service, f"题目 {number + 1}/{len(labels)} · 检索 → 回答/记录", quiet,
                    ):
                        result = (
                            await answer_service.ask(
                                kb_id, label["question"], snapshot_id=snapshot,
                                document_ids=selected,
                            )
                            if answer_service is not None
                            else await service.search(
                                kb_id, label["question"], snapshot_id=snapshot,
                                document_ids=selected,
                            )
                        )
                    result["query_id"] = label["query_id"]
                except Exception as error:
                    failures += 1
                    result = {"kb_id": kb_id, "query_id": label["query_id"],
                              "query": label["question"], "snapshot_id": snapshot,
                              "run_id": new_id("batch_error"),
                              "status": "error", "error_type": type(error).__name__}
                    service.components.repository.save_result(result["run_id"], result)
                links.append({"query_id": label["query_id"], "run_id": result["run_id"]})
                (output / f"{number:04d}.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
                )
            manifest = {
                "run_id": new_id("batch"),
                "kb_id": kb_id, "snapshot_id": snapshot, "mode": mode,
                "questions": len(labels), "failures": failures,
                "question_file_sha256": hashlib.sha256(questions.read_bytes()).hexdigest(),
                "items": links,
                "runtime": service.components.runtime_identity,
            }
            service.components.repository.save_result(str(manifest["run_id"]), manifest)
            (output / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            typer.echo(f"\n[批量完成] 问题 {len(labels)} 道"
                       f"｜成功 {len(labels) - failures}｜失败 {failures}")
            typer.echo(f"结果目录：{output}")

    try:
        asyncio.run(execute())
    except (KeyboardInterrupt, typer.Abort, EOFError):
        typer.echo("批量任务已中断，已保存的逐题结果保留。")
        raise typer.Exit(130) from None
    except Exception as error:
        typer.echo(f"[失败] 批量任务未完成（{type(error).__name__}），请检查配置与输出目录。",
                   err=True)
        raise typer.Exit(1) from None


@app.command()
def chat(
    kb_id: str,
    config: Path = typer.Option(Path("workdir/local.yaml"), exists=True, dir_okay=False),
    root: Path = typer.Option(Path("workdir/kb/runtime")),
    live: bool = typer.Option(False, "--live"),
    quiet: bool = typer.Option(False, "--quiet"),
) -> None:
    """终端逐题问答，证据固定为启动时快照，问题独立处理。"""
    if not live:
        typer.echo("交互问答预览；正式启动需 --live。")
        return

    async def execute() -> None:
        from tc_rag.application.kb.answer import AnswerService
        from tc_rag.bootstrap import kb_session, resolve_prompt_root

        settings = load_config(config)
        if not quiet:
            show_config(settings, kb_id, config)
        async with kb_session(settings, root, with_answer=True) as (search, model):
            value = search.components.repository.active(kb_id)
            if value is None or model is None:
                raise typer.BadParameter("知识库未发布或回答模型未装配")
            snapshot_id = value.snapshot.snapshot_id
            service = AnswerService(
                search, model, (resolve_prompt_root() / "kb_qa/v1.md").read_text("utf-8"),
                counter=search.components.counter,
                max_input_tokens=settings.runtime.max_context_tokens,
            )
            typer.echo(f"知识库 {kb_id}，快照 {snapshot_id}。逐题独立；/sources、/new、/exit。")
            last = None
            while True:
                question = typer.prompt("你")
                if question == "/exit":
                    return
                if question == "/new":
                    last = None
                    typer.echo("已清空当前记录，请输入新问题。")
                    continue
                if question == "/sources":
                    if last:
                        show_sources(last.get("evidence", []))
                    else:
                        typer.echo("当前还没有可查看的证据，请先提问。")
                    continue
                try:
                    last = None
                    async with terminal_task(search, "检索 → 重排 → 引用回答", quiet):
                        last = await service.ask(kb_id, question, snapshot_id=snapshot_id)
                    show_answer(last)
                except Exception as error:
                    typer.echo(f"本题失败（{type(error).__name__}），可继续提问。")

    try:
        asyncio.run(execute())
    except (KeyboardInterrupt, typer.Abort, EOFError):
        typer.echo("已退出知识库问答。")
        raise typer.Exit(130) from None
    except Exception as error:
        typer.echo(f"[失败] 无法进入知识库问答（{type(error).__name__}），请核对配置及快照。",
                   err=True)
        raise typer.Exit(1) from None


@app.command()
def assistant(
    kb_id: str = typer.Argument("cordys"),
    config: Path = typer.Option(Path("workdir/local.yaml"), exists=True, dir_okay=False),
    root: Path = typer.Option(Path("workdir/kb/runtime")),
    live: bool = typer.Option(False, "--live"),
) -> None:
    """测试用例生成问答助手：流式输出答案，不显示检索阶段和证据清单。"""
    if not live:
        typer.echo("问答助手预览；开始实际对话需 --live。")
        return

    async def execute() -> None:
        from tc_rag.application.kb.assistant import AssistantService
        from tc_rag.bootstrap import kb_session, resolve_prompt_root

        settings = load_config(config)
        async with kb_session(settings, root, with_answer=True) as (search, model):
            value = search.components.repository.active(kb_id)
            if value is None or model is None:
                raise typer.BadParameter("知识库未发布或回答模型未装配")
            prompt_name = "assistant_v5"
            prompt = (resolve_prompt_root() / f"kb_qa/{prompt_name}.md").read_text("utf-8")
            service = AssistantService(
                search, model, prompt, counter=search.components.counter,
                max_input_tokens=settings.runtime.max_context_tokens,
            )
            show_config(settings, kb_id, config)
            typer.echo(f"知识库快照：{value.snapshot.snapshot_id}")
            typer.echo(f"回答提示词：kb_qa/{prompt_name}.md")
            typer.echo("我是一个测试用例生成相关的AI问答助手，您有哪些疑问？")
            typer.echo("输入 /new 清空对话，/exit 退出。")
            history: list[dict[str, str]] = []
            while True:
                question = typer.prompt("你").strip()
                if not question:
                    continue
                if question in {"/exit", "exit", "quit", "退出"}:
                    return
                if question == "/new":
                    history.clear()
                    typer.echo("已清空对话记录。")
                    continue
                typer.echo("")
                parts: list[str] = []
                try:
                    async for delta in service.stream(kb_id, question, history=history):
                        parts.append(delta)
                        typer.echo(delta, nl=False)
                        sys.stdout.flush()
                    typer.echo("\n")
                    history.extend([
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": "".join(parts)},
                    ])
                    # 只保留最近三轮，避免上下文和费用无限增长。
                    history[:] = history[-6:]
                except Exception as error:
                    typer.echo(f"本次回答失败（{type(error).__name__}），可继续提问。\n")

    try:
        asyncio.run(execute())
    except (KeyboardInterrupt, typer.Abort, EOFError):
        typer.echo("\n已退出问答助手。")
        raise typer.Exit(130) from None
    except Exception as error:
        typer.echo(f"[失败] 无法启动问答助手（{type(error).__name__}），请核对配置与知识库。",
                   err=True)
        raise typer.Exit(1) from None


def assistant_main() -> None:
    """独立入口 kb-assistant，等价于 `tc-rag kb assistant`。"""
    typer.run(assistant)


@app.command()
def ask(
    kb_id: str,
    query: str = typer.Option(..., "--query", "-q"),
    config: Path = typer.Option(Path("workdir/local.yaml"), exists=True, dir_okay=False),
    root: Path = typer.Option(Path("workdir/kb/runtime")),
    snapshot: str | None = typer.Option(None, "--snapshot"),
    live: bool = typer.Option(False, "--live"),
    quiet: bool = typer.Option(False, "--quiet"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """带引用回答；--live 会调用向量、可选重排与生成模型。"""
    if not live:
        typer.echo("问答预览；真实执行需 --live。")
        return

    async def execute() -> None:
        from tc_rag.application.kb.answer import AnswerService
        from tc_rag.bootstrap import kb_session, resolve_prompt_root

        settings = load_config(config)
        if not quiet and not json_output:
            show_config(settings, kb_id, config)
        prompt = (resolve_prompt_root() / "kb_qa/v1.md").read_text(encoding="utf-8")
        async with kb_session(settings, root, with_answer=True) as (search_service, model):
            if model is None:
                raise ValueError("未装配回答模型")
            service = AnswerService(
                search_service, model, prompt, counter=search_service.components.counter,
                max_input_tokens=settings.runtime.max_context_tokens,
            )
            async with terminal_task(search_service, "检索 → 重排 → 引用回答", quiet):
                result = await service.ask(kb_id, query, snapshot_id=snapshot)
            output = root.parent / "output" / "answer"
            output.mkdir(parents=True, exist_ok=True)
            text = json.dumps(result, ensure_ascii=False, indent=2)
            (output / f"{result['run_id']}.json").write_text(text, encoding="utf-8")
            show_result(
                result, answer=True, raw=json_output, output=output / f"{result['run_id']}.json",
            )

    try:
        asyncio.run(execute())
    except Exception as error:
        typer.echo(f"问答失败（{type(error).__name__}），不计为拒答。", err=True)
        raise typer.Exit(1) from None


@app.command()
def evaluate(
    kb_id: str,
    gold: Path = typer.Option(..., exists=True, dir_okay=False),
    results: Path = typer.Option(..., exists=True, file_okay=False),
    output: Path = typer.Option(...),
    mode: Literal["retrieval", "qa"] = typer.Option("retrieval"),
    reviews: Path | None = typer.Option(None, exists=True, dir_okay=False),
    overlap_threshold: float = typer.Option(0.5, min=0.001, max=1.0),
) -> None:
    """离线复算已保存检索结果；gold 是审核后的 JSONL，results 是逐题 JSON 目录。"""
    from tc_rag.application.kb.answer_evaluation import score_answer
    from tc_rag.application.kb.evaluate import score_retrieval, summarize_metrics

    report_path = output.with_suffix(".md")
    if output.suffix.lower() != ".json":
        raise typer.BadParameter("output 须为 .json 文件")
    if output.exists() or report_path.exists():
        raise typer.BadParameter("输出已存在，拒绝覆盖")
    judgments = []
    if mode == "qa":
        if reviews is None:
            raise typer.BadParameter("问答评分需要 --reviews 人工审核 JSONL")
        judgments = [json.loads(line) for line in reviews.read_text("utf-8").splitlines()
                     if line.strip()]
    labels = [json.loads(line) for line in gold.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    if len({label["query_id"] for label in labels}) != len(labels):
        raise typer.BadParameter("金标题号重复")
    items = [json.loads(path.read_text(encoding="utf-8"))
             for path in sorted(results.glob("*.json"))]
    rows = []
    for label in labels:
        if label.get("review_status") != "reviewed":
            raise typer.BadParameter("评测仅接受 reviewed 金标")
        matches = [item for item in items
                   if item.get("kb_id") == kb_id and item.get("query") == label["question"]
                   and item.get("query_id", label["query_id"]) == label["query_id"]]
        if len(matches) != 1:
            raise typer.BadParameter("每道金标题须对应唯一一次检索；请为各实验使用独立目录")
        result = matches[0]
        if label.get("snapshot_id") != result.get("snapshot_id"):
            raise typer.BadParameter("金标必须绑定本次评测快照")
        if result.get("status") == "error":
            rows.append({"query_id": label["query_id"], "status": "error",
                         "query_family": label.get("query_family", label["query_id"])})
            continue
        if label.get("document_ids") != result.get("document_ids"):
            if label.get("document_ids") is not None:
                raise typer.BadParameter("金标与检索文档范围不一致")
        if mode == "qa":
            matched_reviews = [review for review in judgments
                               if review.get("run_id") == result["run_id"]]
            if len(matched_reviews) != 1:
                raise typer.BadParameter("每个回答须有唯一人工审核")
            rows.append(score_answer(result, label, matched_reviews[0]))
        else:
            rows.append(score_retrieval(result, label, overlap_threshold=overlap_threshold))
    if not rows:
        raise typer.BadParameter("没有金标题目")
    metrics = summarize_metrics(rows) if mode == "retrieval" else summarize_metrics(
        rows, keys=("correctness", "faithfulness", "citation_precision",
                    "citation_recall", "completeness"),
    )
    if mode == "qa":
        tp = sum(row.get("refusal_tp", 0) for row in rows)
        fp = sum(row.get("refusal_fp", 0) for row in rows)
        fn = sum(row.get("refusal_fn", 0) for row in rows)
        metrics["refusal"] = {"tp": tp, "fp": fp, "fn": fn,
                              "precision": tp / (tp + fp) if tp + fp else None,
                              "recall": tp / (tp + fn) if tp + fn else None}
    output.parent.mkdir(parents=True, exist_ok=True)
    from tc_rag.domain.identity import fingerprint

    semantic_fields = (
        "query_id", "question", "query_family", "document_ids", "evidence_groups",
        "answer_units", "scope_complete", "should_abstain",
    )
    semantic_gold = [{key: label.get(key) for key in semantic_fields}
                     for label in sorted(labels, key=lambda item: item["query_id"])]
    with output.open("x", encoding="utf-8") as stream:
        json.dump({"kb_id": kb_id, "metrics": metrics, "rows": rows,
                   "mode": mode,
                   "gold_file_sha256": hashlib.sha256(gold.read_bytes()).hexdigest(),
                   "gold_semantic_fingerprint": fingerprint(semantic_gold),
                   "protocol": {"version": "kb_eval_v1", "overlap": overlap_threshold},
                   "total_questions": len(rows),
                   "failures": sum(row.get("status") == "error" for row in rows)}, stream,
                  ensure_ascii=False, indent=2)
    with report_path.open("x", encoding="utf-8") as stream:
        stream.write(f"# 知识库评测结果\n\n知识库 {kb_id}，模式 {mode}，题数 {len(rows)}。\n\n")
        stream.write("以下指标限于已审核题目。未知项及失败保留在 JSON 明细。\n\n")
        stream.write("| 指标 | 结果 |\n|---|---|\n")
        for name, metric in metrics.items():
            stream.write(f"| {name} | {json.dumps(metric, ensure_ascii=False)} |\n")
    show_metrics(metrics)
    typer.echo(f"\n[评分完成] 问题 {len(rows)} 道｜失败 "
               f"{sum(row.get('status') == 'error' for row in rows)} 道")
    typer.echo(f"详细结果：{output}\n可读报告：{report_path}")


@app.command()
def compare(
    baseline: Path = typer.Option(..., exists=True, dir_okay=False),
    candidate: Path = typer.Option(..., exists=True, dir_okay=False),
    output: Path = typer.Option(...),
) -> None:
    """比较同金标、同协议的两组结果，输出配对差值与区间。"""
    from tc_rag.application.kb.compare import compare_reports

    if output.exists():
        raise typer.BadParameter("拒绝覆盖已有对照结果")
    result = compare_reports(
        json.loads(baseline.read_text("utf-8")), json.loads(candidate.read_text("utf-8")),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    show_metrics(result["metrics"], difference=True)
    typer.echo(f"\n配对对照：{output}")


@app.command()
def search(
    kb_id: str,
    query: str = typer.Option(..., "--query", "-q"),
    config: Path = typer.Option(Path("workdir/local.yaml"), exists=True, dir_okay=False),
    root: Path = typer.Option(Path("workdir/kb/runtime")),
    top_k: int = typer.Option(5, min=1),
    document: list[str] | None = typer.Option(None, "--document"),
    snapshot: str | None = typer.Option(None, "--snapshot"),
    live: bool = typer.Option(False, "--live"),
    quiet: bool = typer.Option(False, "--quiet"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """仅检索并返回候选与来源，显式 --live 才调用查询 embedding。"""
    if not live:
        typer.echo("检索预览；正式查询需 --live，会调用 embedding，不调用回答模型。")
        return

    async def execute() -> None:
        from tc_rag.bootstrap import kb_session

        settings = load_config(config)
        if not quiet and not json_output:
            show_config(settings, kb_id, config)
        async with kb_session(settings, root) as (service, _):
            async with terminal_task(service, "固定快照 → 召回证据 → 可选重排", quiet):
                result = await service.search(
                    kb_id, query, top_k=top_k,
                    document_ids=set(document) if document is not None else None,
                    snapshot_id=snapshot,
                )
            output = root.parent / "output" / "search"
            run_id = result["run_id"]
            output.mkdir(parents=True, exist_ok=True)
            result["run_id"] = run_id
            text = json.dumps(result, ensure_ascii=False, indent=2)
            (output / f"{run_id}.json").write_text(text, encoding="utf-8")
            show_result(result, answer=False, raw=json_output, output=output / f"{run_id}.json")

    try:
        asyncio.run(execute())
    except Exception as error:
        typer.echo(f"检索失败（{type(error).__name__}），请核对配置与快照。", err=True)
        raise typer.Exit(1) from None


@app.command()
def ingest(
    kb_id: str,
    source_dir: Path = typer.Option(..., exists=True, file_okay=False),
    config: Path = typer.Option(Path("workdir/local.yaml"), exists=True, dir_okay=False),
    root: Path = typer.Option(Path("workdir/kb/runtime")),
    versions: Path | None = typer.Option(None, exists=True, dir_okay=False),
    live: bool = typer.Option(False, "--live", help="启用真实 embedding，可能产生费用。"),
    quiet: bool = typer.Option(False, "--quiet"),
    cached_vision_only: bool = typer.Option(
        False, "--cached-vision-only", help="仅复用已有图片缓存，任何未命中立即停止。",
    ),
) -> None:
    """以目录内 PDF/DOCX 完整集合替换知识库成员，文件名作为逻辑标识。"""
    paths = sorted(p for p in source_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in {".docx", ".pdf"})
    if not paths:
        raise typer.BadParameter("目录没有 PDF/DOCX")
    typer.echo(f"知识库 {kb_id}，完整批次 {len(paths)} 份 PDF/DOCX")
    product_versions = None
    if versions is not None:
        product_versions = json.loads(versions.read_text("utf-8"))
        if (
            not isinstance(product_versions, dict)
            or not set(product_versions).issubset({path.name for path in paths})
            or any(value is not None and (not isinstance(value, str) or not value.strip())
                   for value in product_versions.values())
        ):
            raise typer.BadParameter("versions 须为本批次文件名到非空产品版本或 null 的 JSON 对象")
    for path in paths:
        typer.echo(path.name)
    if not live:
        typer.echo("预览完成。正式入库需显式 --live，省略旧成员将从新快照移除。")
        return
    if cached_vision_only:
        typer.echo("[缓存限定恢复] 图片结果仅从缓存读取，未命中立即停止；先执行向量预检。")

    async def execute() -> None:
        from tc_rag.bootstrap import kb_ingestion_session

        settings = load_config(config)
        if not quiet:
            show_config(settings, kb_id, config)
        async with kb_ingestion_session(
            settings, root, cached_vision_only=cached_vision_only,
        ) as service:
            async with terminal_task(service, "接入文档 → 图文解析与分块 → 向量化 → 发布", quiet):
                result = await service.ingest(
                    kb_id, {p.name: p for p in paths},
                    target=settings.chunking.target_tokens,
                    overlap=settings.chunking.overlap_tokens,
                    product_versions=product_versions,
                    preflight=True,
                )
            typer.echo(f"\n[入库完成] 文档 {len(result.snapshot.documents)} 份"
                       f"｜片段 {len(result.chunks)} 条｜问题 {len(result.issues)} 项")
            typer.echo(f"快照：{result.snapshot.snapshot_id}\n运行目录：{root}")

    try:
        asyncio.run(execute())
    except Exception as error:
        typer.echo(f"入库失败（{type(error).__name__}），请核对配置及入库记录。", err=True)
        raise typer.Exit(1) from None


@app.command(name="list")
def list_documents(
    kb_id: str, root: Path = typer.Option(Path("workdir/kb/runtime")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看当前发布快照及其成员。"""
    database = root / "data.sqlite"
    if not database.exists():
        typer.echo('{"status":"not_found"}' if json_output else "尚无已发布知识库")
        return
    value = KnowledgeSqliteRepository(database).active(kb_id)
    if json_output:
        typer.echo(json.dumps(
            value.snapshot.model_dump(mode="json") if value else {"status": "not_found"},
            ensure_ascii=False, indent=2,
        ))
    elif value is None:
        typer.echo("该知识库尚未发布，请先入库文档。")
    else:
        typer.echo(f"\n[知识库] {kb_id}｜文档 {len(value.snapshot.documents)} 份"
                   f"｜片段 {len(value.chunks)} 条")
        typer.echo(f"快照：{value.snapshot.snapshot_id}")
        for number, document in enumerate(value.snapshot.documents, 1):
            typer.echo(f"  {number}. {document.document_id}"
                       f"｜产品版本：{document.product_version or '待确认'}")
