"""分块离线实验的阅读版与标注材料输出；仅写派生目录，不写业务库。"""

import json
import re
from pathlib import Path
from typing import Any

from tc_rag.application.services.chunk_gold import ChunkGold
from tc_rag.domain.models import DocumentBlock


def _code(text: str) -> str:
    fence = "`" * max(3, max((len(s) + 1 for s in re.findall(r"`+", text)), default=3))
    return f"{fence}text\n{text}\n{fence}"


def write_context_review(
    directory: Path, blocks: list[DocumentBlock], preview: dict[str, Any],
) -> None:
    """把授权正文与背景分开，避免仅看 Block ID 误以为可以跨范围引用。"""
    by = {b.block_id: b for b in blocks}
    lines = [
        "# 提炼正文与邻文对照", "", f"窗口策略：{preview['policy']}；模型调用：0。", "",
        "这是字符窗口预览。正式请求还包含原始 Block、提示词、指令与 Schema；",
        "会按完整请求 token 预算删减背景，核心仍超预算则拒绝调用，不能把本页当成实际模型输入。",
        "同一 Block 的其他区间也是背景；只有下列 allowed_sources 可引用。",
        "背景只帮助理解和发现待澄清问题，不自动补成确定规则。vision 仍需核实。", "",
    ]
    for chunk in preview["chunks"]:
        lines.extend([f"## {chunk['chunk_id']}", "", "### 可引用正文", ""])
        for source in chunk["allowed_sources"]:
            block = by[source["block_id"]]
            start, end = source["start_char"], source["end_char"]
            lines.extend([
                f"- {block.block_id} [{start}, {end})；来源：{block.origin}",
                f"- 章节：{' / '.join(block.heading_path) or '未知'}；"
                f"定位：{block.locator.model_dump_json()}",
                "", _code(block.content[start:end]), "",
            ])
        lines.extend(["### 仅作背景（不可引用）", ""])
        if not chunk["neighbor_context"]:
            lines.extend(["无（已关闭、预算为零或没有可提供的邻文）。", ""])
        for item in chunk["neighbor_context"]:
            lines.extend([
                f"- {item['block_id']} [{item['start_char']}, {item['end_char']})；"
                f"{item['position']} / {item['scope']}；来源：{item['origin']}；"
                f"局部：{item['truncated']}",
                f"- 章节：{' / '.join(item['heading_path']) or '未知'}；"
                f"定位：{json.dumps(item['locator'], ensure_ascii=False)}", "",
                _code(item["text"]), "",
            ])
    (directory / "extraction_context.md").write_text("\n".join(lines), encoding="utf-8")


def write_evaluation_materials(
    directory: Path, blocks: list[DocumentBlock], gold: ChunkGold | None,
    matrix: dict[str, Any] | None,
) -> None:
    if gold is not None:
        (directory / "gold.review.json").write_text(
            gold.model_dump_json(indent=2), encoding="utf-8"
        )
        by = {b.block_id: b for b in blocks}
        lines = [
            "# 分块需求单元审核", "",
            f"来源运行：{gold.run_id}", "",
            "候选默认 pending，不能计为已通过或金标。确认是一条完整约束后改 confirmed；",
            "不含测试约束的标题/格式或重复表头可排除；不能只因描述 UI 展示就排除需求。",
            "多条需求须拆分 ID/区间，",
            "跨段的条件、行为、例外可以保存在同一单元的多个 sources。不要只改文字不改区间。",
            "vision 资料仍是待核实解释；确认标注单元不等于确认图片业务事实。", "",
            "复制 gold.review.json 为新的私有标注文件后编辑；--gold 会验证运行、原文指纹、",
            "唯一 ID、逐字引文和区间。评分只用于此抽样，不证明全文规则或用例质量。", "",
        ]
        for unit in gold.units:
            lines.extend([f"## {unit.unit_id} [{unit.status}]", "", unit.description, ""])
            if unit.note:
                lines.extend([unit.note, ""])
            for source in unit.sources:
                block = by[source.block_id]
                lines.extend([
                    f"- {source.block_id} [{source.start_char}, {source.end_char})",
                    f"- 章节：{' / '.join(block.heading_path) or '未知'}；来源：{block.origin}",
                    f"- 定位：{block.locator.model_dump_json()}", "", _code(source.quote), "",
                ])
        (directory / "gold_review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if matrix is not None:
        (directory / "budget_matrix.json").write_text(
            json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lines = [
            "# 分块预算对照", "", matrix["note"], "",
            "|策略/target/overlap|块数|纯标题|超预算|原文覆盖|实际重叠|金标完整/已确认|"
            "边界完整率|分块文字估算 token|本地分块 ms|结构检查/稳定 ID|",
            "|---|---:|---:|---:|---|---|---|---|---:|---:|---|",
        ]
        for row in matrix["variants"]:
            stats, coverage, labels = row["statistics"], row["coverage"], row["gold"]
            overlap = stats["overlap"]
            rate = labels["boundary_completeness"]
            lines.append(
                f"|{row['variant_id']}|{stats['chunks']}|{stats['heading_only_chunks']}|"
                f"{len(stats['oversize'])}|{coverage['source_character_coverage']}|"
                f"{overlap['overlapping_pairs']}/{overlap['eligible_pairs']}|"
                f"{labels['complete_units']}/{labels['confirmed_units']}|"
                f"{rate if rate is not None else '未评估'}|{row['chunk_text_estimated_tokens']}|"
                f"{row['local_chunking_ms']:.2f}|{coverage['passed']} / {row['stable_chunk_ids']}|"
            )
        lines.extend([
            "", "无 confirmed 标注时边界完整率为 null/未评估，不是 0 或 100%。",
            "原文覆盖、重叠、抽样边界完整率是不同指标；不能按块最少或重叠最高自动选参数。",
            "预算和表头取舍仍可能产生风险 Issue，详见 JSON 各变体的 issues。", "",
        ])
        (directory / "budget_matrix.md").write_text("\n".join(lines), encoding="utf-8")
