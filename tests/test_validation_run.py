"""Tests for running task.validation_commands and recording results (#509)."""

from __future__ import annotations

import asyncio
import json

from hub import repository as repo
from hub.services import validation_run
from hub.services.validation_run import (
    FAIL,
    PASS,
    SKIPPED,
    _safe_env,
    run_validation_commands,
)


async def _task(db, *, commands):
    task_id = await repo.create_task(
        db,
        title="t",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.bump_submission_generation(db, task_id)  # generation 1
    await repo.update_task(db, task_id, validation_commands=json.dumps(commands))
    await db.commit()
    return task_id


async def test_run_validation_records_pass_on_generation(db):
    # AC-1 (#509): commands executed, result recorded on the current generation.
    task_id = await _task(db, commands=["echo ok"])

    async def fake(cmds, repo_path):
        return (0, "all good")

    res = await run_validation_commands(db, task_id, runner=fake)
    assert res == {"status": PASS, "generation": 1}
    row = dict(await repo.get_task(db, task_id))
    assert row["validation_status"] == "pass"
    assert row["validation_generation"] == 1


async def test_run_validation_records_fail(db):
    task_id = await _task(db, commands=["false"])

    async def fake(cmds, repo_path):
        return (1, "boom")

    res = await run_validation_commands(db, task_id, runner=fake)
    assert res["status"] == FAIL
    assert dict(await repo.get_task(db, task_id))["validation_status"] == "fail"


async def test_run_validation_skipped_without_commands(db):
    # AC-3 (#509): no commands → skipped, nothing recorded, no error.
    task_id = await _task(db, commands=[])

    async def fake(cmds, repo_path):
        raise AssertionError("runner must not be called when there are no commands")

    res = await run_validation_commands(db, task_id, runner=fake)
    assert res["status"] == SKIPPED
    assert dict(await repo.get_task(db, task_id))["validation_status"] is None


def test_safe_env_strips_secrets(monkeypatch):
    # AC-2 (#509): secret-looking env vars never reach the child environment.
    monkeypatch.setenv("HAIPLANE_HUB_TOKENS", "s3cret")
    monkeypatch.setenv("MY_API_KEY", "k")
    monkeypatch.setenv("DB_PASSWORD", "p")
    monkeypatch.setenv("SAFE_VAR", "ok")
    env = _safe_env()
    assert "HAIPLANE_HUB_TOKENS" not in env
    assert "MY_API_KEY" not in env
    assert "DB_PASSWORD" not in env
    assert env.get("SAFE_VAR") == "ok"


async def test_runner_kills_timed_out_command(monkeypatch, tmp_path):
    # The timed-out child used to survive: wait_for cancels only the read, and
    # every retry leaked another live process into the workspace.
    #
    # The trailing `; :` matters (#544). A shell handed one simple command may
    # exec it, making the payload the very pid we signalled — which is why the
    # first version of this test passed on macOS while the leak was still wide
    # open on the Linux production host. A compound command forces sh to fork,
    # so the payload is a grandchild and only a process-group kill reaches it.
    monkeypatch.setattr(validation_run, "_RUN_TIMEOUT", 0.3)
    marker = tmp_path / "still_alive"
    payload = (
        f'python3 -c "import time,pathlib;'
        f"time.sleep(1.5);pathlib.Path(r'{marker}').write_text('x')\""
    )
    cmd = f"{payload}; :"
    assert await validation_run.default_validation_runner([cmd], str(tmp_path)) is None
    await asyncio.sleep(2.0)
    assert not marker.exists(), "timed-out command kept running after the hub gave up"


async def test_runner_caps_output_in_memory(monkeypatch, tmp_path):
    # A chatty command must not be buffered whole: the cap bounds what we keep,
    # the rest is drained and counted.
    monkeypatch.setattr(validation_run, "_MAX_OUTPUT", 1000)
    cmd = "python3 -c \"print('x' * 200000)\""
    result = await validation_run.default_validation_runner([cmd], str(tmp_path))
    assert result is not None
    rc, log_tail = result
    assert rc == 0
    assert "bytes dropped" in log_tail
    assert len(log_tail) <= validation_run._LOG_TAIL


async def test_a_cancelled_validation_kills_its_group_before_the_cancel_goes_on(
    tmp_path,
):
    # #1650 review: only OSError and TimeoutError were caught, so a cancelled
    # request left the command (and what it spawned) running.
    import asyncio
    import contextlib
    import os
    import time

    from hub.services.validation_run import default_validation_runner

    pid_file = tmp_path / "child.pid"
    command = f"sleep 300 & echo $! > {pid_file}; wait"
    task = asyncio.create_task(default_validation_runner([command], str(tmp_path)))
    try:
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        pid = int(pid_file.read_text())
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.cancelled(), "the cancellation must be passed on"
        deadline = time.monotonic() + 3
        alive = True
        while alive and time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
                await asyncio.sleep(0.05)
            except ProcessLookupError:
                alive = False
        assert not alive, "the validation command's group outlived the cancel"
    finally:
        if pid_file.exists() and pid_file.read_text().strip():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 9)
