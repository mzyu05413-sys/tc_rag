import pytest

from tc_rag.domain.kb.binary_metrics import confusion, metrics


def test_standard_confusion_and_formulas():
    result = confusion(set(map(str, range(150))), set(map(str, range(10))),
                       ["0", "1", "2", "10", "11"])
    assert result == {"tp": 3, "fp": 2, "fn": 7, "tn": 138}
    assert metrics(result) == {"precision": .6, "recall": .3, "f1": .4, "accuracy": .94}


def test_unknown_zero_denominators():
    assert all(v is None for v in metrics(dict(tp=0, fp=0, fn=0, tn=0)).values())
    assert metrics(dict(tp=0, fp=0, fn=1, tn=9))["recall"] == 0


def test_invalid_ids_rejected():
    with pytest.raises(ValueError):
        confusion({"a"}, {"b"}, [])
    with pytest.raises(ValueError):
        confusion({"a"}, {"a"}, ["a", "a"])
