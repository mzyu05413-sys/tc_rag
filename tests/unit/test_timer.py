import asyncio
import inspect

import pytest

from tc_rag.infrastructure.runtime.timer import Timer, timed


def test_sync_result_metadata_and_no_binding():
    @timed(stage="parse")
    def add(value: int) -> int:
        """用于验证装饰器元数据保留行为的示例函数。"""
        return value + 1

    assert add(1) == 2
    assert add.__name__ == "add"
    assert add.__doc__ == "用于验证装饰器元数据保留行为的示例函数。"
    timer = Timer("r1")
    with timer.bind():
        assert add(4) == 5
    (event,) = timer.events
    assert (event.run_id, event.stage, event.name, event.success) == ("r1", "parse", "add", True)
    assert event.duration_ms >= 0
    assert timer.export_events()[0]["kind"] == "call"


def test_exception_is_preserved_and_recorded():
    error = ValueError("demo")

    @timed(stage="extract", name="failure")
    def fail():
        raise error

    timer = Timer("r")
    with pytest.raises(ValueError) as caught, timer.wall():
        fail()
    assert caught.value is error
    assert len(timer.events) == 2
    assert all(not event.success for event in timer.events)
    assert {row["kind"] for row in timer.summary()} == {"call", "wall"}


@pytest.mark.asyncio
async def test_async_concurrent_wall_is_not_call_sum():
    @timed(stage="embedding", name="batch")
    async def work(value):
        await asyncio.sleep(0.1)
        return value

    assert inspect.iscoroutinefunction(work)
    timer = Timer("concurrent")
    with timer.wall():
        assert await asyncio.gather(*(work(i) for i in range(5))) == list(range(5))
    calls = [event for event in timer.events if event.kind == "call"]
    (wall,) = [event for event in timer.events if event.kind == "wall"]
    assert len(calls) == 5
    assert wall.duration_ms < sum(event.duration_ms for event in calls)
    row = next(row for row in timer.summary() if row["kind"] == "call")
    assert row["count"] == 5
    assert row["failed_count"] == 0
    assert row["p50_ms"] <= row["p95_ms"]


@pytest.mark.asyncio
async def test_async_exception_and_cancellation():
    @timed(stage="model")
    async def fail():
        raise RuntimeError("demo")

    @timed(stage="model")
    async def cancel():
        raise asyncio.CancelledError()

    timer = Timer("r")
    with timer.bind():
        with pytest.raises(RuntimeError):
            await fail()
        with pytest.raises(asyncio.CancelledError):
            await cancel()
    assert len(timer.events) == 2
    assert all(not event.success for event in timer.events)


@pytest.mark.asyncio
async def test_concurrent_runs_are_isolated():
    @timed(stage="demo")
    async def work():
        await asyncio.sleep(0)

    async def run(timer):
        with timer.bind():
            await work()

    first, second = Timer("first"), Timer("second")
    await asyncio.gather(run(first), run(second))
    assert [event.run_id for event in first.events] == ["first"]
    assert [event.run_id for event in second.events] == ["second"]


def test_empty_summary_and_nested_binding_restore():
    first, second = Timer("first"), Timer("second")
    assert first.summary() == []

    @timed(stage="demo")
    def work():
        pass

    with first.bind():
        with second.bind():
            work()
        work()
    work()
    assert len(first.events) == len(second.events) == 1


def test_summary_exact_values(monkeypatch):
    import tc_rag.infrastructure.runtime.timer as module

    ticks = iter([0.0, 0.010, 1.0, 1.020, 2.0, 2.030])
    monkeypatch.setattr(module, "perf_counter", lambda: next(ticks))

    @timed(stage="demo")
    def work():
        pass

    timer = Timer("r")
    with timer.bind():
        for _ in range(3):
            work()
    (row,) = timer.summary()
    assert row["count"] == 3
    assert row["total_ms"] == pytest.approx(60)
    assert row["mean_ms"] == pytest.approx(20)
    assert row["p50_ms"] == pytest.approx(20)
    assert row["p95_ms"] == pytest.approx(30)
