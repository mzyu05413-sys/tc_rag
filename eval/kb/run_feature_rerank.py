"""按冻结计划顺序运行四组固定池重排，默认仅预览。"""

import argparse
import asyncio
import hashlib
import json
import runpy
from pathlib import Path


async def run(plan_path, output, config, root, live=False, executor=None):
    if output.exists():
        raise ValueError("输出目录已存在，不自动重复付费；先检查已有结果")
    plan = json.loads(plan_path.read_text("utf-8"))
    for path, digest in plan["source_hashes"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise ValueError("计划来源文件已变化")
    jobs = plan["jobs"]
    if len({job["job_id"] for job in jobs}) != len(jobs):
        raise ValueError("任务ID重复")
    for job in jobs:
        if hashlib.sha256(Path(job["pool_path"]).read_bytes()).hexdigest() != job["pool_sha256"]:
            raise ValueError("候选池指纹变化")
    if executor is None:
        executor = runpy.run_path(str(Path(__file__).with_name("rerank_fixed_pool.py")))["execute"]
    # 先对全部组预检；有错误时不先烧掉前几组费用。
    for job in jobs:
        print(f"[预检] {job['job_id']}，{job['query_count']}题")
        await executor(Path(job["pool_path"]), output / job["job_id"], config, root,
                       live=False, tie_policy=plan["tie_policy"])
    if not live:
        print("[预览完成] 未调用模型。实际执行需--live；只请求重排。")
        return
    output.mkdir(parents=True)
    for job in jobs:
        print(f"[执行] {job['job_id']}")
        await executor(Path(job["pool_path"]), output / job["job_id"], config, root,
                       live=True, tie_policy=plan["tie_policy"])
    (output / "execution.json").write_text(json.dumps(dict(
        status="finished_errors_require_separate_reporting",
        plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        jobs=[job["job_id"] for job in jobs]), indent=2), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--config", type=Path, default=Path("workdir/local.yaml"))
    parser.add_argument("--root", type=Path, default=Path("workdir/kb/runtime"))
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.plan, args.output, args.config, args.root, args.live))
