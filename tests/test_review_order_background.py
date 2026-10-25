"""Заказ ревью после сдачи: durable намерение, свип, фон (#2057)."""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from hub import models
from hub import repository as repo
from hub.services import lifecycle, review_dispatch, review_order
from tests.review_dispatch_support import (
    _any_dispatch_row,
    _DispatchRecorder,
    _pinned_setup,
    _submitted,
    _wire,
)
from tests.test_submit_expected_sha import X, _install_git, _running_task


@pytest.fixture
def detached(monkeypatch, db_dsn):
    monkeypatch.setattr(review_order, "DETACH_ENABLED", True)
    token = review_order.detach_dsn.set(db_dsn)
    yield
    review_order.detach_dsn.reset(token)


async def _intent(db: aiosqlite.Connection, task_id: int) -> dict:
    await db.commit()
    cur = await db.execute(
        "SELECT * FROM review_order_intents WHERE task_id=? ORDER BY generation DESC",
        (task_id,),
    )
    return dict(await cur.fetchone())


async def _due(db: aiosqlite.Connection, task_id: int) -> None:
    await db.execute(
        "UPDATE review_order_intents SET next_attempt_at=datetime('now', '-1 second') "
        "WHERE task_id=?",
        (task_id,),
    )
    await db.commit()


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
    assert (await _intent(db, task.id))["state"] == "pending"
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
    assert calls == [task.id], "одна обработка на (задача, поколение)"
    assert (await _intent(db, task.id))["state"] == "done"


async def test_a_restart_before_the_order_is_replayed_once_by_the_sweep(
    db, monkeypatch
):
    """Намерение легло со сдачей, процесс умер до заказа: свип заказывает."""
    _install_git(monkeypatch, X)
    calls: list[int] = []

    async def spy(_db, task_id, **_kw):
        calls.append(task_id)
        return True

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", spy)

    async def dead(*_a, **_k):  # процесс убит между коммитом сдачи и заказом
        return None

    monkeypatch.setattr(review_order, "kick", dead)
    task = await _running_task(db, "Рестарт")
    await lifecycle.submit_for_review(db, task.id, models.TaskSubmitReview(agent="dev"))
    assert calls == [] and (await _intent(db, task.id))["state"] == "pending"

    await review_dispatch.sweep_review_dispatches(db)  # путь поллера
    await review_dispatch.sweep_review_dispatches(db)
    assert calls == [task.id], "ровно один заказ"
    assert (await _intent(db, task.id))["state"] == "done"


async def test_a_failed_order_backs_off_then_is_named_once_and_rearmed(db, monkeypatch):
    _install_git(monkeypatch, X)
    attempts: list[int] = []

    async def dead(_db, task_id, **_kw):
        attempts.append(task_id)
        raise RuntimeError("cursor down")

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", dead)
    task = await _running_task(db, "Заказ потерян")
    body = models.TaskSubmitReview(agent="dev")
    done = await lifecycle.submit_for_review(db, task.id, body)
    assert done.status == "review", "сдача не страдает от отказа заказа"
    assert len(attempts) == 1
    row = await _intent(db, task.id)
    assert (row["state"], row["attempts"]) == ("pending", 1)

    await review_order.sweep_review_orders(db)
    assert len(attempts) == 1, "откат: до срока свип не зовёт"

    for expected in range(2, review_order.MAX_ATTEMPTS + 1):
        await _due(db, task.id)
        await review_order.sweep_review_orders(db)
        assert len(attempts) == expected
    assert (await _intent(db, task.id))["state"] == "lost"
    assert len(await _alerts(db, task.id)) == 1

    await _due(db, task.id)
    await review_order.sweep_review_orders(db)
    assert len(attempts) == review_order.MAX_ATTEMPTS, "lost не переспрашивается"

    # Повтор того же sha из review возвращает заказ в работу.
    await lifecycle.submit_for_review(db, task.id, body)
    assert len(attempts) == review_order.MAX_ATTEMPTS + 1


async def test_a_held_claim_without_a_dispatch_row_is_not_success(db, monkeypatch):
    """Бронь заказа удержана, строки нет: попытка считается, не «успех»."""
    _install_git(monkeypatch, X)

    async def held(_db, task_id, **_kw):
        await db.execute(
            "INSERT INTO review_order_claims (task_id, submission_generation, "
            "profile) VALUES (?, 1, '')",
            (task_id,),
        )
        await db.commit()
        return False

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", held)
    task = await _running_task(db, "Бронь")
    await lifecycle.submit_for_review(db, task.id, models.TaskSubmitReview(agent="dev"))
    row = await _intent(db, task.id)
    assert (row["state"], row["attempts"]) == ("pending", 1)
    assert "бронь" in row["last_error"]


