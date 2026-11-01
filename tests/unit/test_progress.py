"""终端心跳有明确生命周期；显示异常不会破坏业务执行。"""

import asyncio

import pytest

from tc_rag.application.progress import ProgressEvent, report
from tc_rag.interfaces.progress import TerminalProgress, format_duration


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_heartbeat_reports_waiting_and_stops_on_context_exit(monkeypatch, fail):
    messages, heartbeat = [], asyncio.Event()

    def echo(message, *, err):
        assert err is True
        messages.append(message)
        if "[等待]" in message:
            heartbeat.set()

    monkeypatch.setattr("tc_rag.interfaces.progress.typer.echo", echo)
    reporter = TerminalProgress(interval=0.001)

    async def execute():
        async with reporter:
            reporter(ProgressEvent("stage", "extract"))
            reporter(ProgressEvent("batch_started", "extract", current=1, total=3))
            reporter(ProgressEvent("model_started", "extract"))
            await asyncio.wait_for(heartbeat.wait(), 2)
            reporter(ProgressEvent("model_finished", "extract"))
            if fail:
                raise RuntimeError("fixture")

    if fail:
        with pytest.raises(RuntimeError, match="fixture"):
            await execute()
    else:
        await execute()
    assert reporter.task.done()
    assert reporter.active == 0
    assert any("提炼规则" in message and "1 个模型请求" in message for message in messages)


def test_ui_failure_does_not_change_business_result():
    def broken(event):
        raise OSError("terminal closed")

    report(broken, ProgressEvent("stage", "extract"))
    report(None, ProgressEvent("stage", "extract"))


def test_progress_does_not_echo_error_payload(capsys):
    reporter = TerminalProgress()
    reporter(ProgressEvent("retry", "extract", attempt=1, total=3, error="private-body"))
    reporter(ProgressEvent("repair", "generate", attempt=1))
    reporter(ProgressEvent("cache", "extract"))
    captured = capsys.readouterr()
    assert not captured.out
    assert "重试" in captured.err and "结构修复" in captured.err
    assert "private-body" not in captured.err


def test_vision_failure_and_summary_are_visible_without_raw_error(capsys):
    reporter = TerminalProgress()
    reporter(ProgressEvent("stage", "vision"))
    reporter(ProgressEvent("image_completed", "vision", completed=1, total=2, error="HTTP_429"))
    reporter(ProgressEvent("image_completed", "vision", completed=2, total=2, error="private-body"))
    reporter(ProgressEvent("vision_summary", "vision", total=2, completed=0, failed=2))
    captured = capsys.readouterr()
    assert "服务商限流" in captured.err and "失败 2 张" in captured.err
    assert "待确认" in captured.err and "private-body" not in captured.err
    assert reporter.completed == reporter.total == 2


def test_pacing_wait_is_quiet_until_meaningful(capsys):
    reporter = TerminalProgress()
    reporter(ProgressEvent("stage", "vision"))
    reporter(ProgressEvent("request_wait", "vision", duration_ms=1000, wait_reason="pacing"))
    reporter(ProgressEvent("request_wait", "vision", duration_ms=1000, wait_reason="pacing"))
    captured = capsys.readouterr()
    assert "请求间隔等待" not in captured.err

    reporter(ProgressEvent("request_wait", "vision", duration_ms=3000, wait_reason="pacing"))
    reporter(ProgressEvent("request_wait", "vision", duration_ms=3000, wait_reason="pacing"))
    captured = capsys.readouterr()
    assert captured.err.count("请求间隔等待") == 1
    assert "vision_min_interval_seconds" in captured.err


def test_vision_business_errors_and_cooldown_are_actionable(capsys):
    reporter = TerminalProgress()
    reporter(
        ProgressEvent(
            "retry", "vision", attempt=1, total=3, error="HTTP_429_BIGMODEL_1302", duration_ms=10000
        )
    )
    reporter(ProgressEvent("request_wait", "vision", duration_ms=20000))
    reporter(ProgressEvent("vision_blocked", "vision", error="HTTP_429_BIGMODEL_1113"))
    reporter(ProgressEvent("vision_blocked", "vision", error="private-body"))
    captured = capsys.readouterr()
    assert "1302" in captured.err and "账户请求速率" in captured.err
    assert "冷却" in captured.err and "20.0 秒" in captured.err
    assert "1113" in captured.err and "余额不足" in captured.err
    assert "停止发送" in captured.err and "private-body" not in captured.err


