"""Находки независимого ревью брони перезапуска CI (#1956): каждая - тестом."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hub import repository as repo
from hub import services
from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
    CIRunRequestOutcome,
    CIRunRequestResult,
)
from hub.services import delivery_rerun
from tests.delivery_rerun_support import RUN, TIP, rig_after_base_update

PENDING = CIProbeResult(CIProbeOutcome.pending, "checks_running")
PASSED = CIProbeResult(CIProbeOutcome.passed, "checks_passed")


async def _booked_and_verified(db, monkeypatch):
    """Бронь открыта, attempt 2 зелёная, её отчёт verified."""
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    await rig.sweep()
    rig.start_attempt("2", status="completed", conclusion="success")
    await rig.report_attempt("2")
    return rig


async def test_a_verified_green_does_not_outlive_its_check(db, monkeypatch):
    rig = await _booked_and_verified(db, monkeypatch)
    rig.git.check_pr_ci = AsyncMock(return_value=PENDING)
    ok, _ = await services.merge_before_completion(db, await rig.task())
    assert not ok  # обычное ожидание CI: бронь зелёной не закрыта

    rig.start_attempt("3", status="completed", conclusion="success")  # вручную
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)
    for _ in range(2):
        ok, why = await services.merge_before_completion(db, await rig.task())
        assert not ok, why
    rig.git.merge_pr.assert_not_awaited()
    assert rig.posts == 1


async def test_a_stale_pass_of_an_older_generation_keeps_the_new_booking(
    db, monkeypatch
):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    await rig.sweep()
    assert (await rig.row())["state"] == "requested"

    stale = {**await rig.task(), "submission_generation": 0}  # снимок до пересдачи
    await delivery_rerun.gate_step(db, stale, {"base_branch": "develop"}, PASSED)

    assert (await rig.row())["state"] == "requested", "чужой снимок бронь не снимает"


async def test_an_unreadable_policy_is_not_an_off_switch(db, monkeypatch):
    rig = await _booked_and_verified(db, monkeypatch)
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)
    rig.fake.runs.pop((RUN, "2"))  # отчёта и попытки нет: мерж был бы без проверки
    real = delivery_rerun.policy_mode

    async def unreadable(conn, task_id):
        with monkeypatch.context() as inner:
            inner.setattr(
                repo, "resolve_project_for_task", AsyncMock(side_effect=OSError("boom"))
            )
            return await real(conn, task_id)

    monkeypatch.setattr(delivery_rerun, "policy_mode", unreadable)
    ok, why = await services.merge_before_completion(db, await rig.task())

    assert not ok and why.startswith("fresh_base_ci_failed"), why
    rig.git.merge_pr.assert_not_awaited()
    assert (await rig.row())["outcome"] == "read_failed"


async def test_a_head_the_pass_did_not_check_is_not_rerun(db, monkeypatch):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    other = "someone0pushed0later"
    rig.fake.attempt(
        rig.task_id, other, run_id="999", status="completed", conclusion="failure",
        event="pull_request", artifact=False,
    )  # fmt: skip
    rig.git.pr_head_sha = AsyncMock(return_value=other)

    ok, why = await services.merge_before_completion(db, await rig.task())

    assert not ok and why.startswith("fresh_base_ci_failed")
    assert rig.posts == 0 and TIP[:12] in why


async def test_a_task_without_a_review_is_not_rerun(db, monkeypatch):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    await repo.update_task(db, rig.task_id, auto_review=0, review_verdict=None)
    await db.commit()

    ok, _ = await services.merge_before_completion(db, await rig.task())

    assert not ok and rig.posts == 0, "платный POST требует настоящий APPROVED"


@pytest.mark.parametrize("age", [50])
async def test_the_instruction_names_the_jobs_of_the_attempt_that_ran(
    db, monkeypatch, age
):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.jobs[(RUN, "1")] = [{"name": "старая попытка", "conclusion": "failure"}]
    rig.jobs[(RUN, "2")] = [{"name": "tests (3.12)", "conclusion": "success"}]
    rig.red_first_attempt()
    await rig.sweep()
    rig.start_attempt("2", status="completed", conclusion="success")  # отчёта нет
    await rig.age_request(age)

    ok, why = await services.merge_before_completion(db, await rig.task())

    assert not ok and why.startswith("fresh_base_ci_failed")
    assert "старая попытка" not in why, "устаревшие имена из первой попытки"
    assert (await rig.row())["outcome"] == "timeout"


async def test_a_booking_closed_while_the_pass_waits_on_the_network_blocks_the_merge(
    db, db_dsn, monkeypatch
):
    from hub.db import connect
    from hub.services import orchestration

    rig = await _booked_and_verified(db, monkeypatch)
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)
    other = await connect(db_dsn)
    try:
        real = orchestration.stacking_gate_step

        async def stacking_while_a_neighbour_closes(conn, task):
            # Соседний проход закрыл бронь, пока этот ходил по сети перед мержем.
            await other.execute(
                "UPDATE delivery_reruns SET state='closed', outcome='rerun_not_current'"
            )
            await other.commit()
            return await real(conn, task)

        monkeypatch.setattr(
            orchestration, "stacking_gate_step", stacking_while_a_neighbour_closes
        )
        ok, why = await services.merge_before_completion(db, await rig.task())
    finally:
        await other.close()

    assert not ok and why.startswith("fresh_base_ci_failed"), why
    rig.git.merge_pr.assert_not_awaited()


async def test_a_delivery_of_another_pr_of_the_task_does_not_close_the_booking(
    db, monkeypatch
):
    rig = await _booked_and_verified(db, monkeypatch)
    await repo.record_pipeline_merge(
        db, pr_number=12, merge_sha="old0merge", task_id=rig.task_id
    )
    await db.commit()
    assert (await rig.row())["state"] == "requested", "чужой PR той же задачи"

    await repo.record_pipeline_merge(
        db, pr_number=77, merge_sha="own0merge", task_id=rig.task_id
    )
    await db.commit()
    row = await rig.row()
    assert (row["state"], row["outcome"]) == ("closed", "rerun_green")


async def test_a_snapshot_of_another_pr_cannot_judge_or_close_the_booking(
    db, monkeypatch
):
    rig = await _booked_and_verified(db, monkeypatch)
    old_pr = {**await rig.task(), "pr_number": 12}

    answer = await delivery_rerun.gate_step(db, old_pr, {}, PASSED)

    assert answer.startswith("fresh_base_ci_rerun")
    assert (await rig.row())["state"] == "requested"


@pytest.mark.parametrize("stored", ["{broken", "[1]"], ids=["not-json", "not-a-map"])
async def test_a_corrupt_policy_is_unreadable_not_off(db, monkeypatch, stored):
    rig = await _booked_and_verified(db, monkeypatch)
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE slug='default'", (stored,)
    )
    await db.commit()

    ok, why = await services.merge_before_completion(db, await rig.task())

    assert not ok, why
    rig.git.merge_pr.assert_not_awaited()


async def test_an_unfinished_job_list_is_unread_not_empty(db, monkeypatch):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.post_results = [CIRunRequestResult(CIRunRequestOutcome.declined, "no", "403")]
    rig.jobs[(RUN, "1")] = [
        {"name": "tests (3.12)", "conclusion": ""},
        {"name": "lint", "conclusion": "success"},
    ]
    rig.red_first_attempt()
    await rig.sweep()

    feed = await rig.feed()
    assert "список не прочитан" in feed and "упавших job в списке нет" not in feed
