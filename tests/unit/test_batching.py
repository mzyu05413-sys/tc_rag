"""功能分批（A2）：相邻同功能分组后组内切片，保持全局顺序。"""

import pytest

from tc_rag.application.services.design.batching import group_by_feature


def _entry(feature_id):
    return {"feature_id": feature_id}


def test_groups_adjacent_same_feature_then_slices_by_size():
    entries = [_entry("a"), _entry("a"), _entry("a"), _entry("b"), _entry("b")]
    assert group_by_feature(entries, lambda e: e["feature_id"], 2) == [
        [entries[0], entries[1]],
        [entries[2]],
        [entries[3], entries[4]],
    ]


def test_preserves_global_order_for_non_adjacent_same_feature():
    entries = [_entry("a"), _entry("b"), _entry("a")]
    # 不跨位置聚合，避免破坏按原序归并的约束。
    assert group_by_feature(entries, lambda e: e["feature_id"], 5) == [
        [entries[0]], [entries[1]], [entries[2]]
    ]


def test_empty_input_returns_empty():
    assert group_by_feature([], lambda e: e["feature_id"], 3) == []


def test_non_positive_size_rejected():
    with pytest.raises(ValueError):
        group_by_feature([_entry("a")], lambda e: e["feature_id"], 0)
