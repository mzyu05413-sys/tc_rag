"""编排层纯函数辅助：入库保存、文件名、运行状态与用例模块路径推导。"""

import re
from pathlib import Path
from typing import Any

from tc_rag.application.ports.contracts import Repository
from tc_rag.application.services.effective_text import effective_slice
from tc_rag.domain import models as m
from tc_rag.domain.pipeline import Extraction

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def save_objects(repository: Repository, collection: str, values: list[Any], key: str) -> None:
    repository.save_many((collection, str(getattr(value, key)), value) for value in values)


def safe_document_stem(name: str) -> str:
    """用需求文件名做导出的可读后缀，去掉文件系统非法字符并限制长度。"""
    stem = Path(name).stem.strip()
    stem = _INVALID_FILENAME.sub("_", stem).strip(" .")
    return stem[:60] or "requirement"


def final_status(issues: list[m.Issue]) -> str:
    """只有 warning/error 级问题影响运行状态；info 提示不再拖成失败。"""
    return (
        "partial_failed"
        if any(
            item.status == "open" and item.severity in {"warning", "error"}
            for item in issues
        )
        else "succeeded"
    )


def effective_chunk(chunk: m.Chunk, block_map: dict[str, m.DocumentBlock]) -> m.Chunk:
    """返回去掉作废文字后的分块正文；整段作废时回退原文，避免空正文。"""
    if not any(block_map[s.block_id].deprecated_ranges for s in chunk.sources):
        return chunk
    text = "".join(
        effective_slice(
            block_map[s.block_id].content,
            s.start_char,
            s.end_char,
            block_map[s.block_id].deprecated_ranges,
        )
        for s in chunk.sources
    )
    if not text:
        return chunk
    return chunk.model_copy(update={"text": text})


def assign_module_paths(
    cases: list[m.TestCase],
    points: list[m.TestPoint],
    extracted: Extraction,
    *,
    block_map: dict[str, m.DocumentBlock],
    document_stem: str,
) -> list[m.TestCase]:
    """按规则证据来源的标题路径推导用例模块路径；无路径时退回功能名。"""
    rule_map = {rule.rule_id: rule for rule in extracted.rules}
    evidence_map = {item.evidence_id: item for item in extracted.evidence}
    feature_names = {feature.feature_id: feature.name for feature in extracted.features}

    def module_path(point: m.TestPoint) -> str | None:
        paths = []
        for rule_id in point.rule_ids:
            rule = rule_map.get(rule_id)
            if rule is None:
                continue
            for evidence_id in rule.evidence_ids:
                item = evidence_map.get(evidence_id)
                block = block_map.get(item.block_id) if item else None
                if block is not None and block.heading_path:
                    paths.append(
                        tuple(part.strip() for part in block.heading_path if part.strip())
                    )
        if not paths:
            rule = rule_map.get(point.rule_ids[0]) if point.rule_ids else None
            name = feature_names.get(rule.feature_id) if rule else None
            return f"/{name.replace('/', '_')}" if name else None
        common = list(paths[0])
        for path in paths[1:]:
            length = 0
            while length < min(len(common), len(path)) and common[length] == path[length]:
                length += 1
            common = common[:length]
        if common and common[0] == document_stem:
            common = common[1:]
        if not common:
            return None
        return "/" + "/".join(part.replace("/", "_") for part in common)

    for case in cases:
        if case.module_path:
            continue
        for point_id in case.point_ids:
            candidate_point = next((p for p in points if p.point_id == point_id), None)
            path = module_path(candidate_point) if candidate_point is not None else None
            if path:
                case.module_path = path
                break
    return cases
