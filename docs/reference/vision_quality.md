# 图片转文字：质量优化与独立验收

本次只调整已提取图片的解释，不调整规则提炼、两轮 RAG、测试点、用例或导出规则，不恢复 OCR 扫描。工程检查通过与真实识别准确是两件事；当前真实效果仍待逐图验收。

## 1. 现在如何处理一张图片

```mermaid
flowchart TD
    A[解析器已提取的原图与来源定位] --> B[校正方向与透明背景]
    B --> C[总览图；大图另带原像素重叠局部图]
    D[同章节前后正文；保留原文区间] --> E[第一遍视觉识别]
    C --> E
    E --> F[Schema 与视图、事实、正文引用检查]
    F --> G[第二遍视觉复核；重新查看同一组图片]
    C --> G
    D --> G
    G --> H[最终结构与引用检查]
    H --> I[可读解释与完整审计数据；仍标记 origin=vision]
    H -->|失败| J[保留原图并记录未解析问题；不接受无效草稿]
```

1. **保留细节**：旧正式装配使用最长边 1600 像素的单图。现在总览最长边默认 2048；大图再提供原像素局部图，边长 1536、重叠 192。总览与全部局部在同一次多图请求中提供，避免单独识别后拼接时丢失关系。小图不放大，不伪造清晰度。
2. **带上正文**：同文档、同章节，默认读取图片前后各 2 个原文块；总计最多 6000 字符，平均分配，按原顺序提供。每段带 Block、Locator、字符范围、截断标记。跨章节停止，其他图片解释不作为原文背景。
3. **分类识别**：界面关注字段、必填标记、可见约束、按钮和状态；流程图关注节点、箭头、分支、条件和回路；表格关注表头、行列及合并关系。只描述可见或正文明确支撑的信息。
4. **分开保存**：可见事实 `facts`、图中关系 `relations`、表格 `tables`、正文关联 `context_links`、不确定项 `uncertainties`。每项关联具体视图或已有事实；正文关联另引用正文 ID，不能冒充图片文字。
5. **再次看图核对**：默认再调用一次视觉模型，核对数字、否定词、分支、遗漏与无依据推断。第一遍 JSON 不合法也允许在这次复核重建；默认复核失败仍判本图失败。可显式选择保留有效草稿，约束见第八节，不无限修复。
6. **保留审计与缓存边界**：保存草稿、最终结构、视图坐标、正文和复核标记。图片、上下文、模型、处理参数、两份提示词任一变化都会影响缓存身份；旧缓存不删除，也不会被当作新版本结果命中。

静态界面不能证明点击后的结果、角色独占权限、隐藏校验或后台行为。例如可见 `0/300` 不能自行认定为“最多 300 个汉字”。这类缺口必须保留为疑点，不能为了“业务逻辑丰富”补造事实。模型复核也可能重复第一遍的错误。

## 2. 配置与请求成本

保留 `workdir/local.yaml` 原有 `models.vision`、`parsing.vision_enabled` 和运行限流配置。如需显式调整，把以下内容**合并到已有 parsing 节点中**，不要添加第二个同名节点：

```yaml
parsing:
  vision:
    max_edge: 2048        # 总览最长边；小图不放大
    tile_edge: 1536       # 大图原像素局部边长
    tile_overlap: 192     # 局部图重叠；必须小于 tile_edge 的一半
    max_tiles: 16         # 局部图最多数量；不含总览
    detail: high         # 传给兼容多图接口的图像细节参数
    context_blocks: 2    # 同章节前、后各取多少个原文块
    context_chars: 6000  # 正文总字符预算，不是 token
    verify: true        # 默认进行第二次视觉复核
    verify_failure: fail # 可选 keep_draft；默认行为不变
    min_edge: 64         # 最长边小于该值跳过；0 关闭尺寸过滤
    auto_fact_threshold: 20 # 仅可选 auto 模式使用
```

