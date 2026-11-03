"""模型调用记录、预算及执行状态检查。"""

from typing import Any

from tc_rag.application.services.quality._common import Check, SnapshotMap
from tc_rag.application.services.quality._common import not_applicable as _na
from tc_rag.application.services.quality._common import passed as _pass
from tc_rag.application.services.quality._common import safe as _safe
from tc_rag.application.services.quality._common import warn as _warn


def _invocation_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    invocations = snapshots.get("invocations")

    def failed_check() -> Check:
        if invocations is None:
            return _na("Q-IV-01", "invocations", "旧运行缺少调用记录")
        items = invocations.get("items", [])
        failed = [item for item in items if item.get("error") is not None]
        if failed:
            return _warn(
                "Q-IV-01",
                "invocations",
                "存在失败的模型调用，需结合阶段快照判断影响",
                counts={"calls": len(items), "failed": len(failed)},
                object_ids=[str(item.get("invocation_id", "")) for item in failed],
            )
        return _pass(
            "Q-IV-01",
            "invocations",
            "没有失败的模型调用",
            counts={"calls": len(items)},
        )

    def truncated_check() -> Check:
        if invocations is None:
            return _na("Q-IV-02", "invocations", "旧运行缺少调用记录")
        truncated = [
            item
            for item in invocations.get("items", [])
            if item.get("error") == "OutputTruncatedError"
        ]
        if truncated:
            return _warn(
                "Q-IV-02",
                "invocations",
                "存在模型输出截断调用",
                counts={"truncated": len(truncated)},
                object_ids=[str(item.get("invocation_id", "")) for item in truncated],
            )
        return _pass("Q-IV-02", "invocations", "没有输出截断调用")

    def repair_check() -> Check:
        if invocations is None:
            return _na("Q-IV-03", "invocations", "旧运行缺少调用记录")
        repaired = len(invocations.get("validation_errors", {}))
        if repaired:
            return _warn(
                "Q-IV-03",
                "invocations",
                "存在结构修复记录，需核对最终结构",
                counts={"repaired_calls": repaired},
            )
        return _pass("Q-IV-03", "invocations", "没有结构修复记录")

    def usage_check() -> Check:
        if invocations is None:
            return _na("Q-IV-04", "invocations", "旧运行缺少调用记录")
        unknown = 0
        for item in invocations.get("items", []):
            usage = item.get("usage", {})
            if any(
                usage.get(field) is None
                for field in ("input_tokens", "output_tokens", "total_tokens")
            ):
                unknown += 1
        if unknown:
            return _warn(
                "Q-IV-04",
                "invocations",
                "部分调用 usage 未知，不能当作零消耗",
                counts={"unknown_usage": unknown},
            )
        return _pass("Q-IV-04", "invocations", "调用 usage 完整")

    def budget_check() -> Check:
        if invocations is None:
            return _na("Q-IV-05", "invocations", "旧运行缺少调用记录")
        maximum = (
            (run.get("config_snapshot") or {})
            .get("runtime", {})
            .get("budget", {})
            .get("max_calls")
        )
        calls = len(invocations.get("items", []))
        if maximum is None:
            return _pass(
                "Q-IV-05",
                "invocations",
                "未配置调用预算上限",
                counts={"calls": calls},
            )
        if calls >= int(maximum):
            return _warn(
                "Q-IV-05",
                "invocations",
                "实际调用已达到配置预算上限",
                counts={"calls": calls, "max_calls": int(maximum)},
            )
        return _pass(
            "Q-IV-05",
            "invocations",
            "调用次数未达到配置预算上限",
            counts={"calls": calls, "max_calls": int(maximum)},
        )

    return [
        _safe("Q-IV-01", "invocations", schema_errors, failed_check),
        _safe("Q-IV-02", "invocations", schema_errors, truncated_check),
        _safe("Q-IV-03", "invocations", schema_errors, repair_check),
        _safe("Q-IV-04", "invocations", schema_errors, usage_check),
        _safe("Q-IV-05", "invocations", schema_errors, budget_check),
    ]


# 内部跨模块别名；原定义保留，便于纯搬移 AST 核验。
invocation_checks = _invocation_checks
