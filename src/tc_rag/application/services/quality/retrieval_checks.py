"""RAG 上下文范围、检索与重排记录检查。"""

from collections import Counter
from typing import Any

from tc_rag.application.services.quality._common import Check, SnapshotMap
from tc_rag.application.services.quality._common import evidence_by_id as _evidence_by_id
from tc_rag.application.services.quality._common import fail as _fail
from tc_rag.application.services.quality._common import not_applicable as _na
from tc_rag.application.services.quality._common import passed as _pass
from tc_rag.application.services.quality._common import safe as _safe
from tc_rag.application.services.quality._common import source_evidence_ids as _source_evidence_ids
from tc_rag.application.services.quality._common import warn as _warn


def _retrieval_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    retrieval = snapshots.get("retrieval")
    index = snapshots.get("index")
    run_status = str(run.get("status", ""))

    def required_check() -> Check:
        if retrieval is None or index is None:
            if run_status in {"succeeded", "partial_failed"}:
                return _fail(
                    "Q-RT-01",
                    "retrieval",
                    "运行声称完成但缺少 index/retrieval 快照",
                    counts={
                        "index": int(index is not None),
                        "retrieval": int(retrieval is not None),
                    },
                )
            return _na("Q-RT-01", "retrieval", "运行未完成，缺少 index/retrieval 快照")
        bundles = retrieval.get("bundles", [])
        invalid = [
            str(bundle.get("query_id", ""))
            for bundle in bundles
            if not all(
                bundle.get(key)
                for key in ("query_id", "scope", "space_id", "index_version")
            )
        ]
        version = index.get("index_version")
        if not version or invalid:
            return _fail(
                "Q-RT-01",
                "retrieval",
                "index 或检索记录缺少范围/空间/版本",
                counts={"bundles": len(bundles), "invalid": len(invalid)},
                object_ids=invalid,
            )
        return _pass(
            "Q-RT-01",
            "retrieval",
            "索引与检索记录带完整范围/空间/版本",
            counts={"bundles": len(bundles)},
        )

    def ids_check() -> Check:
        if retrieval is None:
            return _na("Q-RT-02", "retrieval", "缺少 retrieval 快照")
        known = set(_evidence_by_id(snapshots.get("extract")))
        known |= _source_evidence_ids(snapshots.get("chunk"))
        if not known:
            return _na("Q-RT-02", "retrieval", "缺少证据/分块快照，无法核对检索 ID")
        invalid: list[str] = []
        rank_bad: list[str] = []
        for bundle in retrieval.get("bundles", []):
            direct = set(bundle.get("direct_ids", []))
            if not direct <= known:
                invalid.extend(sorted(direct - known))
            supplemental = {
                item.get("evidence_id")
                for item in bundle.get("supplemental_evidence", [])
            }
            if not supplemental <= known:
                invalid.extend(sorted(str(item) for item in supplemental - known))
            for key in ("hits", "fusion"):
                for hit in bundle.get(key, []):
                    object_id = hit.get("object_id")
                    if object_id and object_id not in known:
                        invalid.append(str(object_id))
            invalid.extend(
                str(item)
                for item in bundle.get("keyword_ids", [])
                if item not in known
            )
            ranks = [hit.get("rank") for hit in bundle.get("hits", [])]
            if ranks != list(range(1, len(ranks) + 1)):
                rank_bad.append(str(bundle.get("query_id", "")))
        if invalid or rank_bad:
            return _fail(
                "Q-RT-02",
                "retrieval",
                "检索命中引用了范围外对象或排名不连续",
                counts={"invalid_ids": len(set(invalid)), "bad_ranks": len(rank_bad)},
                object_ids=sorted(set(invalid + rank_bad)),
            )
        return _pass(
            "Q-RT-02",
            "retrieval",
            "检索命中均属于当前证据范围且排名连续",
            counts={"bundles": len(retrieval.get("bundles", []))},
        )

    def adoption_check() -> Check:
        if retrieval is None:
            return _na("Q-RT-03", "retrieval", "缺少 retrieval 快照")
        invalid: list[str] = []
        for bundle in retrieval.get("bundles", []):
            supplemental = {
                item.get("evidence_id")
                for item in bundle.get("supplemental_evidence", [])
            }
            adopted = set(bundle.get("adopted_ids", []))
            omitted = set(bundle.get("omitted_ids", []))
            if not adopted <= supplemental or not omitted <= supplemental:
                invalid.append(str(bundle.get("query_id", "")))
            if adopted & omitted:
                invalid.append(str(bundle.get("query_id", "")))
        if invalid:
            return _fail(
                "Q-RT-03",
                "retrieval",
                "采用/省略证据不是补充材料的合法划分",
                counts={"invalid": len(set(invalid))},
                object_ids=sorted(set(invalid)),
            )
        return _pass("Q-RT-03", "retrieval", "采用/省略证据划分合法")

    def rerank_check() -> Check:
        if retrieval is None:
            return _na("Q-RT-04", "retrieval", "缺少 retrieval 快照")
        statuses = Counter(
            str(bundle.get("rerank", {}).get("status", ""))
            for bundle in retrieval.get("bundles", [])
        )
        invalid = [
            status
            for status in statuses
            if status not in {"disabled", "succeeded", "fallback"}
        ]
        if invalid:
            return _fail(
                "Q-RT-04",
                "retrieval",
                "rerank 状态不在允许枚举内",
                counts={status: statuses[status] for status in statuses},
            )
        if statuses.get("fallback"):
            return _warn(
                "Q-RT-04",
                "retrieval",
                "部分检索发生 rerank 降级，保留召回排名",
                counts={status: statuses[status] for status in statuses},
            )
        return _pass(
            "Q-RT-04",
            "retrieval",
            "rerank 状态合法",
            counts={status: statuses[status] for status in statuses},
        )

    def version_check() -> Check:
        if retrieval is None:
            return _na("Q-RT-05", "retrieval", "缺少 retrieval 快照")
        versions = {
            str(bundle.get("index_version", ""))
            for bundle in retrieval.get("bundles", [])
        }
        if len(versions) > 1:
            return _warn(
                "Q-RT-05",
                "retrieval",
                "同一运行内检索记录使用了不同索引版本",
                counts={"versions": len(versions)},
                object_ids=sorted(versions),
            )
        return _pass(
            "Q-RT-05",
            "retrieval",
            "检索记录索引版本一致",
            counts={"versions": len(versions)},
        )

    return [
        _safe("Q-RT-01", "retrieval", schema_errors, required_check),
        _safe("Q-RT-02", "retrieval", schema_errors, ids_check),
        _safe("Q-RT-03", "retrieval", schema_errors, adoption_check),
        _safe("Q-RT-04", "retrieval", schema_errors, rerank_check),
        _safe("Q-RT-05", "retrieval", schema_errors, version_check),
    ]


# 内部跨模块别名；原定义保留，便于纯搬移 AST 核验。
retrieval_checks = _retrieval_checks
