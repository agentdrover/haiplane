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
    assert (await _intent(db, task.id))["state"] == "failed"
    assert len(await _alerts(db, task.id)) == 1

    await _due(db, task.id)
    await review_order.sweep_review_orders(db)
    assert len(attempts) == review_order.MAX_ATTEMPTS, "failed не переспрашивается"

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
    import tests.conftest as conftest

    monkeypatch.setattr(conftest, "DRAIN_AFTER_RESPONSE", False)
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
    from hub.services import finding_outcome

    refused: list[int] = []
    real_refuse = finding_outcome.refuse_conflicting

    async def spy(*a, **k):
        refused.append(1)
        return await real_refuse(*a, **k)

    monkeypatch.setattr(finding_outcome, "refuse_conflicting", spy)
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
    assert refused == [1], "гонка сверила исходы с записанными"
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


# --- не больше одного автоматического платного POST (#2057) -----------------


async def _lost_kick_submit(client, db, monkeypatch, slug):
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    from hub.integrations import cursor_cloud

    lookups: list[str] = []

    async def find(name, pages=3):
        lookups.append(name)
        return cursor_cloud.Reconciliation("", "", True)

    monkeypatch.setattr(cursor_cloud, "find_agent_by_name", find)

    async def dead(*_a, **_k):
        return None

    monkeypatch.setattr(review_order, "kick", dead)
    task_id = await _submitted(client, db, slug, policy={"review": "dispatch"})
    return recorder, task_id, lookups


async def _expire_order_claim(db, task_id):
    await db.execute("DELETE FROM review_order_claims WHERE task_id=?", (task_id,))
    await db.commit()


async def _unknown_alerts(db, task_id):
    return [a for a in await _alerts(db, task_id) if "исход неизвестен" in a]


async def test_a_failed_dispatch_insert_is_unknown_and_never_reordered(
    client, db, monkeypatch
):
    recorder, task_id, lookups = await _lost_kick_submit(
        client, db, monkeypatch, "dup-insert"
    )
    real = repo.create_review_dispatch

    async def broken(*a, **k):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(repo, "create_review_dispatch", broken)
    await review_dispatch.sweep_review_dispatches(db)
    row = await _intent(db, task_id)
    assert (row["state"], row["ordering_marker"]) == (
        "unknown",
        recorder.calls[0]["name"],
    )
    assert len(await _unknown_alerts(db, task_id)) == 1

    monkeypatch.setattr(repo, "create_review_dispatch", real)
    await _expire_order_claim(db, task_id)
    await _due(db, task_id)
    await review_dispatch.sweep_review_dispatches(db)
    await review_dispatch.sweep_review_dispatches(db)
    assert len(recorder.calls) == 1, "второго автоматического POST нет"
    assert len(await _unknown_alerts(db, task_id)) == 1, "алерт один"

    # Осознанная пересдача того же sha - явный запрос нового заказа.
    await lifecycle.submit_for_review(db, task_id, models.TaskSubmitReview())
    assert (await _intent(db, task_id))["state"] == "pending"
    await review_dispatch.sweep_review_dispatches(db)
    assert len(recorder.calls) == 2


async def test_a_post_that_raises_after_sending_is_unknown_not_retried(
    client, db, monkeypatch
):
    recorder, task_id, lookups = await _lost_kick_submit(
        client, db, monkeypatch, "raise"
    )
    from hub.integrations import cursor_cloud

    async def raising(**kwargs):
        recorder.calls.append(kwargs)
        raise RuntimeError("socket died after send")

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", raising)
    await review_dispatch.sweep_review_dispatches(db)
    await _expire_order_claim(db, task_id)
    await _due(db, task_id)
    await review_dispatch.sweep_review_dispatches(db)
    assert len(recorder.calls) == 1
    assert (await _intent(db, task_id))["state"] == "unknown"
    assert len(await _unknown_alerts(db, task_id)) == 1


async def test_generation_drift_aborts_without_a_post(client, db, monkeypatch):
    """Намерение поколения 1, а диспетчер прочитал поколение 2: POST нет."""
    recorder, task_id, _ = await _lost_kick_submit(client, db, monkeypatch, "drift")
    await db.execute(
        "UPDATE tasks SET submission_generation = 2 WHERE id=?", (task_id,)
    )
    await db.commit()

    async def not_settled(*_a, **_k):
        return False  # устаревание поймано бы раньше; нужен именно шов

    monkeypatch.setattr(review_order, "_settled", not_settled)
    await review_order.process_intent(db, task_id, 1)
    assert recorder.calls == [], "метка поколения 2 не ложится на намерение 1"
    row = await _intent(db, task_id)
    assert (row["state"], row["ordering_marker"]) == ("done", "")


