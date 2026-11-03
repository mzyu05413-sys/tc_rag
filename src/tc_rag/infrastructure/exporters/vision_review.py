"""仅视觉阶段的原图、旧解释、新解释与人工标记页面；无需外网资源。"""

import base64
import json
import mimetypes
from html import escape
from pathlib import Path
from typing import Any


def export_vision_review(
    directory: Path, rows: list[dict[str, Any]], summary: dict[str, Any]
) -> Path:
    sections = []
    for row in rows:
        original = directory / row["original"]
        mime = mimetypes.guess_type(original.name)[0] or "application/octet-stream"
        image = f"data:{mime};base64," + base64.b64encode(original.read_bytes()).decode()
        result = row.get("result") or {}
        text = result.get("content", "尚未调用模型" if not summary["live"] else "本图解释失败")
        if row.get("status") == "skipped":
            text = "图片过小，已跳过；不产生派生文字，不计识别失败。"
        previews = []
        for view in row["views"]:
            previews.append(
                f'<a href="{escape(view["file"], quote=True)}">{escape(view["view_id"])}</a>'
            )
        sections.append(
            f'<section data-image="{escape(row["block_id"], quote=True)}">'
            f'<h2>图片 {row["number"]} · {escape(row["heading"])}</h2>'
            f'<p>视图：{" · ".join(previews)}；'
            f'{escape(row.get("plan_error") or row.get("skip_reason") or "局部覆盖检查通过")}</p>'
            f'<p>同图出现 {row.get("checksum_occurrences", 1)} 次；'
            f'本轮视图 {len(row["views"])} 张；总像素 {row.get("total_pixels", "未记录")}。'
            f'{escape(" ".join(row.get("warnings", [])))}</p>'
            f'<div class="grid"><div><h3>原图</h3><img src="{image}" alt="原图"></div>'
            f'<div><h3>历史解释（对照）</h3><pre>{escape(row["baseline"] or "无历史解释")}</pre>'
            f'<h3>本轮解释</h3><pre>{escape(text)}</pre></div></div>'
            '<details><summary>本轮结构化结果、正文上下文和视图定位</summary>'
            f'<pre>{escape(json.dumps(row, ensure_ascii=False, indent=2))}</pre></details>'
            '<label>本图人工结论 <select class="verdict"><option value="unreviewed">未审核</option>'
            '<option value="pass">通过</option>'
            '<option value="fail">需调整</option></select></label>'
            '<p>分别记录：错字/数字/否定词；漏掉的条件或表格行；无依据推断；图文来源混淆。</p>'
            '<textarea placeholder="填写具体原文及错误/遗漏；未填不代表正确"></textarea></section>'
        )
    # 浏览器只保存本地标注、导出 JSON，不向模型服务或其他站点发送任何数据。
    script = """
const key=document.body.dataset.review;
const sections=[...document.querySelectorAll('section')];
let saved={};try{saved=JSON.parse(localStorage.getItem(key)||'{}')}catch(e){}
for(const s of sections){const v=s.querySelector('select'),n=s.querySelector('textarea');
 const old=saved[s.dataset.image];if(old){v.value=old.verdict;n.value=old.notes}
 s.addEventListener('change',()=>{saved[s.dataset.image]={verdict:v.value,notes:n.value};
 try{localStorage.setItem(key,JSON.stringify(saved))}catch(e){}
 });}
document.querySelector('button').onclick=()=>{const items=sections.map(s=>({
 image_id:s.dataset.image,verdict:s.querySelector('select').value,
 notes:s.querySelector('textarea').value}));
 const data={review_id:key,human_review:true,items,reviewed:items.filter(
 x=>x.verdict!=='unreviewed').length};
 const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
 const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;
 a.download='vision_review.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
"""
    html = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>仅图片转文字验收</title><style>'
        'body{font:16px/1.7 "Microsoft YaHei",sans-serif;background:#eff4fa;color:#1b293b;'
        'max-width:1500px;margin:auto;padding:24px}section,header{background:white;'
        'border:1px solid #d5e0ed;padding:22px;border-radius:10px;margin-bottom:24px}'
        '.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:22px}'
        'img{width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;'
        'background:#f4f7fb;padding:14px;font-size:14px}textarea{width:96%;min-height:90px}'
        'button,select{padding:8px}a{color:#185ca5}@media(max-width:800px){'
        '.grid{grid-template-columns:1fr}}</style>'
        f'<body data-review="{escape(summary["review_id"], quote=True)}"><header>'
        '<h1>仅图片转文字验收</h1><p>不执行规则提炼、embedding、重排、测试点和用例生成。'
        '模型复核不等于人工确认；程序检查通过不等于识别准确。</p>'
        f'<pre>{escape(json.dumps(summary, ensure_ascii=False, indent=2))}</pre>'
        '<button>导出人工审核 JSON</button></header>' + "".join(sections)
        + '<script>' + script + '</script></body></html>'
    )
    path = directory / "review.html"
    path.write_text(html, encoding="utf-8")
    (directory / "results.json").write_text(
        json.dumps({"summary": summary, "images": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
