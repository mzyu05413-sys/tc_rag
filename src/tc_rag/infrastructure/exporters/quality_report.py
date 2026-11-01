"""把只读质检结果渲染为固定的 Markdown 检查表。"""

import json
from typing import Any


def _clean(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", "").replace("\n", " ")


def _counts(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "-"
    return "、".join(f"{key}={_clean(item)}" for key, item in value.items())


def render_quality_markdown(report: dict[str, Any]) -> str:
    overall = report.get("overall", {})
    counts = overall.get("counts", {}) if isinstance(overall, dict) else {}
    lines = [
        "# 运行质量检查",
        "",
        f"- 运行：`{_clean(report.get('run_id', ''))}`",
        f"- 状态：{_clean(report.get('run_status', ''))}",
        f"- 文档：{_clean((report.get('document') or {}).get('name', ''))}",
        f"- 总体：**{_clean(overall.get('status', ''))}**",
        f"- 自动检查：pass {counts.get('pass', 0)} / warn {counts.get('warn', 0)} / "
        f"fail {counts.get('fail', 0)} / not_applicable {counts.get('not_applicable', 0)}",
        f"- 语义审核：{_clean(report.get('semantic_review', ''))}",
        "",
        "> 自动检查只核对已保存快照中的结构、引用和区间，不判断需求语义是否正确；"
        "`not_performed` 表示人工语义审核尚未执行，不能写成通过。",
        "",
        "## 检查项",
        "",
        "| 检查 | 阶段 | 状态 | 计数 | 说明 | 对象 |",
        "|---|---|---|---|---|---|",
    ]
    for check in report.get("checks", []):
        objects = check.get("object_ids", [])
        shown = "、".join(f"`{_clean(item)}`" for item in objects[:6])
        if len(objects) > 6:
            shown += f" 等 {len(objects)} 个"
        lines.append(
            f"| {_clean(check.get('check_id', ''))} | {_clean(check.get('stage', ''))} | "
            f"{_clean(check.get('status', ''))} | {_counts(check.get('counts'))} | "
            f"{_clean(check.get('summary', ''))} | {shown or '-'} |"
        )
    lines += ["", "## 边界", ""]
    for item in report.get("limits", []):
        lines.append(f"- {_clean(item)}")
    details = [
        (check, check.get("detail", {}))
        for check in report.get("checks", [])
        if check.get("detail")
    ]
    if details:
        lines += ["", "## 详情", ""]
        for check, detail in details:
            lines.append(f"### {_clean(check.get('check_id', ''))}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(detail, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
