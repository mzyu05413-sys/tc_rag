"""从原文推导明确约束，保留既有正则和单位规则。"""

import re
import unicodedata
from decimal import Decimal
from typing import Any

from tc_rag.application.services.design._common import _SIZE_UNITS, _TIME_UNITS
from tc_rag.domain.models import StructuredConstraint

_NUMBER = r"(\d+(?:\.\d+)?)"


# 只从证据原文提取，且要求同一段文本里只有一个明确的数值约束。
_CONSTRAINT_PATTERNS = (
    (rf"(?:最多|不超过|至多|不大于|上限(?:为|是)?|最大(?:值|数)?(?:为|是)?)\s*{_NUMBER}", "<="),
    (rf"(?:至少|不少于|不低于|下限(?:为|是)?|最小(?:值|数)?(?:为|是)?)\s*{_NUMBER}", ">="),
    (rf"(?:大于|超过|高于)\s*{_NUMBER}", ">"),
    (rf"(?:小于|低于|少于)\s*{_NUMBER}", "<"),
    (rf"长度(?:限制|上限)?\s*(?:为|是)?\s*{_NUMBER}", "<="),
    (rf"{_NUMBER}\s*(?:个)?字符", "<="),
    (rf"{_NUMBER}\s*(?:位|个|条|次|天|元|张|页|份|人|项)", "<="),
    (rf"{_NUMBER}\s*(?:MB|GB|KB|M|G|K)", "<="),
    (rf"{_NUMBER}\s*(?:秒|分钟|小时)", "<="),
)


_UNIT_WORDS = (
    "字符",
    "位",
    "个",
    "条",
    "次",
    "天",
    "元",
    "张",
    "页",
    "份",
    "人",
    "项",
    "秒",
    "分钟",
    "小时",
)


def derive_constraint(quote: str) -> StructuredConstraint | None:
    """从原文引文提取单一数值约束；多个不同数值视为冲突，不猜测。"""
    text = unicodedata.normalize("NFKC", quote)
    found: set[tuple[str, str]] = set()
    for pattern, operator in _CONSTRAINT_PATTERNS:
        for match in re.finditer(pattern, text):
            found.add((operator, match.group(1)))
    if len(found) != 1:
        return None
    operator, value = next(iter(found))
    upper = text.upper()
    # 优先取紧邻数值的单位，避免“单次…5秒”里的“次”抢走“秒”。
    adjacent = re.search(
        rf"(?<!\d){re.escape(value)}\s*"
        r"(?P<unit>MB|GB|KB|M|G|K|字符|位|个|条|次|天|元|张|页|份|人|项|秒|分钟|小时)",
        text,
        re.IGNORECASE,
    )
    unit = adjacent.group("unit") if adjacent else None
    if unit is not None and unit.upper() in _SIZE_UNITS:
        unit = unit.upper()
    if unit is None:
        unit = next((candidate for candidate in _UNIT_WORDS if candidate in text), None)
    if unit is None:
        unit = next((candidate for candidate in ("MB", "GB", "KB") if candidate in upper), None)
    size_like = unit in _SIZE_UNITS or unit in _TIME_UNITS
    precision = None if size_like else (len(value.split(".", 1)[1]) if "." in value else 0)
    boundary: Any = Decimal(value) if "." in value else int(value)
    return StructuredConstraint(
        operator=operator, boundary=boundary, precision=precision, unit=unit
    )
