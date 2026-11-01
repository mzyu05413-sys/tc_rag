"""证据核验、保守归并与确定性测试设计；无法确定的语义留待人工。"""

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, Literal

from tc_rag.application.services.chunking import normalized_quote, verify_evidence
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


def issue(
    kind: str,
    description: str,
    ids: list[str] | None = None,
    severity: str = "warning",
) -> Issue:
    return Issue.model_validate(
        dict(
            issue_id=new_id("issue"),
            kind=kind,
            severity=severity,
            description=description,
            object_ids=ids or [],
        )
    )


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

_SIZE_UNITS = {"MB", "GB", "KB"}
_TIME_UNITS = {"秒", "分钟", "小时"}
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


_METADATA_MARKERS = (
    "文档标题",
    "章节标题",
    "仅包含标题",
    "内容不包含",
    "文档包含",
)


def _is_metadata_rule(
    rule: RequirementRule,
    evidence_by_id: Mapping[str, EvidenceSpan],
    block_map: Mapping[str, DocumentBlock],
) -> bool:
    """文档标题/封面/纯章节标题不是可执行需求，不进入规则与测试点。"""
    if rule.kind != "other" or rule.structured_constraint is not None:
        return False
    evidence = [evidence_by_id[item] for item in rule.evidence_ids if item in evidence_by_id]
    blocks = [block_map[item.block_id] for item in evidence if item.block_id in block_map]
    if not blocks or len(blocks) != len(evidence):
        return False
    if any(block.kind != "heading" for block in blocks):
        return False
    return any(marker in rule.statement for marker in _METADATA_MARKERS)


def check_extraction(candidate: Extraction, blocks: list[DocumentBlock]) -> Extraction:
    """拒绝不存在或定位不符的引文，引用闭包失效的规则不会进入生成。"""
    block_map = {b.block_id: b for b in blocks}
    result = Extraction(issues=list(candidate.issues))
    for evidence in candidate.evidence:
        block = block_map.get(evidence.block_id)
        if block is not None and verify_evidence(evidence, block):
            result.evidence.append(evidence)
        else:
            result.issues.append(
                issue("validation", "引文不在当前原文块中或定位不符", [evidence.evidence_id])
            )
    valid_evidence = {e.evidence_id for e in result.evidence}
    for feature in candidate.features:
        if set(feature.evidence_ids) <= valid_evidence:
            result.features.append(feature)
        else:
            result.issues.append(issue("validation", "功能引用了无效证据", [feature.feature_id]))
    valid_features = {f.feature_id for f in result.features}
    evidence_by_id = {e.evidence_id: e for e in result.evidence}
    metadata_rule_ids: set[str] = set()
    for rule in candidate.rules:
        if rule.feature_id in valid_features and set(rule.evidence_ids) <= valid_evidence:
            if _is_metadata_rule(rule, evidence_by_id, block_map):
                metadata_rule_ids.add(rule.rule_id)
                result.issues.append(
                    issue(
                        "gap",
                        "文档标题/章节标题不是可执行需求，已从规则集移除",
                        [rule.feature_id],
                        severity="info",
                    )
                )
                continue
            # 词面引文通过仍不能证明模型的规则解释正确，默认保留人工复核状态。
            checked = rule.model_copy(deep=True)
            checked.status = (
                rule.status if rule.status in {"ambiguous", "conflicting"} else "needs_review"
            )
            constraint = checked.structured_constraint
            quote = "\n".join(
                e.quote for e in result.evidence if e.evidence_id in rule.evidence_ids
            )
            derived = False
            if rule.kind == "field_constraint" and (
                constraint is None or constraint.boundary is None
            ):
                inferred = derive_constraint(quote)
                if inferred is not None:
                    checked.structured_constraint = inferred
                    constraint = inferred
                    derived = True
                    result.issues.append(
                        issue(
                            "other",
                            "程序从原文补充数值约束",
                            [rule.rule_id],
                            severity="info",
                        )
                    )
            if constraint is not None:
                if constraint.boundary is not None and str(constraint.boundary) not in quote:
                    constraint.boundary = None
                    result.issues.append(
                        issue("gap", "结构化边界数值在引文中不存在", [rule.rule_id])
                    )
                if constraint.unit and constraint.unit not in quote:
                    constraint.unit = None
                    result.issues.append(issue("gap", "结构化单位缺少引文依据", [rule.rule_id]))
                required_like = any(
                    word in rule.statement
                    for word in (
                        "必填",
                        "必选",
                        "不能为空",
                        "必须填写",
                        "不可为空",
                        "不能缺失",
                    )
                )
                if (
                    constraint.boundary is not None
                    and required_like
                    and not re.search(r"\d", rule.statement)
                ):
                    constraint.boundary = None
                    constraint.unit = None
                    constraint.precision = None
                    result.issues.append(
                        issue(
                            "gap",
                            "必填规则没有数值依据，已移除跨句边界",
                            [rule.rule_id],
                            severity="info",
                        )
                    )
                if constraint.precision is not None and not derived:
                    reliable = (
                        bool(re.search(r"整数|[个条项字符]", quote))
                        if constraint.precision == 0
                        else bool(re.search(rf"小数\s*{constraint.precision}\s*位", quote))
                    )
                    if not reliable:
                        constraint.precision = None
                        result.issues.append(
                            issue("gap", "数值精度缺少明确依据，需人工确认", [rule.rule_id])
                        )
            result.rules.append(checked)
        else:
            result.issues.append(issue("validation", "规则的功能/证据引用无效", [rule.rule_id]))
    if metadata_rule_ids:
        result.issues = [
            item.model_copy(
                update={
                    "object_ids": [
                        object_id
                        for object_id in item.object_ids
                        if object_id not in metadata_rule_ids
                    ]
                }
            )
            for item in result.issues
        ]
    result.entities = [e for e in candidate.entities if set(e.evidence_ids) <= valid_evidence]
    return result


