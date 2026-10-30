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

_CHINESE_HEADERS = {
    "point_id": "测试点ID", "objective": "测试目标", "technique": "测试方法",
    "rule_ids": "关联规则ID", "rule_id": "规则ID", "statement": "规则说明",
    "evidence_ids": "引用证据ID", "status": "技术状态", "applicability": "适用性",
    "kind": "规则类型",
    "case_id": "用例ID", "step_no": "步骤序号", "validation_status": "校验状态",
    "issue_id": "问题ID", "description": "问题说明", "object_ids": "关联对象ID",
    "severity": "严重程度", "display_category": "问题性质",
}
_CHINESE_ENUMS = {
    "technique": {
        "equivalence": "等价类", "boundary": "边界值", "decision_table": "判定表",
        "state": "状态迁移", "permission": "权限", "workflow": "业务流程",
        "error_behavior": "异常行为", "other": "其他",
    },
    "kind": {
        "field_constraint": "字段约束", "business_condition": "业务条件",
        "permission": "权限", "state_transition": "状态迁移", "error_behavior": "异常行为",
        "workflow": "业务流程", "other": "其他",
        "gap": "需求缺漏", "conflict": "需求冲突", "validation": "校验问题",
        "unsupported": "暂不支持", "unparsed": "未解析内容",
        "unsupported_expectation": "预期依据待核对", "duplicate": "重复内容",
        "coverage": "覆盖问题",
    },
    "status": {
        "supported": "有证据支持", "ambiguous": "存在歧义", "conflicting": "存在冲突",
        "needs_review": "待复核", "ready": "可进入生成", "needs_context": "待补充上下文",
        "needs_clarification": "待澄清", "not_applicable": "不适用",
    },
    "applicability": {
        "applicable": "适用", "not_applicable": "不适用", "undetermined": "待确定",
    },
    "validation_status": {
        "pending": "待校验", "validated": "自动校验通过", "invalid": "校验未通过",
        "needs_review": "待复核",
    },
    "severity": {"info": "提示", "warning": "警告", "error": "错误"},
}


def _appendix_value(item: dict[str, Any], field: str, chinese: bool) -> Any:
    value = item.get(field)
    if chinese:
        if isinstance(value, str):
            value = _CHINESE_ENUMS.get(field, {}).get(value, value)
        elif isinstance(value, list):
            value = "\n".join(str(part) for part in value)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return _excel_value(value)


def _semantic_notice(item: dict[str, Any]) -> bool:
    """历史逐步骤词面提醒不是已证实的语义错误；仅在展示层区分。"""
    return (
        item.get("kind") == "unsupported_expectation"
        and item.get("description") == "预期为解释性断言，需人工核对语义"
    )


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
        payload = payload or {}
        issues = payload.get("issues", [])
        concrete = [item for item in issues if not _semantic_notice(item)]
        by_object: dict[str, list[dict[str, Any]]] = {}
        for item in concrete:
            for object_id in item.get("object_ids", []):
                by_object.setdefault(object_id, []).append(item)
        for case in cases:
            steps = case["steps"]
            remarks = case.get("remarks") or ""
            # 不把所有 needs_review 统一翻译成“发现风险”。共享需求问题在追溯表显示；
            # 主表备注仅列此用例自身的具体问题，避免通用声明淹没可执行内容。
            for description in dict.fromkeys(
                item["description"] for item in by_object.get(case.get("case_id", ""), [])
            ):
                remarks += f"\n[用例问题] {description}"
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
        for title, key, fields in (
            ("测试点", "points", ["point_id", "objective", "technique", "rule_ids", "status",
                                  "applicability"]),
            ("规则", "rules", ["rule_id", "statement", "evidence_ids", "status", "kind"]),
            (
                "覆盖矩阵",
                "coverage",
                ["case_id", "step_no", "rule_id", "point_id", "validation_status"],
            ),
            ("问题清单", "issues", ["issue_id", "kind", "description", "object_ids",
                                   "severity", "display_category"]),
            ("未解析范围", "issues", ["issue_id", "description", "object_ids"]),
        ):
            appendix = book.create_sheet(title)
            chinese = True
            appendix.append([
                "问题类型" if f == "kind" and title == "问题清单"
                else _CHINESE_HEADERS.get(f, f)
                for f in fields
            ])
            for item in payload.get(key, []):
                if title == "未解析范围" and item.get("kind") != "unparsed":
                    continue
                if title == "问题清单":
                    item = {
                        **item,
                        "display_category": (
                            "通用语义复核提醒" if _semantic_notice(item) else "具体问题或需求待明确"
                        ),
                    }
                appendix.append(
                    [
                        _appendix_value(item, f, chinese)
                        for f in fields
                    ]
                )
                for cell in appendix[appendix.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
            appendix.freeze_panes = "A2"
            appendix.auto_filter.ref = appendix.dimensions
            for cell in appendix[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="305496")
            for index, field in enumerate(fields, 1):
                appendix.column_dimensions[get_column_letter(index)].width = (
                    75 if field in {"description", "objective", "statement"} else 28
                )

        trace = book.create_sheet("用例追溯")
        trace.append(["用例ID", "用例名称", "技术状态", "测试点ID", "规则ID", "证据ID",
                      "关联需求问题（不含通用语义提醒）"])
        related_count = 0
        for case in cases:
            rule_ids = sorted({r for step in case["steps"] for r in step.get("rule_ids", [])})
            evidence_ids = sorted(
                {e for step in case["steps"] for e in step.get("evidence_ids", [])}
            )
            source_ids = {*case.get("point_ids", []), *rule_ids, *evidence_ids}
            related = list(dict.fromkeys(
                item["description"] for object_id in sorted(source_ids)
                for item in by_object.get(object_id, [])
            ))
            related_count += bool(related)
            trace.append([
                _excel_value(value) for value in (
                    case.get("case_id"), case.get("title"), case.get("status"),
                    "\n".join(case.get("point_ids", [])), "\n".join(rule_ids),
                    "\n".join(evidence_ids), "\n".join(related),
                )
            ])
        summary = book.create_sheet("质量说明")
        for row in [
            ("项目", "说明 / 数量"),
            ("用例数量", len(cases)),
            ("问题记录总数", len(issues)),
            ("通用语义复核提醒（按步骤累计）", len(issues) - len(concrete)),
            ("其余问题记录（含需求缺漏、来源局限、拒绝用例和覆盖问题）", len(concrete)),
            ("用例自身关联具体问题的数量", sum(
                bool(by_object.get(case.get("case_id", ""))) for case in cases
            )),
            ("关联需求或来源问题的用例数量", related_count),
            ("如何使用主表", "备注仅列用例自身的具体问题；共享需求/来源问题见用例追溯。"),
            ("提醒的含义", "预期非逐字原文不等于错误；通用语义复核提醒不能计作已证实缺陷。"),
            ("状态含义", "技术状态与问题记录完整保留。空备注不代表人工批准或已证明语义正确。"),
            ("问题查看", "问题清单保留全部记录，可按性质和类型筛选；问题数不等于缺陷或用例数。"),
        ]:
            summary.append([_excel_value(value) for value in row])
        for appendix in (trace, summary):
            appendix.freeze_panes = "A2"
            appendix.auto_filter.ref = appendix.dimensions
            for cell in appendix[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="305496")
            for row in appendix:
                for cell in row:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
            for index in range(1, appendix.max_column + 1):
                appendix.column_dimensions[get_column_letter(index)].width = 48 if index > 1 else 30
        book.save(destination)
