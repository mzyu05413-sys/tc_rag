"""知识库装配复用正式 PDF 解析，不装配生成模型。"""

import pymupdf
import pytest

from tc_rag.application.kb.ingest import IngestionService
from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.infrastructure.parsing.pdf import PdfParser
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.infrastructure.retrieval.index import NumpyIndex
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.kb.sqlite import KnowledgeSqliteRepository


@pytest.mark.asyncio
async def test_pdf_ingestion_preserves_page_locator(tmp_path):
    path = tmp_path / "demo.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 50), "Approval requires all members to agree.")
        pdf.save(path)
    store = LocalArtifactStore(tmp_path / "runtime")
    service = IngestionService(
        KnowledgeSqliteRepository(tmp_path / "db"), store, lambda suffix: PdfParser(store),
        FakeEmbedder(), EmbeddingSpace("fixture", "m", 3), lambda: NumpyIndex(),
    )
    result = await service.ingest("crm", {"demo.pdf": path})
    assert result.blocks[0].locator.page == 1
    assert "Approval" in result.chunks[0].text
