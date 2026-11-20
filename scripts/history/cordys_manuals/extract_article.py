import pathlib
import re

DESKTOP = pathlib.Path("D:/Desktop")

jobs = [
    ("审批流配置 - Cordys CRM _ 飞致云文档中心.html", "审批流配置_正文.html"),
    ("流程设置 - Cordys CRM _ 飞致云文档中心.html", "流程设置_正文.html"),
]

for src, dst in jobs:
    p = DESKTOP / src
    html = p.read_text(encoding="utf-8")
    m = re.search(r"<article\b[^>]*>(.*?)</article>", html, re.S)
    if not m:
        print(f"SKIP (no article): {src}")
        continue
    body = m.group(1)
    imgs = re.findall(r"<img\b[^>]*>", body, re.I)
    # 打印正文内的图片文件名，确认截图都在里面
    srcs = [re.search(r'src="([^"]+)"', i).group(1) for i in imgs if re.search(r'src="([^"]+)"', i)]
    print(f"[{dst}] article {len(body)} chars, {len(imgs)} img tags")
    for s in srcs:
        print("   img:", s.split("/")[-1])
    wrapper = (
        '<!DOCTYPE html><html lang="zh-Hans"><head><meta charset="utf-8">'
        f"<title>{p.stem}</title></head><body>{body}</body></html>"
    )
    (DESKTOP / dst).write_text(wrapper, encoding="utf-8")
    print(f"   -> wrote {dst}\n")
