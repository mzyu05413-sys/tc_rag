"""分块来源区间、覆盖和预算检查。"""

from typing import Any

from tc_rag.application.services.chunking import cross_page_table_continuations
from tc_rag.application.services.quality._common import Check, SnapshotMap
from tc_rag.application.services.quality._common import blocks as _blocks
from tc_rag.application.services.quality._common import fail as _fail
from tc_rag.application.services.quality._common import not_applicable as _na
from tc_rag.application.services.quality._common import passed as _pass
from tc_rag.application.services.quality._common import safe as _safe
from tc_rag.application.services.quality._common import warn as _warn


def _chunk_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    chunk_payload = snapshots.get("chunk")

    def source_check() -> Check:
        if chunk_payload is None:
            return _na("Q-CH-01", "chunk", "缺少 chunk 快照")
        blocks = {block.block_id: block for block in _blocks(chunk_payload)}
        bad: list[str] = []
        for chunk in chunk_payload.get("chunks", []):
            if not chunk.get("sources"):
                bad.append(chunk.get("chunk_id", ""))
                continue
            for source in chunk["sources"]:
                block = blocks.get(source.get("block_id", ""))
                start, end = source.get("start_char"), source.get("end_char")
                if (
                    block is None
                    or block.kind == "image"
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or not 0 <= start < end <= len(block.content)
                ):
                    bad.append(chunk.get("chunk_id", ""))
                    break
        if bad:
            return _fail(
                "Q-CH-01",
                "chunk",
                "分块来源 Block 缺失、区间越界或指向图片",
                counts={"chunks": len(chunk_payload.get("chunks", [])), "invalid": len(set(bad))},
                object_ids=sorted(set(bad)),
            )
        return _pass(
            "Q-CH-01",
            "chunk",
            "分块来源区间与 Block 有效",
            counts={"chunks": len(chunk_payload.get("chunks", []))},
        )

    def text_check() -> Check:
        if chunk_payload is None:
            return _na("Q-CH-02", "chunk", "缺少 chunk 快照")
        blocks = {block.block_id: block for block in _blocks(chunk_payload)}
        mismatch: list[str] = []
        for chunk in chunk_payload.get("chunks", []):
            try:
                expected = "\n".join(
                    blocks[source["block_id"]].content[
                        source["start_char"] : source["end_char"]
                    ]
                    for source in chunk.get("sources", [])
                )
            except KeyError:
                mismatch.append(chunk.get("chunk_id", ""))
                continue
            if expected != chunk.get("text"):
                mismatch.append(chunk.get("chunk_id", ""))
        if mismatch:
            return _fail(
                "Q-CH-02",
                "chunk",
                "分块文本与来源切片拼接不一致",
                counts={"mismatch": len(mismatch)},
                object_ids=mismatch,
            )
        return _pass(
            "Q-CH-02",
            "chunk",
            "分块文本与来源切片逐字一致",
            counts={"chunks": len(chunk_payload.get("chunks", []))},
        )

    def coverage_check() -> Check:
        if chunk_payload is None:
            return _na("Q-CH-03", "chunk", "缺少 chunk 快照")
        blocks = _blocks(chunk_payload)
        eligible = {
            block.block_id
            for block in blocks
            if block.kind not in {"image", "other"} and block.content.strip()
        }
        covered = {
            source.get("block_id")
            for chunk in chunk_payload.get("chunks", [])
            for source in chunk.get("sources", [])
        }
        missing = sorted(eligible - covered)
        if missing:
            return _warn(
                "Q-CH-03",
                "chunk",
                "存在未进入任何分块的非空文字块",
                counts={"eligible": len(eligible), "missing": len(missing)},
                object_ids=missing,
            )
        return _pass(
            "Q-CH-03",
            "chunk",
            "所有非空文字块均有分块映射",
            counts={"eligible": len(eligible)},
        )

    def budget_check() -> Check:
        if chunk_payload is None:
            return _na("Q-CH-04", "chunk", "缺少 chunk 快照")
        target = int(
            (run.get("config_snapshot") or {})
            .get("chunking", {})
            .get("target_tokens", 900)
        )
        chunks = chunk_payload.get("chunks", [])
        over = [
            chunk.get("chunk_id", "")
            for chunk in chunks
            if int(chunk.get("token_estimate", 0)) > target
        ]
        blocks = _blocks(chunk_payload)
        continuation_missing: list[str] = []
        for item in cross_page_table_continuations(blocks):
            chunks_for_block = [
                chunk
                for chunk in chunks
                if any(
                    source.get("block_id") == item.continuation_id
                    for source in chunk.get("sources", [])
                )
            ]
            if not chunks_for_block:
                continue
            if all(
                not any(
                    source.get("block_id") == item.root_id
                    and source.get("start_char") == item.header_start
                    and source.get("end_char") == item.header_end
                    for source in chunk.get("sources", [])
                )
                for chunk in chunks_for_block
            ):
                continuation_missing.append(item.continuation_id)
        counts = {
            "over_target": len(over),
            "table_header_missing": len(continuation_missing),
        }
        if continuation_missing:
            return _warn(
                "Q-CH-04",
                "chunk",
                "跨页续表片段可能缺少表头",
                counts=counts,
                object_ids=sorted(set(over + continuation_missing)),
            )
        if over:
            return _warn(
                "Q-CH-04",
                "chunk",
                "存在超出目标预算的分块，未截断原文",
                counts=counts,
                object_ids=over,
            )
        return _pass("Q-CH-04", "chunk", "分块预算与跨页续表检查通过", counts=counts)

    return [
        _safe("Q-CH-01", "chunk", schema_errors, source_check),
        _safe("Q-CH-02", "chunk", schema_errors, text_check),
        _safe("Q-CH-03", "chunk", schema_errors, coverage_check),
        _safe("Q-CH-04", "chunk", schema_errors, budget_check),
    ]


# 内部跨模块别名；原定义保留，便于纯搬移 AST 核验。
chunk_checks = _chunk_checks
