"""Перезапуск CI на свежей базе: гонки, устаревшее действие, retention (#1956).

Два соединения к файловой базе, ожидание write-лока, оба порядка. Моками здесь
только форж; бронь, транзакции и поллер настоящие.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hub import repository as repo
from hub import services
from hub.db import connect
from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
    CIRunRequestOutcome,
    CIRunRequestResult,
)
from hub.services import orchestration
from tests.delivery_rerun_support import (
    POLICY_KEY,
    RUN,
    Rig,
    rig_after_base_update,
    set_policy,
)

FAILED = CIProbeResult(CIProbeOutcome.failed, "checks_failed", details=f"run {RUN}")


async def _pass(conn, task_id: int):
    """Один проход доставки на СВОЁМ соединении (как у поллера и report_done)."""
    task = dict(await repo.get_task(conn, task_id))
    return await services.merge_before_completion(conn, task)


async def _change(change: str, rig: Rig, other) -> None:
    tid = rig.task_id
    if change == "approval_revoked":
        await repo.update_task(other, tid, review_verdict="changes_requested")
        await other.commit()
    elif change == "policy_off":
        await set_policy(other, **{POLICY_KEY: "off"})
    elif change == "head_moved":
        rig.git.pr_head_sha = AsyncMock(return_value="someone0pushed0later")
    elif change == "generation_bumped":
        await repo.bump_submission_generation(other, tid)
        await other.commit()
    else:  # pragma: no cover
        raise AssertionError(change)


@pytest.mark.parametrize(
    "case",
    [
        "concurrent_first_second",
        "concurrent_second_first",
        "approval_revoked",
        "policy_off",
        "head_moved",
        "generation_bumped",
    ],
)
async def test_at_most_one_rerun_and_no_stale_apply(db, db_dsn, monkeypatch, case):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    other = await connect(db_dsn)
    try:
        started, release = asyncio.Event(), asyncio.Event()

        async def post(run_id, repo=None, gh_repo=None, forge=""):
            started.set()
            if case.startswith("concurrent"):
                await asyncio.sleep(0.15)
            else:
                await release.wait()
            rig.start_attempt("2")
            return CIRunRequestResult(CIRunRequestOutcome.requested, "ok")

        rig.git.rerun_failed_jobs = AsyncMock(side_effect=post)

        if case.startswith("concurrent"):
            from hub.services import delivery_rerun

            winner, loser = (
                (db, other) if case.endswith("first_second") else (other, db)
            )
            barrier = asyncio.Barrier(2)
            real_reserve = delivery_rerun._reserve

            async def meet_then_reserve(conn, *args):
                # Оба прохода прочитали armed и дошли до брони одновременно:
                # исход решает только условный UPDATE, а не порядок чтения.
                await barrier.wait()
                if conn is loser:
                    await asyncio.sleep(0.1)
                return await real_reserve(conn, *args)

            monkeypatch.setattr(delivery_rerun, "_reserve", meet_then_reserve)
            results = await asyncio.gather(
                *(_pass(c, rig.task_id) for c in (winner, loser))
            )
            reasons = sorted(r[1].split(":")[0] for r in results)
            assert reasons == ["fresh_base_ci_rerun", "fresh_base_ci_rerun"]
            assert rig.posts == 1
            row = await rig.row()
            assert row["state"] == "requested"
            rig.git.merge_pr.assert_not_awaited()
            return

        first = asyncio.create_task(_pass(db, rig.task_id))
        await asyncio.wait_for(started.wait(), 10)
        assert (await rig.row())["state"] == "reserved"
        await _change(case, rig, other)
        stale_pass = await _pass(other, rig.task_id)  # второй проход видит перемену
        assert not stale_pass[0]
        release.set()
        late = await first
        assert not late[0], "поздний ответ POST мержа не даёт"

        row = await rig.row()
        assert (row["state"], row["outcome"]) == ("closed", "superseded")
        assert rig.posts == 1
        rig.git.merge_pr.assert_not_awaited()
        again = await _pass(db, rig.task_id)
        assert not again[0] and rig.posts == 1
        assert row["outcome"] == (await rig.row())["outcome"], "исход не переписан"
    finally:
        await other.close()


async def test_a_late_answer_does_not_reopen_a_closed_outcome(db, db_dsn, monkeypatch):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    other = await connect(db_dsn)
    try:
        started, release = asyncio.Event(), asyncio.Event()

        async def post(run_id, repo=None, gh_repo=None, forge=""):
            started.set()
            await release.wait()
            return CIRunRequestResult(CIRunRequestOutcome.requested, "ok")

        rig.git.rerun_failed_jobs = AsyncMock(side_effect=post)
        first = asyncio.create_task(_pass(db, rig.task_id))
        await asyncio.wait_for(started.wait(), 10)
        await other.execute(
            "UPDATE delivery_reruns SET state='closed', outcome='timeout'"
        )
        await other.commit()
        release.set()
        await first
        row = await rig.row()
        assert (row["state"], row["outcome"]) == ("closed", "timeout")
    finally:
        await other.close()


async def test_classification_and_limit_survive_event_retention(db, monkeypatch):
    from hub.services import delivery_rerun

    rig = await rig_after_base_update(db, monkeypatch)
    # Срок жизни событий вышел, хаб перезапущен: памяти процесса и событий нет.
    await db.execute("DELETE FROM events WHERE kind='gate_branch_update'")
    await db.commit()
    orchestration._gate_branch_updates.clear()
    getattr(delivery_rerun, "forget_in_flight", lambda: None)()

    rig.red_first_attempt()
    await rig.sweep()
    assert rig.posts == 1, "сбой распознан как fresh_base по долговечной записи"
    assert (await rig.row())["state"] == "requested"

    rig.start_attempt("2", status="completed", conclusion="failure")
    rig.git.check_pr_ci = AsyncMock(return_value=FAILED)
    await rig.sweep()
    assert await rig.needs_decision()

    await db.execute("DELETE FROM events")
    await db.commit()
    await repo.prune_events(db, keep_days=0)
    getattr(delivery_rerun, "forget_in_flight", lambda: None)()
    orchestration._gate_branch_updates.clear()
    for _ in range(2):
        ok, why = await services.merge_before_completion(db, await rig.task())
        assert not ok and why.startswith("fresh_base_ci_failed")
    assert rig.posts == 1, "лимит учтён по записи, а не по событиям"
    assert len(await rig.rows()) == 1, "запись живёт, пока поколение действует"


async def test_the_origin_is_recorded_only_when_the_policy_is_on(db, monkeypatch):
    rig = await rig_after_base_update(db, monkeypatch, policy="off")
    assert await rig.rows() == []
