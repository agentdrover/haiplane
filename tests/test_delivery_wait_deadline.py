"""Ожидание base_fetch_pending не бесконечно (#1992).

После #1983 любое расхождение голов при отказе fetch базы даёт временную
причину ``base_fetch_pending``; поллер повторял её без предела. Здесь ожидание
получает долговечный срок (политика проекта, 24 ч по умолчанию): после срока
задача один раз уходит в needs_decision через ``emit_needs_decision``.

Время двигается правкой строки таймера в базе, а не часами теста: так срок
проверяется в том виде, в каком он переживает рестарт поллера.
"""

from __future__ import annotations

import aiosqlite

from hub import poller
from hub import repository as repo
from hub import services
from hub.models import TaskReviewVerdict
from hub.services import watch_notices
from tests.delivery_rerun_support import set_policy
from tests.test_pair_merge_gate import _approved_pair_task, _git

PREFIX = "base_fetch_pending"


def _stuck(text: str = "fatal: unable to access"):
    async def gate(db_, task_):
        return False, f"{PREFIX}: свежую базу получить не удалось ({text})"

    return gate


async def _pass(db: aiosqlite.Connection, task_id: int) -> dict:
    await poller._deliver_pair_task(db, dict(await repo.get_task(db, task_id)))
    return dict(await repo.get_task(db, task_id))


async def _age(db: aiosqlite.Connection, task_id: int, hours: float) -> None:
    """Состарить ожидание: срок отсчитывается от started_at строки таймера."""
    await db.execute(
        "UPDATE delivery_wait_deadlines "
        "SET started_at = datetime('now', ?) WHERE task_id = ?",
        (f"-{hours} hours", task_id),
    )
    await db.commit()


async def _needs_decision_events(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM events WHERE kind='needs_decision' AND task_id=?",
        (task_id,),
    )
    return int(dict(rows[0])["n"])


async def _setup(db: aiosqlite.Connection, monkeypatch, gate=None) -> int:
    _git()
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-1992/x")
    await db.commit()
    monkeypatch.setattr(services, "merge_before_completion", gate or _stuck())
    poller._pair_delivery_waits.clear()
    return task_id


async def test_a_stuck_base_fetch_reaches_the_owner_once(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)

    assert (await _pass(db, task_id))["status"] == "running"
    await _age(db, task_id, 23)
    poller._pair_delivery_waits.clear()  # рестарт поллера посередине срока
    monkeypatch.setattr(services, "merge_before_completion", _stuck("другой текст"))
    assert (await _pass(db, task_id))["status"] == "running", "до срока - ожидание"
    assert await _needs_decision_events(db, task_id) == 0

    await _age(db, task_id, 25)  # срок вышел; текст ошибки опять новый
    monkeypatch.setattr(services, "merge_before_completion", _stuck("третий текст"))
    after = await _pass(db, task_id)

    assert after["status"] == "needs_decision"
    assert await _needs_decision_events(db, task_id) == 1
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any(PREFIX in a and "24" in a for a in alerts), alerts

    # Устаревший снимок задачи и повторные проходы второго перевода не дают.
    await poller._deliver_pair_task(db, {**after, "status": "running"})
    await poller._deliver_pair_task(db, {**after, "status": "running"})
    assert await _needs_decision_events(db, task_id) == 1