这些是程序默认值，不需要为了生效重复填写。正式流程已装配新版；LLM、embedding、rerank 的配置不因此改变。`models.vision` 必须支持 Chat Completions 多图输入，不能只支持单图或纯文本。

默认完整复核、无重试、未命中缓存且未过滤时，每张图 **2 次请求**，6 张图约 12 次请求；两次都发送图片和上下文，所以输入、输出成本都会增加。局部图不是各自一轮请求，但会增加多图输入量。两遍都受原有并发、间隔、限流冷却、超时、重试和调用预算约束；重试额外计数。没有实际定价与账单时费用保持 `null`。

局部图超过 `max_tiles` 时报告 `IMAGE_VIEW_BUDGET`，不会只取前几块而漏掉图尾。可在确认服务商多图数量/像素限制后调整参数，或提供拆分清晰图片；默认最多 16 张局部加 1 张总览。若接口拒绝多图，不自动退回低质量单图。扫描页仍沿用现有未解析告警；本功能不渲染整页或新增 OCR。

## 3. 独立验收：只跑图片转文字

虚拟环境已激活、当前目录在仓库根目录时使用以下命令。`rag` 与 `tc-rag` 等价；未安装入口时可用 `python -m tc_rag.interfaces.cli` 替换 `rag`。

### 第一步：免费离线准备

```powershell
rag vision --run-id run_a188817f8471418991fa3d3556b77e39 --limit 6 --output-dir workdir/diagnostics/vision
```

该运行对应之前的《CW-流程审批全过程可评论_文字版.docx》，复用已保存的原图及 `chunk` 快照，不重新解析文档。更换材料时换成已有且包含图片与快照的运行 ID。

默认不创建模型客户端、不发送图文、不运行规则提炼/向量化/重排/测试点/用例生成。它会验证资产校验和、生成实际将发送的总览与局部图，展示正文、原图和历史解释。没有 `chunk` 快照或没有图片会明确失败。离线“本轮解释”为空是正常现象。

### 第二步：确认付费后先测一张

下面命令是**真实调用**，会把所选图片及邻近正文发送到当前 `models.vision` 的服务地址。只有决定付费验证时执行；本次开发没有替你执行。

```powershell
rag vision --run-id run_a188817f8471418991fa3d3556b77e39 --limit 1 --live --output-dir workdir/diagnostics/vision
```

先检查接口是否支持多图、是否截断输出、两遍是否完成。确认后，将 `--limit 1` 改为 `--limit 6` 测完六张；去掉 `--limit` 才会处理该运行全部图片。每次命令生成独立目录，不覆盖上一轮结果。独立验收不共享主运行的视觉缓存，因此重复执行 `--live` 会再次产生费用。

### 第三步：打开页面逐图核对

命令打印 `.../vision_check_<id>/review.html`。用浏览器打开，对照原图、历史解释、本轮解释，展开结构化数据检查来源；选择“通过/需调整”，填写具体错漏，最后点击“导出人工审核 JSON”。页面无外部脚本，不把标注上传。请导出 JSON 留档，不仅依赖浏览器本地缓存。

| 产物 | 用途 |
|---|---|
| `review.html` | 原图、历史/本轮解释、局部图链接、人工结论 |
| `results.json` | 参数、来源、模型调用数量、耗时、usage、每张图的结构与结果 |
| `images/001/...` | 原图副本、总览与全部局部图；可核对右侧和底部是否完整 |
| `vision.sqlite` | 仅真实验收时生成；独立调用记录与视觉结果，不写主业务库 |
| 浏览器下载的 `vision_review.json` | 人工结论及问题说明；未审核项与通过项分开 |

主库 `workdir/data.sqlite` 只读。验收输出留在私有 `workdir`，不要提交真实需求与图片。

## 4. 怎样判断质量改善

历史解释只作问题定位参考，不能当作严格对照实验：当时模型、配置、提示词可能不同。比较时固定原图、正文、模型及输出额度，并保存各轮 `results.json`；比较事实内容，不能用“文字更多”作为质量指标。

