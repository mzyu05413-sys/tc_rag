"用原生图元复刻截图并绘制当前双遍视觉 / 两轮 RAG；仅生成离线文档。"

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/diagrams/recreated_flows"
FONT = "Microsoft YaHei"


def geometry(cell, x, y, w, h):
    ET.SubElement(
        cell, "mxGeometry", x=str(x), y=str(y), width=str(w), height=str(h), **{"as": "geometry"}
    )


class Diagram:
    def __init__(self, name, width, height, detail=False):
        self.name, self.width, self.height, self.detail = name, width, height, detail
        self.nodes, self.edges = {}, []
        self.background = "#121212"

    def node(
        self,
        key,
        x,
        y,
        w,
        h,
        title,
        body="",
        *,
        kind="box",
        fill=None,
        stroke=None,
        size=None,
        body_size=None,
        color="#e8e8e8",
        align="center",
    ):
        assert key not in self.nodes, key
        self.nodes[key] = dict(
            x=x,
            y=y,
            w=w,
            h=h,
            title=title,
            body=body,
            kind=kind,
            fill=fill or "#292929",
            stroke=stroke or "#c3c3c3",
            size=size or (19 if self.detail else 9),
            body_size=body_size or (15 if self.detail else 7),
            color=color,
            align=align,
        )

    def text(self, key, x, y, w, h, title, *, size=11, color="#e8e8e8", align="left"):
        self.node(key, x, y, w, h, title, kind="text", size=size, color=color, align=align)

    def group(self, key, x, y, w, h, title, *, stroke="#606060", size=10):
        self.node(
            key,
            x,
            y,
            w,
            h,
            "",
            kind="group",
            stroke="#454545" if self.detail else stroke,
            fill="none",
        )
        self.text(key + "_title", x + 8, y + 3, w - 16, 25, title, size=size)

    def edge(self, source, target, points, label="", at=None, dashed=False, color=None, size=None):
        self.edges.append(
            dict(
                source=source,
                target=target,
                points=points,
                label=label,
                at=at,
                dashed=dashed,
                color=color or "#bababa",
                size=size or (13 if self.detail else 7),
            )
        )

    @staticmethod
    def xml_edge(root, key, parent, points, color, *, arrow=False, dashed=False, width=1):
        cell = ET.SubElement(
            root,
            "mxCell",
            id=key,
            edge="1",
            parent=parent,
            style=f"edgeStyle=none;rounded=0;curved=0;sketch=0;html=1;strokeColor={color};"
            f"strokeWidth={width};endArrow={'block' if arrow else 'none'};"
            f"startArrow=none;endFill=1;dashed={int(dashed)};jumpStyle=none;",
        )
        geo = ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})
        for name, (x, y) in (("sourcePoint", points[0]), ("targetPoint", points[-1])):
            ET.SubElement(geo, "mxPoint", x=str(x), y=str(y), **{"as": name})
        if len(points) > 2:
            array = ET.SubElement(geo, "Array", **{"as": "points"})
            for x, y in points[1:-1]:
                ET.SubElement(array, "mxPoint", x=str(x), y=str(y))
        return cell

    def xml(self, key):
        page = ET.Element("diagram", id=key, name=self.name)
        model = ET.SubElement(
            page,
            "mxGraphModel",
            grid="0",
            gridSize="10",
            guides="1",
            connect="1",
            arrows="1",
            page="1",
            pageScale="1",
            math="0",
            pageWidth=str(self.width),
            pageHeight=str(self.height),
            background=self.background,
            adaptiveColors="none",
        )
        root = ET.SubElement(model, "root")
        ET.SubElement(root, "mxCell", id="0")
        for layer, label in (
            ("bg", "背景网格（可隐藏）"),
            ("frames", "分区"),
            ("lines", "连线"),
            ("nodes", "节点与文字"),
        ):
            layer_cell = ET.SubElement(root, "mxCell", id=layer, value=label, parent="0")
            if layer == "bg":
                layer_cell.set("style", "locked=1;")
        cell = ET.SubElement(
            root,
            "mxCell",
            id="paper",
            parent="bg",
            vertex="1",
            style=f"rounded=1;arcSize=3;fillColor={self.background};strokeColor=none;locked=1;",
        )
        geometry(cell, 0, 0, self.width, self.height)
        step = 20 if self.detail else 5 if self.width < 500 else 10
        for axis, maximum in (("x", self.width), ("y", self.height)):
            for pos in range(step, maximum, step):
                major = pos % (step * 5) == 0
                color = "#373737" if major else "#242424"
                # 网格是背景装饰，不能建成连接器，否则会参与交叉跳线计算。
                grid_cell = ET.SubElement(
                    root,
                    "mxCell",
                    id=f"grid_{axis}_{pos}",
                    parent="bg",
                    vertex="1",
                    connectable="0",
                    style=f"shape=rectangle;fillColor={color};strokeColor=none;"
                    "locked=1;pointerEvents=0;",
                )
                if axis == "x":
                    geometry(grid_cell, pos, 0, 0.4, self.height)
                else:
                    geometry(grid_cell, 0, pos, self.width, 0.4)
        for key, n in self.nodes.items():
            shape = "shape=rhombus;" if n["kind"] == "diamond" else "rounded=1;arcSize=10;"
            is_text = n["kind"] == "text"
            value = "<div><b>" + html.escape(n["title"]).replace("\n", "<br>") + "</b>"
            if n["body"]:
                value += (
                    f'<br><span style="font-size:{n["body_size"]}px;color:'
                    + "#9b9b9b"
                    + '">'
                    + html.escape(n["body"]).replace("\n", "<br>")
                    + "</span>"
                )
            value += "</div>"
            style = (
                shape + "whiteSpace=wrap;html=1;spacing"
                "=1;shadow=0;"
                f"fontFamily={FONT};fontSize={n['size']};fontColor={n['color']};"
                f"align={n['align']};verticalAlign=middle;"
                + (
                    "fillColor=none;strokeColor=none;"
                    if is_text
                    else f"fillColor={n['fill']};strokeColor={n['stroke']};strokeWidth=1;"
                )
            )
            cell = ET.SubElement(
                root,
                "mxCell",
                id=key,
                value=value,
                style=style,
                vertex="1",
                parent="frames" if n["kind"] == "group" else "nodes",
            )
            geometry(cell, n["x"], n["y"], n["w"], n["h"])
        for i, e in enumerate(self.edges):
            assert e["source"] in self.nodes and e["target"] in self.nodes
            cell = self.xml_edge(
                root,
                f"edge_{i}",
                "lines",
                e["points"],
                e["color"],
                arrow=True,
                dashed=e["dashed"],
                width=1.4 if self.detail else 1,
            )
            cell.set("source", e["source"])
            cell.set("target", e["target"])
            for endpoint, point in (("source", e["points"][0]), ("target", e["points"][-1])):
                n = self.nodes[e[endpoint]]
                px, py = (point[0] - n["x"]) / n["w"], (point[1] - n["y"]) / n["h"]
                assert 0 <= px <= 1 and 0 <= py <= 1, (self.name, endpoint, e)
                prefix = "exit" if endpoint == "source" else "entry"
                cell.set(
                    "style",
                    cell.get("style", "") + f"{prefix}X={px};{prefix}Y={py};"
                    f"{prefix}Dx=0;{prefix}Dy=0;{prefix}Perimeter=0;",
                )
            if e["label"]:
                x, y = e["at"]
                lines = e["label"].splitlines()
                w = max(len(line) for line in lines) * e["size"] + 8
                label = ET.SubElement(
                    root,
                    "mxCell",
                    id=f"label_{i}",
                    vertex="1",
                    parent="nodes",
                    value=html.escape(e["label"]).replace("\n", "<br>"),
                    style=f"text;html=1;align=center;fontFamily={FONT};"
                    f"fontSize={e['size']};fontColor={e['color']};"
                    f"fillColor={self.background};strokeColor=none;",
                )
                geometry(label, x - w / 2, y - e["size"] / 2 - 2, w, (e["size"] + 4) * len(lines))
        return page

    def svg(self):
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        svg = ET.Element(
            "svg",
            xmlns="http://www.w3.org/2000/svg",
            width=str(self.width),
            height=str(self.height),
            viewBox=f"0 0 {self.width} {self.height}",
        )
        ET.SubElement(svg, "title").text = self.name
        defs = ET.SubElement(svg, "defs")
        step = 20 if self.detail else 5 if self.width < 500 else 10
        for key, size, color in (
            ("minor", step, "#242424"),
            ("major", step * 5, "#373737"),
        ):
            pattern = ET.SubElement(
                defs,
                "pattern",
                id=key,
                width=str(size),
                height=str(size),
                patternUnits="userSpaceOnUse",
            )
            ET.SubElement(
                pattern,
                "path",
                d=f"M {size} 0 L 0 0 0 {size}",
                fill="none",
                stroke=color,
                **{"stroke-width": "0.6"},
            )
        for color in sorted({e["color"] for e in self.edges}):
            marker = ET.SubElement(
                defs,
                "marker",
                id="a" + color[1:],
                markerWidth="8",
                markerHeight="8",
                refX="7",
                refY="4",
                orient="auto",
                markerUnits="userSpaceOnUse",
            )
            ET.SubElement(marker, "path", d="M 0 1 L 7 4 L 0 7 z", fill=color)
        for fill in (self.background, "url(#minor)", "url(#major)"):
            ET.SubElement(
                svg,
                "rect",
                x="0",
                y="0",
                width=str(self.width),
                height=str(self.height),
                rx="12",
                fill=fill,
            )
        for n in self.nodes.values():
            if n["kind"] == "group":
                self._svg_node(svg, n)
        for e in self.edges:
            attrs = dict(
                points=" ".join(f"{x},{y}" for x, y in e["points"]),
                fill="none",
                stroke=e["color"],
                **{
                    "stroke-width": "1.4" if self.detail else "1",
                    "stroke-linejoin": "miter",
                    "marker-end": f"url(#a{e['color'][1:]})",
                },
            )
            if e["dashed"]:
                attrs["stroke-dasharray"] = "5 4"
            ET.SubElement(svg, "polyline", attrs)
        for n in self.nodes.values():
            if n["kind"] != "group":
                self._svg_node(svg, n)
        for e in self.edges:
            if not e["label"]:
                continue
            x, y = e["at"]
            lines = e["label"].splitlines()
            w = max(len(line) for line in lines) * e["size"] + 8
            ET.SubElement(
                svg,
                "rect",
                x=str(x - w / 2),
                y=str(y - e["size"]),
                width=str(w),
                height=str((e["size"] + 4) * len(lines)),
                fill=self.background,
            )
            for i, line in enumerate(lines):
                svg_text(svg, x, y + i * (e["size"] + 4), line, e["size"], e["color"])
        return svg

    def _svg_node(self, svg, n):
        x, y, w, h = (n[k] for k in ("x", "y", "w", "h"))
        if n["kind"] == "diamond":
            ET.SubElement(
                svg,
                "polygon",
                points=f"{x + w / 2},{y} {x + w},{y + h / 2} {x + w / 2},{y + h} {x},{y + h / 2}",
                fill=n["fill"],
                stroke=n["stroke"],
            )
        elif n["kind"] != "text":
            ET.SubElement(
                svg,
                "rect",
                x=str(x),
                y=str(y),
                width=str(w),
                height=str(h),
                rx="5" if self.detail else "3",
                fill=n["fill"],
                stroke=n["stroke"],
            )
        if not n["title"]:
            return
        titles, bodies = n["title"].splitlines(), n["body"].splitlines()
        th, bh = n["size"] + (5 if self.detail else 2), n["body_size"] + (5 if self.detail else 2)
        baseline = y + (h - len(titles) * th - len(bodies) * bh) / 2 + n["size"]
        px, anchor = (x + w / 2, "middle") if n["align"] == "center" else (x + 1, "start")
        for line in titles:
            svg_text(svg, px, baseline, line, n["size"], n["color"], anchor, True)
            baseline += th
        for line in bodies:
            svg_text(
                svg,
                px,
                baseline - 1,
                line,
                n["body_size"],
                "#9b9b9b",
                anchor,
            )
            baseline += bh


