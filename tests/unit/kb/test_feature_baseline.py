import runpy
from pathlib import Path

import pytest

MODULE = runpy.run_path(str(Path("eval/kb/run_feature_baseline.py")))


@pytest.mark.parametrize("spans,expected", [
    ([(0, 3), (3, 8)], True),
    ([(0, 3), (4, 8)], False),
    ([(2, 8)], False),
    ([(0, 8)], True),
    ([], False),
])
def test_evidence_union_must_not_bridge_gaps(spans, expected):
    evidence = dict(block_id="body", start_char=0, end_char=8)
    chunks = [{"sources": [dict(block_id="body", start_char=a, end_char=b)]}
              for a, b in spans]
    assert MODULE["covers"](evidence, chunks) is expected


def test_other_source_cannot_replace_original_text():
    evidence = dict(block_id="body", start_char=0, end_char=8)
    chunks = [{"sources": [dict(block_id="vision", start_char=0, end_char=100)]}]
    assert MODULE["covers"](evidence, chunks) is False
