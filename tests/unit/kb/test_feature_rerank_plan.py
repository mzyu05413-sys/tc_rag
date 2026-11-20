import asyncio
import hashlib
import json
import runpy

import pytest

run = runpy.run_path("eval/kb/run_feature_rerank.py")["run"]


def plan(tmp_path, count=2):
    source = tmp_path / "source.json"
    source.write_text("{}", "utf-8")
    jobs = []
    for i in range(count):
        pool = tmp_path / f"pool{i}.json"
        pool.write_text("{}", "utf-8")
        jobs.append(dict(job_id=f"job{i}", query_count=1, pool_path=str(pool),
                         pool_sha256=hashlib.sha256(pool.read_bytes()).hexdigest()))
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(dict(source_hashes={str(source): hashlib.sha256(
        source.read_bytes()).hexdigest()}, jobs=jobs, tie_policy="input_order")), "utf-8")
    return path


def test_preview_never_executes_live_or_creates_output(tmp_path):
    path = plan(tmp_path)
    calls = []
    async def fake(*args, **kwargs):
        calls.append(kwargs)
    output = tmp_path / "output"
    asyncio.run(run(path, output, None, None, executor=fake))
    assert len(calls) == 2 and all(c["live"] is False for c in calls)
    assert all(c["tie_policy"] == "input_order" for c in calls)
    assert not output.exists()


def test_all_groups_prechecked_before_any_live_call(tmp_path):
    path = plan(tmp_path)
    calls = []
    async def fake(pool, *args, **kwargs):
        calls.append(kwargs["live"])
        if pool.name == "pool1.json":
            raise ValueError("invalid second group")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="invalid second"):
        asyncio.run(run(path, output, None, None, live=True, executor=fake))
    assert calls == [False, False] and not output.exists()


def test_changed_pool_rejected_before_executor(tmp_path):
    path = plan(tmp_path)
    (tmp_path / "pool0.json").write_text("changed", "utf-8")
    async def fake(*args, **kwargs):
        pytest.fail("executor must not be called")
    with pytest.raises(ValueError, match="指纹"):
        asyncio.run(run(path, tmp_path / "output", None, None, live=True, executor=fake))


def test_fake_live_execution_and_output_protection(tmp_path):
    path = plan(tmp_path)
    calls = []
    async def fake(pool, output, *args, **kwargs):
        calls.append(kwargs["live"])
        if kwargs["live"]:
            output.mkdir()
    output = tmp_path / "output"
    asyncio.run(run(path, output, None, None, live=True, executor=fake))
    assert calls == [False, False, True, True]
    assert (output / "execution.json").exists()
    with pytest.raises(ValueError, match="不自动重复付费"):
        asyncio.run(run(path, output, None, None, live=True, executor=fake))
