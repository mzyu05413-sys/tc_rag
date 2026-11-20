"""当前主线的中文流程图，离线生成SVG、独立HTML与可编辑draw.io。"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/diagrams/current_rag_flow"
WIDTH, HEIGHT = 1680, 3040
NODES: dict[str, dict] = {}
EDGES: list[dict] = []


def node(key, x, y, w, h, title, body, color="#2463b3", fill="#ffffff"):
    NODES[key] = dict(x=x, y=y, w=w, h=h, title=title, body=body, color=color, fill=fill)


def edge(source, target, points, *, dashed=False, color="#728398", label="", at=None):
    EDGES.append(dict(source=source, target=target, points=points, dashed=dashed,
                      color=color, label=label, at=at))


def build():
    node("input", 650, 265, 380, 75, "输入需求文档", "PDF / DOCX ＋ 本次生成要求")
    node("parse", 650, 360, 380, 105, "读取文档内容", "读取段落、表格和图片\n保留标题与原文位置")
    node("text", 180, 485, 480, 115, "保留正文与表格", "正文保留原句\n表格保留行列关系和原文位置")
    node("image", 1020, 485, 480, 95, "提取图片", "保存图片文件和所在位置", "#a36b14")
    node("cache", 1020, 615, 480, 105, "检查图片与已有结果", "检查图片是否可用\n已有识别结果可直接复用", "#a36b14")
    node("vision", 1020, 755, 480, 110, "图片转文字与复核", "按配置缩放或分图后识别\n说明图片内容，可再复核并记录疑问", "#a36b14", "#fffaf0")
    node("combine", 600, 900, 480, 80, "汇总可用文字", "原文与图片说明分开标记，保留来源")
    edge("input", "parse", [(840, 340), (840, 360)])
    edge("parse", "text", [(840, 465), (840, 475), (420, 475), (420, 485)])
    edge("parse", "image", [(840, 465), (840, 475), (1260, 475), (1260, 485)])
    edge("image", "cache", [(1260, 580), (1260, 615)], color="#a36b14")
    edge("cache", "vision", [(1260, 720), (1260, 755)], color="#a36b14")
    edge("cache", "combine", [(1020, 667), (920, 667), (920, 880), (840, 880), (840, 900)],
         color="#a36b14", label="可复用", at=(920, 735))
    edge("vision", "combine", [(1260, 865), (1260, 940), (1080, 940)], color="#a36b14")
    edge("text", "combine", [(420, 600), (420, 940), (600, 940)])

    node("chunk", 100, 1160, 400, 115, "把文字拆成小段", "按段落、表格和标题拆分\n每段保留对应的原文位置")
    node("extract", 640, 1160, 400, 115, "逐段提取需求", "模型提取功能、业务规则\n同时给出对应的原文依据")
    node("check", 1180, 1160, 400, 115, "核对并整理结果", "检查引文及对象关联\n归并重复，记录冲突和缺失")
    node("rules", 120, 1390, 590, 145, "功能与需求规则", "保存功能、规则和原文依据\n供后续规划与生成使用\n不会直接把规则当成完整测试用例")
    node("index", 970, 1390, 590, 145, "准备需求证据索引", "原文依据及来源文字转成向量\n可选：增加关键词检索\n两轮RAG使用同一份需求范围", "#2463b3", "#edf5ff")
    edge("combine", "chunk", [(840, 980), (840, 1025), (60, 1025), (60, 1217), (100, 1217)])
    edge("chunk", "extract", [(500, 1217), (640, 1217)])
    edge("extract", "check", [(1040, 1217), (1180, 1217)])
    edge("check", "rules", [(1380, 1275), (1380, 1330), (415, 1330), (415, 1390)])
    edge("check", "index", [(1380, 1275), (1380, 1330), (1265, 1330), (1265, 1390)])

    for suffix, y, color in [("one", 1870, "#2463b3"), ("two", 2460, "#11806b")]:
        first = suffix == "one"
        node("query_" + suffix, 80, y, 300, 165,
             "准备规划输入" if first else "准备用例输入",
             "生成要求与一批规则\n程序先生成基础测试点\n原文依据直接保留" if first else
             "选出可生成的测试点\n补齐所属功能的规则\n保留原文依据", color)
        node("search_" + suffix, 480, y, 300, 165, "查找相关需求",
             "查询文字转成向量\n向量与可选关键词检索\n可选：重新排序", color)
        node("context_" + suffix, 880, y, 300, 165, "整理本轮依据",
             "已有原文优先保留\n补充相关条件与例外\n超出预算先减少补充", color)
        node("generate_" + suffix, 1280, y, 300, 165,
             "生成测试点" if first else "生成并检查用例",
             "保留已有基础测试点\n模型补充测试目标\n每点关联需求规则" if first else
             "模型生成步骤和预期\n检查格式、引用和覆盖\n保留问题与失败记录", color)
        for left, right in [("query", "search"), ("search", "context"), ("context", "generate")]:
            edge(left + "_" + suffix, right + "_" + suffix,
                 [(NODES[left + "_" + suffix]["x"] + 300, y + 82),
                  (NODES[right + "_" + suffix]["x"], y + 82)], color=color)
        edge("index", "search_" + suffix,
             [(1560, 1462), (1620, 1462), (1620, y - 15), (630, y - 15), (630, y)],
             dashed=True, color="#2463b3")
    node("points", 570, 2120, 540, 100, "保存测试点", "保存所属功能、需求规则与原文引用\n待澄清的测试点继续保留，不强行生成用例")
    node("output", 570, 2690, 540, 110, "输出交付文件", "测试点 XMind ＋ 测试用例 Excel\n同时输出质量报告和待确认问题", "#11806b", "#effaf6")
    edge("rules", "query_one", [(415, 1535), (415, 1700), (60, 1700), (60, 1952), (80, 1952)])
    edge("generate_one", "points", [(1430, 2035), (1430, 2170), (1110, 2170)])
    edge("points", "query_two", [(840, 2220), (840, 2280), (60, 2280), (60, 2542), (80, 2542)], color="#11806b")
    edge("generate_two", "output", [(1430, 2625), (1430, 2745), (1110, 2745)], color="#11806b")


PANELS = [
    (150, 865, "01", "读取文档与图片转文字", "图片解释按配置启用；原文和图片说明都保留来源。", "#a36b14"),
    (1040, 660, "02", "分块、提取功能与规则", "先提取并核对需求，再为后续两轮检索准备证据。", "#2463b3"),
    (1750, 500, "03", "第一轮 RAG：规划测试点", "查询来自生成要求与需求规则；检索返回的是需求依据。", "#2463b3"),
    (2320, 520, "04", "第二轮 RAG：生成测试用例", "查询来自测试点目标；检索仍返回需求依据，不是历史用例。", "#11806b"),
]


def svg():
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
             '<title>tc_rag 当前详细流程：图片转文字与两轮RAG</title>',
             '<style>text{font-family:"Microsoft YaHei","Noto Sans CJK SC",sans-serif}</style>',
             '<defs>']
    for color in {e["color"] for e in EDGES}:
        parts.append(f'<marker id="a{color[1:]}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse"><path d="M0 1L9 5L0 9Z" fill="{color}"/></marker>')
    parts.extend(['</defs>', f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#f3f6fa"/>'])

    def text(x, y, value, size=22, color="#25374a", weight="400", anchor="start"):
        parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}">{html.escape(value)}</text>')

    text(60, 65, "需求文档 → 测试点 → 测试用例", 39, "#132b45", "700")
    text(60, 108, "tc_rag 当前已实现流程  ·  一次处理一份需求文档  ·  2026-11-17", 22, "#607389")
    for y, h, number, title, subtitle, color in PANELS:
        parts.append(f'<rect x="40" y="{y}" width="1600" height="{h}" rx="22" fill="#ffffff" stroke="#d7e0ea"/>')
        parts.append(f'<rect x="78" y="{y+24}" width="52" height="42" rx="11" fill="{color}"/>')
        text(104, y+54, number, 24, "#ffffff", "700", "middle")
        text(150, y+55, title, 29, "#18324c", "700")
        text(150, y+87, subtitle, 20, "#607389")
    # 连线先画，节点覆盖连线端点，保持文字可读。
    for e in EDGES:
        points = ' '.join(f'{x},{y}' for x, y in e["points"])
        dash = 'stroke-dasharray="9 7"' if e["dashed"] else ''
        parts.append(f'<polyline points="{points}" fill="none" stroke="{e["color"]}" stroke-width="2.5" {dash} marker-end="url(#a{e["color"][1:]})"/>')
    for n in NODES.values():
        x, y, w, h = (n[k] for k in ("x", "y", "w", "h"))
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="15" fill="{n["fill"]}" stroke="{n["color"]}" stroke-width="1.8"/>')
        body = n["body"].splitlines()
        total = 32 + 14 + len(body) * 28
        top = y + (h - total) / 2 + 27
        text(x + w/2, top, n["title"], 27, n["color"], "700", "middle")
        for i, line in enumerate(body):
            text(x+w/2, top+38+i*28, line, 21, "#3b4e63", anchor="middle")
    for e in EDGES:
        if e["label"]:
            x,y = e["at"]
            parts.append(f'<rect x="{x-44}" y="{y-18}" width="88" height="28" fill="#ffffff"/>')
            text(x,y+2,e["label"],19,e["color"],anchor="middle")
    text(80, 1630, "蓝色虚线：两轮都从同一份需求中查找证据；原文依据始终保留。", 23, "#2463b3")
    text(60, 2895, "RAG：先查找相关需求依据，再帮助模型生成。", 22, "#18324c", "700")
    text(60, 2930, "向量：便于比较文字的数字表示。重新排序：把更相关的候选内容放在前面。", 21, "#607389")
    text(60, 2965, "全程保存原文位置、图片说明、规则、检索结果和生成记录；有引用不等于业务已经审核通过。", 21, "#607389")
    text(60, 3000, "说明：本图展示当前实现，不包含尚未开发的功能归并增强、跨文档生成或历史用例检索。", 20, "#607389")
    return ''.join(parts) + '</svg>'


def drawio():
    root = ET.Element("mxfile", host="app.diagrams.net")
    diagram = ET.SubElement(root, "diagram", name="当前详细流程", id="current")
    model = ET.SubElement(diagram, "mxGraphModel", page="1", pageWidth=str(WIDTH), pageHeight=str(HEIGHT))
    cells = ET.SubElement(model, "root")
    ET.SubElement(cells, "mxCell", id="0")
    ET.SubElement(cells, "mxCell", id="1", parent="0")
    for key,n in NODES.items():
        label = '<b>'+html.escape(n["title"])+'</b><br>'+html.escape(n["body"]).replace('\n','<br>')
        cell = ET.SubElement(cells, "mxCell", id=key, value=label, vertex="1", parent="1",
            style=f'rounded=1;whiteSpace=wrap;html=1;fontFamily=Microsoft YaHei;fontSize=21;fillColor={n["fill"]};strokeColor={n["color"]};fontColor=#25374a;')
        ET.SubElement(cell, "mxGeometry", x=str(n["x"]), y=str(n["y"]), width=str(n["w"]), height=str(n["h"]), **{"as":"geometry"})
    for i,e in enumerate(EDGES):
        cell = ET.SubElement(cells,"mxCell",id=f'e{i}',edge="1",parent="1",source=e["source"],target=e["target"],value=e["label"],
            style=f'edgeStyle=none;html=1;endArrow=block;strokeWidth=2;strokeColor={e["color"]};dashed={int(e["dashed"])};')
        geo=ET.SubElement(cell,"mxGeometry",relative="1",**{"as":"geometry"})
        pts=ET.SubElement(geo,"Array",**{"as":"points"})
        for x,y in e["points"][1:-1]:ET.SubElement(pts,"mxPoint",x=str(x),y=str(y))
    return ET.tostring(root,encoding="unicode")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    build()
    graphic=svg()
    (OUT/"RAG详细流程图.svg").write_text(graphic,encoding="utf-8")
    (OUT/"RAG详细流程图.drawio").write_text(drawio(),encoding="utf-8")
    (OUT/"render.html").write_text('<!doctype html><meta charset="utf-8"><style>body{margin:0}</style>'+graphic,encoding="utf-8")
    viewer = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>RAG详细流程图</title>
<style>body{margin:0;background:#e9eef5;font-family:"Microsoft YaHei",sans-serif}header{position:sticky;top:0;z-index:2;display:flex;gap:12px;align-items:center;padding:14px 22px;background:#16304b;color:white}button{border:0;border-radius:7px;padding:9px 15px;cursor:pointer}main{padding:20px;overflow:auto}.sheet{margin:auto;box-shadow:0 8px 36px #233c5620;line-height:0}svg{width:100%;height:auto}@media print{header{display:none}main{padding:0}.sheet{width:100%!important;box-shadow:none}@page{size:A3 portrait;margin:7mm}}</style>
<header><strong>tc_rag 当前详细流程</strong><button onclick="zoom(.2)">放大</button><button onclick="zoom(-.2)">缩小</button><button onclick="fit()">适合窗口</button><button onclick="window.print()">打印 / 保存PDF</button><span id="percent"></span></header>
<main><div class="sheet" id="sheet">GRAPHIC</div></main>
<script>let scale=1;function apply(){document.getElementById('sheet').style.width=(1680*scale)+'px';document.getElementById('percent').textContent=Math.round(scale*100)+'%'}function zoom(d){scale=Math.min(2,Math.max(.3,scale+d));apply()}function fit(){scale=Math.min(1,(innerWidth-50)/1680);apply()}fit();</script></html>'''
    (OUT/"打开查看.html").write_text(viewer.replace("GRAPHIC",graphic),encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