@pytest.mark.asyncio
async def test_numbered_stages_include_two_index_preparations_and_rag_summary(capsys, monkeypatch):
    """按真实事件编号；两轮索引准备均显示，控制状态不占用编号。"""
    clock = [100.0]
    monkeypatch.setattr("tc_rag.interfaces.progress.perf_counter", lambda: clock[0])
    reporter = TerminalProgress()
    async with reporter:
        reporter(ProgressEvent("stage", "index"))
        clock[0] += 2
        reporter(ProgressEvent("stage", "plan_points"))
        reporter(ProgressEvent("retrieval_completed", "plan_points", rerank_status="succeeded"))
        reporter(ProgressEvent("retrieval_completed", "plan_points", rerank_status="fallback"))
        clock[0] += 3
        reporter(ProgressEvent("stage", "points_ready"))
        reporter(ProgressEvent("stage", "index"))
        clock[0] += 1
        reporter(ProgressEvent("stage", "generate"))
        reporter(ProgressEvent("retrieval_completed", "generate", rerank_status="disabled"))
        clock[0] += 4
        reporter(ProgressEvent("stage", "completed"))
    output = capsys.readouterr()
    assert not output.out
    assert "[阶段] 01 准备检索索引" in output.err
    assert "[阶段] 03 准备检索索引" in output.err
    assert "[阶段] 04 RAG + Skill 生成用例" in output.err
    assert "[阶段] 05" not in output.err
    assert "[衔接]" in output.err
    assert "RAG 检索 2 批｜重排成功 1 批 / 降级 1 批" in output.err
    assert "RAG 检索 1 批｜重排：配置关闭" in output.err
    assert "[阶段汇总] 流程完成｜总用时 10秒" in output.err
    assert [row[2] for row in reporter.history] == [2, 3, 1, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_failure_or_cancellation_marks_current_stage_interrupted(capsys, error):
    reporter = TerminalProgress()
    with pytest.raises(error):
        async with reporter:
            reporter(ProgressEvent("stage", "extract"))
            raise error("private-body")
    output = capsys.readouterr().err
    assert "[中断] 01 提炼规则" in output
    assert "[阶段汇总] 流程中断" in output
    assert "[完成]" not in output and "流程完成" not in output
    assert "private-body" not in output
    assert reporter.task.done()


@pytest.mark.asyncio
async def test_duplicate_stage_keeps_counters_and_cache_is_summarized(capsys):
    reporter = TerminalProgress()
    async with reporter:
        reporter(ProgressEvent("stage", "extract"))
        reporter(ProgressEvent("batch_started", "extract", current=1, total=2))
        reporter(ProgressEvent("model_started", "extract"))
        for _ in range(30):
            reporter(ProgressEvent("cache", "extract"))
        reporter(ProgressEvent("stage", "extract"))
        assert reporter.total == 2 and reporter.active == 1
        reporter(ProgressEvent("model_finished", "extract"))
        reporter(ProgressEvent("retry", "extract", attempt=1, total=3))
        reporter(ProgressEvent("repair", "extract", attempt=1))
        reporter(ProgressEvent("stage", "points_ready"))
    output = capsys.readouterr().err
    assert output.count("[阶段]") == 1
    assert "模型缓存 30 批｜重试 1 次｜结构修复 1 次" in output
    assert "复用已保存的模型结果" not in output
    assert "本次执行结束" in output and "流程完成" not in output


@pytest.mark.asyncio
async def test_finalization_failure_does_not_report_overall_success(capsys):
    """completed 事件后还有清单导出，收尾失败不能显示整体成功。"""
    with pytest.raises(OSError):
        async with TerminalProgress() as reporter:
            reporter(ProgressEvent("stage", "export_cases"))
            reporter(ProgressEvent("stage", "completed"))
            raise OSError("private-path")
    output = capsys.readouterr().err
    assert "流程中断" in output and "流程完成" not in output
    assert "private-path" not in output


@pytest.mark.asyncio
async def test_terminal_write_failure_during_summary_preserves_business_error(monkeypatch):
    reporter = TerminalProgress()

    def broken(*args, **kwargs):
        raise OSError("terminal closed")

    with pytest.raises(RuntimeError, match="business failure"):
        async with reporter:
            reporter(ProgressEvent("stage", "extract"))
            monkeypatch.setattr("tc_rag.interfaces.progress.typer.echo", broken)
            raise RuntimeError("business failure")
    assert reporter.task.done()


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0796, "80毫秒"),
        (0.0006, "1毫秒"),
        (0.9995, "1秒"),
        (1.0, "1秒"),
        (10.4, "10秒"),
        (65.2, "1分5秒"),
        (0, "0秒"),
        (-1, "0秒"),
    ],
)
def test_format_duration_keeps_subsecond_stages_visible(seconds, expected):
    # 快阶段若按整秒显示会被误读成 0 秒，这里固定毫秒与整秒的分界。
    assert format_duration(seconds) == expected


@pytest.mark.parametrize("kind,stage", [
    ("batch_completed", "extract"), ("image_completed", "vision"),
])
def test_normal_updates_share_interval_and_completion_is_immediate(
    monkeypatch, capsys, kind, stage
):
    clock = [100.0]
    monkeypatch.setattr("tc_rag.interfaces.progress.perf_counter", lambda: clock[0])
    reporter = TerminalProgress()
    reporter(ProgressEvent("stage", stage))
    capsys.readouterr()
    clock[0] += 1
    reporter(ProgressEvent(kind, stage, completed=1, total=4))
    assert not capsys.readouterr().err
    clock[0] = 120
    reporter(ProgressEvent(kind, stage, completed=2, total=4))
    assert "2/4" in capsys.readouterr().err
    clock[0] += 1
    reporter(ProgressEvent(kind, stage, completed=3, total=4))
    assert not capsys.readouterr().err
    reporter(ProgressEvent(kind, stage, completed=4, total=4))
    assert "4/4" in capsys.readouterr().err


def test_failure_and_capped_safe_repair_details_are_not_throttled(capsys):
    reporter = TerminalProgress()
    reporter(ProgressEvent("stage", "vision"))
    capsys.readouterr()
    reporter(ProgressEvent("image_completed", "vision", completed=1, total=100, error="HTTP_429"))
    assert "服务商限流" in capsys.readouterr().err
    reporter(ProgressEvent("repair", "generate", attempt=1, validation_errors=(
        "cases: list_type", "cases.0.priority: literal_error", "<unknown_field>: extra_forbidden",
        "fourth-safe-detail",
    )))
    message = capsys.readouterr().err
    assert "cases: list_type" in message and "<unknown_field>" in message
    assert "fourth-safe-detail" not in message