建议每张图按以下顺序核对，并记录分母及具体错误：

| 维度 | 核对方法 | 建议验收门槛（目标，不是已测结果） |
|---|---|---|
| 可见文字与数值 | 字段、标记、数字、单位、否定词逐项对照原图 | 关键数字、条件、否定词错误为 0 |
| 业务关系完整性 | 对照明确可见的流程边、条件、状态、表格行；正文关系单独核对 | 关键分支和约束遗漏为 0 |
| 无依据推断 | 查是否把静态按钮写成已发生行为、把一般权限写成独占权限 | 无依据确定断言为 0 |
| 来源可追溯 | 每个视图/事实/正文引用可定位；正文解释没有冒充图片原文 | 程序引用检查全通过，人工来源混淆为 0 |
| 不确定性 | 模糊文字、歧义连线、缺少条件是否明确标记 | 不用猜测替代“不确定” |
| 工程可靠性 | 局部图覆盖全图、超预算显式失败、有效结果才缓存 | 不静默漏图；失败有记录 |

如需量化：`关键事实覆盖率 = 正确识别的人工标记关键事实数 / 原图全部已标记关键事实数`；`断言准确率 = 人工认定有依据且正确的断言数 / 本轮全部待核对断言数`。没有标注分母时不填写百分比；不能把 Schema 成功率当业务准确率。未核对、看不清、存在争议的项单列，不能自动算正确。

可先填此表，一次审完六张：

| 图片 | 关键事实总数 | 正确覆盖数 | 错字/数字/否定错误 | 关键遗漏 | 无依据断言 | 来源混淆 | 结论 |
|---|---|---|---|---|---|---|---|
| 1～6（逐图填写） | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 | 待审核 |

现有六张主要覆盖界面图；还应补充至少一张长流程图、一张密集表格和一张模糊/歧义图。合成像素测试只能证明切图不漏像素，不能证明真实模型识别出全部流程逻辑。

## 5. 数据与代码审核入口

正式运行仍使用原有 `rag_records` 的 `kind=vision` JSON 载荷；原有 `text/model/version` 保留，新增可选 `details` 包含草稿、结构化最终解释、正文、视图坐标、`draft_valid`、`model_verified`、`human_verified=false`。没有新增数据库列或依赖。

`document_blocks` 中派生文字仍是 `origin=vision`，保留图片 Locator、模型和版本，始终带不确定性。下游继续读取可读解释；没有修改下游 Schema、业务规则或导出格式。更丰富的视觉输入可能影响后续提炼数量与内容，这是需要后续验收的输入质量变化，不能声称与旧模型输出逐字相同。

关闭 `runtime.cache_enabled` 时不读取旧结果，但新版结构化视觉审计仍然保存，避免验收资料随缓存开关丢失；旧端口无结构化审计时保留原语义。

| 文件 | 本轮作用 |
|---|---|
| `src/tc_rag/core/config.py` | 类型化视觉质量配置与范围校验 |
| `src/tc_rag/domain/vision.py`、`domain/rag.py` | 视觉事实/关系/审计模型与已有缓存载荷兼容 |
| `src/tc_rag/application/ports/contracts.py` | 视觉结果可携带结构化审计数据 |
| `src/tc_rag/application/services/vision_content.py` | 相邻正文与可读解释组织 |
| `src/tc_rag/application/services/vision.py` | 上下文参与缓存、保留来源、保存审计 |
| `src/tc_rag/infrastructure/providers/vision_images.py` | 像素保真、重叠切图与覆盖限制 |
| `src/tc_rag/infrastructure/providers/vision.py` | 多图识别、视觉复核、最终校验及原有请求治理 |
| `src/tc_rag/bootstrap.py` | 正式流程与独立验收共用视觉装配 |
| `src/tc_rag/interfaces/vision_check.py`、`interfaces/cli.py` | `vision` 独立命令，默认离线 |
| `src/tc_rag/interfaces/progress.py` | 独立视觉流程可显示自己的阶段说明；原流程默认不变 |
| `src/tc_rag/infrastructure/exporters/vision_review.py` | 私有离线对照页面、JSON 与人工标记 |
| `prompts/vision/v2.md`、`prompts/vision_verify/v1.md` | 识别与复核各自版本化，不改其他阶段提示词 |
| `pyproject.toml` | 安装包包含两份新提示词；未新增依赖 |