def svg_text(svg, x, y, text, size, color, anchor="middle", bold=False):
    element = ET.SubElement(
        svg,
        "text",
        x=str(x),
        y=str(y),
        fill=color,
        **{
            "font-family": "Microsoft YaHei, Noto Sans CJK SC, sans-serif",
            "font-size": str(size),
            "text-anchor": anchor,
            "font-weight": "600" if bold else "400",
        },
    )
    element.text = text


def replica_workflow():
    d = Diagram("01 · 原图复刻：主流程", 393, 732)
    for key, y, h, label in (
        ("parse", 8, 307, "解析"),
        ("extraction", 332, 97, "提炼与建库"),
        ("generation", 446, 203, "检索与生成"),
        ("delivery", 665, 57, "交付"),
    ):
        d.group(key, 9, y, 349, h, label, size=8)
    for key, x, y, w, h, title, body, kind in [
        ("input", 114, 28, 139, 27, "需求文档", "", "box"),
        ("format", 114, 73, 139, 40, "PDF 或 DOCX?", "", "diamond"),
        ("pdf", 21, 132, 116, 27, "PDF 解析", "文字、表格、图片、页码", "box"),
        ("docx", 230, 132, 116, 27, "DOCX 解析", "段落、表格、图片位置", "box"),
        ("has_vision", 114, 176, 139, 39, "启用视觉\n且有图片?", "", "diamond"),
        ("vision", 230, 237, 116, 27, "图片转文字", "视觉模型，标注为推断", "box"),
        ("chunk", 114, 279, 139, 27, "分块", "按章节、段落、表格行", "box"),
        ("rules", 114, 352, 139, 27, "规则提炼与核验", "DeepSeek 提炼，逐字核对原文", "box"),
        ("index", 114, 394, 139, 27, "建立向量索引", "缓存复用，写入 numpy 或 faiss", "box"),
        ("plan", 114, 467, 139, 27, "规划轮", "检索与重排（可选），规划测试点", "box"),
        ("ready", 114, 510, 139, 40, "有就绪\n测试点?", "", "diamond"),
        ("cases", 114, 571, 139, 27, "生成轮", "检索与重排（可选），生成用例", "box"),
        ("save", 114, 613, 139, 27, "校验并保存", "引用、依据、重复、覆盖检查", "box"),
        ("xmind", 30, 686, 99, 27, "XMind 目录", "全部测试点及状态", "box"),
        ("excel", 134, 686, 99, 27, "Excel 用例", "实际生成的用例", "box"),
        ("quality", 238, 686, 99, 27, "质量报告", "问题与覆盖记录", "box"),
    ]:
        d.node(key, x, y, w, h, title, body, kind=kind, size=8 if kind == "diamond" else 9)
    for args in [
        ("input", "format", [(183.5, 55), (183.5, 73)]),
        ("format", "pdf", [(114, 93), (79, 93), (79, 132)]),
        ("format", "docx", [(253, 93), (288, 93), (288, 132)]),
        ("pdf", "has_vision", [(79, 159), (79, 167), (183.5, 167), (183.5, 176)]),
        ("docx", "has_vision", [(288, 159), (288, 167), (183.5, 167), (183.5, 176)]),
        ("vision", "chunk", [(288, 264), (288, 292.5), (253, 292.5)]),
        ("chunk", "rules", [(183.5, 306), (183.5, 352)]),
        ("rules", "index", [(183.5, 379), (183.5, 394)]),
        ("index", "plan", [(183.5, 421), (183.5, 467)]),
        ("plan", "ready", [(183.5, 494), (183.5, 510)]),
        ("cases", "save", [(183.5, 598), (183.5, 613)]),
        ("save", "xmind", [(183.5, 640), (183.5, 663), (79.5, 663), (79.5, 686)]),
        ("save", "excel", [(183.5, 640), (183.5, 686)]),
        ("save", "quality", [(183.5, 640), (183.5, 663), (287.5, 663), (287.5, 686)]),
    ]:
        d.edge(*args)
    d.edge("has_vision", "vision", [(253, 195.5), (288, 195.5), (288, 237)], "是", (263, 187))
    d.edge("has_vision", "chunk", [(183.5, 215), (183.5, 279)], "否", (193, 223))
    d.edge("ready", "cases", [(183.5, 550), (183.5, 571)], "是", (193, 560))
    d.edge("ready", "save", [(253, 530), (347, 530), (347, 626.5), (253, 626.5)], "否", (263, 522))
    d.text("pdf_label", 90, 78, 28, 10, "PDF", size=7)
    d.text("docx_label", 258, 78, 32, 10, "DOCX", size=7)
    return d


