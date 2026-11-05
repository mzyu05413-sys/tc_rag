"""同语料预算对照的边界、确定性和不自动挑选策略。"""

import pytest

from tc_rag.application.services.chunk_experiments import budget_experiment, budget_pairs
from tc_rag.application.services.chunk_gold import candidate_review
from tc_rag.application.services.chunking import character_estimate
from tc_rag.domain.models import DocumentBlock


@pytest.mark.parametrize("targets,overlaps,minimum", [
    ([], [0], 0), ([10], [], 0), ([0], [0], 0), ([10], [-1], 0),
    ([10], [10], 0), ([10], [0], 10), ([10], [0], -1),
    ([1, True], [0], 0), ([10.0], [0], 0), (list(range(100, 133)), [0], 0),
])
def test_bad_grid_is_rejected_before_running_any_variant(targets, overlaps, minimum):
    with pytest.raises(ValueError):
        budget_pairs(targets, overlaps, minimum)


def test_grid_is_complete_deduplicated_and_deterministic():
    pairs = budget_pairs([60, 40, 60], [10, 0, 0], 0)
    assert pairs == [(40, 0), (40, 10), (60, 0), (60, 10)]
    blocks = [DocumentBlock(
        block_id=f"b{i}", document_id="d", order=i, kind="paragraph", locator={},
        content="条件满足时保存。尾句。" * 3, heading_path=["模块"],
    ) for i in range(4)]
    gold = candidate_review("run", blocks)
    options = {
        "strategy": "structural_v2", "attach_headings": False,
        "min_tokens": 0, "split_long_paragraphs": True,
    }
    result = budget_experiment("run", blocks, character_estimate, pairs, options, gold)
    assert result["model_calls"] == 0 and result["selected_variant"] is None
    assert len(result["variants"]) == 4
    for variant in result["variants"]:
        assert variant["coverage"]["passed"] and variant["stable_chunk_ids"]
        assert variant["gold"]["boundary_completeness"] is None
        assert variant["local_chunking_ms"] >= 0
    with pytest.raises(ValueError, match="完整网格"):
        budget_experiment("run", blocks, character_estimate, pairs[1:], options, gold)