新增离线验证覆盖：长宽图全像素覆盖与底部保留、透明背景、预算失败、正文边界与顺序、结构和引用拒绝、复核纠错、调用预算、缓存失效、历史载荷兼容、默认零模型调用、独立数据库与仅视觉调用、页面内容转义。相关测试在 `test_vision_quality.py`、`test_vision_quality_http.py`、`test_vision_check_cli.py`，并保留原适配器回归。

## 6. 审核与回滚

重点审核三件事：是否正确分开“图片可见”和“正文关联”；关键条件有没有漏或被补造；两遍调用的成本与延迟是否可接受。`model_verified=true` 只表示完成模型复核，不表示人工批准或语义已证实。

临时关闭 `parsing.vision.verify` 可以省第二遍费用，但属于质量与成本取舍，不是完整回滚。完整回滚应恢复本次视觉相关代码、默认装配与提示词资源，保持它们成套兼容；不能只把提示词改为自由文本 v1，却继续要求 v2 JSON。旧 v1 文件、历史事实与原图都保留。不要用 `git reset --hard` 覆盖工作区其他修改。

本次未改本地密钥、模型供应商、并发或业务数据库，也没有执行真实模型验收。真实质量、实际费用与真实延迟必须在上述独立验收后填写。

## 7. 本轮验证记录（2026-11-03）

- `python -m pytest -q --basetemp=workdir/dev/tests/vision-quality-verified-final`：652 通过，2 跳过，66.68 秒；默认离线。
- `python -m ruff check .`：通过；`python -m mypy src`：62 个源文件通过。
- `git diff --check`：通过；CLI 主帮助与 `vision --help` 可用；两份新提示词已列入安装资源且文件存在，未声称实际构建 wheel。
- 本机六图[离线对照页面](../../workdir/dev/benchmarks/vision-quality-20261103/vision_check_589761f8fd26421c902418515821e2f4/review.html)与同目录 `results.json` 已生成。该链接只适用于本工作区，产物不纳入 Git。
- 离线结果：6 张图，视图准备失败 0 张，模型调用 0 次；6 张均在总览尺寸内，每图 1 张视图，不需要局部切图。真实新解释尚为空，`business_accuracy=null`。

用户现在可先看该页面的原图、正文与视图计划，再按第三节显式执行一张的 `--live` 验收。真实调用后请审查全部关键文字和条件；本轮没有填写未经测量的质量增益。

## 8. 增量策略与兼容边界

### 响应容错与服务商参数

现在在严格 Schema 校验之前提取 JSON 对象，接受外层 Markdown 围栏及前后说明；JSON 字符串内部的括号正常保留。
不补括号、不接受被截断的对象、不合并多个对象；空串依旧失败。`details.draft` 保留未处理的原始响应。
如果服务商明确支持，可以设置 `models.vision.supports_json_object: true`；仅该端点才发送
`response_format: {type: json_object}`。复核模型有自己的同名能力开关，不跟随主模型隐式开启。
不支持图像 `detail` 字段的兼容服务可设 `parsing.vision.detail: null`，请求体将不发送该字段。

更换视觉服务商后先用第三节的 `--live --limit 1` 命令检查协议。建议按模型文档显式设置
`models.vision.max_output_tokens`，复核模型启用时也设置自己的上限；例如服务确实支持时可从 8192 测起，
不是所有模型都接受这个数值。本轮不擅自修改用户本地额度。复杂表格/流程图截断通常表现为
`INCOMPLETE_OUTPUT`，应检查服务商输出上限，不把截断当作有效 JSON 修复。
离线报告逐图列出视图数量、发送视图总像素与多图兼容性提醒；该提醒不是供应商实际限额。