def replica_architecture():
    d = Diagram("02 · 原图复刻：系统分层架构", 711, 606)
    d.text("title", 76, 29, 400, 25, "系统分层架构", size=18)
    for key, y, h in (
        ("entry", 69, 73),
        ("app", 170, 75),
        ("infra", 279, 129),
        ("external", 441, 73),
    ):
        d.node(key, 76, y, 389, h, "", kind="group", stroke="#454545", fill="none")
    for key, x, y, w, h, text, size in [
        ("entry_label", 94, 92, 62, 27, "入口与配置", 11),
        ("app_label", 102, 193, 48, 20, "应用层", 11),
        ("app_sub", 87, 210, 69, 20, "编排与接口定义", 9),
        ("infra_label", 98, 327, 52, 20, "基础设施", 11),
        ("infra_sub", 88, 344, 68, 20, "实现应用层接口", 9),
        ("external_label", 99, 456, 55, 20, "外部资源", 11),
        ("external_sub", 84, 474, 72, 26, "模型服务与本地文\n件", 9),
    ]:
        d.text(key, x, y, w, h, text, size=size, color="#a8a8a8" if size == 9 else "#e8e8e8")
    for key, x, y, title, body in [
        ("cli", 157, 81, "命令行入口", "启动、查看、导出"),
        ("config", 261, 81, "配置装配", "多级配置，装配实现"),
        ("prompts", 364, 81, "提示词", "按阶段与版本管理"),
        ("orchestration", 157, 186, "流程编排", "阶段串联与进度"),
        ("services", 261, 186, "业务服务", "接入、分块、校验、设\n计、评估"),
        ("ports", 364, 186, "外部能力接口", "解析、模型、存储等接\n口"),
        ("parsers", 157, 291, "文档解析", "DOCX、PDF"),
        ("models", 261, 291, "模型适配", "对话、向量、视觉、重\n排"),
        ("retrieval", 364, 291, "检索", "numpy 或 faiss"),
        ("storage", 157, 349, "存储", "数据库与文件"),
        ("export", 261, 349, "导出", "XMind、Excel、报告"),
        ("runtime", 364, 349, "运行支持", "计时与用量估算"),
    ]:
        d.node(key, x, y, 93, 49 if y == 81 else 48, title, body, size=11, body_size=9)
    d.node("domain", 479, 69, 89, 339, "", fill="#121212", stroke="#454545")
    d.text("domain_title", 492, 152, 64, 22, "领域层", size=11, align="center")
    d.text(
        "domain_body",
        483,
        181,
        81,
        130,
        "业务对象\n块、证据、规则\n测试点、用例\n问题与覆盖\n\n检索与"
        "标识记录\n向量空间、索引\n上下文、文档指纹",
        size=9,
        color="#a8a8a8",
        align="center",
    )
    d.text(
        "domain_bottom",
        483,
        327,
        81,
        29,
        "被各层共用\n不依赖外部实现",
        size=9,
        color="#a8a8a8",
        align="center",
    )
    d.node(
        "remote", 157, 454, 145, 48, "模型服务", "DeepSeek 对话、智谱重排等", size=11, body_size=9
    )
    d.node("local", 312, 454, 145, 48, "本地文件", "数据库、原件、索引、输出", size=11, body_size=9)
    d.edge("entry", "app", [(270, 142), (270, 170)], "调用", (270, 155), size=11)
    d.edge("app", "infra", [(270, 245), (270, 279)], "通过接口使用", (270, 262), size=10)
    d.edge("infra", "external", [(270, 408), (270, 441)], "访问", (270, 426), size=11)
    d.node("legend1", 76, 534, 16, 16, "")
    d.node("legend2", 203, 534, 16, 16, "", fill="#1c1c1c")
    d.text("legend1_text", 98, 533, 101, 20, "白底：项目内模块", size=10)
    d.text("legend2_text", 225, 533, 101, 20, "灰底：外部资源", size=10)
    return d