async def test_the_real_dispatcher_orders_once_through_the_background(
    client, db, monkeypatch, detached
):
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "bg-real", policy={"review": "dispatch"})
    await review_order.drain()
    assert len(recorder.calls) == 1
    assert await _any_dispatch_row(db, task_id) is not None
    assert (await _intent(db, task_id))["state"] == "done"


async def test_the_real_dispatcher_orders_once_after_a_lost_kick(
    client, db, monkeypatch
):
    """Рестарт с настоящим диспетчером: намерение есть, подталкивания не было."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-2"}, "run": {"id": "run-2"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)

    async def dead(*_a, **_k):
        return None

    monkeypatch.setattr(review_order, "kick", dead)
    task_id = await _submitted(client, db, "bg-restart", policy={"review": "dispatch"})
    assert recorder.calls == []
    assert (await _intent(db, task_id))["state"] == "pending"

    await review_dispatch.sweep_review_dispatches(db)  # путь поллера
    await review_dispatch.sweep_review_dispatches(db)
    assert len(recorder.calls) == 1
    assert await _any_dispatch_row(db, task_id) is not None
    assert (await _intent(db, task_id))["state"] == "done"


async def test_the_route_answers_before_a_slow_order_finishes(client, db, monkeypatch):
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
    assert review_order.inflight_count() == 1
    release.set()
    await review_order.drain()
    assert calls == [task.id]


async def test_a_new_sha_retried_after_a_lost_answer_opens_one_generation(
    db, db_dsn, monkeypatch
):
    """Новый sha из review: запрос, чей ответ потерян, и его повтор (#2057).

    Оба прочитали старый sha до лока; под локом второй видит, что этот sha уже
    сдан, и не открывает ещё одно поколение.
    """
    from hub.db import connect
    from hub.services import ci_trust

    git = _install_git(monkeypatch, X)
    task = await _running_task(db, "Гонка")
    body = models.TaskSubmitReview(agent="dev")
    await lifecycle.submit_for_review(db, task.id, body)
    await db.commit()
    git.tip = "b" * 40

    held, go = asyncio.Event(), asyncio.Event()
    real = ci_trust.refresh_quietly
    first = {"taken": False}

    async def gate(*a, **k):
        if not first["taken"]:
            first["taken"] = True
            held.set()
            await go.wait()  # запрос B застрял после снимка, до лока
        return await real(*a, **k)

    monkeypatch.setattr(ci_trust, "refresh_quietly", gate)
    other = await connect(db_dsn)
    try:
        racer = asyncio.ensure_future(lifecycle.submit_for_review(other, task.id, body))
        await asyncio.wait_for(held.wait(), 5)
        winner = await lifecycle.submit_for_review(db, task.id, body)
        assert winner.submission_generation == 2
        go.set()
        loser = await asyncio.wait_for(racer, 10)
    finally:
        go.set()
        await other.close()
    assert loser.submission_generation == 2, "повтор вернул текущее поколение"
    await db.commit()
    cur = await db.execute(
        "SELECT COUNT(*), MAX(generation) FROM submissions WHERE task_id=?", (task.id,)
    )
    assert tuple(await cur.fetchone()) == (2, 2), (
        "второго поколения для того же sha нет"
    )


async def test_a_fresh_claim_blocks_and_a_stale_one_is_retaken(db, monkeypatch):
    _install_git(monkeypatch, X)
    calls: list[int] = []

    async def spy(_db, task_id, **_kw):
        calls.append(task_id)
        return True

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", spy)

    async def dead(*_a, **_k):
        return None

    monkeypatch.setattr(review_order, "kick", dead)
    task = await _running_task(db, "Бронь обработки")
    await lifecycle.submit_for_review(db, task.id, models.TaskSubmitReview(agent="dev"))

    async def claim_at(offset: str) -> None:
        await db.execute(
            "UPDATE review_order_intents SET claimed_at=datetime('now', ?) "
            "WHERE task_id=?",
            (offset, task.id),
        )
        await db.commit()

    await claim_at("-1 minutes")  # другой обработчик занят
    await review_order.sweep_review_orders(db)
    assert calls == []
    await claim_at("-10 minutes")  # обработчик умер вместе с процессом
    await review_order.sweep_review_orders(db)
    assert calls == [task.id]
