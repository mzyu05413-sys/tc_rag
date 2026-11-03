"""视觉关键事实的离线词面辅助评分；不宣称完成语义判断。"""

import re
import unicodedata
from typing import Any

from pydantic import Field, model_validator

from tc_rag.domain.models import DomainModel
from tc_rag.domain.vision import VisionAudit


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


class GoldImage(DomainModel):
    must_have: list[str] = Field(default_factory=list)
    must_not_assert: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_targets(self) -> "GoldImage":
        for items in (self.must_have, self.must_not_assert):
            normalized = [normalize(item) for item in items]
            if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
                raise ValueError("评分关键词不得为空或归一化后重复")
        return self


class VisionGold(DomainModel):
    images: dict[str, GoldImage]


def score_images(rows: list[dict[str, Any]], gold: VisionGold) -> dict[str, Any]:
    """只查事实、关系和表格；疑点及正文关联不混入确定断言。"""
    output = []
    seen = set()
    hits = targets = assertions = 0
    for row in rows:
        number = f"{row['number']:03d}"
        seen.add(number)
        annotation = gold.images.get(number)
        item: dict[str, Any] = {
            "image": number, "request_stats": row.get("request_stats"),
            "status": "unlabeled" if annotation is None else "unavailable",
            "recall": None, "unsupported_assertions": None,
        }
        if annotation is not None and row.get("structured") and row.get("result"):
            description = VisionAudit.model_validate(row["structured"]).description
            statements = "\n".join(
                [fact.text for fact in description.facts]
                + [relation.text for relation in description.relations]
            )
            table_text = "\n".join(
                (table.title or "") + "\n" + "\n".join(
                    [" ".join(table.headers), *[" ".join(cells) for cells in table.rows]]
                ) for table in description.tables
            )
            content = normalize(statements + "\n" + table_text)
            found = [text for text in annotation.must_have if normalize(text) in content]
            missing = [text for text in annotation.must_have if text not in found]
            # 禁止断言词只查事实与关系，不能把“不确定项提到了该词”算成编造。
            prohibited = [
                text for text in annotation.must_not_assert
                if normalize(text) in normalize(statements)
            ]
            count = len(annotation.must_have)
            item.update(
                status="scored", matched=found, missing=missing,
                recall=len(found) / count if count else None,
                unsupported_matches=prohibited, unsupported_assertions=len(prohibited),
                targets=count,
            )
            hits += len(found)
            targets += count
            assertions += len(prohibited)
        output.append(item)
    return {
        "method": "NFKC_whitespace_removed_substring",
        "warning": "子串匹配只是辅助指标，不能替代人工审核；否定、同义词及语境可能造成误判。",
        "recall": hits / targets if targets else None,
        "scored_targets": targets, "matched_targets": hits,
        "unsupported_assertions": assertions,
        "unavailable_images": [item["image"] for item in output if item["status"] == "unavailable"],
        "unlabeled_images": [item["image"] for item in output if item["status"] == "unlabeled"],
        "gold_images_not_in_results": sorted(set(gold.images) - seen),
        "images": output,
    }
