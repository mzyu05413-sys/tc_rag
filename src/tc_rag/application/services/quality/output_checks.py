"""导出文件、清单及交付产物检查。"""

from typing import Any

from tc_rag.application.services.quality._common import Check, SnapshotMap
from tc_rag.application.services.quality._common import case_point as _case_point
from tc_rag.application.services.quality._common import contains_api_key as _contains_api_key
from tc_rag.application.services.quality._common import fail as _fail
from tc_rag.application.services.quality._common import not_applicable as _na
from tc_rag.application.services.quality._common import passed as _pass
from tc_rag.application.services.quality._common import safe as _safe
from tc_rag.application.services.quality._common import warn as _warn


def _output_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    cases = snapshots.get("cases")
    manifest = snapshots.get("manifest")
    invocations = snapshots.get("invocations")
    failure = snapshots.get("failure")
    run_status = str(run.get("status", ""))

    def self_consistency() -> Check:
        if cases is None:
            return _na("Q-OU-01", "output", "缺少 cases 快照")
        quality = cases.get("quality", {})
        points = [
            _case_point(item) for item in cases.get("points", [])
        ]
        ready = sum(
            point.status == "ready" and point.applicability == "applicable"
            for point in points
        )
        covered = len(
            {item.get("point_id") for item in cases.get("coverage", [])}
        )
        expected = {
            "points": ready,
            "cases": len(cases.get("cases", [])),
            "covered_points": covered,
        }
        actual = {key: quality.get(key) for key in expected}
        if actual != expected:
            return _fail(
                "Q-OU-01",
                "output",
                "cases.quality 计数与快照事实不一致",
                counts={"expected": sum(expected.values()), "actual": sum(
                    value for value in actual.values() if isinstance(value, int)
                )},
                detail={"expected": expected, "actual": actual},
            )
        return _pass(
            "Q-OU-01",
            "output",
            "cases.quality 计数与快照事实一致",
            counts=expected,
        )

    def run_identity() -> Check:
        if cases is None:
            return _na("Q-OU-02", "output", "缺少 cases 快照")
        if str(cases.get("run_id", "")) != str(run.get("run_id", "")):
            return _fail(
                "Q-OU-02",
                "output",
                "cases 快照的 run_id 与运行不一致",
                object_ids=[str(cases.get("run_id", "")), str(run.get("run_id", ""))],
            )
        output = snapshots.get("cases_output") or {}
        if not output.get("directory"):
            return _warn("Q-OU-02", "output", "运行未记录输出目录")
        return _pass(
            "Q-OU-02",
            "output",
            "运行与输出目录已记录",
            object_ids=[str(output.get("directory", ""))],
        )

    def usage_consistency() -> Check:
        if manifest is None or invocations is None:
            return _na("Q-OU-03", "output", "缺少 manifest/invocations 快照")
        items = invocations.get("items", [])
        expected: dict[str, int | None] = {}
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            values = [item.get("usage", {}).get(field) for item in items]
            expected[field] = (
                sum(value for value in values if value is not None)
                if values and all(value is not None for value in values)
                else None
            )
        actual = manifest.get("metrics", {}).get("usage", {})
        if actual != expected:
            return _warn(
                "Q-OU-03",
                "output",
                "manifest usage 聚合与调用记录不一致",
                detail={"manifest": actual, "invocations": expected},
            )
        return _pass("Q-OU-03", "output", "manifest usage 聚合与调用记录一致")

    def failure_check() -> Check:
        if failure is not None:
            if run_status in {"failed", "cancelled"}:
                return _warn(
                    "Q-OU-04",
                    "output",
                    f"运行状态为 {run_status}，失败阶段已记录",
                    object_ids=[str(failure.get("stage", ""))],
                    detail={"error_type": failure.get("error_type")},
                )
            return _fail(
                "Q-OU-04",
                "output",
                "运行存在失败阶段快照",
                object_ids=[str(failure.get("stage", ""))],
                detail={"error_type": failure.get("error_type")},
            )
        if run_status == "partial_failed":
            return _warn("Q-OU-04", "output", "运行以 partial_failed 结束")
        if run_status in {"failed", "cancelled"}:
            return _warn("Q-OU-04", "output", f"运行状态为 {run_status}")
        return _pass("Q-OU-04", "output", "运行没有失败阶段快照")

    def prompt_check() -> Check:
        if manifest is None:
            return _na("Q-RU-02", "manifest", "缺少 manifest 快照")
        prompts = manifest.get("prompts")
        if not isinstance(prompts, dict) or not prompts:
            return _warn("Q-RU-02", "manifest", "manifest 缺少提示词版本/指纹")
        bad = [
            key
            for key, value in prompts.items()
            if not isinstance(value, dict)
            or not value.get("version")
            or not value.get("fingerprint")
        ]
        if bad:
            return _fail(
                "Q-RU-02",
                "manifest",
                "提示词版本或指纹缺失",
                object_ids=bad,
            )
        return _pass(
            "Q-RU-02",
            "manifest",
            "提示词版本与指纹完整",
            counts={"prompts": len(prompts)},
        )

    def secret_check() -> Check:
        if _contains_api_key(run.get("config_snapshot", {})) or _contains_api_key(
            (manifest or {}).get("config", {})
        ):
            return _fail("Q-RU-03", "manifest", "运行快照包含未排除的 api_key 字段")
        return _pass("Q-RU-03", "manifest", "运行快照未包含 api_key 字段")

    def semantic_check() -> Check:
        if cases is None:
            return _na("Q-SEMANTIC-01", "cases", "缺少 cases 快照")
        value = cases.get("quality", {}).get("semantic_review")
        if value == "not_performed":
            return _pass(
                "Q-SEMANTIC-01",
                "cases",
                "语义审核未执行，自动检查不冒充业务正确性",
            )
        if value is None:
            return _warn(
                "Q-SEMANTIC-01",
                "cases",
                "旧快照未记录 semantic_review，按未审核处理",
            )
        return _fail(
            "Q-SEMANTIC-01",
            "cases",
            "semantic_review 不是 not_performed",
            detail={"value": str(value)},
        )

    return [
        _safe("Q-OU-01", "output", schema_errors, self_consistency),
        _safe("Q-OU-02", "output", schema_errors, run_identity),
        _safe("Q-OU-03", "output", schema_errors, usage_consistency),
        _safe("Q-OU-04", "output", schema_errors, failure_check),
        _safe("Q-RU-02", "manifest", schema_errors, prompt_check),
        _safe("Q-RU-03", "manifest", schema_errors, secret_check),
        _safe("Q-SEMANTIC-01", "cases", schema_errors, semantic_check),
    ]


# 内部跨模块别名；原定义保留，便于纯搬移 AST 核验。
output_checks = _output_checks
