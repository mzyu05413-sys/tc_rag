"""按组标注展开：同组多个 C### 必须一起算相关，未知组号不能静默忽略。"""

import pytest
from expand_group_labels import expand

GROUPS = [
    {"group_id": "G001", "corpus_ids": ["C001"]},
    {"group_id": "G002", "corpus_ids": ["C002", "C010", "C014"]},
]


def _entry(**overrides):
    base = {"record_id": "Q001", "relevant_groups": None}
    base.update(overrides)
    return base


def test_group_expands_to_all_member_corpus_ids():
    expanded, stats = expand(GROUPS, [_entry(relevant_groups=["G002"])])
    assert expanded[0]["relevant_corpus_ids"] == ["C002", "C010", "C014"]
    assert stats == {"labelled": 1, "empty": 0, "unlabelled": 0}


def test_multiple_groups_are_merged_without_duplicates():
    expanded, _ = expand(
        GROUPS, [_entry(relevant_groups=["G001", "G002", "G001"])]
    )
    assert expanded[0]["relevant_corpus_ids"] == ["C001", "C002", "C010", "C014"]


def test_null_stays_unlabelled_and_empty_means_no_relevant():
    expanded, stats = expand(
        GROUPS,
        [_entry(record_id="Q001"), _entry(record_id="Q002", relevant_groups=[])],
    )
    assert expanded[0]["relevant_corpus_ids"] is None
    assert expanded[1]["relevant_corpus_ids"] == []
    assert stats == {"labelled": 0, "empty": 1, "unlabelled": 1}


def test_unknown_group_is_rejected():
    with pytest.raises(ValueError, match="不存在的组"):
        expand(GROUPS, [_entry(relevant_groups=["G999"])])
