"""终端执行进度：有限行输出与等待心跳，业务层不依赖 Typer。"""

import asyncio
import os
import sys
from contextlib import suppress
from time import perf_counter

import typer

from tc_rag.application.progress import ProgressEvent

_RESET = "\x1b[0m"


def _color_enabled() -> bool:
    """只在真实终端着色；管道、CI 与测试环境保持纯文本。"""
    return sys.stderr.isatty() and not os.environ.get("NO_COLOR")


def _style(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}{_RESET}" if _color_enabled() else text


def _bar(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return ""
    filled = max(0, min(width, int(width * done / total)))
    return "█" * filled + "░" * (width - filled) + f" {done / total * 100:.0f}%"

STAGE_NAMES = {
    "ingest": "接入文件",
    "parse": "解析文档",
    "vision": "图片解释",
    "chunk": "分块",
    "extract": "提炼规则",
    "index": "准备检索索引",
    "plan_points": "RAG 规划测试点",
    "export_points": "导出 XMind",
    "points_ready": "测试点已保存，准备生成用例",
    "generate": "RAG + Skill 生成用例",
    "validate": "校验引用和覆盖",
    "export_cases": "联合导出 XMind、Excel 与报告",
    "completed": "已完成",
}

STAGE_DETAILS = {
    "ingest": "保存文档副本与指纹",
    "parse": "提取文字、表格、图片及来源定位",
    "vision": "解释已提取图片，保留图片来源与待确认项",
    "chunk": "按结构分块，保留原文字符区间",
    "extract": "分批提炼功能、规则及原文证据",
    "index": "向量化证据并准备可查询索引；同一运行符合条件时复用",
    "plan_points": "按规则批次检索相关证据，按配置重排后规划测试点",
    "generate": "按测试点批次检索，结合用例 Skill 生成步骤和预期",
    "validate": "检查引用、规则覆盖、重复与可疑预期",
    "export_cases": "输出同批测试点目录、用例文档与质量报告",
}

CONTROL_STAGES = {"completed", "points_ready"}

# 只展示程序内固定说明；不把供应商的任意错误正文直接写入终端。
VISION_ERRORS = {
    "HTTP_429": "HTTP 429：服务商限流或额度受限",
    "HTTP_429_BIGMODEL_1302": "智谱 1302：账户请求速率受限",
    "HTTP_429_BIGMODEL_1305": "智谱 1305：模型服务繁忙",
    "HTTP_429_BIGMODEL_1113": "智谱 1113：账户余额不足",
    "HTTP_429_BIGMODEL_1308": "智谱 1308：周期使用额度已用完",
    "HTTP_429_BIGMODEL_1309": "智谱 1309：套餐已到期",
    "HTTP_429_BIGMODEL_1310": "智谱 1310：每周或每月额度已用完",
    "HTTP_429_BIGMODEL_1311": "智谱 1311：套餐未开放该模型权限",
    "HTTP_429_BIGMODEL_1313": "智谱 1313：账户使用受到限制",
    "HTTP_429_BIGMODEL_1314": "智谱 1314：企业套餐已失效",
    "HTTP_429_BIGMODEL_1315": "智谱 1315：密钥产品类型不适用",
}
for _code in range(1316, 1322):
    VISION_ERRORS[f"HTTP_429_BIGMODEL_{_code}"] = f"智谱 {_code}：额度及超额付费受限"


def format_duration(seconds: float) -> str:
    """把秒数转成用户可读的时长，避免长任务只显示大数字。"""
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    return f"{minutes}分{secs}秒" if minutes else f"{secs}秒"


class TerminalProgress:
    def __init__(self, *, interval: float = 10, stage_total: int | None = None) -> None:
        self.interval = interval
        self.stage_total = stage_total
        self.stage = ""
        self.started = perf_counter()
        self.stage_started = self.started
        self.completed = self.total = self.active = 0
        self.report_every = 1
        self.task: asyncio.Task[None] | None = None
        self.run_started = self.started
        self.stage_number = 0
        self.stage_open = False
        self.history: list[tuple[int, str, float, str]] = []
        self.retrieved = self.reranked = self.fallbacks = 0
        self.rerank_disabled = False
        self.pacing_warned = False
        self.cached = self.retries = self.repairs = 0

    def _statistics(self) -> str:
        """阶段内只累积已发生的事实；重排关闭时只说明一次，不逐批计数。"""
        items = []
        if self.retrieved:
            items.append(f"RAG 检索 {self.retrieved} 批")
            if self.reranked or self.fallbacks:
                items.append(f"重排成功 {self.reranked} 批 / 降级 {self.fallbacks} 批")
            if self.rerank_disabled:
                items.append("重排：配置关闭")
        if self.cached:
            items.append(f"模型缓存 {self.cached} 批")
        if self.retries:
            items.append(f"重试 {self.retries} 次")
        if self.repairs:
            items.append(f"结构修复 {self.repairs} 次")
        return "｜".join(items)

    def _finish_stage(self, *, interrupted: bool = False) -> None:
        if not self.stage_open:
            return
        elapsed = perf_counter() - self.stage_started
        label = STAGE_NAMES.get(self.stage, "模型处理")
        state = "中断" if interrupted else "完成"
        statistics = self._statistics()
        total = f"/{self.stage_total}" if self.stage_total else ""
        head = _style(
            f"[{state}] {self.stage_number:02d}{total} {label}｜用时 {format_duration(elapsed)}",
            "32" if state == "完成" else "31",
        )
        typer.echo(head + (f"｜{statistics}" if statistics else ""), err=True)
        self.history.append((self.stage_number, label, elapsed, state))
        self.stage_open = False

    def _eta(self) -> str:
        if not self.total or not self.completed:
            return ""
        elapsed = perf_counter() - self.stage_started
        remaining = elapsed / self.completed * (self.total - self.completed)
        return f"，预计剩余 {format_duration(remaining)}"

    def __call__(self, event: ProgressEvent) -> None:
        label = STAGE_NAMES.get(event.stage, "模型处理")
        message = ""
        if event.kind == "stage":
            # 编号只表示本次实际执行的顺序，跳过图片或恢复运行均不虚构进度。
            if self.stage == event.stage:
                return
            self._finish_stage()
            self.stage, self.started = event.stage, perf_counter()
            self.stage_started = self.started
            self.completed = self.total = self.active = 0
            self.retrieved = self.reranked = self.fallbacks = 0
            self.rerank_disabled = False
            self.pacing_warned = False
            self.cached = self.retries = self.repairs = 0
            self.report_every = 1
            if event.stage in CONTROL_STAGES:
                # completed 后仍有 manifest 等收尾操作，整体成功在上下文正常退出后显示。
                if event.stage == "points_ready":
                    message = "[衔接] 测试点已保存，继续准备检索上下文和用例生成"
            else:
                self.stage_number += 1
                self.stage_open = True
                total = f"/{self.stage_total}" if self.stage_total else ""
                message = (
                    "\n"
                    + _style("─" * 52, "2")
                    + "\n"
                    + _style(f"[阶段] {self.stage_number:02d}{total} {label}", "1;36")
                )
                detail = STAGE_DETAILS.get(event.stage)
                if detail:
                    message += f"\n       {detail}"
        elif event.kind == "batch_started":
            if event.total != self.total:
                self.total = event.total
                # 批次很多时只报告里程碑，避免刷屏。
                self.report_every = max(1, event.total // 10)
            if event.current == 1:
                message = f"       · 共 {event.total} 批，开始处理"
        elif event.kind == "batch_completed":
            self.completed, self.total = event.completed, event.total
            if self.completed == self.total or self.completed % self.report_every == 0:
                percent = self.completed / self.total * 100 if self.total else 0
                if self.total >= 10:
                    message = (
                        f"       · 进度 {self.completed}/{self.total}  "
                        f"{_bar(self.completed, self.total)}{self._eta()}"
                    )
                else:
                    message = (
                        f"       · 进度 {self.completed}/{self.total}"
                        f"（{percent:.0f}%）{self._eta()}"
                    )
        elif event.kind == "model_started":
            self.active += 1
        elif event.kind == "model_finished":
            self.active = max(0, self.active - 1)
        elif event.kind == "retrieval_completed":
            self.retrieved += 1
            if event.rerank_status == "succeeded":
                self.reranked += 1
            elif event.rerank_status == "fallback":
                self.fallbacks += 1
            elif event.rerank_status == "disabled":
                self.rerank_disabled = True
        elif event.kind == "retry":
            self.retries += 1
            message = "⚠ " + _style(
                f"[{label}] 请求失败，将重试（第 {event.attempt + 1}/{event.total} 次尝试）",
                "33",
            )
            if event.stage == "vision" and event.error in VISION_ERRORS:
                message += f"，{VISION_ERRORS[event.error]}"
            if event.duration_ms:
                message += f"，等待 {event.duration_ms / 1000:.1f} 秒"
        elif event.kind == "request_wait":
            seconds = event.duration_ms / 1000
            if event.wait_reason == "pacing":
                # 本地节奏等待：只在等待明显时提示一次，避免逐条刷屏。
                if seconds >= 2 and not self.pacing_warned:
                    self.pacing_warned = True
                    message = (
                        f"[{label}] 请求间隔等待 {seconds:.1f} 秒"
                        "（由 vision_min_interval_seconds 控制）"
                    )
            else:
                message = f"[{label}] 限流冷却，等待 {seconds:.1f} 秒；Ctrl+C 可取消"
        elif event.kind == "vision_blocked":
            reason = VISION_ERRORS.get(event.error or "", "视觉服务账户或额度异常")
            message = (
                f"[{label}] {reason}；请检查服务商控制台。"
                "本次停止发送其余未缓存图片，未解析图片列为待确认"
            )
        elif event.kind == "image_completed":
            self.completed, self.total = event.completed, event.total
            step = max(1, self.total // 10) if self.total else 1
            if self.completed == self.total or self.completed % step == 0:
                outcome = "失败，已列为待确认" if event.error else "成功"
                if event.error in VISION_ERRORS:
                    outcome = VISION_ERRORS[event.error] + "，失败，已列为待确认"
                if self.total >= 10:
                    message = (
                        f"       · 图片 {self.completed}/{self.total}  "
                        f"{_bar(self.completed, self.total)}：{outcome}"
                    )
                else:
                    message = f"       · 图片 {self.completed}/{self.total}：{outcome}"
        elif event.kind == "vision_summary":
            message = (
                f"[图片汇总] 共 {event.total} 张，成功 {event.completed} 张"
                f"（含缓存 {event.cached} 张），失败 {event.failed} 张；图片解释需人工核对"
            )
        elif event.kind == "repair":
            self.repairs += 1
            message = "⚠ " + _style(
                f"[{label}] 返回结构不合要求，正在进行第 {event.attempt} 次结构修复",
                "33",
            )
        elif event.kind == "cache":
            # 命中很多时逐批输出会刷屏，统一进入心跳和阶段汇总。
            self.cached += 1
        if message:
            # stderr 保留进度，stdout 保留运行 ID 和结构化结果供脚本消费。
            typer.echo(message, err=True)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            if self.stage in {"", "completed", "points_ready"}:
                continue
            label = STAGE_NAMES.get(self.stage, "处理中")
            unit = "张图片" if self.stage == "vision" else "批"
            if self.total:
                percent = self.completed / self.total * 100
                if self.total >= 10:
                    count = (
                        f"，{_bar(self.completed, self.total)} "
                        f"{self.completed}/{self.total} {unit}"
                    )
                else:
                    count = f"，已完成 {self.completed}/{self.total} {unit}（{percent:.0f}%）"
            else:
                count = ""
            # embedding 等端口未上报生成请求计数时，不把未知状态显示为“零个请求”。
            activity = f"，{self.active} 个模型请求处理中" if self.active else ""
            statistics = self._statistics()
            total = f"/{self.stage_total}" if self.stage_total else ""
            typer.echo(
                _style(
                    f"[等待] 阶段 {self.stage_number:02d}{total} {label}{count}{activity}，"
                    f"阶段已用 {format_duration(perf_counter() - self.stage_started)}"
                    f"{self._eta()}｜总用时 {format_duration(perf_counter() - self.run_started)}"
                    + (f"｜{statistics}" if statistics else ""),
                    "2",
                ),
                err=True,
            )

    async def __aenter__(self) -> "TerminalProgress":
        self.run_started = perf_counter()
        # 展示是旁路能力；终端写入失败不能影响生成或原始异常。
        with suppress(Exception):
            typer.echo(
                _style(
                    "[流程] 接入 → 图文解析 → 分块 → 规则提炼 → 索引 → RAG 测试点规划"
                    " → RAG + Skill 用例生成 → 校验 → 联合导出\n"
                    "       编号按本次实际执行顺序显示；图片解释按配置和图片数量执行。",
                    "1",
                ),
                err=True,
            )
        self.task = asyncio.create_task(self._heartbeat())
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        interrupted = bool(args and args[0] is not None)
        with suppress(Exception):
            self._summary(interrupted)

    def _summary(self, interrupted: bool) -> None:
        """只在上下文退出时总结；逐阶段完成行已展示细节，这里只给整体与最慢阶段。"""
        self._finish_stage(interrupted=interrupted)
        if not self.history:
            return
        state = "流程中断" if interrupted else (
            "流程完成" if self.stage == "completed" else "本次执行结束"
        )
        slowest = max(self.history, key=lambda item: item[2])
        color = "31" if state == "流程中断" else "1;32"
        typer.echo(
            _style(
                f"\n[阶段汇总] {state}｜总用时 "
                f"{format_duration(perf_counter() - self.run_started)}"
                f"｜最慢阶段 {slowest[0]:02d} {slowest[1]}（{format_duration(slowest[2])}）",
                color,
            ),
            err=True,
        )
