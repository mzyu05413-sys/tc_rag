import runpy

classify = runpy.run_path("eval/kb/diagnose_feature_gaps.py")["classify_missing"]


def chunk(block):
    return {"sources": [dict(block_id=block, start_char=0, end_char=10)]}


def rule(blocks):
    return dict(rule_id="r", evidence=[dict(block_id=b, start_char=0, end_char=10)
                                       for b in blocks])


def test_missing_body_can_supply_required_source_and_complete_rule():
    row = dict(body_label=1, image_evidence=[])
    result = classify(row, [chunk("a")], chunk("b"), [rule(["a", "b"])])
    assert result["category"] == "adds_missing_required_body_evidence"
    assert result["would_complete_rules_if_added"] == ["r"]


def test_one_added_source_may_still_not_complete_all_conditions():
    row = dict(body_label=1, image_evidence=[])
    result = classify(row, [], chunk("b"), [rule(["a", "b"])])
    assert result["adds_required_rule_evidence"] == ["r"]
    assert result["would_complete_rules_if_added"] == []


def test_duplicate_body_does_not_increase_rule_coverage():
    row = dict(body_label=1, image_evidence=[])
    result = classify(row, [chunk("a")], chunk("a"), [rule(["a"])])
    assert result["category"] == "body_evidence_already_covered"
    assert result["would_complete_rules_if_added"] == []


def test_image_link_does_not_replace_missing_body_condition():
    row = dict(body_label=0, image_evidence=[dict(rule_ids=["r"])])
    result = classify(row, [], chunk("vision"), [rule(["a"])])
    assert result["category"] == "partial_image_for_incomplete_rule"
    assert result["would_complete_rules_if_added"] == []
    result = classify(row, [chunk("a")], chunk("vision"), [rule(["a"])])
    assert result["category"] == "image_subscenario_for_complete_rule"