def error_rule_ids(issues: list[Issue], rules: list[RequirementRule]) -> set[str]:
    """模型以 error 明确判定无法提炼的规则不再进入测试点规划。"""
    known = {rule.rule_id for rule in rules}
    return {
        object_id
        for item in issues
        if item.severity == "error"
        for object_id in item.object_ids
        if object_id in known
    }


def _normalized_statement(text: str) -> str:
    """用于近义比较：统一全半角、去标点空白和常见模态词，保留否定词。"""
    value = unicodedata.normalize("NFKC", text).casefold()
    value = re.sub(r"[\W_]+", "", value)
    for word in ("需要", "必须", "应当", "应该", "需", "须", "应"):
        value = value.replace(word, "")
    return value


_NEGATION_WORDS = ("不", "禁止", "不得", "不可", "无需", "仅", "只")


def _negation_profile(text: str) -> frozenset[str]:
    return frozenset(word for word in _NEGATION_WORDS if word in text)


def _constraint_key(rule: RequirementRule) -> str | None:
    constraint = rule.structured_constraint
    if constraint is None:
        return None
    data = constraint.model_dump(mode="json")
    data["operator"] = _normalized_operator(constraint.operator)
    boundary = data.get("boundary")
    if isinstance(boundary, str):
        try:
            data["boundary"] = format(Decimal(boundary), "f")
        except (InvalidOperation, ValueError):
            pass
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def _find_near_duplicate(
    rule: RequirementRule, bucket: list[RequirementRule]
) -> RequirementRule | None:
    """同功能、同类型、同约束下，语句完全或高度相似才视为同一规则。"""
    normalized = _normalized_statement(rule.statement)
    profile = _negation_profile(rule.statement)
    for candidate in bucket:
        other = _normalized_statement(candidate.statement)
        if other == normalized:
            return candidate
        if profile != _negation_profile(candidate.statement):
            continue
        if abs(len(other) - len(normalized)) > 8:
            continue
        if SequenceMatcher(None, other, normalized).ratio() >= 0.90:
            return candidate
    return None


_RULE_STATUS_ORDER = {"supported": 0, "needs_review": 1, "ambiguous": 2, "conflicting": 3}
RuleStatus = Literal["supported", "needs_review", "ambiguous", "conflicting"]


def _worse_rule_status(left: RuleStatus, right: RuleStatus) -> RuleStatus:
    return left if _RULE_STATUS_ORDER[left] >= _RULE_STATUS_ORDER[right] else right


