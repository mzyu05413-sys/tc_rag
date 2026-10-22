# P0.4 计时与审核点

`infrastructure/runtime/timer.py` 提供 `Timer` 和 `@timed`。
无需新增依赖，不采集函数参数、返回内容或异常消息。

```python
import asyncio
from tc_rag.infrastructure.runtime.timer import Timer, timed

@timed(stage="demo", name="work")
async def work():
    await asyncio.sleep(0.1)

async def main():
    timer = Timer("demo_run")
    with timer.wall(stage="run", name="total"):
        await asyncio.gather(*(work() for _ in range(5)))
    print(timer.summary())

asyncio.run(main())
```

## 需要审核的点

- 单次调用标记 `kind=call`，显式墙钟区间标记 `kind=wall`，分别汇总。
  上例通常累计单次耗时约 500ms、墙钟约 100ms，具体数值取决于机器调度。
  `total_ms` 是同一分组的累计时长，不能当整个运行的墙钟时间。
- 每个事件包含 run_id、stage、name、duration_ms、success、kind。
- 使用 `perf_counter`，同步和 async 都在 finally 记录；异常和取消继续向上传播。
- `bind()` 只绑定收集器；`wall()` 还会测量包裹区间，并自动绑定。
  没有绑定 Timer 时，装饰器直接执行，不产生事件。
- 使用 ContextVar 隔离并发运行，并恢复嵌套绑定；事件列表由锁保护。
  asyncio 子任务继承绑定；手动新建线程需要调用方传递上下文或在该线程绑定。
- 统计包含失败调用，按 stage/name/kind 分组；输出次数、失败次数、累计、均值、
  p50、p95。百分位采用最近秩法，小样本仍显示样本量，不推断统计可靠性。
- 当前为内存收集，尚未接入 SQLite、CLI 或日志；task_id、attempt_id 等随后续编排扩展。

测试覆盖同步、异步、异常、取消、上下文隔离、并发墙钟、百分位精确值与空汇总。
下一步 P0.5 为样例需求与人工标注，需要用户提供可用于评审的脱敏文档。
