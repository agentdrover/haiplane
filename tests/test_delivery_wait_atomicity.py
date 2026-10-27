"""Атомарность и актуальность уведомления о сроке ожидания (#1992).

Продолжение ``test_delivery_wait_deadline.py`` (бюджет размера файла тестов).
"""

from __future__ import annotations

import aiosqlite  # noqa: F401

from hub import poller, services  # noqa: F401
from hub import repository as repo
from hub.models import TaskReviewVerdict  # noqa: F401
from tests.delivery_rerun_support import set_policy  # noqa: F401
from tests.test_delivery_wait_deadline import (  # noqa: F401
    PREFIX,
    _age,
    _aged_wait,
    _no_decision,
    _notices,
    _open_waits,
    _pass,
    _setup,
    _stuck,
)

# --- Круг 3: данные вне транзакции перехода ---------------------------------


async def test_a_deadline_raised_by_another_connection_is_honoured(
    db, db_dsn, monkeypatch
):
    """Срок читается под локом прохода: 72 ч, сохранённые другим соединением."""
    task_id = await _setup(db, monkeypatch)
    await set_policy(db, base_fetch_wait_hours=24)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    other = await aiosqlite.connect(db_dsn, uri=True)
    try:
        await other.execute(
            "UPDATE projects SET gate_policy = ? WHERE slug = 'default'",
            ('{"base_fetch_wait_hours": 72}',),
        )
        await other.commit()
    finally:
        await other.close()

    await _pass(db, task_id)

    assert await _notices(db, task_id) == 0
    assert await _open_waits(db, task_id) == 1


async def test_a_policy_unreadable_at_the_pass_gives_no_notice(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    await set_policy(db, base_fetch_wait_hours=24)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    await db.execute("UPDATE projects SET gate_policy = '{broken'")
    await db.commit()

    await _pass(db, task_id)

    assert await _notices(db, task_id) == 0


async def test_a_booking_with_an_unreadable_date_holds_nothing(db, monkeypatch):
    async def booking(db_, task_id, gen):
        await db_.execute(
            "INSERT INTO delivery_reruns (task_id, submission_generation, "
            "pr_number, state, requested_at) "
            "VALUES (?, ?, 360, 'requested', '2026-10-01T00:00:00Z')",
            (task_id, gen),
        )

    task_id = await _aged_wait(db, monkeypatch, booking)
    await set_policy(db, delivery_fresh_base_rerun="on")
    await _pass(db, task_id)

    assert await _notices(db, task_id) == 1


# --- Круг 3, Codex: актуальность результата гейта ---------------------------


async def test_a_pin_moved_during_the_pass_makes_its_refusal_stale(db, monkeypatch):
    async def repin(db_, task_id, gen):
        await db_.execute(
            "UPDATE tasks SET submission_sha = ? WHERE id = ?", ("d" * 40, task_id)
        )

    task_id = await _aged_wait(db, monkeypatch, repin)
    await _pass(db, task_id)

    assert (await repo.get_task(db, task_id))["status"] == "running"
    assert await _notices(db, task_id) == 0


async def test_a_merge_in_the_gate_memory_is_not_an_escalation(db, monkeypatch):
    import asyncio

    from hub.services import orchestration

    async def merging(db_, task_id, gen):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(True)  # форж ответил, реестр ещё не записан
        orchestration._gate_merges[(task_id, 360)] = fut

    task_id = await _aged_wait(db, monkeypatch, merging)
    try:
        await _pass(db, task_id)
    finally:
        orchestration._gate_merges.pop((task_id, 360), None)

    assert (await repo.get_task(db, task_id))["status"] == "running"
    assert await _notices(db, task_id) == 0


async def test_a_failed_notice_inside_a_callers_transaction_is_not_half_kept(
    db, monkeypatch
):
    """Сбой записи уведомления откатывает notified, но не записи вызывающего."""
    from hub.services import delivery_wait

    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    snap = dict(await repo.get_task(db, task_id))
    real = repo.insert_event
    calls = {"n": 0}

    async def flaky(*a, **kw):
        if kw.get("kind") == "delivery_wait_deadline" and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("event write failed")
        return await real(*a, **kw)

    monkeypatch.setattr(repo, "insert_event", flaky)
    await db.execute("BEGIN IMMEDIATE")
    await repo.add_task_update(db, task_id, "hub", "status", "caller pending write")
    detail = f"{PREFIX}: x"

    assert await delivery_wait.settle(db, snap, detail, 360) is False
    await db.commit()  # вызывающий фиксирует свою транзакцию

    texts = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert "caller pending write" in texts
    assert await _notices(db, task_id) == 0
    assert await _open_waits(db, task_id) == 1, "notified не остался без уведомления"
    assert not any("base_fetch_wait_hours" in t for t in texts), "alert не остался"

    # Следующий проход доносит уведомление.
    assert await delivery_wait.settle(db, snap, detail, 360) is True
    assert await _notices(db, task_id) == 1


async def test_a_steward_notice_failure_rolls_the_whole_notice_back(db, monkeypatch):
    from hub.services import delivery_wait

    task_id = await _setup(db, monkeypatch)
    await set_policy(
        db, base_fetch_wait_hours=24
    )  # строка проекта, steward_watch по умолчанию
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    snap = dict(await repo.get_task(db, task_id))
    real = repo.insert_agent_message
    broken = {"on": True}

    async def flaky(*a, **kw):
        if broken["on"]:
            raise RuntimeError("message write failed")
        return await real(*a, **kw)

    monkeypatch.setattr(repo, "insert_agent_message", flaky)
    detail = f"{PREFIX}: x"

    assert await delivery_wait.settle(db, snap, detail, 360) is False
    assert await _notices(db, task_id) == 0
    assert await _open_waits(db, task_id) == 1

    broken["on"] = False
    assert await delivery_wait.settle(db, snap, detail, 360) is True
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM agent_messages WHERE related_task_id = ?",
        (task_id,),
    )
    assert dict(rows[0])["n"] == 1