def reconcile(extractions: list[Extraction]) -> Extraction:
    """只合并同功能、同原文块范围、同类型、同结构约束的精确重复规则。"""
    merged = Extraction()
    buckets: dict[tuple[str, str, str | None], list[RequirementRule]] = {}
    evidence_map: dict[str, EvidenceSpan] = {}
    features: dict[str, Feature] = {}
    feature_keys: dict[str, str] = {}
    feature_mapping: dict[str, str] = {}
    for extraction in extractions:
        merged.issues.extend(extraction.issues)
        evidence_map.update({e.evidence_id: e for e in extraction.evidence})
        for feature in extraction.features:
            scope = sorted({evidence_map[e].block_id for e in feature.evidence_ids})
            key = fingerprint([normalized_quote(feature.name), feature.description, scope])
            canonical = feature_keys.setdefault(key, feature.feature_id)
            feature_mapping[feature.feature_id] = canonical
            if canonical not in features:
                features[canonical] = feature.model_copy(deep=True)
            else:
                features[canonical].evidence_ids = sorted(
                    set(features[canonical].evidence_ids + feature.evidence_ids)
                )
        merged.entities.extend(extraction.entities)
        for rule in extraction.rules:
            rule = rule.model_copy(
                update={"feature_id": feature_mapping[rule.feature_id]}, deep=True
            )
            bucket_key = (rule.feature_id, rule.kind, _constraint_key(rule))
            bucket = buckets.setdefault(bucket_key, [])
            known = _find_near_duplicate(rule, bucket)
            if known is None:
                bucket.append(rule)
                known = rule
            else:
                known.evidence_ids = sorted(set(known.evidence_ids + rule.evidence_ids))
                known.status = _worse_rule_status(known.status, rule.status)
            merged.rule_mapping[rule.rule_id] = known.rule_id
    merged.evidence = list(evidence_map.values())
    merged.features = list(features.values())
    merged.rules = [rule for bucket in buckets.values() for rule in bucket]
    # 不猜测不同条件是否等价；同字段出现不同约束时显式提示，而非覆盖前一条。
    by_field: dict[tuple[str, str, str | None, str | None], list[RequirementRule]] = {}
    for rule in merged.rules:
        constraint = rule.structured_constraint
        if constraint and constraint.field_path:
            by_field.setdefault(
                (
                    rule.feature_id,
                    constraint.field_path,
                    constraint.applicable_when,
                    constraint.operator,
                ),
                [],
            ).append(rule)
    for rules in by_field.values():
        if len({fingerprint(r.structured_constraint) for r in rules}) > 1:
            merged.issues.append(
                issue("conflict", "同字段存在不同约束，需核对适用条件", [r.rule_id for r in rules])
            )
            for rule in rules:
                rule.status = "conflicting"
    return merged


_OPERATOR_ALIASES = {
    "lt": "<",
    "le": "<=",
    "leq": "<=",
    "gt": ">",
    "ge": ">=",
    "geq": ">=",
    "eq": "==",
    "≤": "<=",
    "≥": ">=",
    "＜": "<",
    "＞": ">",
    "＝": "==",
    "max_length": "<=",
    "min_length": ">=",
    "不大于": "<=",
    "不超过": "<=",
    "至多": "<=",
    "最多": "<=",
    "不小于": ">=",
    "不低于": ">=",
    "至少": ">=",
    "大于": ">",
    "小于": "<",
    "等于": "==",
}
_COMPARISONS = {"<", "<=", ">", ">=", "=="}