### 复核失败策略（默认仍为 fail）

- `fail`：复核请求或最终结构失败，本图失败，保持原行为。
- `keep_draft`：只有第一遍结构及引用有效，才允许在复核的 `VisionReadError` 后保留草稿。
  正文前标“未复核”，审计 `model_verified=false`、`verify_error=<固定错误码>`，追加问题；无效草稿不保留。
  价格表等配置错误不按该策略降级。显式保留的有效草稿可能进入缓存；要重新看图复核，可关闭缓存或新开独立验收。

这不是“生成成功即事实正确”；图片证据的 `uncertain=True`、来源定位及后续策略没有削弱。
`VisionAudit` 新增可选 `verify_error`、`review_model`，旧三字段缓存与旧审计 JSON 均可读取。

### 小图与重复图

最长边严格小于 `min_edge` 的图片在缓存、预算前跳过；默认 64，等于 64 时正常处理，设 0 禁用尺寸过滤。
跳过不产生派生 Block，只留 info 问题，不计失败或成功；对应解析占位告警撤销，但不会伪造“已识别”。
尺寸过滤不能识别所有大 logo/装饰图，也可能跳过重要的小符号；有此类业务图片时请降低阈值并人工检查。
报告的 `checksum_occurrences` 是源运行里同图出现次数，汇总的重复校验和计数针对本次选中图片。
本轮不按校验和去重：不同章节的相同图片可能需要不同正文解释。

### 可选 auto 与单遍/双遍对照

默认 `verify=true` 不变。可配置 `verify="auto"`，仅当草稿无效、非 UI、多个视图、存在疑点、
或事实数严格超过 `auto_fact_threshold`（默认 20）时再看第二遍。它是确定性实验选项，并未证明优于固定双遍。
先固定同一组图片、正文、模型与额度，分别测单遍和双遍；不要同时修改提示词或换模型：

```powershell
# 两条都是付费真实请求；仅决定开展真实 A/B 时手动执行。
rag vision --run-id run_a188817f8471418991fa3d3556b77e39 --limit 6 --no-verify --live
rag vision --run-id run_a188817f8471418991fa3d3556b77e39 --limit 6 --verify --live
```

分别保存两轮结果及人工 gold 后比较覆盖、无依据断言、延迟和 usage。独立请求有模型随机性，
差异不能全部归因于复核；必要时重复同组实验。本轮没有执行这些请求，因此不建议默认开启 auto。

### 可选第二视觉模型

默认 `models.vision_review.enabled=false`，不创建第二客户端、不向第二家外发。
决定采用后，仅在私有 `workdir/local.yaml` 显式配置兼容 Chat、多图及密钥；此时**图片和相邻正文会同时发送给第二端点**。
主读使用 `models.vision`，第二遍使用 `models.vision_review`，其响应结构和提示词与同模型复核一致。
两端点的 URL 与模型都相同会在启动时报错；同服务不同模型允许。两者独立限制并发/限流/封禁，
共享调用总预算及 admit 回调，不复制请求治理代码。跨模型请求记录 `stage=vision_review`；
同模型复核仍记录 `stage=vision`。最终 `VisionResult.model` 是实际采用结果的模型，保留草稿时仍为主模型。

```yaml
models:
  vision_review:
    enabled: false                # 先保持关闭；明确决定外发后才设 true
    supports_images: true         # 须按实际服务能力声明
    adapter: openai_sdk
    api_mode: chat_completions
    supports_json_object: false
    base_url: ''                  # 用户选择的复核服务地址
    model: ''
    api_key_env: TC_RAG_VISION_REVIEW_API_KEY
    api_key: null                 # 仅私有配置可填写；不打印、不导出
    max_output_tokens: null
```

