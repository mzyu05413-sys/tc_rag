"""按原阶段顺序汇总质量报告；自动检查不代替语义审核。"""

from collections import Counter
from typing import Any

from tc_rag.application.services.quality._common import _FAIL, _NA, _PASS, _WARN, Check, SnapshotMap
from tc_rag.application.services.quality._common import check as _check
from tc_rag.application.services.quality.case_checks import case_checks as _case_checks
from tc_rag.application.services.quality.chunk_checks import chunk_checks as _chunk_checks
from tc_rag.application.services.quality.extract_checks import extract_checks as _extract_checks
from tc_rag.application.services.quality.ingest_checks import input_checks as _input_checks
from tc_rag.application.services.quality.ingest_checks import parse_checks as _parse_checks
from tc_rag.application.services.quality.invocation_checks import (
    invocation_checks as _invocation_checks,
)
from tc_rag.application.services.quality.output_checks import output_checks as _output_checks
from tc_rag.application.services.quality.point_checks import point_checks as _point_checks
from tc_rag.application.services.quality.retrieval_checks import (
    retrieval_checks as _retrieval_checks,
)
from tc_rag.application.services.quality.snapshots import schema_errors as _schema_errors


def _overall(checks: list[Check]) -> dict[str, Any]:
    counts = Counter(check["status"] for check in checks)
    statuses = {status: counts.get(status, 0) for status in (_PASS, _FAIL, _WARN, _NA)}
    executed = statuses[_PASS] + statuses[_FAIL] + statuses[_WARN]
    if statuses[_FAIL]:
        status = _FAIL
    elif statuses[_WARN]:
        status = _WARN
    elif executed:
        status = _PASS
    else:
        status = _NA
    return {"status": status, "counts": statuses, "executed": executed}


def inspect_quality(run: dict[str, Any], snapshots: SnapshotMap) -> dict[str, Any]:
    """只读核对已完成运行的阶段快照；不调用模型，不修改数据库。"""
    schema_errors = _schema_errors(snapshots)
    checks: list[Check] = [
        _check(
            "Q-SC-01",
            "snapshot",
            "error" if schema_errors else "info",
            _FAIL if schema_errors else _PASS,
            "阶段快照可解析" if not schema_errors else "阶段快照结构无效",
            counts={"stages": len(snapshots), "invalid_stages": len(schema_errors)},
            object_ids=sorted(schema_errors),
            detail={"errors": schema_errors} if schema_errors else {},
        )
    ]
    checks.extend(_input_checks(run, snapshots, schema_errors))
    checks.extend(_parse_checks(run, snapshots, schema_errors))
    checks.extend(_chunk_checks(run, snapshots, schema_errors))
    checks.extend(_extract_checks(snapshots, schema_errors))
    checks.extend(_point_checks(run, snapshots, schema_errors))
    checks.extend(_case_checks(snapshots, schema_errors))
    checks.extend(_retrieval_checks(run, snapshots, schema_errors))
    checks.extend(_invocation_checks(run, snapshots, schema_errors))
    checks.extend(_output_checks(run, snapshots, schema_errors))

    chunk = snapshots.get("chunk") or {}
    document = chunk.get("document") or {}
    limits = [
        "只核对已保存快照，不重新解析、不调用模型",
        "自动检查通过不等于需求语义正确",
        "不覆盖客户端打开效果、表格列语义和用例可执行性",
        "semantic_review=not_performed；人工结论需另行记录",
    ]
    limits.extend(f"missing_stage:{stage}" for stage in _missing_stages(snapshots))
    return {
        "schema_version": "1.0",
        "run_id": str(run.get("run_id", "")),
        "run_status": str(run.get("status", "")),
        "document": {
            "name": document.get("name"),
            "content_hash": document.get("content_hash"),
        },
        "stages_present": sorted(snapshots),
        "overall": _overall(checks),
        "semantic_review": "not_performed",
        "checks": checks,
        "limits": limits,
    }


_EXPECTED_STAGES = (
    "input",
    "chunk",
    "extract",
    "index",
    "plan_points",
    "cases",
    "retrieval",
    "invocations",
    "manifest",
)


def _missing_stages(snapshots: SnapshotMap) -> list[str]:
    return [stage for stage in _EXPECTED_STAGES if stage not in snapshots]


# 内部跨模块别名；原定义保留，便于纯搬移 AST 核验。
overall = _overall
missing_stages = _missing_stages
