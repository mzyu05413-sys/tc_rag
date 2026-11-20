# Cordys 手册一次性转换脚本

从项目根目录移入的历史工具，不属于 tc_rag 正式运行入口。

- extract_article.py 从本机已保存 HTML 中抽取 article。
- html2docx.py 通过 Windows Word COM 转换 HTML，依赖本机 Word 与 pywin32。

脚本保留原有桌面路径和两份文件名，运行前检查输入及输出。执行会写入桌面并可能覆盖同名产物，按实际任务授权使用。正式知识库入库直接读取 PDF/DOCX，毋须运行这些脚本。pywin32 属于历史工具依赖，不纳入核心运行依赖。
