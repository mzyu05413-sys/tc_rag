"""校验输入文件并登记内容版本；应用层通过端口保存文件与事实，不执行SQL。"""

import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from tc_rag.application.ports.contracts import ArtifactStore, Repository
from tc_rag.domain.models import Asset, Document


@dataclass
class IngestResult:
    document: Document
    asset: Asset
    reused: bool


def ingest_document(
    path: Path, store: ArtifactStore, repository: Repository, max_bytes: int = 25 * 1024 * 1024
) -> IngestResult:
    """文件上限默认25MiB；先完整验证再发布，重复内容复用原登记。"""
    if max_bytes <= 0 or not path.is_file():
        raise ValueError("文件路径或大小限制无效")
    suffix = path.suffix.lower()
    if suffix not in {".docx", ".pdf"}:
        raise ValueError("仅支持 DOCX 或 PDF")
    with path.open("rb") as stream:
        content = stream.read(max_bytes + 1)
    if not content or len(content) > max_bytes:
        raise ValueError("文件为空或超出大小限制")
    if suffix == ".docx":
        try:
            with ZipFile(BytesIO(content)) as package:
                names = set(package.namelist())
                if not {"word/document.xml", "[Content_Types].xml"} <= names:
                    raise ValueError("不是有效的 DOCX 包")
                if sum(info.file_size for info in package.infolist()) > 128 * 1024 * 1024:
                    raise ValueError("DOCX 解压后超出大小限制")
                if package.testzip() is not None:
                    raise ValueError("DOCX 包校验失败")
        except BadZipFile as error:
            raise ValueError("DOCX 包损坏") from error
    elif not content.startswith(b"%PDF-"):
        raise ValueError("PDF 文件头无效")
    checksum = hashlib.sha256(content).hexdigest()
    existing = repository.find_document(checksum)
    if existing is not None:
        asset = repository.get("assets", existing.asset_id, Asset)
        if asset is None:
            raise ValueError("文档登记缺少资产记录")
        store.read(asset)  # 复用前检查受管理文件未损坏。
        return IngestResult(existing, asset, True)
    asset = store.put(f"assets/{checksum}/original{suffix}", content)
    document = Document(
        document_id="doc_" + checksum,
        name=path.name,
        content_hash=checksum,
        asset_id=asset.asset_id,
    )
    # 文件与数据库无共同事务：失败可能留下未登记文件，下次同内容可复用。
    repository.save_many(
        [("assets", asset.asset_id, asset), ("documents", document.document_id, document)]
    )
    return IngestResult(document, asset, False)
