"""quality 兼容入口；旧路径与旧公共名称继续可用。"""

import re
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from tc_rag.application.services.chunking import (
    cross_page_table_continuations,
    normalized_quote,
    verify_evidence,
)
from tc_rag.application.services.design import (
    boundary_values,
    case_signature,
    is_ui_display_point,
    is_ui_display_rule,
    qualitative_boundary_values,
    resolved_image_asset_ids,
    unsupported_numbers,
)
from tc_rag.application.services.quality._common import Check, SnapshotMap
from tc_rag.application.services.quality.report import inspect_quality
from tc_rag.domain import models as m
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.pipeline import Extraction
from tc_rag.domain.rag import ContextPayload
from tc_rag.domain.review import PointTree

# 包含历史导入符号，避免纯结构拆分缩小旧模块的可导入名称集合。
__all__ = [
    "Any",
    "Callable",
    "Check",
    "ContextPayload",
    "Counter",
    "Extraction",
    "Mapping",
    "PointTree",
    "SnapshotMap",
    "ValidationError",
    "boundary_values",
    "case_signature",
    "cross_page_table_continuations",
    "fingerprint",
    "inspect_quality",
    "is_ui_display_point",
    "is_ui_display_rule",
    "m",
    "normalized_quote",
    "qualitative_boundary_values",
    "re",
    "resolved_image_asset_ids",
    "unsupported_numbers",
    "verify_evidence",
]
