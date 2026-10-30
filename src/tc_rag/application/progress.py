"""只传递执行状态的进度事件，不携带需求正文、模型思考或凭据。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProgressEvent:
    kind: Literal[
        "stage",
        "batch_started",
        "batch_completed",
        "model_started",
        "model_finished",
        "retry",
        "repair",
        "cache",
        "image_completed",
        "vision_summary",
        "request_wait",
        "vision_blocked",
        "retrieval_completed",
    ]
    stage: str
    current: int = 0
    total: int = 0
    completed: int = 0
    duration_ms: float = 0
    attempt: int = 0
    error: str | None = None
    # 仅传递按输出契约过滤后的字段路径和错误类型，不携带模型输出值。
    validation_errors: tuple[str, ...] = ()
    failed: int = 0
    cached: int = 0
    # pacing=本地请求间隔；cooldown=429/重试后的冷却。
    wait_reason: Literal["pacing", "cooldown"] | None = None
    rerank_status: Literal["disabled", "succeeded", "fallback"] | None = None


ProgressReporter = Callable[[ProgressEvent], None]


def report(reporter: ProgressReporter | None, event: ProgressEvent) -> None:
    """显示进度是旁路能力；终端关闭或回调异常不能改变业务执行结果。"""
    if reporter is not None:
        try:
            reporter(event)
        except Exception:
            pass
