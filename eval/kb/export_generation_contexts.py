"""只读导出指定RAG运行的生成上下文；不调用模型、不修改运行库。"""

import argparse
import json
import sqlite3
from pathlib import Path

from tc_rag.domain.pipeline import StageSnapshot


def export(database: Path, run_id: str, output: Path):
    if output.exists():
        raise ValueError("输出已存在")
    if not database.is_file():
        raise ValueError("运行数据库不存在")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("BEGIN")
        index = connection.execute("SELECT payload FROM stage_snapshots WHERE id=?",
                                   (f"{run_id}:generation_contexts",)).fetchone()
        if index is None:
            raise ValueError("该运行没有新版上下文记录；历史运行不会自动补写")
        manifest = StageSnapshot.model_validate_json(index[0])
        if manifest.run_id != run_id:
            raise ValueError("运行记录不匹配")
        records = []
        for key in manifest.payload["record_ids"]:
            row = connection.execute(
                "SELECT payload FROM stage_snapshots WHERE id=?", (key,),
            ).fetchone()
            if row is None:
                raise ValueError("上下文记录缺失")
            record = StageSnapshot.model_validate_json(row[0])
            if record.run_id != run_id or record.stage != "generation_context":
                raise ValueError("上下文归属或类型错误")
            records.append(record.model_dump(mode="json"))
    lines = ["# 两轮RAG生成上下文", "", f"运行：{run_id}", "",
             "模型完成只代表调用完成，不代表语义正确；缓存复用没有新增模型请求。",
             "本文件包含需求正文，只保存在受控评测目录。", ""]
    for number, record in enumerate(records, 1):
        payload = record["payload"]
        lines.extend([f"## {number}．{payload['stage']}｜{payload['state']}", "",
            f"模型调用尝试：{payload['provider_attempted']}",
            f"文档指纹：{payload['document_fingerprint']}", ""])
        for field, label in [("core_evidence", "已有原文"), ("retrieved_evidence", "检索补充")]:
            lines.extend([f"### {label}（{len(payload[field])}条）", ""])
            for evidence in payload[field]:
                lines.extend([f"来源：{evidence.get('block_id')}｜{evidence.get('origin', 'text')}",
                              "", evidence.get("quote", ""), ""])
    output.mkdir(parents=True)
    (output / "contexts.json").write_text(json.dumps({"run_id": run_id, "records": records},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "先看这里_两轮上下文.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("database", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    export(args.database, args.run_id, args.output)
