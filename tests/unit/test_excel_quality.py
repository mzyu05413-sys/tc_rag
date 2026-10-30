"""保留首张十一列模板，同时确保附表来源文本不会变成公式。"""

import json
from copy import deepcopy

from openpyxl import load_workbook

from tc_rag.infrastructure.exporters.reports import ReportExporter


def test_quality_sheets_and_formula_text(tmp_path):
    output = tmp_path / "cases.xlsx"
    ReportExporter().export(
        {
            "cases": [],
            "rules": [{"rule_id": "r", "statement": "=1+1"}],
            "issues": [
                {"issue_id": "i", "kind": "unparsed", "description": "@source", "object_ids": []}
            ],
        },
        output,
    )
    book = load_workbook(output)
    assert book.sheetnames == [
        "模版", "测试点", "规则", "覆盖矩阵", "问题清单", "未解析范围",
        "用例追溯", "质量说明",
    ]
    assert book["规则"]["B2"].value == "=1+1"
    assert book["规则"]["B2"].data_type == "s"
    assert book["未解析范围"].max_row == 2


def test_custom_case_column_order_and_header(tmp_path):
    output = tmp_path / "custom.xlsx"
    ReportExporter([("priority", "级别"), ("title", "标题")]).export(
        {"cases": [{"title": "+source", "priority": "P1", "steps": []}]}, output
    )
    sheet = load_workbook(output).active
    assert list(sheet.values) == [("级别", "标题"), ("P1", "+source")]
    assert sheet["B2"].data_type == "s"


def test_rule_and_point_sheets_use_chinese_without_changing_ids_or_facts(tmp_path):
    payload = {
        "points": [{"point_id": "p_1", "objective": "验证边界", "technique": "boundary",
                    "rule_ids": ["r_1", "r_2"], "status": "ready",
                    "applicability": "applicable"}],
        "rules": [{"rule_id": "r_1", "statement": "规则原文", "evidence_ids": ["e_1", "e_2"],
                   "status": "needs_review", "kind": "field_constraint"}],
    }
    before = deepcopy(payload)
    output = tmp_path / "chinese.xlsx"
    ReportExporter().export(payload, output)
    book = load_workbook(output)
    try:
        assert list(book["测试点"].values) == [
            ("测试点ID", "测试目标", "测试方法", "关联规则ID", "技术状态", "适用性"),
            ("p_1", "验证边界", "边界值", "r_1\nr_2", "可进入生成", "适用"),
        ]
        assert list(book["规则"].values) == [
            ("规则ID", "规则说明", "引用证据ID", "技术状态", "规则类型"),
            ("r_1", "规则原文", "e_1\ne_2", "待复核", "字段约束"),
        ]
    finally:
        book.close()
    assert payload == before


def test_coverage_issue_and_unparsed_sheets_are_chinese_for_each_export(tmp_path):
    payload = {
        "coverage": [{"case_id": "c1", "step_no": 1, "rule_id": "r1", "point_id": "p1",
                      "validation_status": "needs_review"}],
        "issues": [{"issue_id": "i1", "kind": "unparsed", "description": "图片未解析",
                    "object_ids": ["b1", "b2"], "severity": "warning"}],
    }
    before = deepcopy(payload)
    # 两次独立实例模拟后续生成和重新导出，防止只改某一份现有文件。
    for name in ("run.xlsx", "reexport.xlsx"):
        target = tmp_path / name
        ReportExporter().export(payload, target)
        book = load_workbook(target)
        try:
            assert list(book["覆盖矩阵"].values) == [
                ("用例ID", "步骤序号", "规则ID", "测试点ID", "校验状态"),
                ("c1", 1, "r1", "p1", "待复核"),
            ]
            assert list(book["问题清单"].values) == [
                ("问题ID", "问题类型", "问题说明", "关联对象ID", "严重程度", "问题性质"),
                ("i1", "未解析内容", "图片未解析", "b1\nb2", "警告", "具体问题或需求待明确"),
            ]
            assert list(book["未解析范围"].values) == [
                ("问题ID", "问题说明", "关联对象ID"),
                ("i1", "图片未解析", "b1\nb2"),
            ]
        finally:
            book.close()
    assert payload == before