async def test_a_corrupt_timer_value_is_not_an_overdue_wait(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await db.execute("UPDATE delivery_wait_deadlines SET started_at = '!invalid!'")
    await db.commit()

    await _pass(db, task_id)

    assert await _notices(db, task_id) == 0


async def test_a_steward_notice_that_never_started_rolls_the_notice_back(
    db, monkeypatch
):
    """notify_steward вернул False без события сбоя: это не «выключено» и не дедуп."""
    from hub.services import delivery_wait, watch_notices

    task_id = await _setup(db, monkeypatch)
    await set_policy(db, base_fetch_wait_hours=24)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    snap = dict(await repo.get_task(db, task_id))
    real = watch_notices.notify_steward
    mode = {"silent_fail": True}

    async def silent(db_, reason, **kw):
        if mode["silent_fail"]:
            return False  # ни сообщения, ни события сбоя
        return await real(db_, reason, **kw)

    monkeypatch.setattr(watch_notices, "notify_steward", silent)
    detail = f"{PREFIX}: x"

    assert await delivery_wait.settle(db, snap, detail, 360) is False
    assert await _notices(db, task_id) == 0
    assert await _open_waits(db, task_id) == 1

    mode["silent_fail"] = False
    assert await delivery_wait.settle(db, snap, detail, 360) is True


async def test_a_watch_switched_off_is_a_legal_silence(db, monkeypatch):
    from hub.services import delivery_wait

    task_id = await _setup(db, monkeypatch)
    await set_policy(db, base_fetch_wait_hours=24, steward_watch=["release"])
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    snap = dict(await repo.get_task(db, task_id))

    assert await delivery_wait.settle(db, snap, f"{PREFIX}: x", 360) is True
    assert await _notices(db, task_id) == 1  # alert и событие остались


async def test_a_repin_by_the_pass_itself_does_not_make_it_stale(db, monkeypatch):
    """Сам проход перезакрепил голову (_checked_sha) и ответил иначе: ожидание закрыто."""
    from hub.services import delivery_wait

    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    snap = dict(await repo.get_task(db, task_id))
    await db.execute(
        "UPDATE tasks SET submission_sha = ? WHERE id = ?", ("f" * 40, task_id)
    )
    await db.commit()
    snap["_checked_sha"] = "f" * 40  # как делает _approved_code_check

    await delivery_wait.settle(db, snap, "ci_pending: x", 360)

    assert await _open_waits(db, task_id) == 0
