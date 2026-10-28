# P2.8：Excel 与质量附表

[返回 P2 总览](README.md)

## 文件结构

ReportExporter 保留参考模板的第一张“模版”及默认十一列，另加：测试点、规则、覆盖矩阵、问题清单、未解析范围。最后一张只列 kind=unparsed 的问题。

SQLite cases 快照是事实来源，可重新导出：

```powershell
tc-rag export RUN_ID --format xlsx --config workdir/local.yaml --output-dir workdir/output
```

## 团队列定义

默认 `export.case_columns: []` 使用原十一列；填写时按列表顺序输出：

```yaml
export:
  case_columns:
    - {field: title, header: 用例名称}
    - {field: priority, header: 等级}
    - {field: actions, header: 执行步骤}
    - {field: expected, header: 预期结果}
```

支持字段见 core/config.py 的 ExcelColumn：title、external_id、preconditions、module_path、actions、expected、tags、remarks、priority、product_version、edit_mode。未知或重复字段报错。列配置不改变用例事实。

## 来源文本和验证

所有字符串单元格强制为文本，避免 `= + - @` 等来源内容被执行为公式，保留原文显示。测试检查附表、未解析筛选、自定义列和公式文本，原十一列回归继续运行。

openpyxl 回读不等于 Excel 桌面验收；人工应检查列宽、换行和团队导入工具兼容性。