也支持扁平环境变量 `TC_RAG_VISION_REVIEW_MODEL` / `TC_RAG_VISION_REVIEW_BASE_URL`，
但仅填写模型或 URL 不会自动启用。禁用角色及新增默认字段不进入配置快照；默认预处理和缓存身份
与增量改动前完全一致，已有结果不会仅因加入开关失效。主动调整过滤、复核策略或启用第二模型则改变相关身份。

## 9. 离线关键事实辅助评分

先人工标记关键事实，私有 `workdir/dev/demo/vision_gold.json` 的格式为：

```json
{"images":{"001":{"must_have":["0/300","必填"],"must_not_assert":["最多300字"]}}}
```

这些只是格式示例，不是替你的图片做业务标注。编号对应同批结果里的图片序号，关键词不能为空或归一化后重复。
给已有真实结果评分，**完全离线且不读取模型配置**：

```powershell
rag vision --results "workdir/diagnostics/vision/vision_check_实际ID/results.json" --gold "workdir/dev/demo/vision_gold.json"
```

也可在 `--run-id` 验收命令上附加 `--gold`，在报告旁同时生成 `score.json`；不加 `--live` 时没有新解释，
评分记 `unavailable` 与 `recall=null`，不是 0 分。`--results` 不能与 `--live` 或 `--run-id` 同时使用。

评分将关键词与文本做 NFKC 全半角归一化、去空白后子串匹配。
`must_have` 只查事实、关系与表格；`must_not_assert` 只查事实和关系，不查疑点与正文关联。
每个禁止关键词命中算一次；未标注图记 `unlabeled`，标注有但结果没有的图单列，不能悄悄并入分母。
汇总召回只计算有本轮结构化结果的已标注图，同时列出未评分图；不要把局部统计讲成全样本准确率。

每张图片的 `request_stats` 保存请求次数、请求时长合计、输入/输出 token 和调用模型/阶段。
请求时长不包含本地排队与冷却，未知 usage 保持 null；跳过图的真实请求数为 0。
**子串匹配不能判断同义词、否定或完整语义，仍需人工对照原图。**

## 10. 已完成的只读历史诊断与暂不修改的部分

本轮诊断脚本为 `scripts/history/vision_increment_diagnostics.py`；仅读历史 SQLite、原件与本地编码缓存。
编码缓存缺失时 token 留空，不联网下载，不重新运行模型。示例：

```powershell
python scripts/history/vision_increment_diagnostics.py --run-id run_a188817f8471418991fa3d3556b77e39 --output workdir/dev/benchmarks/vision-increment-20261103/diagnostics-word.json
```

实测来源与结论：

| 历史运行 | 统计 | 可得出的结论 |
|---|---|---|
| `run_a188817f8471418991fa3d3556b77e39` | 6 个视觉文字块，194～738 token，0 块超过目标 900；6 条 aggregate 问题涉及 9 个点引用；文本框/嵌入对象 issue 为 0 | 暂无该样本必须拆视觉 Block 的依据；图片证据被聚合确实存在，但不能推断所有点都应生成用例 |
| `run_ee9660935a704e74b081554e27d55e41` | 原件实测 49 页；61 个视觉文字块，5 块超过目标；5 页绘图路径数≥20（2、3、5、25、43 页） | 值得抽查长块与绘图页；路径密集不等于已确认漏图 |

token 口径为本地 `cl100k_base` 的 `tiktoken_approx`，仍不是供应商计费口径。
原始统计保存在 `workdir/dev/benchmarks/vision-increment-20261103/diagnostics-word.json` 与 `diagnostics-pdf.json`。
两份报告都是旧模型解释，不能冒充本次新视觉策略的实测。

旧缓存没有 `image_type` 或 aggregate 删除后没有点→规则链时，类型归为 `unknown`；本轮六条汇总问题均无法可靠分类。
不凭章节名称推定它们属于流程图或表格。后续如需分类策略，应补齐图类型到证据的追溯，再评估：

