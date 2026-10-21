"""Единая функция приёма облачной сдачи (#1953, круг 3).

Проверка критика, политика, закрепление sha и смена статуса - одна операция в
пишущей транзакции. Тесты здесь не латают входы по одному, а проверяют именно это:
итоговый статус и доставку (не только колонку), один коммит со статусом и sha и
настоящую конкуренцию на двух соединениях с ожиданием write-lock.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from hub import db as hub_db
from hub import repository as repo
from hub.integrations.protocols import CIProbeOutcome
from hub.services import critic_orders, lifecycle, orchestration
from tests import test_critic_orders as co
from tests.test_pair_merge_gate import _git

world = co.world
keys = co.keys
tip = co.tip
_clean_limiter = co._clean_limiter
_no_cloud = co._no_cloud


async def _row(world, task_id: int) -> dict:
    await world.db.commit()
    return dict(
        (await co._rows(world.db, "SELECT * FROM tasks WHERE id=?", (task_id,)))[0]
    )


async def test_automatic_completion_delivers_the_pr_over_an_old_pin(world, tip):
    """Старый пин B, голова A, passed(A), авто-вход: PR влит, задача completed."""
    git = _git(CIProbeOutcome.passed, merged=True)
    cloud = await co._cloud(world, tip, auto_review=False)
    await repo.update_task(
        world.db, cloud.task_id, submission_sha=co.SHA_B, pr_number=77
    )
    await world.db.commit()
    await co._closed_order(world, cloud, co.SHA_A, "passed")
    tip.sha = co.SHA_A
    row = await _row(world, cloud.task_id)
    outcome = await orchestration.transition_after_agent_done(
        world.db, row, has_done=True
    )
    await world.db.commit()
    final = await _row(world, cloud.task_id)
    assert (outcome, final["status"], final["submission_sha"]) == (
        "completed",
        "completed",
        co.SHA_A,
    )
    assert git.merge_pr.await_count == 1


async def test_pending_report_completes_with_its_sha_in_one_commit(
    world, tip, monkeypatch
):
    """pending_report -> completed: к первому коммиту статус и проверенный sha вместе."""
    cloud = await co._cloud(world, tip, auto_review=False)
    await repo.update_task(
        world.db, cloud.task_id, status="pending_report", submission_sha=co.SHA_B
    )
    await world.db.commit()
    await co._closed_order(world, cloud, co.SHA_A, "passed")
    tip.sha = co.SHA_A
    seen: list[tuple[str, str]] = []
    real = lifecycle.log_activity

    async def _spy(db, *args, **kwargs):
        row = dict(await repo.get_task(db, cloud.task_id))
        seen.append((row["status"], row["submission_sha"]))
        return await real(db, *args, **kwargs)

    monkeypatch.setattr(lifecycle, "log_activity", _spy)
    resp = await co._done(world, cloud)
    assert resp.status_code == 200, resp.text
    completed = [sha for status, sha in seen if status == "completed"]
    assert completed, f"завершение не наблюдено ни одним коммитящим вызовом: {seen}"
    assert set(completed) == {co.SHA_A}, seen
    assert (await _row(world, cloud.task_id))["status"] == "completed"


# --- настоящая конкуренция: два соединения, ожидание write-lock ----------------


async def _policy_writer(
    world, host: str, *, delay: float, hold: float, done: dict
) -> None:
    """Второе соединение: ждёт write-lock, пишет critic_host, держит лок hold секунд."""
    await asyncio.sleep(delay)
    other = await hub_db.connect(world.dsn)
    try:
        await other.execute("BEGIN IMMEDIATE")
        await repo.update_project(
            other,
            world.pid,
            gate_policy=json.dumps({"executor_launch": "manual", "critic_host": host}),
        )
        await asyncio.sleep(hold)
        await other.commit()
        done["policy_at"] = time.monotonic()
    finally:
        await other.close()


def _slow_inside_lock(monkeypatch, seconds: float = 0.5, always: bool = False) -> None:
    """Пауза после проверки критика: под пишущей транзакцией или всегда (always)."""
    real = critic_orders.evaluate

    async def _slow(db, task, sha):
        verdict = await real(db, task, sha)
        if always or db.in_transaction:
            await asyncio.sleep(seconds)
        return verdict

    monkeypatch.setattr(critic_orders, "evaluate", _slow)


async def _enter(world, cloud, entry: str, done: dict, delay: float):
    await asyncio.sleep(delay)
    if entry == "submit":
        result = await co._submit(world, cloud)
        outcome = "refused" if result.status_code == 422 else "accepted"
    else:
        row = await _row(world, cloud.task_id)
        result = await orchestration.transition_after_agent_done(
            world.db, row, has_done=True
        )
        await world.db.commit()
        outcome = "refused" if result == "needs_decision" else "accepted"
    done["entry_at"] = time.monotonic()
    return outcome


@pytest.mark.parametrize("entry", ["submit", "automatic", "automatic_review"])
async def test_policy_committed_first_refuses_the_entry_that_waited(
    world, tip, monkeypatch, entry
):
    """Политика включена и закоммичена до записи входа: вход ждал лок и отказал."""
    cloud = await co._cloud(
        world, tip, critic_host="", auto_review=entry != "automatic"
    )
    _slow_inside_lock(monkeypatch)
    done: dict = {}
    _, outcome = await asyncio.gather(
        _policy_writer(world, "k8", delay=0.0, hold=0.6, done=done),
        _enter(world, cloud, entry, done, delay=0.15),
    )
    assert outcome == "refused"
    assert (await _row(world, cloud.task_id))["status"] in ("running", "needs_decision")


@pytest.mark.parametrize("entry", ["submit", "automatic", "automatic_review"])
async def test_entry_committed_first_is_accepted_and_the_policy_waits(
    world, tip, monkeypatch, entry
):
    """Вход взял лок первым при выключенной политике: принят, включение ждёт его коммита."""
    cloud = await co._cloud(
        world, tip, critic_host="", auto_review=entry != "automatic"
    )
    # Пауза сразу после чтения политики: кто не держит лок, даёт политике обогнать запись.
    _slow_inside_lock(monkeypatch, always=entry.startswith("automatic"))
    done: dict = {}
    _, outcome = await asyncio.gather(
        _policy_writer(world, "k8", delay=0.25, hold=0.0, done=done),
        _enter(world, cloud, entry, done, delay=0.0),
    )
    assert outcome == "accepted"
    assert done["policy_at"] >= done["entry_at"] - 0.05, (
        "политика закоммитилась раньше входа"
    )
    assert (await _row(world, cloud.task_id))["status"] in (
        "review",
        "completed",
        "ci_check",
    )


async def test_an_empty_first_observation_is_refused_not_replaced_by_the_pin(
    world, monkeypatch
):
    """Наблюдение при приёме пусто: отказ head_unobserved, а не прежний sha; bump нет."""
    cloud = await co._cloud(world, None)
    await co._closed_order(world, cloud, co.SHA_A, "passed")
    await _set_pin(world, cloud.task_id, co.SHA_A)

    async def _blink(db, task_id, branch):
        return "", "boom"

    monkeypatch.setattr(lifecycle, "resolve_branch_tip", _blink)
    before = await co._task_state(world, cloud.task_id)
    assert co._refused(await co._done(world, cloud)) == "head_unobserved"
    assert await co._task_state(world, cloud.task_id) == before


async def _set_pin(world, task_id: int, sha: str) -> None:
    await repo.update_task(world.db, task_id, submission_sha=sha)
    await world.db.commit()


async def test_ci_check_to_review_trusts_the_accepted_generation(world, tip):
    """ci_check -> review (dispatch_review): критика нет, принятое поколение не пересматривается."""
    cloud = await co._cloud(world, tip, critic_host="")
    await repo.update_task(
        world.db, cloud.task_id, status="ci_check", submission_sha=co.SHA_A
    )
    await world.db.commit()
    await _policy_writer(world, "k8", delay=0.0, hold=0.0, done={})
    row = await _row(world, cloud.task_id)
    await orchestration.dispatch_review(world.db, row)
    await world.db.commit()
    assert (await _row(world, cloud.task_id))["status"] == "review"
