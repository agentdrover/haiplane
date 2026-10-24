"""Доработка по ревью #1991: устаревшая попытка, чужая метка, выход через #1362.

Помощники гонок и подмены сети - в ``test_rework_dispatch_once``.
"""

from __future__ import annotations


import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub import services
from tests.test_rework_dispatch_once import (
    _age_reservations,
    _crash_on_submit,
)


async def _booking_id(db, task_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT MAX(id) FROM rework_launches WHERE task_id=?", (task_id,)
    )
    return int(rows[0][0])


async def test_a_marker_quoted_in_a_foreign_job_is_not_ours(db, monkeypatch):
    """Чужое задание цитирует метку нашей брони в тексте: привязки нет."""
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=False)
    rid = await _booking_id(db, task_id)
    jobs["job-B"] = {
        "id": "job-B",
        "message": f"описание задачи B цитирует {rework_launch.marker(rid)} в тексте\n"
        "и дальше своя работа\n(метка заказа rework-launch:99, задача #7)",
    }
    await _age_reservations(db, task_id)

    await rework_launch.sweep(db)

    task = dict(await repo.get_task(db, task_id))
    assert not task["job_id"] and task["status"] == "needs_decision"
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["needs_human"]


async def test_the_own_job_wins_over_a_foreign_quote_listed_before_it(db, monkeypatch):
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=False)
    rid = await _booking_id(db, task_id)
    jobs["job-B"] = {
        "id": "job-B",
        "message": f"цитата {rework_launch.marker(rid)} посреди текста\nконец B",
    }
    jobs["job-A"] = {"id": "job-A", "message": jobs["calls"][0]}
    await _age_reservations(db, task_id)

    await rework_launch.sweep(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["job_id"] == "job-A" and task["status"] == "fix_requested"


async def test_two_jobs_with_the_same_marker_are_ambiguous(db, monkeypatch):
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=False)
    jobs["job-A"] = {"id": "job-A", "message": jobs["calls"][0]}
    jobs["job-A2"] = {"id": "job-A2", "message": jobs["calls"][0]}
    await _age_reservations(db, task_id)

    await rework_launch.sweep(db)

    task = dict(await repo.get_task(db, task_id))
    assert not task["job_id"] and task["status"] == "needs_decision"
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["needs_human"]


async def test_resubmission_is_refused_while_an_unproven_outcome_stands(
    db, monkeypatch
):
    """#1362: пересдача из needs_decision не уводит задачу от неснятого needs_human."""
    from hub.models import TaskSubmitReview
    from tests.test_lifecycle import _conflicted_after_approval

    task_id = await _conflicted_after_approval(db, monkeypatch)
    await db.execute(
        "INSERT INTO rework_launches (task_id, state) VALUES (?, 'needs_human')",
        (task_id,),
    )
    await db.commit()
    before = dict(await repo.get_task(db, task_id))

    with pytest.raises(HTTPException) as refused:
        await services.submit_for_review(
            db, task_id, TaskSubmitReview(agent="dev", branch=before["branch"] or "")
        )

    assert refused.value.status_code == 409
    assert "confirm_no_job" in refused.value.detail
    assert (await repo.get_task(db, task_id))["status"] == "needs_decision"


async def test_a_closed_booking_gives_no_right_to_submit(db, monkeypatch):
    from hub.services import rework_launch

    task_id, _ = await _crash_on_submit(db, monkeypatch, accepted=False)
    rid = await _booking_id(db, task_id)
    assert await rework_launch.claim_submit(db, rid)

    await db.execute("UPDATE rework_launches SET state='closed' WHERE id=?", (rid,))
    await db.commit()

    assert await rework_launch.claim_submit(db, rid) is None