- 保持 `aggregate`：保守汇总仅图片依据的问题，继续要求补充文字事实。
- 对流程图/表格来源改为 `downgrade`：保留 `needs_context` 点供核对，仍不自动进入可执行用例；
  需讨论类型如何进入 EvidenceSpan 或独立元数据映射，以及历史兼容、混合证据、测试点状态和覆盖口径。

本轮不改 `planning.vision_policy`、EvidenceSpan、派生 Block 粒度，不实现 Word 形状/SmartArt/文本框提取或 PDF 矢量图提取。
文本框告警为 0 不能证明没有形状图；解析器未发现的内容无法靠问题数量证明完整。
这些后续事项需在可复核样本与用户决定后开展。

## 11. 增量交付与审核记录（2026-11-03）

用户已明确选择：默认保持 `verify_failure=fail`。`verify=true`、跨模型复核关闭也保持不变，
本轮没有修改私有 `workdir/local.yaml`，没有真实模型调用、Git 提交或依赖/数据库列变更。

增量文件分工：

| 文件组 | 变化 |
|---|---|
| `core/config.py`、两份公共 YAML、README | 配置开关、部分端点配置默认关闭、默认快照兼容与操作说明 |
| `providers/vision.py`、`vision_images.py` | 可复用请求器、JSON 包装提取、可选降级/auto/跨模型、过滤与参数兼容 |
| `domain/vision.py` | 两个可选审计字段，旧审计仍可读取 |
| `services/vision.py`、`vision_content.py`、`application/progress.py`、`ports/errors.py` | 小图状态、未复核问题、并发调用与图片关联 |
| `services/design/vision_policy.py`、`services/quality/`、`interfaces/progress.py` | 跳过图不再报缺失或伪造成功；成功/失败/跳过分别统计 |
| `services/vision_scoring.py`、`interfaces/vision_check.py`、`exporters/vision_review.py` | 离线评分、单次复核覆盖、重复图/视图/像素/逐图请求统计 |
| `bootstrap.py` | 可选复核角色的启动检查、共享预算与条件式缓存身份 |
| `scripts/history/vision_increment_diagnostics.py` | 历史只读统计，不实现新的图形提取 |
| `tests/fixtures/vision_defaults_before_increment.json` 与相关测试 | 从增量前提交捕获的无密钥默认快照基线；行为与失败路径回归 |

最终验证：

- `python -m pytest -q --basetemp=workdir/dev/tests/vision-increment-final`：**686 通过、2 跳过，67.35 秒**；跳过不计通过，测试默认离线。
- `python -m ruff check .` 与 `python -m mypy src`：通过，类型检查覆盖 63 个源文件。
- 默认快照与增量前完整 JSON 基线相等；默认视觉缓存身份逐项相等。复核端点仅设置模型/URL仍默认关闭；密钥不进入身份与快照。
- HTTP 模拟验证 JSON 包装/截断/空串，降级及预算，auto 触发，第二端点路由和阶段，detail 省略，小图零调用及质检，CLI 复核覆盖，gold 评分及无标注情形。
- 新六图[离线对照页](../../workdir/dev/benchmarks/vision-increment-20261103/vision_check_4ebd9ebc3a174127a53e6528ccf91ea6/review.html)：6 张、跳过 0 张、模型调用 0 次；不是新视觉解释结果。
- 空 gold 的离线评分烟测：6 张均为 `unlabeled`，召回率 `null`；空标注仅供技术烟测，不是正式金标。

待真实验收的项目：识别准确性、true/false/auto 的实际收益与成本、第二供应商多图协议及复核收益、业务相关 gold 标注。
建议先用一张授权图片做显式真实连通检查，再固定同批样本开展单遍/双遍对照；自动模式、第二模型、下游分图类型策略和矢量提取应分别决定，不能用离线绿灯替代业务证据。
