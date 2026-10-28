"""解析文字PDF，提取页码、坐标、图片和表格候选；不执行OCR或模型调用。"""

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from threading import get_ident
from typing import Any, Literal

import pdfplumber
import pymupdf

from tc_rag.application.ports.contracts import (
    ArtifactStore,
    PageOutcome,
    ParsedDocument,
    ParseState,
)
from tc_rag.domain.models import Asset, DocumentBlock, Issue, Locator

BlockKind = Literal["heading", "paragraph", "table", "image", "other"]
Box = tuple[float, float, float, float]


class _FontWarningSummary(logging.Filter):
    """同一次解析只显示一次缺失字体框警告，其他警告和其他线程不受影响。"""

    def __init__(self) -> None:
        super().__init__()
        self.thread_id = get_ident()
        self.count = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.thread == self.thread_id and str(record.msg).startswith(
            "Could not get FontBBox from font descriptor because "
        ):
            self.count += 1
            return self.count == 1
        return True


def _box(values: Any) -> Box:
    """显式转换四元组，让坐标类型与Locator契约保持一致。"""
    left, top, right, bottom = values
    return float(left), float(top), float(right), float(bottom)


class _PdfSession:
    """一次打开文档，按页解析并在线程内累计标题层级。"""

    def __init__(
        self,
        parser: "PdfParser",
        asset: Asset,
        document: Any,
        tables_document: Any,
        body_size: float,
        heading_sizes: list[float],
        summary: _FontWarningSummary,
    ) -> None:
        self.parser = parser
        self.asset = asset
        self.document = document
        self.tables_document = tables_document
        self.body_size = body_size
        self.heading_sizes = heading_sizes
        self.summary = summary
        self.document_id = "doc_" + asset.checksum
        self._last_font_count = 0

    @property
    def page_count(self) -> int:
        return int(self.document.page_count)

    def parse_page(self, page_index: int, state: ParseState) -> PageOutcome:
        page = self.document[page_index]
        page_no = page_index + 1
        blocks: list[DocumentBlock] = []
        assets: list[Asset] = []
        issues: list[Issue] = []

        def issue(message: str) -> None:
            issues.append(
                Issue(
                    issue_id=f"{self.document_id}_pdf_issue_{state.issue_count}",
                    kind="unparsed",
                    severity="warning",
                    object_ids=[self.document_id],
                    description=f"PDF第{page_no}页：{message}",
                )
            )
            state.issue_count += 1

        text = page.get_text()
        visible = [char for char in text if not char.isspace()]
        good = sum(char.isprintable() and char != "�" for char in visible)
        # 扫描页即使带少量页码文字也不能当成正文已解析；本页不生成业务块。
        if len(visible) < self.parser.min_text_chars or good / max(1, len(visible)) < 0.85:
            issue("文本缺失或质量低，整页暂不进入后续处理；需要人工核对或OCR")
        else:
            items: list[tuple[Box, BlockKind, str, float]] = []
            table_boxes: list[Box] = []
            if self.tables_document is None or page.rotation:
                issue("表格候选未提取（解析库不可用或页面旋转），保留原文字块")
            else:
                try:
                    candidates = self.tables_document.pages[page_index].find_tables()
                    for table in candidates:
                        rows = table.extract()
                        if not rows:
                            continue
                        box = _box(table.bbox)
                        table_boxes.append(box)
                        items.append(
                            (
                                box,
                                "table",
                                json.dumps(
                                    {
                                        "schema_version": "1.0",
                                        "candidate": True,
                                        "rows": rows,
                                    },
                                    ensure_ascii=False,
                                ),
                                self.body_size,
                            )
                        )
                except Exception:
                    # 不输出异常原文，避免库异常携带整页业务文本。
                    issue("表格候选提取不完整，请人工检查；原文字块继续保留")
            for block in page.get_text("dict")["blocks"]:
                box = _box(block["bbox"])
                if block["type"] == 1:
                    image_content = block["image"]
                    checksum = hashlib.sha256(image_content).hexdigest()
                    image = self.parser.store.put(
                        f"assets/{self.asset.checksum}/images/{checksum}.{block['ext']}",
                        image_content,
                    )
                    assets.append(image)
                    items.append((box, "image", image.model_dump_json(), self.body_size))
                    issue(f"图片 {image.asset_id} 已提取定位，但图片文字未解析")
                    continue
                for line in block.get("lines", []):
                    spans = line["spans"]
                    line_text = "".join(span["text"] for span in spans)
                    if not line_text.strip():
                        continue
                    line_box = _box(line["bbox"])
                    # 只在文字行完全落入表格框时去重，防止删除跨表格边界的文字。
                    if any(self.parser._contains(table_box, line_box) for table_box in table_boxes):
                        continue
                    size = max(span["size"] for span in spans)
                    kind: BlockKind = "heading" if size > self.body_size * 1.2 else "paragraph"
                    items.append((line_box, kind, line_text, round(size, 1)))
            # 先上后下、同高度从左到右，仅为简单单栏阅读顺序的启发式。
            for box, kind, value, size in sorted(items, key=lambda item: (item[0][1], item[0][0])):
                if kind == "heading":
                    if size not in state.heading_sizes:
                        state.heading_sizes.append(size)
                        state.heading_sizes.sort(reverse=True)
                    level = state.heading_sizes.index(size) + 1
                    state.headings = {
                        key: title for key, title in state.headings.items() if key < level
                    }
                    state.headings[level] = value
                order = state.order
                blocks.append(
                    DocumentBlock(
                        block_id=f"{self.document_id}_pdf_block_{order}",
                        document_id=self.document_id,
                        kind=kind,
                        order=order,
                        content=value,
                        heading_path=[state.headings[key] for key in sorted(state.headings)],
                        locator=Locator(page=page_no, bbox=box),
                    )
                )
                state.order += 1

        delta = self.summary.count - self._last_font_count
        self._last_font_count = self.summary.count
        state.font_warnings += delta
        return PageOutcome(page_no, blocks, assets, issues, state)


