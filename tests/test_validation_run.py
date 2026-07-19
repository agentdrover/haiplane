"""Tests for running task.validation_commands and recording results (#509)."""

from __future__ import annotations

import json

from hub import repository as repo
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
    monkeypatch.setenv("OPENCLAW_HUB_TOKENS", "s3cret")
    monkeypatch.setenv("MY_API_KEY", "k")
    monkeypatch.setenv("DB_PASSWORD", "p")
    monkeypatch.setenv("SAFE_VAR", "ok")
    env = _safe_env()
    assert "OPENCLAW_HUB_TOKENS" not in env
    assert "MY_API_KEY" not in env
    assert "DB_PASSWORD" not in env
    assert env.get("SAFE_VAR") == "ok"
