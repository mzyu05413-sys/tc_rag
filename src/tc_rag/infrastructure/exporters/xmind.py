"""导出模块和测试点目录；用例步骤与预期只在 Excel 中展示。"""

import json
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from tc_rag.domain.review import PointTree


class XMindAdapter:
    def export(self, tree: PointTree, destination: Path) -> Path:
        nodes = {node.node_id: node for node in tree.nodes}

        def topic(node_id: str) -> dict[str, Any]:
            node = nodes[node_id]
            children = sorted(
                [n for n in tree.nodes if n.parent_id == node_id], key=lambda n: n.order
            )
            item: dict[str, Any] = {"id": node_id, "title": node.title}
            if node.kind == "point":
                item["tcRag"] = {"point_id": node.point_id}
                if node.technique:
                    # 方法不进标题，作为标签保留，避免标题冗长。
                    item["labels"] = [node.technique]
            if children:
                item["children"] = {"attached": [topic(n.node_id) for n in children]}
            return item

        root = next(n for n in tree.nodes if n.parent_id is None)
        sheet = {
            "id": tree.tree_id,
            "title": "测试点",
            "class": "sheet",
            "rootTopic": topic(root.node_id),
            "tcRag": {
                "run_id": tree.run_id,
                "tree_id": tree.tree_id,
                "version": tree.version,
                "document_fingerprint": tree.document_fingerprint,
            },
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
            archive.writestr("content.json", json.dumps([sheet], ensure_ascii=False))
            archive.writestr("metadata.json", json.dumps({"creator": {"name": "tc-rag"}}))
            archive.writestr(
                "manifest.json",
                json.dumps({"file-entries": {"content.json": {}, "metadata.json": {}}}),
            )
        return destination
