"""Доработка по ревью #1991: устаревшая попытка, чужая метка, выход через #1362.

Помощники гонок и подмены сети - в ``test_rework_dispatch_once``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub import services
from hub.integrations.registry import plugins
from tests.test_decision_brief import _task_in
from tests.test_rework_dispatch_once import (
    REWORK,
    _age_reservations,
    _crash_on_submit,
)


async def test_a_stale_attempt_does_not_submit_after_its_booking_was_closed(
    client, db, monkeypatch
):
    """Попытка 1 встала после коммита брони; сверка закрыла её, человек повторил."""
    from hub.services import lifecycle, rework_launch

    sent: list[str] = []

    async def submit(message: str, **_kwargs: Any) -> dict[str, Any]:
        sent.append(message)
        return {"job_id": f"job-{len(sent)}"}

    monkeypatch.setattr(plugins.dispatch, "submit_task", submit)
    monkeypatch.setattr(plugins.dispatch, "list_jobs", lambda limit=50: [])
    entered, go, real, first = asyncio.Event(), asyncio.Event(), None, [True]
    real = lifecycle._dispatch_rework

    async def stalled(*args: Any, **kwargs: Any) -> None:
        if first[0]:
            first[0] = False
            entered.set()
            await asyncio.wait_for(go.wait(), 10)
        return await real(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_dispatch_rework", stalled)
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    attempt = asyncio.create_task(
        client.post(f"/api/tasks/{task_id}/decide", json=REWORK)
    )
    await asyncio.wait_for(entered.wait(), 10)
    await _age_reservations(db, task_id)
    await rework_launch.sweep(db)

    second = await client.post(
        f"/api/tasks/{task_id}/decide", json={**REWORK, "confirm_no_job": True}
    )
    go.set()
    await asyncio.wait_for(attempt, 10)

    assert second.status_code == 200, second.text
    assert len(sent) == 1, "устаревшая попытка запустила второе задание"
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "fix_requested" and task["job_id"] == "job-1"


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


async def test_an_attempt_holding_the_right_to_submit_is_not_swept(
    client, db, monkeypatch
):
    """Бронь состарилась, пока попытка стояла; взяв право, она держит бронь от сверки."""
    from hub.services import lifecycle, rework_launch

    in_net, release = asyncio.Event(), asyncio.Event()
    sent: list[str] = []

    async def submit(message: str, **_kwargs: Any) -> dict[str, Any]:
        sent.append(message)
        in_net.set()
        await asyncio.wait_for(release.wait(), 10)
        return {"job_id": "job-1"}

    monkeypatch.setattr(plugins.dispatch, "submit_task", submit)
    monkeypatch.setattr(plugins.dispatch, "list_jobs", lambda limit=50: [])
    entered, go, real = asyncio.Event(), asyncio.Event(), lifecycle._dispatch_rework

    async def stalled(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await asyncio.wait_for(go.wait(), 10)
        return await real(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_dispatch_rework", stalled)
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    attempt = asyncio.create_task(
        client.post(f"/api/tasks/{task_id}/decide", json=REWORK)
    )
    await asyncio.wait_for(entered.wait(), 10)
    await _age_reservations(db, task_id)
    go.set()
    await asyncio.wait_for(in_net.wait(), 10)

    await rework_launch.sweep(db)

    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["reserved"]
    release.set()
    assert (await asyncio.wait_for(attempt, 10)).status_code == 200
    assert (await repo.get_task(db, task_id))["job_id"] == "job-1"


@pytest.mark.parametrize("accepted", [False, True], ids=["no-job", "job-found"])
async def test_a_sweep_snapshot_does_not_close_a_booking_renewed_after_it(
    db, monkeypatch, accepted
):
    """Сверка выбрала бронь, затем попытка взяла право заказать: сверка отступает."""
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=accepted)
    await _age_reservations(db, task_id)
    rows = await db.execute_fetchall(
        "SELECT * FROM rework_launches WHERE task_id=?", (task_id,)
    )
    snapshot = dict(rows[0])
    if accepted:
        jobs["job-77"]["message"] = jobs["calls"][0]
    # Тот же срок в секундах, но уже новый: право попытки продлило бронь.
    await db.execute(
        "UPDATE rework_launches SET reserved_at=datetime('now') WHERE id=?",
        (snapshot["id"],),
    )
    await db.commit()

    await rework_launch._reconcile(db, snapshot)

    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["reserved"]
    assert not dict(await repo.get_task(db, task_id))["job_id"]
