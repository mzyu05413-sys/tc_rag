"""使用单调时钟计时，并通过上下文隔离不同运行的记录。

本模块是可复用的内存计时组件，需显式绑定 Timer 才会记录。
主流程的阶段耗时、模型调用耗时和检索分段耗时由 Pipeline、模型适配器和
RagContext 分别持久化，不经过本装饰器；不要把两者混为同一数据来源。
"""

import inspect
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from functools import wraps
from math import ceil
from threading import Lock
from time import perf_counter
from typing import Any, TypeVar, cast


@dataclass(frozen=True)
class TimingEvent:
    run_id: str
    stage: str
    name: str
    duration_ms: float
    success: bool
    kind: str = "call"


_active: ContextVar["Timer | None"] = ContextVar("active_timer", default=None)
# 当前收集器属于执行上下文，asyncio 子任务会继承；并发运行各自绑定，避免串记。
F = TypeVar("F", bound=Callable[..., Any])


class Timer:
    """收集一次运行的计时事件；通过显式绑定指定要记录的执行范围。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._events: list[TimingEvent] = []
        self._lock = Lock()

    @property
    def events(self) -> tuple[TimingEvent, ...]:
        # 返回快照，调用方不能直接修改收集器内部列表。
        with self._lock:
            return tuple(self._events)

    def _record(
        self, stage: str, name: str, start: float, success: bool, kind: str = "call"
    ) -> None:
        event = TimingEvent(
            self.run_id, stage, name, (perf_counter() - start) * 1000, success, kind
        )
        with self._lock:
            self._events.append(event)

    @contextmanager
    def bind(self) -> Iterator["Timer"]:
        """激活当前收集器；异步子任务继承此绑定。"""
        token = _active.set(self)
        try:
            yield self
        finally:
            # 恢复进入前的绑定，支持嵌套使用；不能简单设置为 None。
            _active.reset(token)

    @contextmanager
    def wall(self, *, stage: str = "run", name: str = "total") -> Iterator[None]:
        """测量实际经过的墙钟时间，与并发调用的累计耗时分开记录。"""
        # 单调时钟不受系统时间校准影响；墙钟单独记录，不能用并发调用之和替代。
        start = perf_counter()
        success = False
        try:
            with self.bind():
                yield
            success = True
        finally:
            self._record(stage, name, start, success, "wall")

    def summary(self) -> list[dict[str, Any]]:
        """按阶段、名称和类型分组；包含失败调用，百分位使用最近秩法。"""
        groups: dict[tuple[str, str, str], list[TimingEvent]] = defaultdict(list)
        for event in self.events:
            groups[event.stage, event.name, event.kind].append(event)
        result: list[dict[str, Any]] = []
        for (stage, name, kind), events in sorted(groups.items()):
            # 失败调用也参与耗时统计；最近秩百分位采用 ceil(n*p)-1 的零基下标。
            values = sorted(event.duration_ms for event in events)
            count = len(values)
            result.append(
                {
                    "run_id": self.run_id,
                    "stage": stage,
                    "name": name,
                    "kind": kind,
                    "count": count,
                    "failed_count": sum(not event.success for event in events),
                    "total_ms": sum(values),
                    "mean_ms": sum(values) / count,
                    "p50_ms": values[ceil(count * 0.50) - 1],
                    "p95_ms": values[ceil(count * 0.95) - 1],
                }
            )
        return result

    def export_events(self) -> list[dict[str, Any]]:
        return [asdict(event) for event in self.events]


def timed(*, stage: str, name: str | None = None) -> Callable[[F], F]:
    """绑定收集器后记录同步或异步调用，保留原返回值与异常。"""

    def decorate(func: F) -> F:
        event_name = name or func.__name__
        if inspect.iscoroutinefunction(func):
            # 必须 await 原协程，才能测到执行耗时，而不是仅创建协程对象的耗时。
            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                timer = _active.get()
                start = perf_counter()
                success = False
                try:
                    result = await func(*args, **kwargs)
                    success = True
                    return result
                finally:
                    # 异常或取消也记录失败，且不吞掉原异常；未绑定时只执行原函数。
                    if timer is not None:
                        timer._record(stage, event_name, start, success)

            return cast(F, async_wrapper)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            # wraps 保留函数名、文档和原函数引用，方便调试与检查。
            timer = _active.get()
            start = perf_counter()
            success = False
            try:
                result = func(*args, **kwargs)
                success = True
                return result
            finally:
                if timer is not None:
                    timer._record(stage, event_name, start, success)

        return cast(F, sync_wrapper)

    return decorate
