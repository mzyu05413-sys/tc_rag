"""Monotonic timings with context-local run attribution."""

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
F = TypeVar("F", bound=Callable[..., Any])


class Timer:
    """Own one run's events; bind explicitly around the work to measure."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._events: list[TimingEvent] = []
        self._lock = Lock()

    @property
    def events(self) -> tuple[TimingEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def _record(self, stage: str, name: str, start: float, success: bool,
                kind: str = "call") -> None:
        event = TimingEvent(self.run_id, stage, name, (perf_counter() - start) * 1000,
                            success, kind)
        with self._lock:
            self._events.append(event)

    @contextmanager
    def bind(self) -> Iterator["Timer"]:
        """Make this collector active; asyncio child tasks inherit the binding."""
        token = _active.set(self)
        try:
            yield self
        finally:
            _active.reset(token)

    @contextmanager
    def wall(self, *, stage: str = "run", name: str = "total") -> Iterator[None]:
        """Measure actual elapsed time, separately from concurrent call totals."""
        start = perf_counter()
        success = False
        try:
            with self.bind():
                yield
            success = True
        finally:
            self._record(stage, name, start, success, "wall")

    def summary(self) -> list[dict[str, Any]]:
        """Group by stage/name/kind. Percentiles use nearest rank, including failures."""
        groups: dict[tuple[str, str, str], list[TimingEvent]] = defaultdict(list)
        for event in self.events:
            groups[event.stage, event.name, event.kind].append(event)
        result: list[dict[str, Any]] = []
        for (stage, name, kind), events in sorted(groups.items()):
            values = sorted(event.duration_ms for event in events)
            count = len(values)
            result.append({
                "run_id": self.run_id, "stage": stage, "name": name, "kind": kind,
                "count": count, "failed_count": sum(not event.success for event in events),
                "total_ms": sum(values), "mean_ms": sum(values) / count,
                "p50_ms": values[ceil(count * 0.50) - 1],
                "p95_ms": values[ceil(count * 0.95) - 1],
            })
        return result

    def export_events(self) -> list[dict[str, Any]]:
        return [asdict(event) for event in self.events]


def timed(*, stage: str, name: str | None = None) -> Callable[[F], F]:
    """Record sync/async calls when bound; preserve return values and exceptions."""
    def decorate(func: F) -> F:
        event_name = name or func.__name__
        if inspect.iscoroutinefunction(func):
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
                    if timer is not None:
                        timer._record(stage, event_name, start, success)
            return cast(F, async_wrapper)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
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
