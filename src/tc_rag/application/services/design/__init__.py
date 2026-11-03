"""design 兼容入口；旧路径与旧公共名称继续可用。"""

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, Literal

from tc_rag.application.services.chunking import normalized_quote, verify_evidence
from tc_rag.application.services.design._common import issue
from tc_rag.application.services.design.case_validation import (
    case_signature,
    unsupported_numbers,
    validate_cases,
)
from tc_rag.application.services.design.constraints import derive_constraint
from tc_rag.application.services.design.dedupe import dedupe_issues, deduplicate_points
from tc_rag.application.services.design.extraction import (
    RuleStatus,
    check_extraction,
    error_rule_ids,
    reconcile,
)
from tc_rag.application.services.design.planning import (
    boundary_values,
    plan_points,
    qualitative_boundary_values,
)
from tc_rag.application.services.design.ui_display import (
    is_ui_display_point,
    is_ui_display_rule,
    merge_ui_display_points,
    plan_ui_display_summary,
)
from tc_rag.application.services.design.vision_policy import (
    apply_vision_policy,
    drop_resolved_image_issues,
    is_vision_only,
    resolved_image_asset_ids,
)
from tc_rag.domain.identity import fingerprint, new_id
from tc_rag.domain.models import (
    CoverageLink,
    DocumentBlock,
    EvidenceSpan,
    Feature,
    Issue,
    RequirementRule,
    StructuredConstraint,
    TestCase,
    TestPoint,
    TestStep,
)
from tc_rag.domain.pipeline import Extraction

# 包含历史导入符号，避免纯结构拆分缩小旧模块的可导入名称集合。
__all__ = [
    "Any",
    "CoverageLink",
    "Decimal",
    "DocumentBlock",
    "EvidenceSpan",
    "Extraction",
    "Feature",
    "InvalidOperation",
    "Issue",
    "Literal",
    "Mapping",
    "RequirementRule",
    "RuleStatus",
    "Sequence",
    "SequenceMatcher",
    "StructuredConstraint",
    "TestCase",
    "TestPoint",
    "TestStep",
    "apply_vision_policy",
    "boundary_values",
    "case_signature",
    "check_extraction",
    "dedupe_issues",
    "deduplicate_points",
    "derive_constraint",
    "drop_resolved_image_issues",
    "error_rule_ids",
    "fingerprint",
    "is_ui_display_point",
    "is_ui_display_rule",
    "is_vision_only",
    "issue",
    "json",
    "merge_ui_display_points",
    "new_id",
    "normalized_quote",
    "plan_points",
    "plan_ui_display_summary",
    "qualitative_boundary_values",
    "re",
    "reconcile",
    "resolved_image_asset_ids",
    "unicodedata",
    "unsupported_numbers",
    "validate_cases",
    "verify_evidence",
]
