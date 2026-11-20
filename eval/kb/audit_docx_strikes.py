"""离线盘点DOCX删除线；不修改原件和已发布快照。"""

import argparse
import hashlib
import json
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn


def audit(path: Path) -> dict:
    document = Document(path)
    hits = []
    for paragraph_no, element in enumerate(document.element.body.iter(qn("w:p"))):
        # 精确文本只取w:t，避免itertext在python-docx对象重复展开。
        context = "".join(n.text or "" for n in element.iter(qn("w:t")))
        for run_no, run in enumerate(element.iter(qn("w:r"))):
            props = run.find(qn("w:rPr"))
            if props is None:
                continue
            flags = [props.find(qn(name)) for name in ("w:strike", "w:dstrike")]
            if not any(f is not None and f.get(qn("w:val"), "true").lower()
                       not in {"false", "0", "off"} for f in flags):
                continue
            text = "".join(n.text or "" for n in run.iter(qn("w:t")))
            if text:
                hits.append({"xml_paragraph_order": paragraph_no, "run_order": run_no,
                             "struck_text": text, "paragraph_text": context,
                             "detection": "direct_run_format"})
    return {"filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "hits": hits, "limitations": ["仅直接run删除线，不含样式继承或修订删除w:del",
                                           "XML顺序不是正式Block ID或页码"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("输出已存在")
    rows = [audit(p) for p in sorted(args.source.glob("*.docx"))]
    if not rows:
        raise ValueError("没有DOCX")
    args.output.mkdir(parents=True)
    (args.output / "strikes.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# DOCX直接删除线盘点", "", "仅盘点，不是解析器修复结果。"]
    for row in rows:
        lines += ["", "## " + row["filename"], f"直接删除线run数：{len(row['hits'])}"]
        for hit in row["hits"]:
            lines += [f"- XML段落{hit['xml_paragraph_order']}：{hit['struck_text']}"]
    (args.output / "删除线盘点.md").write_text("\n".join(lines), encoding="utf-8")
