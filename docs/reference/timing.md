# 计时与指标参考

[返回参考导航](README.md) · [P0 计时入门](../learn/P0/01_功能架构与流程.md)

## 同步与异步通用计时

`src/tc_rag/infrastructure/runtime/timer.py` 提供 `Timer` 和 `@timed`。使用 `perf_counter`，不记录函数参数、返回内容或异常消息。

```python
import asyncio
from tc_rag.infrastructure.runtime.timer import Timer, timed

@timed(stage="demo", name="work")
async def work():
    await asyncio.sleep(0.1)

async def main():
    timer = Timer("run_demo")
    with timer.wall(stage="run", name="total"):
        await asyncio.gather(*(work() for _ in range(5)))
    print(timer.summary())

asyncio.run(main())
```

单次调用为 `kind=call`，显式包裹区间为 `kind=wall`，分别汇总。上例调用耗时合计约 500ms，墙钟约 100ms，实际取决于调度。`total_ms` 是同组累计，不能当整次运行墙钟。

事件包含 run_id、stage、name、duration_ms、success、kind。异常和取消在 finally 记录失败并继续传播。`bind()` 只激活收集器，`wall()` 同时测量区间；未绑定时装饰器只执行原函数。

ContextVar 隔离不同运行并恢复嵌套绑定，锁保护事件列表。asyncio 子任务继承上下文；手动新建线程须显式传递上下文或重新绑定。

summary 按 stage/name/kind 统计次数、失败数、累计、均值、p50、p95，包含失败调用。百分位用最近秩法，小样本不能据此推断稳定性能。

## 当前运行中另有哪些耗时

| 来源 | 保存内容 | 查看方式 |
|---|---|---|
| 通用 Timer | 内存事件和分组汇总 | `events`、`summary()`、`export_events()`；不自动写库 |
| Pipeline | 阶段耗时及整体运行记录 | SQLite progress / manifest 快照与 status 命令 |
| 模型适配器 | 每次实际请求耗时、usage、错误类型 | model_invocations 与 invocations 快照 |
| RagContext | 每次查询的向量化、候选搜索、重排、上下文组装 | retrieval 快照中的 timings_ms |

```powershell
tc-rag status RUN_ID --config workdir/local.yaml
tc-rag inspect RUN_ID --stage retrieval --config workdir/local.yaml
tc-rag inspect RUN_ID --stage manifest --config workdir/local.yaml
```

检索字段为 query_embedding_ms、candidate_search_ms、rerank_ms、context_build_ms；组装时间包含补充证据预算处理。缓存命中的耗时仍记录，但不计为新模型调用。并发请求累计耗时和阶段墙钟要分别解释，未知 usage/费用不能推成零。

`tests/unit/test_timer.py` 覆盖同步/异步、异常/取消、隔离、并发墙钟、百分位及空汇总；检索与运行指标由 RAG 集成测试验证。`scripts/bench_similarity.py` 提供固定数据的算法基准，不代表实际网络模型性能或业务准确率。