async def test_deadline_is_one_notice_through_the_single_door(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    calls: list[int] = []
    real = watch_notices.emit_needs_decision

    async def spy(db_, **kw):
        calls.append(int(kw["task_id"]))
        return await real(db_, **kw)

    monkeypatch.setattr(watch_notices, "emit_needs_decision", spy)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    await _pass(db, task_id)
    await _pass(db, task_id)

    assert calls == [task_id]


async def _open_waits(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM delivery_wait_deadlines "
        "WHERE task_id=? AND state='open'",
        (task_id,),
    )
    return int(dict(rows[0])["n"])


async def test_success_or_new_generation_clears_the_wait(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)

    # Fetch снова работает, доставка идёт дальше другой причиной: ожидание
    # закрыто, и старый возраст позже не срабатывает.
    await _pass(db, task_id)
    await _age(db, task_id, 30)

    async def ci_pending(db_, task_):
        return False, services.TRANSIENT_GATE_PREFIXES[0] + " CI ещё идёт"

    monkeypatch.setattr(services, "merge_before_completion", ci_pending)
    assert (await _pass(db, task_id))["status"] == "running"
    assert await _open_waits(db, task_id) == 0
    monkeypatch.setattr(services, "merge_before_completion", _stuck())
    assert (await _pass(db, task_id))["status"] == "running", "отсчёт с нуля"
    assert await _needs_decision_events(db, task_id) == 0

    # Новое поколение сдачи: старый возраст не переносится.
    await _age(db, task_id, 30)
    await services.submit_for_review(db, task_id)
    await repo.update_task(db, task_id, submission_sha="b" * 40)
    await db.commit()
    await services.record_review_verdict(
        db, task_id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )
    assert (await _pass(db, task_id))["status"] == "running"
    assert await _needs_decision_events(db, task_id) == 0
    assert await _open_waits(db, task_id) == 1, "одно открытое, прежнее закрыто"


async def test_delivery_success_closes_the_wait(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    assert await _open_waits(db, task_id) == 1
    await _age(db, task_id, 30)

    async def delivered(db_, task_):
        return True, "already delivered"

    monkeypatch.setattr(services, "merge_before_completion", delivered)
    await _pass(db, task_id)

    assert await _open_waits(db, task_id) == 0
    assert await _needs_decision_events(db, task_id) == 0


async def test_revoked_approval_is_not_turned_into_a_decision(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    snapshot = dict(await repo.get_task(db, task_id))
    await repo.update_task(db, task_id, review_verdict="changes_requested")
    await db.commit()

    await poller._deliver_pair_task(db, snapshot)  # снимок ещё помнит одобрение

    assert (await repo.get_task(db, task_id))["status"] == "running"
    assert await _needs_decision_events(db, task_id) == 0


async def test_project_policy_sets_the_deadline(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    await set_policy(db, base_fetch_wait_hours=2)
    await _pass(db, task_id)
    await _age(db, task_id, 3)

    assert (await _pass(db, task_id))["status"] == "needs_decision"


async def test_only_base_fetch_pending_gets_a_deadline(db, monkeypatch):
    async def other(db_, task_):
        return False, services.TRANSIENT_GATE_PREFIXES[0] + " CI ещё идёт"

    task_id = await _setup(db, monkeypatch, other)
    await _pass(db, task_id)
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM delivery_wait_deadlines"
    )

    assert dict(rows[0])["n"] == 0
    assert (await _pass(db, task_id))["status"] == "running"


# --- Находки Codex (первый круг) -------------------------------------------


async def test_a_task_completed_meanwhile_is_not_pulled_back(db, monkeypatch):
    """Проверка статуса входит в атомарный переход, а не стоит перед локом."""
    from hub.services import delivery_wait

    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    live = dict(await repo.get_task(db, task_id))
    key = (task_id, live["submission_generation"], live["review_verdict_revision"])
    await repo.update_task(db, task_id, status="completed")
    await db.commit()

    moved = await delivery_wait._escalate(
        db, (*key, "base_fetch_pending"), 360, "x", 24
    )

    assert moved is False
    assert (await repo.get_task(db, task_id))["status"] == "completed"
    assert await _needs_decision_events(db, task_id) == 0


async def test_a_new_verdict_revision_starts_its_own_wait(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    await db.execute(
        "UPDATE tasks SET review_verdict_revision = review_verdict_revision + 1 "
        "WHERE id = ?",
        (task_id,),
    )
    await db.commit()

    assert (await _pass(db, task_id))["status"] == "running"
    assert await _needs_decision_events(db, task_id) == 0
    assert await _open_waits(db, task_id) == 1


async def test_an_unreadable_policy_is_not_a_deadline(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    await set_policy(db, base_fetch_wait_hours=72)
    await _pass(db, task_id)
    await _age(db, task_id, 30)

    async def broken(db_, task_id_):
        raise RuntimeError("policy unreadable")

    monkeypatch.setattr(repo, "resolve_project_for_task", broken)

    assert (await _pass(db, task_id))["status"] == "running"
    assert await _needs_decision_events(db, task_id) == 0


async def test_a_pass_without_a_wait_keeps_no_write_lock(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)

    async def delivered(db_, task_):
        return True, "already delivered"

    monkeypatch.setattr(services, "merge_before_completion", delivered)
    from hub.services import delivery_wait

    assert (
        await delivery_wait.settle(db, dict(await repo.get_task(db, task_id)), "", 360)
        is False
    )
    assert db.in_transaction is False


async def test_a_timer_failure_does_not_roll_back_the_callers_writes(db, monkeypatch):
    from hub import repository_delivery_wait as journal
    from hub.services import delivery_wait

    task_id = await _setup(db, monkeypatch)
    await db.execute("BEGIN IMMEDIATE")
    await repo.add_task_update(db, task_id, "hub", "status", "pending gate write")

    async def boom(*a, **kw):
        raise RuntimeError("timer failed")

    monkeypatch.setattr(journal, "open_wait", boom)
    detail = f"{PREFIX}: x"
    assert (
        await delivery_wait.settle(
            db, dict(await repo.get_task(db, task_id)), detail, 360
        )
        is False
    )

    assert db.in_transaction, "чужая транзакция прохода жива"
    texts = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert "pending gate write" in texts
    await db.commit()


async def test_another_gate_answer_closes_the_wait_even_when_explicit(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)

    async def head_moved(db_, task_):
        return False, "approval_head_moved: голова ушла"

    monkeypatch.setattr(services, "merge_before_completion", head_moved)
    await _pass(db, task_id)
    assert await _open_waits(db, task_id) == 0

    monkeypatch.setattr(services, "merge_before_completion", _stuck())
    assert (await _pass(db, task_id))["status"] == "running", "отсчёт с нуля"
    assert await _needs_decision_events(db, task_id) == 0
