"""构建待执行阶段的预览，不实际执行这些阶段。"""

from pathlib import Path

from tc_rag.core.config import AppConfig


def run_plan(document: Path, config: AppConfig) -> list[str]:
    # 这里只校验路径和扩展名；不读取正文，也不能据此证明文件内部格式有效。
    if not document.is_file():
        raise ValueError("Document must be an existing file")
    if document.suffix.lower().lstrip(".") not in config.parsing.formats:
        raise ValueError("Document format is not enabled (expected PDF/DOCX)")
    # 规划测试点和生成用例都必须检索；两步连续执行，最终联合导出。
    return [
        "ingest",
        "parse",
        "chunk",
        "extract",
        "reconcile",
        "embed",
        "index",
        "retrieve",
        "plan_points",
        "points_ready",
        "retrieve",
        "context",
        "generate",
        "validate",
        "export_cases",
    ]