def _normalized_operator(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return _OPERATOR_ALIASES.get(text) or _OPERATOR_ALIASES.get(text.lower()) or text


def boundary_values(rule: RequirementRule) -> list[str]:
    """精度明确时生成边界两侧及边界本身；300、整数精度0 → 299/300/301。"""
    c = rule.structured_constraint
    if c is None or c.boundary is None:
        return []
    unit = (c.unit or "").strip()
    if unit.upper() in _SIZE_UNITS or unit in _TIME_UNITS:
        # 尺寸/时长没有可推断的最小步长，改由定性边界描述，不编造小数。
        return []
    operator = _normalized_operator(c.operator)
    if operator not in _COMPARISONS:
        return []
    precision = c.precision
    if precision is None:
        # 长度类约束的精度天然是整数，可确定步长；其他缺精度时保守返回空。
        if (c.operator or "").strip().lower() in {"max_length", "min_length"}:
            precision = 0
        else:
            return []
    try:
        value = Decimal(str(c.boundary))
        step = Decimal(1).scaleb(-precision)
        if not value.is_finite() or value.quantize(step) != value:
            return []
        return [format(v, "f") for v in (value - step, value, value + step)]
    except (InvalidOperation, ValueError):
        return []


def qualitative_boundary_values(rule: RequirementRule) -> list[str]:
    """尺寸/时长类约束用定性边界描述，避免编造原文没有的精度。"""
    c = rule.structured_constraint
    if c is None or c.boundary is None:
        return []
    unit = (c.unit or "").strip()
    if unit.upper() in _SIZE_UNITS:
        label = f"{c.boundary}{unit.upper()}"
    elif unit in _TIME_UNITS:
        label = f"{c.boundary}{unit}"
    else:
        return []
    return [f"略低于{label}", f"等于{label}", f"略高于{label}"]


def plan_points(rules: list[RequirementRule], methods: Sequence[str]) -> list[TestPoint]:
    """按规则种类选择适用方法；不强制为所有规则凑齐所有设计方法。"""
    mapping = {
        "field_constraint": ["equivalence", "boundary"],
        "business_condition": ["decision_table"],
        "permission": ["permission"],
        "state_transition": ["state"],
        "workflow": ["workflow"],
        "error_behavior": ["error_behavior"],
        "other": ["other"],
    }
    points: list[TestPoint] = []
    for rule in rules:
        boundary_values_for_rule = (
            boundary_values(rule) or qualitative_boundary_values(rule)
            if rule.kind == "field_constraint"
            else []
        )
        for method in mapping[rule.kind]:
            if method not in methods and method not in {"workflow", "error_behavior", "other"}:
                continue
            if method == "equivalence" and boundary_values_for_rule:
                # 数值/长度边界点已携带完整数据，不再另造无数据的等价类点。
                continue
            values = boundary_values_for_rule if method == "boundary" else []
            if method == "boundary" and not values:
                continue
            objective = (
                f"{rule.statement}；边界数据={'/'.join(values)}"
                if values
                else rule.statement
            )
            undetermined = rule.status in {"conflicting", "ambiguous"}
            points.append(
                TestPoint.model_validate(
                    dict(
                        point_id=new_id("point"),
                        rule_ids=[rule.rule_id],
                        technique=method,
                        objective=objective,
                        data_variants=values,
                        applicability="undetermined" if undetermined else "applicable",
                        status="needs_clarification" if undetermined else "ready",
                    )
                )
            )
    return points


def _normalized_case_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def case_signature(case: TestCase) -> str:
    """步骤与输入归一化后相同的用例签名；只描述事实，不判断语义是否正确。"""
    return fingerprint(
        [
            [_normalized_case_text(item) for item in case.preconditions],
            case.data,
            [
                [_normalized_case_text(step.action), _normalized_case_text(step.expected)]
                for step in case.steps
            ],
        ]
    )


_NUMBER_PATTERN = r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?"
_TIME_PATTERN = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_CONTRAST_PATTERN = re.compile(r"(?:并非|而非|不是|不按)\s*(\d+(?:\.\d+)?)")


def _numeric_tokens(text: str) -> set[str]:
    """数字与时间统一成可比较的 token，避免 09:00 被拆成 9/00。"""
    times = {
        f"{int(hour):02d}:{minute}"
        for hour, minute in _TIME_PATTERN.findall(text)
    }
    cleaned = _TIME_PATTERN.sub(" ", text)
    numbers = {
        str(int(token)) if token.isdigit() else token
        for token in re.findall(_NUMBER_PATTERN, cleaned)
    }
    return times | numbers


def _case_data_tokens(data: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, (str, int, float)):
            tokens.update(_numeric_tokens(str(value)))

    collect(data)
    return tokens


def unsupported_numbers(
    step: TestStep,
    rules: dict[str, RequirementRule],
    evidence: dict[str, EvidenceSpan],
    allowed_numbers: set[str] | None = None,
) -> list[str]:
    """预期里既不在引文、动作、数据或程序推导边界中的数字。"""
    quote = "\n".join(evidence[e].quote for e in step.evidence_ids if e in evidence)
    known = _numeric_tokens(quote) | _numeric_tokens(step.action)
    known |= allowed_numbers or set()
    for rule_id in step.rule_ids:
        rule = rules.get(rule_id)
        if rule is not None:
            known.update(_numeric_tokens(" ".join(boundary_values(rule))))
    known.update(_CONTRAST_PATTERN.findall(step.expected))
    return [
        number
        for number in sorted(_numeric_tokens(step.expected))
        if number not in known
    ]


_UI_DISPLAY_HINTS = (
    # 只认强展示标记；单凭“页面/列表/按钮”不能判定为纯展示。
    "界面",
    "显示",
    "展示",
    "呈现",
    "标题为",
    "位于",
    "高亮",
    "图标",
    "颜色",
    "配色",
    "布局",
    "样式",
    "字体",
    "文案",
    "标签",
    "表头",
    "菜单",
    "链接",
    "占位",
    "左上角",
    "右上角",
    "左下角",
    "右下角",
    "底部",
    "顶部",
    "列名",
)
_UI_FUNCTION_HINTS = (
    # 只有可执行动作才算功能点；外观描述里的名词（删除/分页/状态）不再否决合并。
    "点击",
    "输入",
    "提交",
    "保存",
    "校验",
    "必填",
    "跳转",
    "上传",
    "下载",
)


def _resolved_rule_evidence(
    rule_ids: list[str],
    rules: Mapping[str, RequirementRule],
    evidence: Mapping[str, EvidenceSpan],
) -> tuple[list[RequirementRule], list[EvidenceSpan]] | None:
    resolved_rules = [rules[rule_id] for rule_id in rule_ids if rule_id in rules]
    if len(resolved_rules) != len(rule_ids) or not resolved_rules:
        return None
    evidence_ids = {item for rule in resolved_rules for item in rule.evidence_ids}
    resolved_evidence = [evidence[item] for item in evidence_ids if item in evidence]
    if len(resolved_evidence) != len(evidence_ids) or not resolved_evidence:
        return None
    return resolved_rules, resolved_evidence


def is_ui_display_rule(
    rule: RequirementRule,
    evidence: Mapping[str, EvidenceSpan],
) -> bool:
    """纯界面展示规则：无数字约束、只有图片外观证据、无可执行动作。"""
    if rule.kind != "other" or rule.structured_constraint is not None:
        return False
    items = [evidence[item] for item in rule.evidence_ids if item in evidence]
    if not items or any(item.origin != "vision" for item in items):
        return False
    if re.search(r"\d", rule.statement):
        return False
    if any(hint in rule.statement for hint in _UI_FUNCTION_HINTS):
        return False
    return any(hint in rule.statement for hint in _UI_DISPLAY_HINTS)


def plan_ui_display_summary(
    rules: list[RequirementRule],
    features: Mapping[str, Feature],
    evidence: Mapping[str, EvidenceSpan],
) -> tuple[list[TestPoint], list[Issue]]:
    """纯界面规则按功能收敛为一条展示汇总点。"""
    groups: dict[str, list[RequirementRule]] = {}
    for rule in rules:
        if is_ui_display_rule(rule, evidence):
            groups.setdefault(rule.feature_id, []).append(rule)
    points: list[TestPoint] = []
    issues: list[Issue] = []
    for feature_id, feature_rules in groups.items():
        name = features[feature_id].name if feature_id in features else "未归类"
        rule_ids = sorted(rule.rule_id for rule in feature_rules)
        objective = (
            feature_rules[0].statement
            if len(feature_rules) == 1
            else f"{name}：界面展示样式与内容正确"
        )
        points.append(
            TestPoint(
                point_id=new_id("point"),
                rule_ids=rule_ids,
                technique="other",
                objective=objective,
                status="ready",
                applicability="applicable",
                display_summary=True,
            )
        )
        issues.append(
            issue(
                "other",
                "纯界面展示规则已在规划前归并汇总，不再逐条生成测试点",
                rule_ids,
                severity="info",
            )
        )
    return points, issues


def is_ui_display_point(
    point: TestPoint,
    rules: Mapping[str, RequirementRule],
    evidence: Mapping[str, EvidenceSpan],
) -> bool:
    """纯界面展示点：vision 外观证据、无功能行为、无结构化约束。"""
    if point.display_summary:
        return False
    if point.technique not in {"other", "equivalence"}:
        return False
    resolved = _resolved_rule_evidence(point.rule_ids, rules, evidence)
    if resolved is None:
        return False
    point_rules, point_evidence = resolved
    if len({rule.feature_id for rule in point_rules}) != 1:
        return False
    if any(
        rule.kind != "other" or rule.structured_constraint is not None
        for rule in point_rules
    ):
        return False
    if any(item.origin != "vision" for item in point_evidence):
        return False
    text = point.objective
    if not any(hint in text for hint in _UI_DISPLAY_HINTS):
        return False
    if any(hint in text for hint in _UI_FUNCTION_HINTS):
        return False
    return True


def _merged_display_state(group: list[TestPoint]) -> tuple[str, str]:
    statuses = {point.status for point in group}
    if "needs_clarification" in statuses:
        return "needs_clarification", "undetermined"
    if "needs_context" in statuses:
        return "needs_context", "undetermined"
    if all(point.status == "not_applicable" for point in group):
        return "not_applicable", "not_applicable"
    if all(point.applicability == "applicable" for point in group):
        return "ready", "applicable"
    return "needs_context", "undetermined"


def merge_ui_display_points(
    points: list[TestPoint],
    rules: Mapping[str, RequirementRule],
    features: Mapping[str, Feature],
    evidence: Mapping[str, EvidenceSpan],
    policy: Literal["merge", "keep"] = "merge",
) -> tuple[list[TestPoint], list[Issue]]:
    """把同一功能的纯界面展示点替换为一条概括点；规则和证据引用保留在汇总点。"""
    if policy != "merge" or not points:
        return list(points), []
    groups: dict[str, list[TestPoint]] = {}
    for point in points:
        if not is_ui_display_point(point, rules, evidence):
            continue
        feature_id = rules[point.rule_ids[0]].feature_id
        groups.setdefault(feature_id, []).append(point)
    if not groups:
        return list(points), []

    merged_by_first: dict[str, TestPoint] = {}
    dropped: set[str] = set()
    issues: list[Issue] = []
    for feature_id, group in groups.items():
        first = group[0]
        rule_ids = sorted({rule_id for point in group for rule_id in point.rule_ids})
        name = features[feature_id].name if feature_id in features else "未归类"
        status, applicability = _merged_display_state(group)
        merged_by_first[first.point_id] = first.model_copy(
            update={
                "rule_ids": rule_ids,
                "technique": "other",
                "objective": f"{name}：界面展示样式与内容正确",
                "status": status,
                "applicability": applicability,
                "display_summary": True,
            }
        )
        dropped.update(point.point_id for point in group[1:])
        description = (
            f"将同一功能的 {len(group)} 个纯界面展示测试点归并为 1 个汇总测试点"
            if len(group) > 1
            else "纯界面展示测试点已改写为功能汇总点"
        )
        issues.append(
            issue(
                "other",
                description,
                [point.point_id for point in group],
                severity="info",
            )
        )
    result = [
        merged_by_first.get(point.point_id, point)
        for point in points
        if point.point_id not in dropped
    ]
    return result, issues


_TECHNIQUE_ORDER = (
    "boundary",
    "decision_table",
    "state",
    "permission",
    "workflow",
    "error_behavior",
    "equivalence",
    "other",
)
_STATUS_ORDER = {"ready": 0, "needs_context": 1, "needs_clarification": 2, "not_applicable": 3}


def _technique_rank(point: TestPoint) -> int:
    try:
        return _TECHNIQUE_ORDER.index(point.technique)
    except ValueError:
        return len(_TECHNIQUE_ORDER)


def _applicability_for(status: str) -> str:
    if status == "ready":
        return "applicable"
    if status == "not_applicable":
        return "not_applicable"
    return "undetermined"


def deduplicate_points(points: list[TestPoint]) -> tuple[list[TestPoint], list[Issue]]:
    """同一规则集合下的相同或近义目标只保留一条，优先保留更具体的测试方法。"""
    kept: list[TestPoint] = []
    merged_ids: dict[str, list[str]] = {}
    for point in points:
        normalized = _normalized_statement(point.objective)
        match_index: int | None = None
        for index, candidate in enumerate(kept):
            if not set(candidate.rule_ids) & set(point.rule_ids):
                continue
            other = _normalized_statement(candidate.objective)
            if other == normalized or (
                candidate.technique != point.technique
                and SequenceMatcher(None, other, normalized).ratio() >= 0.90
            ):
                match_index = index
                break
        if match_index is None:
            kept.append(point)
            continue
        candidate = kept[match_index]
        survivor = (
            candidate if _technique_rank(candidate) <= _technique_rank(point) else point
        )
        dropped = point if survivor is candidate else candidate
        status = min(
            (candidate.status, point.status), key=lambda item: _STATUS_ORDER.get(item, 1)
        )
        kept[match_index] = survivor.model_copy(
            update={
                "rule_ids": sorted(set(candidate.rule_ids) | set(point.rule_ids)),
                "status": status,
                "applicability": _applicability_for(status),
            }
        )
        merged_ids.setdefault(survivor.point_id, []).append(dropped.point_id)
    issues = [
        issue(
            "duplicate",
            f"合并 {len(dropped) + 1} 个重复或近义测试目标",
            [point_id, *dropped],
            severity="info",
        )
        for point_id, dropped in merged_ids.items()
    ]
    return kept, issues


def dedupe_issues(issues: list[Issue]) -> list[Issue]:
    """同类型、同严重度、同描述的问题合并对象 ID，保留首次出现顺序。"""
    merged: dict[tuple[str, str, str], Issue] = {}
    order: list[tuple[str, str, str]] = []
    for item in issues:
        key = (item.kind, item.severity, _normalized_statement(item.description))
        if key in merged:
            previous = merged[key]
            merged[key] = previous.model_copy(
                update={
                    "object_ids": sorted(set(previous.object_ids) | set(item.object_ids)),
                    "status": "resolved",
                    "resolution": "deduplicated",
                }
            )
        else:
            merged[key] = item
            order.append(key)
    return [merged[key] for key in order]


def is_vision_only(
    point: TestPoint,
    rules: Mapping[str, RequirementRule],
    evidence: Mapping[str, EvidenceSpan],
) -> bool:
    """测试点引用的全部证据都来自图片解释等非正文来源。"""
    resolved = _resolved_rule_evidence(point.rule_ids, rules, evidence)
    if resolved is None:
        return False
    _, point_evidence = resolved
    return bool(point_evidence) and all(
        item.origin != "text" or item.uncertain for item in point_evidence
    )


def apply_vision_policy(
    points: list[TestPoint],
    rules: Mapping[str, RequirementRule],
    features: Mapping[str, Feature],
    evidence: Mapping[str, EvidenceSpan],
    policy: Literal["aggregate", "downgrade", "keep"] = "aggregate",
) -> tuple[list[TestPoint], list[Issue]]:
    """仅图片证据默认不生成测试点；每个功能汇总一条待补充问题。"""
    if not points or policy == "keep":
        return list(points), []
    grouped: dict[str, list[str]] = {}
    output: list[TestPoint] = []
    for point in points:
        if not is_vision_only(point, rules, evidence):
            output.append(point)
            continue
        if point.display_summary or "界面展示汇总" in point.objective:
            output.append(point)
            continue
        if policy == "aggregate" and point.technique == "boundary":
            output.append(
                point.model_copy(
                    update={"status": "needs_context", "applicability": "undetermined"}
                )
            )
            continue
        if policy == "downgrade":
            if point.status not in {"needs_clarification", "not_applicable"}:
                point = point.model_copy(
                    update={"status": "needs_context", "applicability": "undetermined"}
                )
            output.append(point)
        resolved = _resolved_rule_evidence(point.rule_ids, rules, evidence)
        feature_id = resolved[0][0].feature_id if resolved is not None else "未归类"
        grouped.setdefault(feature_id, []).append(point.point_id)
    issues = [
        issue(
            "other",
            f"{features[feature_id].name if feature_id in features else feature_id}："
            + (
                "测试点仅有图片证据，未生成测试点，待补充文字依据"
                if policy == "aggregate"
                else "测试点仅有图片证据，待文本依据核对"
            ),
            point_ids,
            severity="info",
        )
        for feature_id, point_ids in sorted(grouped.items())
    ]
    return output, issues


def resolved_image_asset_ids(blocks: list[DocumentBlock]) -> set[str]:
    """列出已有 vision 派生块的图片资产，供问题撤销与质量检查共用。"""
    vision_ids = [block.block_id for block in blocks if block.origin == "vision"]
    resolved: set[str] = set()
    for image in (block for block in blocks if block.kind == "image"):
        if not any(item.startswith(image.block_id + "_vision_") for item in vision_ids):
            continue
        try:
            asset = json.loads(image.content)
        except (TypeError, ValueError):
            continue
        if isinstance(asset, dict) and asset.get("asset_id"):
            resolved.add(str(asset["asset_id"]))
    return resolved


def _mentions_image_asset(item: Issue, asset_ids: set[str]) -> bool:
    """解析阶段的图片问题用 domain 定位，资产 ID 只在描述中；两种形态都识别。"""
    return any(obj in asset_ids for obj in item.object_ids) or any(
        asset_id in item.description for asset_id in asset_ids
    )


def drop_resolved_image_issues(
    issues: list[Issue], blocks: list[DocumentBlock]
) -> list[Issue]:
    """图片已有 vision 派生块时，移除解析阶段遗留的“未解析”问题。"""
    resolved = resolved_image_asset_ids(blocks)
    if not resolved:
        return list(issues)
    return [
        item
        for item in issues
        if not (item.kind == "unparsed" and _mentions_image_asset(item, resolved))
    ]


def validate_cases(
    cases: list[TestCase],
    points: list[TestPoint],
    rules: list[RequirementRule],
    evidence: list[EvidenceSpan],
) -> tuple[list[TestCase], list[CoverageLink], list[Issue]]:
    """校验当前运行引用闭包、步骤覆盖和词面风险；语义正确性仍需人工判断。"""
    pm, rm, em = (
        {p.point_id: p for p in points},
        {r.rule_id: r for r in rules},
        {e.evidence_id: e for e in evidence},
    )
    output: list[TestCase] = []
    links: list[CoverageLink] = []
    issues: list[Issue] = []
    signatures: set[str] = set()
    for case in cases:
        if not set(case.point_ids) <= pm.keys():
            issues.append(issue("validation", "用例引用了当前批次之外的测试点", [case.case_id]))
            continue
        allowed_rules = {r for p in case.point_ids for r in pm[p].rule_ids}
        if any(not step.action.strip() or not step.expected.strip() for step in case.steps):
            issues.append(issue("validation", "用例动作或预期只有空白，拒绝该用例", [case.case_id]))
            continue
        if len(case.point_ids) > 1 and any(pm[p].technique == "boundary" for p in case.point_ids):
            issues.append(
                issue("validation", "边界测试点必须分别生成用例，不能合并", [case.case_id])
            )
            continue
        if any(
            not set(s.rule_ids) <= allowed_rules
            or not set(s.rule_ids) <= rm.keys()
            or not set(s.evidence_ids) <= em.keys()
            for s in case.steps
        ):
            issues.append(issue("validation", "步骤规则/证据引用无效，拒绝该用例", [case.case_id]))
            continue
        flagged = False
        signature = case_signature(case)
        duplicated = signature in signatures
        if duplicated:
            issues.append(issue("duplicate", "步骤及输入完全重复，保留并标记", [case.case_id]))
            flagged = True
        signatures.add(signature)
        for number, step in enumerate(case.steps, 1):
            expected_evidence = {e for r in step.rule_ids for e in rm[r].evidence_ids}
            quote = "\n".join(em[e].quote for e in step.evidence_ids)
            unsupported = unsupported_numbers(step, rm, em, _case_data_tokens(case.data))
            step_flagged = not set(step.evidence_ids) <= expected_evidence or bool(unsupported)
            if step_flagged:
                issues.append(
                    issue(
                        "unsupported_expectation", "预期数值或证据范围缺乏直接依据", [case.case_id]
                    )
                )
                flagged = True
            if normalized_quote(step.expected) not in normalized_quote(quote):
                # 业务化预期通常不会逐字出现在原文中，只作提示，不阻断自动校验状态。
                issues.append(
                    issue(
                        "unsupported_expectation",
                        "预期为业务表达，未逐字出现在原文中，建议人工确认",
                        [case.case_id],
                        severity="info",
                    )
                )
            status: Literal["validated", "needs_review"] = (
                "needs_review" if step_flagged or duplicated else "validated"
            )
            for rule_id in step.rule_ids:
                for point_id in case.point_ids:
                    if rule_id in pm[point_id].rule_ids:
                        links.append(
                            CoverageLink(
                                case_id=case.case_id,
                                step_no=number,
                                rule_id=rule_id,
                                point_id=point_id,
                                validation_status=status,
                            )
                        )
        # 自动检查始终不替代人工批准；此状态只描述技术检查结果。
        output.append(
            case.model_copy(update={"status": "needs_review" if flagged else "validated"})
        )
    covered = {link.point_id for link in links}
    for point in points:
        if point.point_id not in covered:
            issues.append(issue("coverage", "测试点没有用例覆盖", [point.point_id]))
    return output, links, issues
