import pathlib
import re

import win32com.client

DESKTOP = pathlib.Path("D:/Desktop")
jobs = [
    ("审批流配置_正文.html", "审批流配置_正文.docx"),
    ("流程设置_正文.html", "流程设置_正文.docx"),
]

tmp_files = []
for src, dst in jobs:
    hp = DESKTOP / src
    s = hp.read_text(encoding="utf-8")

    def repl(m):
        rel = m.group(1)
        abs_p = (DESKTOP / rel).resolve()
        return f'src="{abs_p.as_uri()}"'

    s2 = re.sub(r'src="(\./[^"]+)"', repl, s)
    abs_html = DESKTOP / (src.replace(".html", "_abs.html"))
    abs_html.write_text(s2, encoding="utf-8")
    tmp_files.append(abs_html)
    print(f"rewrote img paths -> {abs_html.name}")

word = win32com.client.Dispatch("Word.Application")
word.Visible = False
word.DisplayAlerts = 0

try:
    for abs_html, (src, dst) in zip(tmp_files, jobs):
        print(f"converting {abs_html.name} ...")
        doc = word.Documents.Open(str(abs_html), ConfirmConversions=False)
        doc.SaveAs2(str(DESKTOP / dst), FileFormat=12)  # wdFormatXMLDocument
        doc.Close(False)
        print(f"  OK -> {dst}")
finally:
    word.Quit()
