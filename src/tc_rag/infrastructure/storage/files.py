"""受管理的文件存储：约束相对路径、原子写入并校验内容指纹。"""

import hashlib
import mimetypes
import os
import tempfile
from pathlib import Path

from tc_rag.domain.models import Asset


class LocalArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, relative_path: str) -> Path:
        path = Path(relative_path)
        if path.is_absolute() or path.drive or ".." in path.parts:
            raise ValueError("资产路径必须位于管理目录内")
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root) or resolved == self.root:
            raise ValueError("资产路径越界")
        return resolved

    def put(self, relative_path: str, content: bytes) -> Asset:
        path = self._path(relative_path)
        checksum = hashlib.sha256(content).hexdigest()
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ValueError("目标资产路径已有不同内容，拒绝覆盖")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 同目录临时文件保证替换原子性；异常时清理尚未发布的临时文件。
            descriptor, temp = tempfile.mkstemp(dir=path.parent, prefix=".asset-")
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        return Asset(
            asset_id="asset_" + checksum,
            relative_path=Path(relative_path).as_posix(),
            checksum=checksum,
            mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )

    def read(self, asset: Asset) -> bytes:
        content = self._path(asset.relative_path).read_bytes()
        if hashlib.sha256(content).hexdigest() != asset.checksum:
            raise ValueError("资产内容校验失败")
        return content
