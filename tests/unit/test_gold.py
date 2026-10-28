"""使用虚构数据测试通用标注校验，不读取私有需求。"""

import copy
import importlib.util
from pathlib import Path

import pytest
from test_domain import examples

spec = importlib.util.spec_from_file_location("convert_gold", Path("scripts/convert_gold.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def sample():
    rows = examples()
    return {
        key: [rows[index].model_dump()]
        for key, index in [
            ("evidence", 4),
            ("features", 5),
            ("rules", 6),
            ("test_points", 7),
            ("test_cases", 8),
            ("issues", 10),
        ]
    }


def test_valid_synthetic_annotation():
    module.validate_gold(sample())


@pytest.mark.parametrize(
    "collection,field",
    [("rules", "evidence_ids"), ("test_points", "rule_ids"), ("test_cases", "point_ids")],
)
def test_invalid_references(collection, field):
    data = sample()
    data[collection][0][field] = ["missing"]
    with pytest.raises(ValueError, match="reference"):
        module.validate_gold(data)


def test_duplicate_ids():
    data = sample()
    data["rules"].append(copy.deepcopy(data["rules"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        module.validate_gold(data)


def test_conversion(tmp_path):
    import json

    source, target = tmp_path / "gold.md", tmp_path / "gold.json"
    source.write_text("```json\n" + json.dumps(sample()) + "\n```", encoding="utf-8")
    module.convert(source, target)
    assert json.loads(target.read_text(encoding="utf-8")) == sample()
