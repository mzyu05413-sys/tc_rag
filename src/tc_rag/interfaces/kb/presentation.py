"""知识库终端阅读层，结构化事实继续保留为 SQLite 和 JSON。"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import typer

from tc_rag.application.kb.ingest import IngestionService
from tc_rag.application.kb.search import SearchService
from tc_rag.application.progress import ProgressEvent
from tc_rag.core.config import AppConfig
from tc_rag.interfaces.progress import TerminalProgress, format_duration


def show_config(config: AppConfig, kb_id: str, path: Path) -> None:
    typer.echo(f"配置：{path}｜知识库：{kb_id}")
    typer.echo(f"向量模型：{config.models.embedding.model or '未配置'}"
               f"｜后端：{config.retrieval.backend}｜度量：{config.retrieval.metric}")
    mode_label = {
        "vector": "纯向量", "hybrid": "混合", "keyword": "纯关键词",
    }[config.retrieval.mode]
    rerank_label = config.models.rerank.model if config.models.rerank.enabled else "关闭"
    typer.echo(f"检索：{mode_label}｜重排：{rerank_label}")
    vision = config.models.vision.model if config.parsing.vision_enabled else "关闭"
    typer.echo(f"回答模型：{config.models.llm.model or '未配置'}｜图片解释：{vision}")


@asynccontextmanager
async def terminal_task(
    service: IngestionService | SearchService, flow: str, quiet: bool = False,
) -> AsyncIterator[None]:
    previous = service.progress
    if quiet:
        service.progress = None
        try:
            yield
        finally:
            service.progress = previous
        return
    async with TerminalProgress(flow_label=f"[流程] {flow}") as progress:
        service.progress = progress
        try:
            yield
            progress(ProgressEvent("stage", "completed"))
        except BaseException:
            progress(ProgressEvent("stage", "failed"))
            raise
        finally:
            service.progress = previous


def source_label(sources: list[dict[str, Any]]) -> str:
    labels = []
    for source in sources:
        label = source.get("document_id", source.get("document_version", "来源待定位"))
        locator = source.get("locator", {})
        if locator.get("page") is not None:
            label += f" · 第 {locator['page']} 页"
        elif locator.get("table_index") is not None:
            label += f" · 表格 {locator['table_index'] + 1}"
            if locator.get("row_index") is not None:
                label += f" 第 {locator['row_index'] + 1} 行"
        elif locator.get("paragraph_index") is not None:
            label += f" · 段落 {locator['paragraph_index'] + 1}"
        if source.get("origin") == "vision":
            label += " · 图片解释"
        if label not in labels:
            labels.append(label)
    return "；".join(labels) or "来源待定位"


def show_sources(evidence: list[dict[str, Any]]) -> None:
    for number, item in enumerate(evidence, 1):
        typer.echo(f"[{number}] {source_label(item.get('sources', []))}")
        text = item["text"]
        typer.echo(f"    {text[:300]}{'…' if len(text) > 300 else ''}")


def show_search(result: dict[str, Any]) -> None:
    typer.echo(f"\n[检索完成] 命中 {len(result['candidates'])} 条"
               f"｜用时 {format_duration(result['timings']['total_seconds'])}")
    typer.echo(f"快照：{result['snapshot_id']}")
    if not result["candidates"]:
        typer.echo("指定范围未返回候选证据。")
    for candidate in result["candidates"]:
        score = candidate.get("raw_score")
        score_text = f"{score:.4f}" if score is not None else "仅关键词命中"
        typer.echo(f"\n[{candidate['rank']}] {source_label(candidate['sources'])}")
        typer.echo(f"    向量原始分数：{score_text}")
        if candidate.get("fusion_score") is not None:
            typer.echo(f"    融合分数：{candidate['fusion_score']:.6f}")
        if candidate.get("rerank_score") is not None:
            typer.echo(f"    重排分数：{candidate['rerank_score']:.4f}")
        text = candidate["text"]
        typer.echo(f"    {text[:300]}{'…' if len(text) > 300 else ''}")
    if result.get("rerank_degradation"):
        typer.echo("[提示] 重排降级，本次保留召回顺序。")


def show_answer(result: dict[str, Any]) -> None:
    answer = result["answer"]
    if answer["status"] == "abstained":
        typer.echo(f"\n[证据不足] {answer.get('reason', '')}")
    else:
        typer.echo("\n[回答]")
        evidence = {item["chunk_id"]: index for index, item in enumerate(result["evidence"], 1)}
        for claim in answer["claims"]:
            numbers = list(dict.fromkeys(evidence[c["chunk_id"]] for c in claim["citations"]))
            typer.echo(f"  {claim['text']} " + " ".join(f"[{n}]" for n in numbers))
    typer.echo(f"\n用时：{format_duration(result['total_seconds'])}｜运行：{result['run_id']}")
    if result["evidence"]:
        typer.echo("\n[证据来源]")
        show_sources(result["evidence"])


def show_result(result: dict[str, Any], *, answer: bool, raw: bool, output: Path) -> None:
    if raw:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        typer.echo(f"完整结果：{output}", err=True)
        return
    show_answer(result) if answer else show_search(result)
    typer.echo(f"\n完整结果：{output}")


def show_metrics(metrics: dict[str, Any], *, difference: bool = False) -> None:
    names = {
        "recall": "证据召回率", "hit": "命中率", "mrr": "首条相关证据排名 MRR",
        "precision": "候选精确率", "all_evidence_found": "完整证据命中率",
        "ndcg": "排序质量 nDCG", "correctness": "回答正确率",
        "faithfulness": "回答忠实性", "citation_precision": "引用精确率",
        "citation_recall": "引用召回率", "completeness": "答案完整性",
    }
    typer.echo("\n[配对结果] 候选配置减基线配置" if difference else "\n[评测结果]")
    for name, value in metrics.items():
        if name == "refusal":
            precision, recall = value.get("precision"), value.get("recall")
            p = f"{precision:.4f}" if precision is not None else "不可计算"
            r = f"{recall:.4f}" if recall is not None else "不可计算"
            typer.echo(f"  拒答精确率：{p}｜拒答召回率：{r}")
            continue
        mean, interval = value.get("mean"), value.get("ci95")
        text = f"{mean:.4f}" if mean is not None else "不可计算"
        bounds = f"[{interval[0]:.4f}, {interval[1]:.4f}]" if interval else "未计算"
        typer.echo(f"  {names.get(name, name)}：{text}｜有效样本 {value.get('n', 0)}"
                   f"｜95% 区间 {bounds}")
