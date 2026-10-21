"""Бронь перезапуска CI, круг 3: решение внутри транзакции и headless при любой пробе (#1956)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hub import poller
from hub import repository as repo
from hub import services
from hub.db import connect
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.services import ci_contract, delivery_rerun
from tests.ci_trust_support import SAT_CONTRACT
from tests.delivery_rerun_support import RUN, rig_after_base_update

PASSED = CIProbeResult(CIProbeOutcome.passed, "checks_passed")
PENDING = CIProbeResult(CIProbeOutcome.pending, "checks_running")
FAILED = CIProbeResult(CIProbeOutcome.failed, "checks_failed", details=f"run {RUN}")


async def _verified(db, monkeypatch):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    await rig.sweep()
    rig.start_attempt("2", status="completed", conclusion="success")
    await rig.report_attempt("2")
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)
    return rig


@pytest.mark.parametrize(
    "change",
    ["attempt_three_projection", "version_bump_only", "contract_changed"],
)
async def test_the_decision_is_taken_inside_the_confirming_transaction(
    db, db_dsn, monkeypatch, change
):
    rig = await _verified(db, monkeypatch)
    other = await connect(db_dsn)
    real = delivery_rerun.journal.report_in_time
    fired = []
    calls = []

    async def late_neighbour(conn, task_id, head, *args):
        # Последнее чтение merge_hold позади, транзакции подтверждения ещё нет.
        calls.append(1)
        if len(calls) == 2:  # первый вызов - gate_step, второй - точка мержа
            fired.append(1)
            if change != "contract_changed":
                if change == "attempt_three_projection":  # ключ без сдвига версии
                    await other.execute(
                        "UPDATE ci_run_reports SET attempt_key=?", (f"ci:{RUN}:3",)
                    )
                else:  # чужой писатель сдвинул версию, ключ прежний
                    await repo.bump_ci_state_version(other, task_id, head)
                await other.commit()
            else:
                other_fp = ci_contract.contract_from_raw(
                    SAT_CONTRACT, "sat"
                ).fingerprint
                monkeypatch.setattr(
                    ci_contract,
                    "fingerprint_for_task",
                    AsyncMock(return_value=other_fp),
                )
        return await real(conn, task_id, head, *args)

    monkeypatch.setattr(delivery_rerun.journal, "report_in_time", late_neighbour)
    try:
        ok, why = await services.merge_before_completion(db, await rig.task())
    finally:
        await other.close()

    assert fired, "соседнее изменение не было применено"
    assert not ok, why
    rig.git.merge_pr.assert_not_awaited()


async def test_headless_pending_after_the_deadline_times_out_to_the_owner(
    db, monkeypatch
):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    await rig.sweep()
    rig.git.check_pr_ci = AsyncMock(return_value=PENDING)
    await rig.age_request(46)

    await poller._deliver_approved_review(db, await rig.task())

    assert await rig.needs_decision()
    assert (await rig.row())["outcome"] == "timeout"
    assert "fresh_base_ci_failed" in await rig.feed()
    rig.git.merge_pr.assert_not_awaited()


async def test_headless_failed_new_attempt_ends_as_rerun_red_with_an_instruction(
    db, monkeypatch
):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.jobs[(RUN, "2")] = [{"name": "tests (3.12)", "conclusion": "failure"}]
    rig.red_first_attempt()
    await rig.sweep()
    rig.start_attempt("2", status="completed", conclusion="failure")
    rig.git.check_pr_ci = AsyncMock(return_value=FAILED)

    await poller._deliver_approved_review(db, await rig.task())

    assert await rig.needs_decision()
    assert (await rig.row())["outcome"] == "rerun_red"
    feed = await rig.feed()
    assert "tests (3.12)" in feed and "decide rework" in feed


async def test_a_version_taken_by_a_neighbour_is_not_credited_to_an_older_projection(
    db, db_dsn, monkeypatch
):
    from hub.services import ci_trust

    rig = await _verified(db, monkeypatch)
    other = await connect(db_dsn)
    real = ci_trust.refresh_quietly

    async def refresh_then_the_neighbour_starts_checking(conn, task_id, head, **kw):
        found = await real(conn, task_id, head, **kw)
        # Сосед начал проверку попытки 3: версия сдвинута, проекция ещё прежняя.
        await repo.bump_ci_state_version(other, task_id, head)
        await other.commit()
        return found

    monkeypatch.setattr(
        ci_trust, "refresh_quietly", refresh_then_the_neighbour_starts_checking
    )
    try:
        ok, why = await services.merge_before_completion(db, await rig.task())
    finally:
        await other.close()

    assert not ok, why
    rig.git.merge_pr.assert_not_awaited()


async def test_a_fresh_reservation_without_a_flag_is_not_a_stalled_post(
    db, monkeypatch
):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    await rig.sweep()
    await db.execute("UPDATE delivery_reruns SET state='reserved'")
    await db.commit()
    rig.fake.runs.pop((RUN, "2"), None)
    delivery_rerun.forget_in_flight()

    await rig.sweep()

    assert not await rig.needs_decision()
    assert (await rig.row())["outcome"] == ""


async def test_the_post_flag_is_held_until_the_answer_is_written(db, monkeypatch):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    seen = []
    real = delivery_rerun._after_post

    async def check_flag(db_, task, ctx, ci, scope, row_id, *args):
        seen.append(row_id in delivery_rerun._in_flight)
        return await real(db_, task, ctx, ci, scope, row_id, *args)

    monkeypatch.setattr(delivery_rerun, "_after_post", check_flag)
    await rig.sweep()

    assert seen == [True]
