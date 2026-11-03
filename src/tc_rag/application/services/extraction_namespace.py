"""按对象类型映射提炼局部 ID；不访问文件或数据库。"""

from tc_rag.application.ports.errors import WorkflowError
from tc_rag.domain.pipeline import Extraction


def namespace_extraction(candidate: Extraction, prefix: str) -> Extraction:
    """把模型局部 ID 映射到当前运行；不同类型允许复用同一个本地 ID。"""
    data = candidate.model_dump(mode="json")
    collections = (
        ("evidence", "evidence_id"),
        ("features", "feature_id"),
        ("rules", "rule_id"),
        ("entities", "entity_id"),
        ("issues", "issue_id"),
    )
    mappings: dict[str, dict[str, str]] = {}
    for collection, key in collections:
        mapping: dict[str, str] = {}
        for obj in data[collection]:
            old = obj[key]
            if old in mapping:
                raise WorkflowError(f"模型在当前批次返回了重复的 {key}")
            mapping[old] = f"{prefix}_{collection}_{old}"
        mappings[collection] = mapping
    # object_ids 可能引用任意类型；只有归属唯一时才改写，避免猜错对象。
    owners: dict[str, list[str]] = {}
    for mapping in mappings.values():
        for old, new in mapping.items():
            owners.setdefault(old, []).append(new)
    cross = {old: values[0] for old, values in owners.items() if len(values) == 1}
    field_collection = {
        "evidence_id": "evidence",
        "feature_id": "features",
        "rule_id": "rules",
        "entity_id": "entities",
        "issue_id": "issues",
    }
    for collection in data.values():
        if not isinstance(collection, list):
            continue
        for obj in collection:
            for field, value in list(obj.items()):
                if field == "evidence_ids":
                    mapping = mappings["evidence"]
                    obj[field] = [mapping.get(v, v) for v in value]
                elif field in field_collection:
                    mapping = mappings[field_collection[field]]
                    obj[field] = mapping.get(value, value)
                elif field == "object_ids":
                    obj[field] = [cross.get(v, v) for v in value]
    return Extraction.model_validate(data)
