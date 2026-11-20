"""按功能/场景分批：先按功能相邻分组，再在组内按数量切片，保持全局顺序。

保持全局顺序是因为 pipeline 归并结果时严格按输入顺序（见 pipeline 中“归并仍严格
按规则的原始顺序进行”）。因此这里只把相邻的同一功能合并成组，不跨位置重排，避免
把同功能但分散在不同位置的条目强行聚合而破坏归并顺序。功能先被规范目录（A1）排序
后，相邻分组自然形成按功能的批次边界。
"""

from collections.abc import Callable, Hashable, Sequence
from typing import TypeVar

T = TypeVar("T")


def group_by_feature(
    entries: Sequence[T], feature_key: Callable[[T], Hashable], size: int
) -> list[list[T]]:
    """相邻同键分组，组内按 size 切片；空输入返回空列表，size 必须为正。"""
    if size <= 0:
        raise ValueError("批次大小必须为正")
    groups: list[list[T]] = []
    for entry in entries:
        key = feature_key(entry)
        if groups and feature_key(groups[-1][0]) == key:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    batches: list[list[T]] = []
    for group in groups:
        batches.extend(group[offset : offset + size] for offset in range(0, len(group), size))
    return batches
