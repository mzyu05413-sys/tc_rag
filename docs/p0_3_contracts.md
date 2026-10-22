# P0.3 领域契约与审核点

实现范围：设计书第 7 节的 P1 对象，包含 Asset 和简化 BusinessEntity。
本步只定义数据契约，不实现数据库、提炼、生成或跨对象引用校验。

## 需审核的约定

- 所有对象携带 `schema_version=1.0`，拒绝未知字段。
- 页码、CoverageLink.step_no 从 1 开始；文档顺序和 DOCX 索引从 0 开始。
- ChunkSource 记录 Block 内字符区间 `[start_char, end_char)`。
  区间是否超出原文长度，要在后续应用层加载 Block 后校验。
- Locator 支持页码、坐标、段落、表格、行列；暂不强制至少一个定位字段，
  以容纳解析不完整时的问题记录，后续引用校验必须识别缺失定位。
- EvidenceSpan 的 OCR/vision 来源必须记录处理模型和版本。
- TestPoint.applicability 使用 applicable / not_applicable / undetermined。
  status 沿用设计书的 ready / needs_context / needs_clarification / not_applicable。
- CoverageLink.validation_status 使用 pending / validated / invalid / needs_review。
  设计书未明确这两组枚举的全部取值，上述为本次补充约定。
- 测试用例必须至少有一步，步骤必须带规则和证据引用；
  `validated` 仅代表自动校验通过，不代表人工批准。
- StructuredConstraint 的未知精度、单位、边界保留 None，不猜测填值。
- ID 使用带前缀 UUID4，与内容无关；fingerprint 使用规范 JSON 的 SHA-256。
  字典键排序、紧凑编码、拒绝 NaN；保留列表顺序、空白和 Unicode 原文，
  不做业务文本归一化，也不会自动排除 ID 等字段。调用方应明确选择参与指纹的内容。
- 模型 usage 未知时用 None，不填 0。

Run.config_snapshot 是数据容器；调用方必须传入配置加载器生成的无密钥快照。
领域对象本身不访问环境变量、不执行 I/O，也不证明引用 ID 存在或语义有依据。

## 验证

累计 37 项测试通过：包括 P0.2 的 10 项，以及本步序列化往返、非法枚举、
区间、来源元数据、步骤证据、指纹、ID 和领域层导入边界检查。
ruff、mypy 尚未运行，当前环境缺少工具；不视为完整质量门禁通过。

下一步为 P0.4：同步/异步计时装饰器与计时收集器。