def test_remarks_separate_case_risks_from_generic_review_and_source_gaps(tmp_path):
    cases = [
        {"case_id": name, "title": name, "status": "needs_review",
         "point_ids": ["p"], "remarks": "原始备注" if name == "c2" else None,
         "steps": [{"action": "操作", "expected": "预期", "rule_ids": ["r"],
                    "evidence_ids": ["e"]}]}
        for name in ("c1", "c2")
    ]
    payload = {
        "cases": cases,
        "issues": [
            {"issue_id": "i1", "kind": "unsupported_expectation", "severity": "warning",
             "description": "预期为解释性断言，需人工核对语义", "object_ids": ["c1", "c2"]},
            {"issue_id": "i2", "kind": "unsupported_expectation", "severity": "warning",
             "description": "预期数值或证据范围缺乏直接依据", "object_ids": ["c2"]},
            {"issue_id": "i3", "kind": "gap", "severity": "warning",
             "description": "来源需求缺少精度", "object_ids": ["r"]},
        ],
    }
    before = deepcopy(payload)
    output = tmp_path / "remarks.xlsx"
    ReportExporter().export(payload, output)
    book = load_workbook(output)
    try:
        assert book["模版"]["H2"].value is None
        assert book["模版"]["H3"].value == (
            "原始备注\n[用例问题] 预期数值或证据范围缺乏直接依据"
        )
        assert book["用例追溯"]["C2"].value == "needs_review"
        assert book["用例追溯"]["G2"].value == "来源需求缺少精度"
        assert book["问题清单"].max_row == 4
        assert book["问题清单"]["F2"].value == "通用语义复核提醒"
        summary = dict(list(book["质量说明"].values)[1:])
        assert summary["问题记录总数"] == 3
        assert summary["通用语义复核提醒（按步骤累计）"] == 1
        assert summary["用例自身关联具体问题的数量"] == 1
        assert summary["关联需求或来源问题的用例数量"] == 2
    finally:
        book.close()
    assert payload == before


def test_illegal_controls_are_visible_in_excel_without_mutating_facts(tmp_path):
    # 覆盖 openpyxl 拒绝的全部 C0 控制字符，而不是只特判这次文档的 U+0001。
    codes = [*range(9), 11, 12, *range(14, 32)]
    original = "=中文" + "".join(chr(code) for code in codes) + "\t换行\n次行\r末行\\u0001"
    expected = "=中文" + "".join(f"\\u{code:04x}" for code in codes) + "\t换行\n次行\r末行\\u0001"
    payload = {
        "cases": [
            {
                "title": original,
                "external_id": "c",
                "status": "validated",
                "preconditions": [original],
                "remarks": original,
                "steps": [{"action": original, "expected": original}],
            }
        ],
        "points": [{"point_id": "p", "objective": original}],
        "rules": [{"rule_id": "r", "statement": original}],
        "coverage": [{"case_id": "c", "step_no": 1, "rule_id": "r", "point_id": "p"}],
        "issues": [
            {"issue_id": "i", "kind": "unparsed", "description": original, "object_ids": [original]}
        ],
    }
    before = deepcopy(payload)
    output = tmp_path / "controls.xlsx"
    exporter = ReportExporter()
    exporter.export(payload, output)
    book = load_workbook(output)
    try:
        sheet = book["模版"]
        assert sheet.max_column == 11 and sheet.max_row == 2
        assert sheet["A2"].value == expected and sheet["A2"].data_type == "s"
        assert sheet["C2"].value == expected and sheet["H2"].value == expected
        assert sheet["E2"].value == "[1] " + expected
        assert sheet["F2"].value == "[1] " + expected
        assert book["测试点"]["B2"].value == expected
        assert book["规则"]["B2"].value == expected
        assert book["问题清单"]["C2"].value == expected
        assert book["未解析范围"]["B2"].value == expected
        assert book["覆盖矩阵"]["B2"].value == 1
        assert book["问题清单"]["D2"].value == expected
        assert not any(cell.data_type == "f" for page in book for row in page for cell in row)
    finally:
        book.close()
    assert payload == before
    exporter.export(payload, tmp_path / "facts.json")
    assert json.loads((tmp_path / "facts.json").read_text(encoding="utf-8")) == before


def test_excel_controls_in_custom_headers_and_literal_escape(tmp_path):
    output = tmp_path / "custom-controls.xlsx"
    ReportExporter([("title", "标题\x01"), ("priority", "等级")]).export(
        {"cases": [{"title": r"字面量\u0001", "priority": None, "steps": []}]}, output
    )
    book = load_workbook(output)
    try:
        assert list(book["模版"].values) == [(r"标题\u0001", "等级"), (r"字面量\u0001", None)]
    finally:
        book.close()
