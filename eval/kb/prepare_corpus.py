"""使用正式 DOCX 解析器准备离线标注材料，不装配模型或读取密钥。"""

import argparse
import hashlib
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from tc_rag.infrastructure.parsing.docx import DocxParser
from tc_rag.infrastructure.storage.files import LocalArtifactStore


def prepare(source: Path, output: Path) -> dict:
    """拒绝覆盖，完整生成后发布目录，失败清理本次暂存材料。"""
    if output.exists():
        raise FileExistsError("输出目录已存在，请使用新版本目录")
    files = sorted(source.glob("*.docx"))
    if not files:
        raise ValueError("输入目录没有 DOCX")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".corpus-", dir=output.parent))
    try:
        store = LocalArtifactStore(staging / "assets")
        parser = DocxParser(store)
        documents = []
        for file in files:
            content = file.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            asset = store.put(f"originals/{digest}/{file.name}", content)
            parsed = parser.parse(asset)
            folder = staging / digest
            folder.mkdir(exist_ok=True)
            blocks = [block.model_dump(mode="json") for block in parsed.blocks]
            (folder / "blocks.json").write_text(
                json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (folder / "reading.md").write_text(
                "\n\n".join(
                    f"## {b.block_id} · {b.kind}\n\n{b.content}"
                    for b in parsed.blocks
                ), encoding="utf-8",
            )
            documents.append({
                "name": file.name, "sha256": digest,
                "asset": asset.model_dump(mode="json"),
                "block_counts": dict(Counter(b.kind for b in parsed.blocks)),
                "issues": [issue.model_dump(mode="json") for issue in parsed.issues],
                "version_review": "pending", "image_review": "pending",
            })
        manifest = {
            "schema_version": "1.0", "status": "prepared_unreviewed",
            "parser": "DocxParser", "parser_options": {
                "infer_headings": False, "extended": False, "table_headers": False,
            }, "vision_called": False, "documents": documents,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        staging.rename(output)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(args.source, args.output)
    print(f"已准备 {len(manifest['documents'])} 份文档，状态为待人工审核")


if __name__ == "__main__":
    main()
