"""导出可阅读报告及目标十一列 Excel；这些文件都是 SQLite 事实的视图。"""

import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]
from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]

DEFAULT_COLUMNS = [
    ("title", "用例名称"),
    ("external_id", "ID"),
    ("preconditions", "前置条件"),
    ("module_path", "所属模块"),
    ("actions", "步骤描述"),
    ("expected", "预期结果"),
    ("tags", "标签"),
    ("remarks", "备注"),
    ("priority", "用例等级"),
    ("product_version", "CRM版本"),
    ("edit_mode", "编辑模式"),
]


def _excel_value(value: Any) -> Any:
    """仅改变 Excel 展示：控制字符可见转义，原始事实及换行、制表符不变。"""
    if isinstance(value, str):
        # 必须在 append 前处理；openpyxl 在赋值时就会拒绝 XML 不允许的控制字符。
        # 不直接删除，保留字符码供用户追溯；已有字面量转义不会被再次转换。
        return ILLEGAL_CHARACTERS_RE.sub(lambda match: f"\\u{ord(match[0]):04x}", value)
    return value


class ReportExporter:
    def __init__(self, case_columns: list[tuple[str, str]] | None = None):
        self.case_columns = case_columns or DEFAULT_COLUMNS

    def export(self, payload: dict[str, Any], destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        suffix = destination.suffix.lower()
        if suffix == ".json":
            destination.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        elif suffix == ".md":
            lines = [
                "# 测试设计运行报告",
                "",
                "自动检查不代表人工批准；语义、覆盖率及业务预期需要审核。",
                "",
            ]
            labels = {
                "rules": "需求规则",
                "points": "测试点",
                "cases": "测试用例",
                "evidence": "原文证据",
                "issues": "待审核问题",
                "coverage": "覆盖关联",
                "features": "功能",
                "quality": "质量统计",
                "document": "文档来源",
            }
            for section, values in payload.items():
                if section in {"schema_version", "tree", "entities"}:
                    continue
                lines.extend([f"## {labels.get(section, section)}", ""])
                if isinstance(values, list):
                    for index, item in enumerate(values, 1):
                        if not isinstance(item, dict):
                            lines.extend([f"- {item}", ""])
                            continue
                        title = next(
                            (
                                item[k]
                                for k in ("title", "objective", "statement", "name", "description")
                                if item.get(k)
                            ),
                            f"记录 {index}",
                        )
                        lines.extend([f"### {index}. {title}", ""])
                        for key, value in item.items():
                            if key == "schema_version":
                                continue
                            if key == "steps":
                                for number, step in enumerate(value, 1):
                                    lines.extend(
                                        [
                                            f"{number}. 操作：{step['action']}",
                                            f"   预期：{step['expected']}",
                                            "",
                                        ]
                                    )
                            else:
                                rendered = (
                                    json.dumps(value, ensure_ascii=False)
                                    if isinstance(value, (dict, list))
                                    else str(value)
                                )
                                lines.extend([f"- {key}：{rendered}", ""])
                elif isinstance(values, dict):
                    lines.extend([f"- {key}：{value}\n" for key, value in values.items()])
                else:
                    lines.extend([str(values), ""])
            destination.write_text("\n".join(lines), encoding="utf-8")
        elif suffix == ".xlsx":
            self._excel(payload.get("cases", []), destination, payload, self.case_columns)
        else:
            raise ValueError("导出仅支持 json/md/xlsx")
        return destination

    @staticmethod
    def _excel(
        cases: list[dict[str, Any]],
        destination: Path,
        payload: dict[str, Any] | None = None,
        columns: list[tuple[str, str]] | None = None,
    ) -> None:
        book = Workbook()
        sheet = book.active
        assert sheet is not None
        sheet.title = "模版"
        columns = columns or DEFAULT_COLUMNS
        sheet.append([_excel_value(header) for _, header in columns])
        for case in cases:
            steps = case["steps"]
            remarks = case.get("remarks") or ""
            if case.get("status") != "validated":
                remarks += "\n[需核实：自动校验发现风险，详见问题清单]"
            values = {
                **case,
                "preconditions": "\n".join(case.get("preconditions", [])),
                "actions": "\n".join(f"[{i}] {s['action']}" for i, s in enumerate(steps, 1)),
                "expected": "\n".join(f"[{i}] {s['expected']}" for i, s in enumerate(steps, 1)),
                "tags": ",".join(case.get("tags", [])),
                "remarks": remarks.strip(),
                "edit_mode": case.get("edit_mode", "STEP"),
            }
            sheet.append([_excel_value(values.get(field)) for field, _ in columns])
            # 强制文本，避免把需求中的 =、+、-、@ 解释成电子表格公式。
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="305496")
        for index, (field, _) in enumerate(columns, 1):
            sheet.column_dimensions[get_column_letter(index)].width = (
                55 if field in {"actions", "expected"} else 26
            )
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        payload = payload or {}
        for title, key, fields in (
            ("测试点", "points", ["point_id", "objective", "technique", "rule_ids", "status"]),
            ("规则", "rules", ["rule_id", "statement", "evidence_ids", "status"]),
            (
                "覆盖矩阵",
                "coverage",
                ["case_id", "step_no", "rule_id", "point_id", "validation_status"],
            ),
            ("问题清单", "issues", ["issue_id", "kind", "description", "object_ids"]),
            ("未解析范围", "issues", ["issue_id", "description", "object_ids"]),
        ):
            appendix = book.create_sheet(title)
            appendix.append(fields)
            for item in payload.get(key, []):
                if title == "未解析范围" and item.get("kind") != "unparsed":
                    continue
                appendix.append(
                    [
                        _excel_value(
                            json.dumps(item.get(f), ensure_ascii=False)
                            if isinstance(item.get(f), (dict, list))
                            else item.get(f)
                        )
                        for f in fields
                    ]
                )
                for cell in appendix[appendix.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
            appendix.freeze_panes = "A2"
        book.save(destination)