class PdfParser:
    def __init__(
        self,
        store: ArtifactStore,
        scan_policy: Literal["report_unsupported", "ocr"] = "report_unsupported",
        min_text_chars: int = 20,
    ) -> None:
        if scan_policy != "report_unsupported":
            raise ValueError("当前阶段尚未实现OCR，请使用report_unsupported")
        if min_text_chars < 1:
            raise ValueError("文本质量阈值必须为正整数")
        self.store = store
        self.min_text_chars = min_text_chars

    @contextmanager
    def session(self, asset: Asset, *, install_filter: bool = True) -> Iterator[_PdfSession]:
        content = self.store.read(asset)
        logger = logging.getLogger("pdfminer.pdffont")
        summary = _FontWarningSummary()
        if install_filter:
            logger.addFilter(summary)
        tables_document: Any = None
        try:
            # 当前PyMuPDF构造函数缺少类型声明，仅对此调用作精确豁免。
            with pymupdf.open(stream=content, filetype="pdf") as document:  # type: ignore[no-untyped-call]
                if document.needs_pass:
                    raise ValueError("不支持读取加密PDF")
                font_weights = self._font_weights(document)
                body_size = font_weights.most_common(1)[0][0] if font_weights else 12.0
                # 全文预先确定字号等级，避免后面遇到更大标题时让已有层级漂移。
                heading_sizes = sorted(
                    (size for size in font_weights if size > body_size * 1.2), reverse=True
                )
                try:
                    tables_document = pdfplumber.open(BytesIO(content))
                except Exception:
                    # 表格适配失败不覆盖正文结果，但每页必须报告缺失范围。
                    tables_document = None
                yield _PdfSession(
                    self, asset, document, tables_document, body_size, heading_sizes, summary
                )
        finally:
            if tables_document is not None:
                tables_document.close()
            if install_filter:
                # 不修改库的全局日志级别；本次调用结束（含失败）必须撤销过滤器。
                logger.removeFilter(summary)

    @staticmethod
    def _font_weights(document: Any) -> Counter[float]:
        """以文字数量加权的常见字号估算正文，避免大号标题占多数文本块时误判。"""
        weights: Counter[float] = Counter()
        for page in document:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        weights[round(span["size"], 1)] += len(span["text"].strip())
        return weights

    def parse(self, asset: Asset) -> ParsedDocument:
        logger = logging.getLogger("pdfminer.pdffont")
        summary = _FontWarningSummary()
        logger.addFilter(summary)
        try:
            result = self._parse(asset)
        finally:
            logger.removeFilter(summary)
        if summary.count:
            result.issues.append(
                Issue(
                    issue_id=f"doc_{asset.checksum}_font_bbox",
                    kind="unparsed",
                    severity="warning",
                    object_ids=["doc_" + asset.checksum],
                    description=(
                        f"PDF字体边界信息缺失警告共{summary.count}次，已合并重复终端提示；"
                        "正文解析继续，表格及文字位置需人工核对"
                    ),
                )
            )
        return result

    def _parse(self, asset: Asset) -> ParsedDocument:
        """整文档解析；外层已安装字体告警过滤器，这里只负责按页累计正文。"""
        result = ParsedDocument(blocks=[])
        with self.session(asset, install_filter=False) as session:
            state = ParseState(heading_sizes=list(session.heading_sizes))
            for page_index in range(session.page_count):
                outcome = session.parse_page(page_index, state)
                result.blocks.extend(outcome.blocks)
                result.assets.extend(outcome.assets)
                result.issues.extend(outcome.issues)
                state = outcome.state
        return result

    @staticmethod
    def _contains(outer: tuple[float, ...], inner: tuple[float, ...]) -> bool:
        """坐标按左上右下比较，容忍少量浮点坐标误差。"""
        return (
            outer[0] - 1 <= inner[0]
            and outer[1] - 1 <= inner[1]
            and outer[2] + 1 >= inner[2]
            and outer[3] + 1 >= inner[3]
        )