def detailed_workflow():
    d = Diagram("03 · 当前详细流程：双遍视觉与两轮 RAG", 1840, 2860, detail=True)
    blue = purple = teal = green = orange = "#c3c3c3"
    d.text("title", 40, 18, 1760, 42, "tc_rag · 需求文档 → 双遍视觉 → 两轮 RAG → 测试资产", size=30)
    d.text(
        "subtitle",
        42,
        64,
        1760,
        26,
        "当前实现核对版 · 2026-11-06 · 模型由配置决定 · 阅读步骤不固定 CLI 阶段编号",
        size=17,
        color="#9b9b9b",
    )
    d.group("access", 30, 110, 1780, 140, "A  接入与图文分离 · 本地处理", stroke=blue, size=19)
    d.node(
        "cli",
        70,
        153,
        400,
        75,
        "CLI 输入",
        "PDF / DOCX 路径＋生成目标\n配置校验，创建 run_id",
        stroke=blue,
    )
    d.node(
        "ingest",
        535,
        153,
        450,
        75,
        "接入文件",
        "保存原件、指纹、文档版本与运行记录\nSQLite 结构化事实；assets 原始资产",
        stroke=blue,
    )
    d.node(
        "parse",
        1050,
        153,
        700,
        75,
        "文档解析：文字 / 表格 / 图片分离",
        "PDF：页码与位置；DOCX：段落、表格与图片位置\n保留 B"
        "lock、章节和 Locator；无独立 OCR 扫描支路",
        stroke=blue,
    )
    d.edge("cli", "ingest", [(470, 190), (535, 190)])
    d.edge("ingest", "parse", [(985, 190), (1050, 190)])

    d.group(
        "vision_group",
        30,
        280,
        1780,
        650,
        "B  图片逐张处理 · 图片之间有界并发；同图主读 → 复核按顺序执行",
        stroke=purple,
        size=19,
    )
    for key, x, y, w, h, title, body, kind in [
        ("vision_on", 70, 350, 200, 90, "视觉启用\n且存在图片？", "", "diamond"),
        (
            "size",
            320,
            350,
            285,
            80,
            "图片尺寸校验",
            "过小图片跳过并记 Issue\n原图保留，不执行独立 OCR",
            "box",
        ),
        (
            "context",
            665,
            350,
            290,
            80,
            "构造图片上下文",
            "章节＋同章节邻近正文\n正文 context_id 与原图 Locator",
            "box",
        ),
        ("cached", 1030, 340, 300, 100, "视觉缓存命中？", "", "diamond"),
        (
            "cached_result",
            1450,
            350,
            300,
            80,
            "复用视觉结果与审计",
            "指纹包含图片、上下文、模型\n提示词与预处理参数",
            "box",
        ),
        ("skip", 70, 485, 200, 70, "该图片跳过", "保留来源与跳过原因", "box"),
        (
            "views",
            1030,
            480,
            300,
            85,
            "图像预处理与视图",
            "总览图＋原像素重叠局部图\n坐标、view_id；按配置决定切图",
            "box",
        ),
        (
            "vision_a",
            1030,
            605,
            300,
            100,
            "第一遍：视觉主读 A",
            "models.vision\n同次请求提供全部视图与上下文\nprompts/vision/v2.md",
            "box",
        ),
        (
            "draft_check",
            665,
            605,
            290,
            100,
            "草稿结构与引用检查",
            "VisionDescription Schema\n视图 ID、事实 ID、正文引用\n无效草稿也可交复核修正",
            "box",
        ),
        ("verify_on", 370, 605, 200, 100, "需要第二遍\n复核？", "", "diamond"),
        (
            "vision_b",
            320,
            755,
            350,
            100,
            "第二遍：视觉复核 B",
            "原图视图＋正文上下文＋主读草稿\nmodels.vision_"
            "review（独立启用时）\nprompts/vision_v"
            "erify/v1.md",
            "box",
        ),
        (
            "review_check",
            740,
            755,
            300,
            100,
            "复核结构与引用检查",
            "重新校验事实 / 视图 / 图文关联\n表格列数、ID 唯一与非空内容\n校验通过不等于语义正确",
            "box",
        ),
        (
            "vision_result",
            1160,
            755,
            300,
            100,
            "派生图片文字 Block",
            "facts / relations / tables\ncon"
            "text_links / uncertainties\nori"
            "gin=vision；保留模型与版本",
            "box",
        ),
        (
            "vision_fail",
            1510,
            755,
            250,
            100,
            "该图片失败 → Issue",
            "默认 fail：复核失败不保留草稿\n其他图片继续；原图与错误"
            "可查\n未复核草稿另受 keep_draft 策略",
            "box",
        ),
    ]:
        d.node(
            key,
            x,
            y,
            w,
            h,
            title,
            body,
            kind=kind,
            stroke=orange
            if key in {"skip", "vision_fail"}
            else green
            if key in {"cached_result", "vision_result"}
            else purple,
            size=17 if key == "vision_fail" else 18 if kind == "diamond" else 19,
            body_size=13 if key == "vision_fail" else 15,
        )
    d.edge(
        "parse",
        "vision_on",
        [(1400, 228), (1400, 265), (1785, 265), (1785, 325), (170, 325), (170, 350)],
    )
    d.edge("vision_on", "size", [(270, 395), (320, 395)], "是", (295, 382))
    d.edge("size", "context", [(605, 390), (665, 390)], "可用", (635, 378))
    d.edge("context", "cached", [(955, 390), (1030, 390)])
    d.edge(
        "size",
        "skip",
        [(320, 390), (290, 390), (290, 520), (270, 520)],
        "过小",
        (290, 460),
        color=orange,
    )
    d.edge("cached", "cached_result", [(1330, 390), (1450, 390)], "命中", (1390, 377))
    d.edge("cached", "views", [(1180, 440), (1180, 480)], "未命中", (1240, 463))
    d.edge("views", "vision_a", [(1180, 565), (1180, 605)])
    d.edge("vision_a", "draft_check", [(1030, 655), (955, 655)])
    d.edge(
        "vision_a",
        "vision_fail",
        [(1330, 655), (1490, 655), (1490, 730), (1635, 730), (1635, 755)],
        "主读请求失败",
        (1435, 640),
        True,
        orange,
        12,
    )
    d.edge("draft_check", "verify_on", [(665, 655), (570, 655)])
    d.edge("verify_on", "vision_b", [(470, 705), (470, 755)], "是", (490, 730))
    d.edge("vision_b", "review_check", [(670, 805), (740, 805)])
    d.edge(
        "vision_b",
        "vision_fail",
        [(670, 805), (705, 805), (705, 870), (1635, 870), (1635, 855)],
        "复核请求失败",
        (790, 870),
        True,
        orange,
        12,
    )
    d.edge("review_check", "vision_result", [(1040, 805), (1160, 805)], "通过", (1100, 791))
    d.edge(
        "review_check",
        "vision_fail",
        [(890, 855), (890, 900), (1635, 900), (1635, 855)],
        "失败 / 重试耗尽",
        (1410, 900),
        True,
        orange,
    )
    d.edge(
        "draft_check",
        "vision_fail",
        [(810, 705), (810, 730), (1635, 730), (1635, 755)],
        "未复核且草稿无效",
        (1420, 730),
        True,
        orange,
    )
    d.edge(
        "verify_on",
        "vision_result",
        [(370, 655), (300, 655), (300, 885), (1310, 885), (1310, 855)],
        "否：只接受有效草稿",
        (535, 885),
        True,
    )
    d.edge(
        "cached_result",
        "vision_result",
        [(1750, 390), (1785, 390), (1785, 710), (1460, 710), (1460, 805)],
    )

    d.group(
        "extract_group",
        30,
        970,
        1780,
        360,
        "C  结构分块、需求提炼、事实保存与索引",
        stroke=blue,
        size=19,
    )
    for key, x, y, w, h, title, body, color in [
        (
            "chunk",
            70,
            1030,
            350,
            100,
            "结构分块 → Chunk",
            "章节 / 完整段落 / 完整表格行\n保留 sources 的"
            " [start,end) 区间\nstructural / 可"
            "选 structural_v2",
            blue,
        ),
        (
            "extract",
            520,
            1030,
            450,
            100,
            "LLM 提炼功能、规则、实体与证据",
            "models.llm ＋ prompts/extract/v"
            "2.md\n可选邻文仅作背景；完整请求预算预检\n逐批并发，缓存"
            "按内容与配置区分",
            purple,
        ),
        (
            "fact_check",
            1070,
            1030,
            350,
            100,
            "结构、引文与引用闭包核验",
            "证据须在当前 Chunk 授权区间\nFeature / Ru"
            "le / Entity / Evidence / Issue"
            "\n来源取自解析事实，不由模型改写",
            blue,
        ),
        (
            "sqlite",
            1510,
            1030,
            250,
            100,
            "SQLite 保存事实",
            "功能、规则、实体、证据与问题\n阶段快照、调用与耗时记录\nJSON / 索引均为派生产物",
            green,
        ),
        (
            "index",
            1070,
            1210,
            350,
            95,
            "证据与原文区间向量索引",
            "models.embedding：批量、并发、缓存\nNumP"
            "y / 可选 FAISS；当前需求版本\n保留 ID、向量空间"
            "、索引版本",
            teal,
        ),
        (
            "extract_note",
            70,
            1180,
            900,
            115,
            "此处保留的边界",
            (
                "邻文不扩大证据授权范围；缺少联合依据进入 Issue\n视觉文"
                "字始终是不确定来源；仅图片点按 "
                "vision_policy 处理\n新分块 / 邻文策略默认关"
                "闭；不自动查询历史全库或代码仓库"
            ),
            "#c3c3c3",
        ),
    ]:
        d.node(
            key,
            x,
            y,
            w,
            h,
            title,
            body,
            stroke=color,
            size=18 if key in {"sqlite", "extract_note"} else 19,
            body_size=13 if key == "sqlite" else 15,
        )
    d.edge(
        "vision_result",
        "chunk",
        [(1310, 855), (1310, 950), (1000, 950), (1000, 1010), (245, 1010), (245, 1030)],
    )
    d.edge(
        "vision_fail",
        "chunk",
        [(1635, 855), (1635, 950), (1000, 950), (1000, 1010), (245, 1010), (245, 1030)],
        dashed=True,
        color=orange,
    )
    d.edge(
        "vision_on",
        "chunk",
        [(70, 395), (50, 395), (50, 950), (1000, 950), (1000, 1010), (245, 1010), (245, 1030)],
        "否：原文继续",
        (125, 925),
    )
    d.edge(
        "skip",
        "chunk",
        [(170, 555), (170, 950), (1000, 950), (1000, 1010), (245, 1010), (245, 1030)],
        dashed=True,
    )
    for a, b, x1, x2 in (
        ("chunk", "extract", 420, 520),
        ("extract", "fact_check", 970, 1070),
        ("fact_check", "sqlite", 1420, 1510),
    ):
        d.edge(a, b, [(x1, 1080), (x2, 1080)])
    d.edge("fact_check", "index", [(1245, 1130), (1245, 1210)])

    d.group("rag1", 30, 1370, 850, 900, "D  第一轮 RAG · 规划需要测什么", stroke=teal, size=20)
    d.group(
        "rag2", 940, 1370, 870, 900, "E  第二轮 RAG ＋ Skill · 生成如何测", stroke=teal, size=20
    )
    for key, x, y, h, title, body, color in [
        (
            "base_points",
            90,
            1425,
            75,
            "本地基础测试点",
            "按有效规则计算可确定的边界等基础意图\n不凭空补精度、角色、数字；缺失信息进入 Issue",
            blue,
        ),
        (
            "query1",
            90,
            1530,
            85,
            "按规则批次构造查询",
            "生成目标＋本批规则陈述\n输入当前规则、直接证据与已有基础点",
            teal,
        ),
        (
            "query2",
            1020,
            1530,
            85,
            "按测试点批次构造查询",
            "生成目标＋本批测试点目标\n经 point → rule → feature 关联加载业务背景",
            teal,
        ),
        (
            "context1",
            90,
            1775,
            100,
            "组装第一轮上下文",
            "规则直接证据不经检索裁剪；检索资料仅作补充\n完整请求超预算时"
            "先裁低优先级补充资料\n补充内容不能自动新增规则、扩大引用范围",
            teal,
        ),
        (
            "context2",
            1020,
            1775,
            100,
            "组装第二轮上下文",
            "直接加载点关联规则、所属功能相关证据及 Skill\n每步允许"
            "引用范围仍受所选点的规则 / 证据约束\n补充证据按预算裁减，"
            "不替代直接依据",
            teal,
        ),
        (
            "plan",
            90,
            1905,
            85,
            "LLM 补充测试意图 → PointBatch",
            "models.llm ＋ prompts/plan_poin"
            "ts/v2.md\n补正常、异常、权限、状态等意图；不重复程序"
            "已计算边界",
            purple,
        ),
        (
            "point_tree",
            90,
            2020,
            85,
            "校验、归并并保存 PointTree",
            "批次规则引用、重复与来源风险检查，保留各状态\n不要求人工批准"
            "，不把 XMind 回读成生成输入",
            green,
        ),
        (
            "selected_points",
            1020,
            1425,
            75,
            "筛选就绪点，准备 / 复用检索索引",
            "当前运行满足版本与空间条件时复用索引\n不会因为第二轮就必然重新向量化全部文档",
            teal,
        ),
        (
            "generate",
            1020,
            1905,
            85,
            "LLM ＋ Skill 生成 CaseBatch",
            "models.llm ＋ generate/v2.md ＋ "
            "case_skill/v2.md\n仅为本批测试点生成：前置、"
            "数据、动作、可观察预期",
            purple,
        ),
        (
            "case_check",
            1020,
            2020,
            85,
            "用例校验与有限覆盖补生成",
            "Schema / 批次 ID / 引用闭包 / 依据风险 /"
            " 重复 / 覆盖\n可修复缺口受次数和调用预算限制；仍需检索与"
            "模型调用",
            blue,
        ),
        (
            "cases_save",
            1020,
            2135,
            85,
            "保存实际用例、覆盖与问题",
            "SQLite 保持事实记录，校验通过不证明业务语义正确\n没有通过校验的内容不伪造成有效用例",
            green,
        ),
    ]:
        d.node(key, x, y, 650, h, title, body, stroke=color, body_size=16)
    retrieval_body = (
        "models.embedding：查询向量化＋缓存；当前需求"
        "索引向量 Top K\n可选关键词"
        "检索＋RRF；可选 models.rerank 重排前 N "
        "个\n重排结果接回其余候选；失败保"
        "留召回排名，不是只保留 N 项"
    )
    for key, x in (("retrieval1", 90), ("retrieval2", 1020)):
        d.node(key, x, 1645, 650, 100, "检索 → 可选重排", retrieval_body, stroke=teal, body_size=16)
    d.node(
        "ready",
        250,
        2140,
        330,
        100,
        "存在 ready 且\napplicable 的测试点？",
        kind="diamond",
        stroke=teal,
        size=18,
    )
    d.edge(
        "index",
        "base_points",
        [(1245, 1305), (1245, 1345), (805, 1345), (805, 1410), (415, 1410), (415, 1425)],
    )
    for a, b, x, y1, y2 in [
        ("base_points", "query1", 415, 1500, 1530),
        ("query1", "retrieval1", 415, 1615, 1645),
        ("retrieval1", "context1", 415, 1745, 1775),
        ("context1", "plan", 415, 1875, 1905),
        ("plan", "point_tree", 415, 1990, 2020),
        ("point_tree", "ready", 415, 2105, 2140),
        ("selected_points", "query2", 1345, 1500, 1530),
        ("query2", "retrieval2", 1345, 1615, 1645),
        ("retrieval2", "context2", 1345, 1745, 1775),
        ("context2", "generate", 1345, 1875, 1905),
        ("generate", "case_check", 1345, 1990, 2020),
        ("case_check", "cases_save", 1345, 2105, 2135),
    ]:
        d.edge(a, b, [(x, y1), (x, y2)])
    d.edge(
        "ready",
        "selected_points",
        [(580, 2190), (910, 2190), (910, 1462), (1020, 1462)],
        "是：就绪点进入生成",
        (910, 1508),
        size=12,
    )
    d.edge(
        "case_check",
        "query2",
        [(1670, 2062), (1780, 2062), (1780, 1572), (1670, 1572)],
        "有限补生成\n再次检索",
        (1765, 1850),
        True,
        orange,
        12,
    )

    d.group(
        "delivery", 30, 2320, 1780, 310, "F  联合导出 · 本地实现，不调用模型", stroke=green, size=20
    )
    d.node(
        "export",
        620,
        2390,
        600,
        80,
        "生成联合交付视图",
        "保留全部测试点目录、实际用例与问题覆盖报告\n没有就绪点也可导"
        "出目录 / 问题，Excel 可能只有表头",
        stroke=green,
        body_size=16,
    )
    d.edge(
        "ready",
        "export",
        [(415, 2240), (415, 2290), (805, 2290), (805, 2355), (920, 2355), (920, 2390)],
        "否：保留问题与全部目录",
        (565, 2290),
        color=orange,
    )
    d.edge("cases_save", "export", [(1345, 2220), (1345, 2355), (920, 2355), (920, 2390)])
    for key, x, title, body in [
        (
            "xmind",
            70,
            "XMind · 全部测试点目录",
            "模块 / 业务维度 / 功能 / 目标与状态；不包含完整步骤",
        ),
        (
            "excel",
            680,
            "Excel · 实际生成用例",
            "标题、前置、数据、步骤与预期；列与格式由导出器负责",
        ),
        (
            "reports",
            1290,
            "质量 / 覆盖 / 调用报告",
            "Issues、覆盖、阶段耗时、调用统计；启用的 JSON / Markdown",
        ),
    ]:
        d.node(key, x, 2520, 480, 80, title, body, stroke=green, body_size=14)
        center = x + 240
        d.edge("export", key, [(920, 2470), (920, 2495), (center, 2495), (center, 2520)])
    d.node(
        "notes",
        40,
        2660,
        1760,
        160,
        "配置、调用次数与能力边界",
        (
            "视觉 verify=true / auto / false "
            "控制第二遍是否执行；缓存命中不发"
            "请求，重试会增加调用次数。\n独立复核 models.visi"
            "on_review 启用时，启动"
            "校验要求与主读不是同端点同模型；未启用时适配器可复用主模型。"
            "\n可选 keep_draft 仅"
            "保留结构及引用有效的第一遍草稿，明确标记未复核；默认 fai"
            "l 将该图记失败。\n两轮 RAG"
            " 按不同批次查询；索引与向量缓存可复用。直接证据不依赖召回命"
            "中，补充证据按预算裁减。\n全程记"
            "录调用与阶段耗时；无中间人工审批、无源码输入、无 XMind"
            " 回导。视觉语义和最终业务质量仍"
            "需验收。"
        ),
        stroke="#c3c3c3",
        size=20,
        body_size=17,
    )
    return d


def write_xml(path, root):
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    diagrams = [replica_workflow(), replica_architecture(), detailed_workflow()]
    bundle = ET.Element("mxfile", host="app.diagrams.net", agent="tc-rag-native-diagrams")
    filenames = ["01_workflow_replica", "02_architecture_replica", "03_detailed_workflow"]
    for name, diagram in zip(filenames, diagrams, strict=True):
        bundle.append(diagram.xml(name))
        single = ET.Element("mxfile", host="app.diagrams.net", agent="tc-rag-native-diagrams")
        single.append(diagram.xml(name))
        write_xml(OUT / (name + ".drawio"), single)
        write_xml(OUT / (name + ".svg"), diagram.svg())
    write_xml(OUT / "tc_rag_recreated_and_detailed.drawio", bundle)
    print("Created three native diagram pages, three single-page files and three SVG previews.")


if __name__ == "__main__":
    main()
