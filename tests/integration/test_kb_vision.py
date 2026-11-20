"""真实 DOCX 图片解析与既有 Vision 服务离线组合，派生来源可回查。"""

from io import BytesIO

import pytest
from docx import Document
from PIL import Image
from test_kb_ingestion import setup

from tc_rag.application.kb.search import SearchService
from tc_rag.application.ports.contracts import VisionResult
from tc_rag.application.ports.errors import VisionCacheMissError
from tc_rag.application.services.vision import VisionEvidenceService
from tc_rag.infrastructure.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_vision_origin_preserved_through_ingestion_and_search(tmp_path):
    components, _, _, paths = setup(tmp_path)
    document = Document()
    document.add_paragraph("审批流程如下图。")
    image = BytesIO()
    Image.new("RGB", (100, 100), "white").save(image, format="PNG")
    image.seek(0)
    document.add_picture(image)
    document.save(paths["approval"])

    class Reader:
        async def read(self, image, context):
            return VisionResult("主管审批通过后完成。", "fixture", "v1")

    components.vision = VisionEvidenceService(
        SqliteRepository(tmp_path / "vision.sqlite"), components.store, Reader(),
        {"model": "fixture"},
    )
    snapshot = await components.ingest("crm", {"approval": paths["approval"]})
    assert any(block.origin == "vision" for block in snapshot.blocks)
    result = await SearchService(components).search("crm", "审批", top_k=20)
    sources = [source for candidate in result["candidates"] for source in candidate["sources"]]
    assert any(source["origin"] == "vision" for source in sources)
    assert all(source["block_id"] in snapshot.original_block_ids for source in sources)


@pytest.mark.asyncio
async def test_cache_only_miss_never_calls_reader(tmp_path):
    components, _, _, paths = setup(tmp_path)
    document = Document()
    image = BytesIO()
    Image.new("RGB", (100, 100), "white").save(image, format="PNG")
    image.seek(0)
    document.add_picture(image)
    document.save(paths["approval"])

    class Reader:
        async def read(self, image, context):
            raise AssertionError("缓存限定模式不能调用视觉模型")

    processor = VisionEvidenceService(
        SqliteRepository(tmp_path / "vision.sqlite"), components.store, Reader(),
        {"model": "fixture"},
    )
    processor.require_cache = True
    components.vision = processor
    with pytest.raises(VisionCacheMissError):
        await components.ingest("crm", {"approval": paths["approval"]})
