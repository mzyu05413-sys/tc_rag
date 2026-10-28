"""固定随机样本，对比逐项余弦、批量余弦、平方欧氏距离与内积。"""

import argparse
import json
import platform
from pathlib import Path
from statistics import median
from time import perf_counter

import numpy as np

from tc_rag.infrastructure.retrieval.metrics import get_metric


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dimension", type=int, default=128)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.dimension, args.count, args.batch_size, args.repeats) <= 0 or args.warmup < 0:
        parser.error("维度、条数、批大小和重复次数必须为正，预热次数不能为负")
    rng = np.random.default_rng(args.seed)
    matrix = rng.normal(size=(args.count, args.dimension))
    queries = rng.normal(size=(args.batch_size, args.dimension))
    rows = []
    reference = np.stack([get_metric("cosine_math").score(q, matrix) for q in queries])
    print("度量                 中位耗时(ms/整批)   分数类型             排序方向")
    for name in ("cosine_math", "cosine_numpy", "euclidean", "dot"):
        metric = get_metric(name)
        durations = []
        for iteration in range(args.warmup + args.repeats):
            started = perf_counter()
            values = np.stack([metric.score(q, matrix) for q in queries])
            duration = (perf_counter() - started) * 1000
            if iteration >= args.warmup:
                durations.append(duration)
        if name.startswith("cosine") and not np.allclose(reference, values, rtol=1e-12, atol=1e-12):
            raise ValueError("余弦实现结果不一致")
        direction = "越大越相关" if metric.higher_is_better else "越小越相关"
        rows.append(
            {
                "metric": name,
                "median_ms": median(durations),
                "samples_ms": durations,
                "score_kind": metric.score_kind,
                "higher_is_better": metric.higher_is_better,
            }
        )
        print(f"{name:20} {median(durations):14.3f}   {metric.score_kind:20} {direction}")
    report = {
        "parameters": {k: v for k, v in vars(args).items() if k != "output"},
        "python": platform.python_version(),
        "numpy": np.__version__,
        "platform": platform.platform(),
        "cosine_consistent": True,
        "results": rows,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
