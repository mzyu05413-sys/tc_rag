"""将人工可见事实预判绑定图片和Vision字符区间；不发布标签或计算分数。"""

import argparse
import hashlib
import html
import json
import re
from pathlib import Path


def build(directory: Path):
    inventory_path = directory / "image_inventory.json"
    decisions_path = directory / "audit_decisions.json"
    inventory = json.loads(inventory_path.read_text("utf-8"))
    decisions = json.loads(decisions_path.read_text("utf-8"))
    output = directory / "evidence.prelabel.json"
    if output.exists():
        raise ValueError("输出已存在")
    observed, rows = set(), []
    for image in inventory["images"]:
        image_path = Path(image["copied_path"])
        if hashlib.sha256(image_path.read_bytes()).hexdigest() != image["image_sha256"]:
            raise ValueError("图片哈希不匹配")
        key = image["image_key"]
        decision = decisions.get(key)
        row = {**image, "review_status": "pending", "annotator": "assistant_visual_audit",
               "visual_inspection": "full_image" if decision else "contact_sheet_scope_screen",
               "snapshot_id": inventory["snapshot_id"], "fact_spans": []}
        if decision:
            observed.add(key)
            row.update(decision)
            text = image["vision_text"]
            start = text.index("【图片可见事实】")
            end = text.find("【", start + len("【图片可见事实】"))
            end = len(text) if end == -1 else end
            for fid in decision["fact_ids"]:
                matches = list(re.finditer(r"(?m)^- " + re.escape(fid) + r"(?=\s).*$", text))
                matches = [m for m in matches if start <= m.start() < m.end() <= end]
                if len(matches) != 1:
                    raise ValueError(f"可见事实ID未唯一匹配：{key}/{fid}")
                match = matches[0]
                mappings = []
                for chunk in image["vision_chunks"]:
                    for source in chunk["sources"]:
                        left = max(match.start(), source["start_char"])
                        right = min(match.end(), source["end_char"])
                        if source["block_id"] == image["vision_block_id"] and left < right:
                            mappings.append(dict(chunk_id=chunk["chunk_id"],
                                                 start_char=left, end_char=right,
                                                 complete_fact=left == match.start()
                                                 and right == match.end()))
                if not mappings:
                    raise ValueError("事实未映射冻结候选")
                row["fact_spans"].append(dict(fact_id=fid, quote=match.group(),
                                              block_id=image["vision_block_id"],
                                              start_char=match.start(), end_char=match.end(),
                                              chunks=mappings))
        else:
            row.update(title="其他功能或背景截图", proposed_relevance=0, rules=[],
                       visible="缩略图及解释主题与当前任务必要规则无直接支持关系。",
                       limit="排除针对本任务，不表示图片识别内容一定错误。")
        rows.append(row)
    if observed != set(decisions):
        raise ValueError("决策包含不存在的图片")
    payload = dict(status="assistant_prelabels_pending_adoption", images=rows,
                   snapshot_id=inventory["snapshot_id"], formal_metrics=None,
                   hashes={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (inventory_path, decisions_path)},
                   policy="only_verified_visible_fact_spans_not_context_restating")
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    esc = html.escape
    positive = sum(row["proposed_relevance"] == 1 for row in rows)
    unknown = sum(row["proposed_relevance"] is None for row in rows)
    negative = sum(row["proposed_relevance"] == 0 for row in rows)
    parts = ["<!doctype html><html lang='zh-CN'><meta charset='utf-8'>",
             "<title>图片证据审核</title><style>body{max-width:1100px;margin:32px auto;"
             "padding:0 20px;font:17px/1.7 sans-serif;color:#172b4d}img{max-width:100%;"
             "max-height:650px;object-fit:contain}article{border:1px solid #ddd;"
             "border-radius:8px;padding:22px;margin:22px 0}pre{white-space:pre-wrap;"
             "overflow-wrap:anywhere;font:14px/1.6 monospace}h2{font-size:23px}"
             ".note{background:#fff5cf;padding:16px}</style>",
             f"<h1>图片证据审核</h1><p>{len(rows)}张图片，{positive}张建议计相关子场景，"
             f"{unknown}张待裁定，{negative}张按其他主题预排除。"
             "全部尚待采纳，正文金标和原分数保留。</p>",
             "<p class='note'>只认可原图可见且被当前候选文本覆盖的事实。正文复述、"
             "样例数值推广、静态图无法证明的执行行为均不算完整规则证据。"
             "图片相关不代表该图片解释拆出的所有片段都相关。</p>"]
    ordered = sorted(rows, key=lambda r: (0 if r["proposed_relevance"] is None
                                        else 1 if r["proposed_relevance"] == 1 else 2,
                                        r["image_key"]))
    for row in ordered:
        excluded = row["proposed_relevance"] == 0
        parts.append("<details><summary>其他主题：" + esc(row["image_key"])
                     + "</summary>" if excluded else "")
        parts.extend(["<article>", f"<h2>{esc(row['image_key'])}｜{esc(row['title'])}</h2>",
                      "<p>任务：" + esc(row["task_id"]) + "</p>",
                      "<img loading='lazy' src='images/" + esc(Path(row["copied_path"]).name)
                      + "' alt='原始需求图片'>",
                      "<p><b>图中能证明：</b>" + esc(row["visible"]) + "</p>",
                      "<p><b>不能推导：</b>" + esc(row["limit"]) + "</p>"])
        if row.get("question"):
            parts.append("<p class='note'>需要裁定：" + esc(row["question"]) + "</p>")
        for span in row["fact_spans"]:
            parts.append("<pre>" + esc(span["quote"]) + "</pre>")
        parts.append("<details><summary>全部原始图片解释（含正文复述）</summary><pre>"
                     + esc(row["vision_text"] or "无解释") + "</pre></details></article>")
        if excluded:
            parts.append("</details>")
    parts.append("</html>")
    (directory / "打开审核.html").write_text("\n".join(parts), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    build(parser.parse_args().directory)
