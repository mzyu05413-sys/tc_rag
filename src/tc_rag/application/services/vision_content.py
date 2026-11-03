"""组织图文上下文和可追溯的视觉文字，不调用外部服务。"""

import json
from contextvars import ContextVar
from typing import Any

from tc_rag.domain.models import DocumentBlock
from tc_rag.domain.vision import VisionDescription

# 只关联本地审计回调，不放入模型消息，也不改变业务 ID。
vision_image_id: ContextVar[str | None] = ContextVar("vision_image_id", default=None)


def image_context(
    blocks: list[DocumentBlock], index: int, neighbors: int, char_budget: int
) -> str:
    image = blocks[index]
    candidates: list[tuple[int, str, DocumentBlock]] = []
    for direction, indexes in (
        ("before", range(index - 1, -1, -1)),
        ("after", range(index + 1, len(blocks))),
    ):
        found = 0
        for position in indexes:
            block = blocks[position]
            if block.document_id != image.document_id or block.heading_path != image.heading_path:
                break
            if block.kind == "image" or block.origin != "text" or not block.content.strip():
                continue
            if found >= neighbors:
                break
            candidates.append((position, direction, block))
            found += 1
    excerpts: list[dict[str, Any]] = []
    # 先均分预算，避免上一长段吃光全部额度，使图片后的业务解释永远无法进入。
    per_block = char_budget // len(candidates) if candidates else 0
    for _, direction, block in sorted(candidates, key=lambda item: item[0]):
        text = block.content[:per_block]
        if text:
            excerpts.append({
                "context_id": f"context_{len(excerpts) + 1}", "position": direction,
                "block_id": block.block_id, "text": text,
                "start_char": 0, "end_char": len(text),
                "truncated": len(text) < len(block.content),
                "locator": block.locator.model_dump(mode="json"),
            })
    return json.dumps(
        {"heading_path": image.heading_path, "neighbors": excerpts}, ensure_ascii=False
    )


def render_description(description: VisionDescription) -> str:
    """保留事实、关系、图文解释和疑点的边界；不将模型核对称为人工确认。"""
    lines = [f"图片解释（未确认事实）；图片类型：{description.image_type}"]
    if description.facts:
        lines.append("\n【图片可见事实】")
        lines.extend(
            f"- {fact.fact_id} [{','.join(fact.view_ids)}] {fact.text}"
            for fact in description.facts
        )
    if description.relations:
        lines.append("\n【图片明确表达的关系】")
        lines.extend(
            f"- [{','.join(item.fact_ids)}] {item.text}" for item in description.relations
        )
    for table in description.tables:
        lines.append(f"\n【图片表格：{table.title or '未命名'}；{','.join(table.view_ids)}】")
        lines.append(json.dumps({"headers": table.headers, "rows": table.rows}, ensure_ascii=False))
    if description.context_links:
        lines.append("\n【正文关联解释；不能冒充图片原文，仍待核实】")
        lines.extend(
            f"- [{','.join(item.context_ids)}；{','.join(item.fact_ids)}] {item.text}"
            for item in description.context_links
        )
    if description.uncertainties:
        lines.append("\n【无法辨认、歧义与缺漏】")
        lines.extend(
            f"- [{','.join(item.view_ids)}] {item.text}" for item in description.uncertainties
        )
    return "\n".join(lines)