async def test_a_call_that_crossed_the_seam_without_a_row_is_unknown(
    client, db, monkeypatch
):
    recorder, task_id, _ = await _lost_kick_submit(client, db, monkeypatch, "norow")

    async def crossed_then_quiet(conn, tid, **_kw):
        await review_order.begin_ordering(conn, tid, f"haiplane:review:t{tid}:g1:a1")
        return False  # POST мог уйти, строки заказа нет

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", crossed_then_quiet)
    await review_order.sweep_review_orders(db)
    assert (await _intent(db, task_id))["state"] == "unknown"
    assert len(await _unknown_alerts(db, task_id)) == 1


async def test_lost_ownership_never_posts(client, db, monkeypatch):
    recorder, task_id, _ = await _lost_kick_submit(client, db, monkeypatch, "owner")
    real = review_order.begin_ordering

    async def stolen(conn, tid, marker):
        await conn.execute(
            "UPDATE review_order_intents SET claimed_by='thief' WHERE task_id=?",
            (tid,),
        )
        await conn.commit()
        await real(conn, tid, marker)

    monkeypatch.setattr(review_order, "begin_ordering", stolen)
    await review_order.sweep_review_orders(db)
    assert recorder.calls == []
    assert (await _intent(db, task_id))["state"] == "pending"


async def test_restart_with_pending_orders_once_and_with_ordering_is_unknown(
    client, db, monkeypatch
):
    recorder, task_id, lookups = await _lost_kick_submit(
        client, db, monkeypatch, "restart"
    )
    # перезапуск при pending: ровно один заказ (путь поллера)
    await review_dispatch.sweep_review_dispatches(db)
    await review_dispatch.sweep_review_dispatches(db)
    assert len(recorder.calls) == 1
    assert (await _intent(db, task_id))["state"] == "done"


async def test_restart_with_ordering_is_unknown_with_no_post_and_no_lookup(
    client, db, monkeypatch
):
    recorder, task_id, lookups = await _lost_kick_submit(
        client, db, monkeypatch, "restart-ord"
    )
    await db.execute(
        "UPDATE review_order_intents SET state='ordering', claimed_by='dead', "
        "ordering_marker='m', ordering_at=datetime('now', '-20 minutes')"
    )
    await db.commit()
    await review_dispatch.sweep_review_dispatches(db)
    await review_dispatch.sweep_review_dispatches(db)
    assert recorder.calls == [], "ordering автоматически не заказывается"
    assert lookups == [], "сверки провайдера нет"
    assert (await _intent(db, task_id))["state"] == "unknown"
    assert len(await _unknown_alerts(db, task_id)) == 1


async def test_a_resubmit_rearms_closed_intents_but_not_an_order_in_flight(
    db, monkeypatch
):
    _install_git(monkeypatch, X)

    async def dead(*_a, **_k):
        return None

    monkeypatch.setattr(review_order, "kick", dead)
    task = await _running_task(db, "Перевзвод")
    body = models.TaskSubmitReview(agent="dev")
    await lifecycle.submit_for_review(db, task.id, body)
    for closed in ("unknown", "failed"):
        await db.execute(
            "UPDATE review_order_intents SET state=?, attempts=3, ordering_marker='m'",
            (closed,),
        )
        await db.commit()
        await lifecycle.submit_for_review(db, task.id, body)
        row = await _intent(db, task.id)
        assert (row["state"], row["attempts"], row["ordering_marker"]) == (
            "pending",
            0,
            "",
        ), "внутри транзакции сдачи, без kick"
    await db.execute("UPDATE review_order_intents SET state='ordering'")
    await db.commit()
    await lifecycle.submit_for_review(db, task.id, body)
    assert (await _intent(db, task.id))["state"] == "ordering"


async def test_a_raced_submit_refuses_conflicting_outcomes_but_not_identical(
    db, monkeypatch
):
    from fastapi import HTTPException

    from hub.services import finding_outcome

    async def reports(*_a):
        return [{"id": 7}]

    async def rows(_db, _rid):
        return [{"finding_uid": "f1", "outcome": "fixed"}]

    monkeypatch.setattr(finding_outcome, "reports_owed_an_answer", reports)
    monkeypatch.setattr(repo, "list_finding_outcomes", rows)

    def item(outcome):
        return models.FindingOutcomeItem(
            finding_uid="f1", outcome=outcome, note="n", linked_task_id=None
        )

    await finding_outcome.refuse_conflicting(db, 1, 2, [item("fixed")])
    with pytest.raises(HTTPException) as err:
        await finding_outcome.refuse_conflicting(db, 1, 2, [item("deferred")])
    assert err.value.status_code == 409
