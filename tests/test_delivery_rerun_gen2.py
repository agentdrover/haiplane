"""Бронь перезапуска CI: ОДНА точка мержа, актуальность, off, срок (#1956, поколение 2).

Находки CHANGES_REQUESTED на поколение 1: headless-конвейер мержил мимо брони;
разрешение пережило смену попытки; резерв не сверял идентичность проекта;
закрытая бронь влияла при off; срок считался по времени прохода, не отчёта.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hub import poller
from hub import services
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.services import ci_trust, orchestration
from tests.delivery_rerun_support import (
    POLICY_KEY,
    RUN,
    rig_after_base_update,
    set_policy,
)

PASSED = CIProbeResult(CIProbeOutcome.passed, "checks_passed")
FAILED = CIProbeResult(CIProbeOutcome.failed, "checks_failed", details=f"run {RUN}")


async def _booked(db, monkeypatch):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    await rig.sweep()
    assert rig.posts == 1
    return rig


async def test_the_headless_path_does_not_merge_past_an_open_booking(db, monkeypatch):
    rig = await _booked(db, monkeypatch)
    rig.start_attempt("2", status="completed", conclusion="success")  # отчёта нет
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)

    await poller._deliver_approved_review(db, await rig.task())

    rig.git.merge_pr.assert_not_awaited()
    assert (await rig.row())["state"] == "requested"


async def test_the_headless_path_does_not_merge_past_a_closed_human_outcome(
    db, monkeypatch
):
    rig = await _booked(db, monkeypatch)
    await db.execute("UPDATE delivery_reruns SET state='closed', outcome='rerun_red'")
    await db.commit()
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)
    rig.git.merge_pr_with_detail = AsyncMock(return_value=(True, ""))

    await poller._deliver_approved_review(db, await rig.task())

    rig.git.merge_pr_with_detail.assert_not_awaited()
    rig.git.merge_pr.assert_not_awaited()


async def test_a_manual_attempt_after_the_check_blocks_the_merge(db, monkeypatch):
    rig = await _booked(db, monkeypatch)
    rig.start_attempt("2", status="completed", conclusion="success")
    await rig.report_attempt("2")
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)
    real = orchestration.stacking_gate_step

    async def stacking_while_attempt_three_starts(conn, task):
        rig.start_attempt("3", status="in_progress")  # вручную, на том же sha
        return await real(conn, task)

    monkeypatch.setattr(
        orchestration, "stacking_gate_step", stacking_while_attempt_three_starts
    )
    ok, why = await services.merge_before_completion(db, await rig.task())

    assert not ok, why
    rig.git.merge_pr.assert_not_awaited()


async def test_the_report_of_another_attempt_never_permits_the_merge(db, monkeypatch):
    rig = await _booked(db, monkeypatch)
    rig.start_attempt("2", status="completed", conclusion="success")
    await rig.report_attempt("2")
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)
    real = ci_trust.trusted_report

    async def report_of_attempt_three(conn, task_id, head):
        row = await real(conn, task_id, head)
        return None if row is None else {**dict(row), "attempt_key": f"ci:{RUN}:3"}

    monkeypatch.setattr(ci_trust, "trusted_report", report_of_attempt_three)
    ok, why = await services.merge_before_completion(db, await rig.task())

    assert not ok, why
    rig.git.merge_pr.assert_not_awaited()


@pytest.mark.parametrize("change", ["repo", "forge"])
async def test_a_project_identity_changed_before_the_reservation_means_no_post(
    db, monkeypatch, change
):
    from hub.services import delivery_rerun

    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    real = delivery_rerun._reserve

    async def reserve_after_the_project_moved(conn, *args):
        column = "repo" if change == "repo" else "forge"
        value = "other/repository" if change == "repo" else "gitverse"
        await conn.execute(
            f"UPDATE projects SET {column}=? WHERE slug='default'", (value,)
        )
        await conn.commit()
        return await real(conn, *args)

    monkeypatch.setattr(delivery_rerun, "_reserve", reserve_after_the_project_moved)
    await services.merge_before_completion(db, await rig.task())

    assert rig.posts == 0, "POST ушёл в прежнюю область"


async def test_off_makes_a_closed_booking_inert(db, monkeypatch):
    rig = await _booked(db, monkeypatch)
    await db.execute("UPDATE delivery_reruns SET state='closed', outcome='rerun_red'")
    await db.commit()
    await set_policy(db, **{POLICY_KEY: "off"})
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)

    ok, why = await services.merge_before_completion(db, await rig.task())

    assert ok, why
    assert rig.git.merge_pr.await_count == 1


async def test_a_report_in_time_is_honoured_by_a_late_pass_and_a_late_one_is_not(
    db, monkeypatch
):
    rig = await _booked(db, monkeypatch)
    rig.start_attempt("2", status="completed", conclusion="success")
    await rig.report_attempt("2")
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)
    await db.execute(
        "UPDATE delivery_reruns SET requested_at=datetime('now', '-50 minutes')"
    )
    # Отчёт пришёл на 44-й минуте, проход на 50-й: срок отчёта выдержан.
    await db.execute(
        "UPDATE ci_report_attempts SET reported_at=datetime('now', '-6 minutes')"
    )
    await db.commit()
    ok, why = await services.merge_before_completion(db, await rig.task())
    assert ok, why


async def test_a_report_after_the_deadline_is_late_whenever_the_pass_looks(
    db, monkeypatch
):
    rig = await _booked(db, monkeypatch)
    rig.start_attempt("2", status="completed", conclusion="success")
    await rig.report_attempt("2")
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)
    await db.execute(
        "UPDATE delivery_reruns SET requested_at=datetime('now', '-50 minutes')"
    )
    # Отчёт пришёл на 49-й минуте (после 45), проход на 50-й: поздний отчёт.
    await db.execute(
        "UPDATE ci_report_attempts SET reported_at=datetime('now', '-1 minutes')"
    )
    await db.commit()

    ok, why = await services.merge_before_completion(db, await rig.task())

    assert not ok and why.startswith("fresh_base_ci_failed"), why
    assert (await rig.row())["outcome"] == "timeout"
    rig.git.merge_pr.assert_not_awaited()
