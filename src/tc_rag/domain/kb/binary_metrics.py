"""固定片段全集下的标准二分类计数与指标。"""


def confusion(universe: set[str], relevant: set[str], returned: list[str]) -> dict[str, int]:
    if not relevant <= universe or not set(returned) <= universe:
        raise ValueError("标签或返回片段超出全集")
    if len(returned) != len(set(returned)):
        raise ValueError("返回片段重复")
    selected = set(returned)
    return {"tp": len(selected & relevant), "fp": len(selected - relevant),
            "fn": len(relevant - selected), "tn": len(universe - relevant - selected)}


def metrics(counts: dict[str, int]) -> dict[str, float | None]:
    tp, fp, fn, tn = (counts[k] for k in ("tp", "fp", "fn", "tn"))
    if any(type(v) is not int or v < 0 for v in (tp, fp, fn, tn)):
        raise ValueError("混淆矩阵计数必须是非负整数")
    return {"precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            "accuracy": (tp + tn) / (tp + fp + fn + tn) if tp + fp + fn + tn else None}
