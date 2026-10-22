# tc_rag

将 PDF/DOCX 需求文档转化为可追溯、可评审的规则、测试点和测试用例。

配置加载：`from tc_rag.core.config import load_config`。
优先级：默认值 → YAML → 环境变量 → CLI 覆盖字典。
嵌套环境变量示例：`TC_RAG_RETRIEVAL__MODE=rag`。
密钥只通过 `api_key_env` 引用，不读取到配置对象中。
验证命令：`python -m pytest -q`；代码检查：`ruff check .`。

当前进度：P0.4 计时装饰器与收集器已实现。

## 依赖安装与用途

`pyproject.toml` 是依赖声明的主来源；`requirements/requirements.txt` 和
`requirements/requirements-dev.txt` 是带用途注释的安装清单，改依赖时需要同步更新。
这些文件尚未锁定精确版本；网络可用时生成并保留 `uv.lock`。

```powershell
pip install -r requirements/requirements-dev.txt
pip install -e . --no-deps
pytest -q
```

可选依赖用途见 `requirements/requirements-optional.txt`，按需使用对应 extras 安装。
单独运行源码时，PowerShell 先设置 `$env:PYTHONPATH='src'`。

领域契约和待审核约定见 [P0.3 说明](docs/p0_3_contracts.md)。
计时用法与审核点见 [P0.4 说明](docs/p0_4_timing.md)。
