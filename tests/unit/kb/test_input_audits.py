import json
import runpy

from docx import Document


def test_strike_audit_table_and_explicit_off(tmp_path):
    audit = runpy.run_path("eval/kb/audit_docx_strikes.py")["audit"]
    doc = Document()
    doc.add_paragraph().add_run("old").font.strike = True
    doc.add_paragraph().add_run("active").font.strike = False
    cell = doc.add_table(rows=1, cols=1).cell(0, 0)
    cell.paragraphs[0].add_run("table-old").font.double_strike = True
    path = tmp_path / "demo.docx"
    doc.save(path)
    assert [r["struck_text"] for r in audit(path)["hits"]] == ["old", "table-old"]


def test_projection_preserves_ids_and_only_uses_text_sources(tmp_path):
    prepare = runpy.run_path("eval/kb/prepare_text_pool.py")["prepare"]
    pool = {"snapshot_id": "s", "questions": [{"query_id": "q", "split": "dev",
        "candidates": [{"chunk_id": "c", "text": "raw+vision", "sources": [
            {"block_id": "b", "start_char": 0, "end_char": 3},
            {"block_id": "v", "start_char": 0, "end_char": 3}]}]}]}
    snapshot = {"snapshot": {"snapshot_id": "s"}, "blocks": [
        {"block_id": "b", "origin": "text", "kind": "paragraph", "content": "raw"},
        {"block_id": "v", "origin": "vision", "kind": "paragraph", "content": "img"}]}
    p, s = tmp_path / "pool.json", tmp_path / "snapshot.json"
    p.write_text(json.dumps(pool), encoding="utf-8")
    s.write_text(json.dumps(snapshot), encoding="utf-8")
    prepare(p, s, tmp_path / "out")
    row = json.loads((tmp_path / "out/pool.json").read_text("utf-8"))
    candidate = row["questions"][0]["candidates"][0]
    assert candidate["chunk_id"] == "c" and candidate["text"] == "raw"
    assert json.loads(p.read_text("utf-8")) == pool
