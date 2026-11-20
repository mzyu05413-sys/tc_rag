import runpy

import pytest


@pytest.mark.parametrize("token,expected", [
    ("，", False), ("。！？", False), (" ", False), ("", False),
    ("审批", True), ("CRM-07", True), ("1.5", True), ("+", True),
])
def test_punctuation_filter(token, expected):
    keep = runpy.run_path("eval/kb/token_filters.py")["keep_token"]
    assert keep(token) is expected
