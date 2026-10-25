"""Заказ машинного ревью после сдачи не держит HTTP-ответ (#2057)."""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from hub import models
from hub import repository as repo
from hub.services import lifecycle, review_dispatch, review_order
from tests.test_submit_expected_sha import X, _install_git, _running_task


@pytest.fixture
def detached(monkeypatch, db_dsn):
    monkeypatch.setattr(review_order, "DETACH_ENABLED", True)
    monkeypatch.setattr(review_order, "RETRY_PAUSE_SECONDS", 0.0)
    token = review_order.detach_dsn.set(db_dsn)
    yield
    review_order.detach_dsn.reset(token)


async def _alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    await db.commit()
    return [
        u["content"]
        for u in (dict(r) for r in await repo.get_task_updates(db, task_id))
        if u["kind"] == "alert" and "Заказ ревью" in u["content"]
    ]


async def test_a_slow_order_does_not_hold_the_submit_and_is_ordered_once(
    db, monkeypatch, detached
):
    _install_git(monkeypatch, X)
    release = asyncio.Event()
    calls: list[int] = []

    async def slow(_db, task_id, **_kw):
        calls.append(task_id)
        await release.wait()
        return True

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", slow)
    task = await _running_task(db, "Медленный заказ")
    body = models.TaskSubmitReview(agent="dev")

    first = await asyncio.wait_for(
        lifecycle.submit_for_review(db, task.id, body), timeout=5
    )
    assert first.status == "review" and first.submission_generation == 1
    assert review_order.inflight_count() == 1, "заказ ушёл из запроса"

    # Повтор того же sha: то же поколение, ни новой сдачи, ни второго заказа.
    again = await asyncio.wait_for(
        lifecycle.submit_for_review(db, task.id, body), timeout=5
    )
    assert again.submission_generation == 1
    cur = await db.execute(
        "SELECT COUNT(*) FROM submissions WHERE task_id=?", (task.id,)
    )
    assert (await cur.fetchone())[0] == 1

    release.set()
    await review_order.drain()
    assert calls == [task.id], "один заказ на (задача, поколение)"


async def test_a_failed_order_is_retried_and_then_named(db, monkeypatch, detached):
    _install_git(monkeypatch, X)
    attempts: list[int] = []

    async def flaky(_db, task_id, **_kw):
        attempts.append(task_id)
        if len(attempts) < 3:
            raise RuntimeError("cursor down")
        return True

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", flaky)
    task = await _running_task(db, "Сбой с повтором")
    await lifecycle.submit_for_review(db, task.id, models.TaskSubmitReview(agent="dev"))
    await review_order.drain()
    assert len(attempts) == 3
    assert await _alerts(db, task.id) == []

    attempts.clear()

    async def dead(_db, task_id, **_kw):
        attempts.append(task_id)
        raise RuntimeError("cursor down")

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", dead)
    lost = await _running_task(db, "Заказ потерян")
    done = await lifecycle.submit_for_review(
        db, lost.id, models.TaskSubmitReview(agent="dev")
    )
    assert done.status == "review", "сдача не страдает от отказа заказа"
    await review_order.drain()
    assert len(attempts) == review_order.ATTEMPTS
    named = await _alerts(db, lost.id)
    assert len(named) == 1 and "Повторная сдача того же sha" in named[0]


async def test_without_a_dsn_the_order_stays_inline(db, monkeypatch):
    monkeypatch.setattr(review_order, "DETACH_ENABLED", True)
    _install_git(monkeypatch, X)
    calls: list[int] = []

    async def spy(_db, task_id, **_kw):
        calls.append(task_id)
        return True

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", spy)
    task = await _running_task(db, "На месте")
    await lifecycle.submit_for_review(db, task.id, models.TaskSubmitReview(agent="dev"))
    assert calls == [task.id] and review_order.inflight_count() == 0


async def test_the_route_answers_before_a_slow_order_finishes(client, db, monkeypatch):
    monkeypatch.setattr(review_order, "DETACH_ENABLED", True)
    _install_git(monkeypatch, X)
    release = asyncio.Event()
    calls: list[int] = []

    async def slow(_db, task_id, **_kw):
        calls.append(task_id)
        await release.wait()
        return True

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", slow)
    task = await _running_task(db, "Через маршрут")
    await db.commit()
    resp = await asyncio.wait_for(
        client.post(f"/api/tasks/{task.id}/submit-review", json={"agent": "dev"}),
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["submission_generation"] == 1
    release.set()
    await review_order.drain()
    assert calls == [task.id]
