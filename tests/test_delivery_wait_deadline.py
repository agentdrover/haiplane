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


async def test_the_transition_is_won_once_per_wait(db, monkeypatch):
    from hub.services import delivery_wait

    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    live = dict(await repo.get_task(db, task_id))
    key = (
        task_id,
        live["submission_generation"],
        live["review_verdict_revision"],
        "base_fetch_pending",
    )

    assert await delivery_wait._escalate(db, key, 360, "x", 24) is True
    # Владелец вернул задачу в running, но ожидание уже эскалировано: второй
    # проход с тем же ключом перехода не выигрывает.
    await repo.update_task(db, task_id, status="running")
    await db.commit()

    assert await delivery_wait._escalate(db, key, 360, "x", 24) is False
    assert await _needs_decision_events(db, task_id) == 1


async def test_repeated_passes_do_not_extend_the_clock(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 23)
    poller._pair_delivery_waits.clear()  # рестарт
    for text in ("a", "b", "c"):
        monkeypatch.setattr(services, "merge_before_completion", _stuck(text))
        assert (await _pass(db, task_id))["status"] == "running"

    # Прошло ещё два часа: суммарно 25, и повторы ничего не сдвинули.
    await db.execute(
        "UPDATE delivery_wait_deadlines SET started_at = datetime(started_at, '-2 hours')"
    )
    await db.commit()

    assert (await _pass(db, task_id))["status"] == "needs_decision"


# --- Находки независимого Codex (круг 2) -----------------------------------


def _stuck_after(neighbour):
    """Гейт отвечает base_fetch_pending, а сосед пишет своё посреди прохода."""

    async def gate(db_, task_):
        await neighbour(db_, int(task_["id"]), int(task_["submission_generation"]))
        await db_.commit()
        return False, f"{PREFIX}: свежую базу получить не удалось (x)"

    return gate


async def _aged_wait(db, monkeypatch, neighbour) -> int:
    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    monkeypatch.setattr(services, "merge_before_completion", _stuck_after(neighbour))
    return task_id


async def test_a_merge_recorded_mid_pass_is_not_an_escalation(db, monkeypatch):
    async def merge(db_, task_id, gen):
        await db_.execute(
            "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
            "VALUES (1, 360, ?, 'neighbour')",
            (task_id,),
        )

    task_id = await _aged_wait(db, monkeypatch, merge)
    await _pass(db, task_id)

    assert (await repo.get_task(db, task_id))["status"] != "needs_decision"
    assert await _needs_decision_events(db, task_id) == 0
    assert await _open_waits(db, task_id) == 0


async def test_a_live_rerun_booking_is_not_an_escalation(db, monkeypatch):
    async def booking(db_, task_id, gen):
        await db_.execute(
            "INSERT INTO delivery_reruns (task_id, submission_generation, "
            "pr_number, state, requested_at) "
            "VALUES (?, ?, 360, 'reserved', datetime('now'))",
            (task_id, gen),
        )

    task_id = await _aged_wait(db, monkeypatch, booking)
    await set_policy(db, delivery_fresh_base_rerun="on")
    await _pass(db, task_id)

    assert (await repo.get_task(db, task_id))["status"] == "running"
    assert await _needs_decision_events(db, task_id) == 0
    assert await _open_waits(db, task_id) == 1, "срок не продлевается бронью"


async def test_an_expired_rerun_booking_does_not_hold_the_deadline(db, monkeypatch):
    async def booking(db_, task_id, gen):
        await db_.execute(
            "INSERT INTO delivery_reruns (task_id, submission_generation, "
            "pr_number, state, requested_at) "
            "VALUES (?, ?, 360, 'requested', datetime('now', '-3 hours'))",
            (task_id, gen),
        )

    task_id = await _aged_wait(db, monkeypatch, booking)
    await set_policy(db, delivery_fresh_base_rerun="on")
    await _pass(db, task_id)

    assert (await repo.get_task(db, task_id))["status"] == "needs_decision"
    assert await _needs_decision_events(db, task_id) == 1


async def test_a_replaced_pr_delivered_by_a_neighbour_is_not_an_escalation(
    db, monkeypatch
):
    async def replaced(db_, task_id, gen):
        await db_.execute("UPDATE tasks SET pr_number = 361 WHERE id = ?", (task_id,))
        await db_.execute(
            "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
            "VALUES (1, 361, ?, 'neighbour')",
            (task_id,),
        )

    task_id = await _aged_wait(db, monkeypatch, replaced)
    await _pass(db, task_id)

    assert (await repo.get_task(db, task_id))["status"] == "running"
    assert await _needs_decision_events(db, task_id) == 0


async def test_a_closed_rerun_outcome_is_named_in_the_escalation(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    gen = (await repo.get_task(db, task_id))["submission_generation"]
    await db.execute(
        "INSERT INTO delivery_reruns (task_id, submission_generation, pr_number, "
        "state, outcome, failed_jobs) VALUES (?, ?, 360, 'closed', 'rerun_red', "
        "'[\"tests\"]')",
        (task_id, gen),
    )
    await db.commit()
    await _age(db, task_id, 30)
    await _pass(db, task_id)

    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("rerun_red" in a and "tests" in a for a in alerts), alerts


async def test_a_stale_snapshot_neither_closes_nor_opens_the_live_wait(db, monkeypatch):
    from hub.services import delivery_wait

    task_id = await _setup(db, monkeypatch)
    stale = dict(await repo.get_task(db, task_id))  # поколение 1
    await _pass(db, task_id)
    await services.submit_for_review(db, task_id)  # поколение 2
    await repo.update_task(db, task_id, submission_sha="c" * 40)
    await db.commit()
    await services.record_review_verdict(
        db, task_id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )
    await _pass(db, task_id)  # ожидание поколения 2 открыто
    await _age(db, task_id, 5)
    before = await db.execute_fetchall(
        "SELECT started_at FROM delivery_wait_deadlines WHERE state='open'"
    )

    # Старый проход прошлого поколения: иная причина не закрывает чужой таймер...
    assert await delivery_wait.settle(db, stale, "ci_pending: x", 360) is False
    # ...а его base_fetch_pending не открывает и не двигает ожидание по живому ключу.
    assert await delivery_wait.settle(db, stale, f"{PREFIX}: x", 360) is False

    after = await db.execute_fetchall(
        "SELECT started_at FROM delivery_wait_deadlines WHERE state='open'"
    )
    assert [dict(r) for r in after] == [dict(r) for r in before]
    assert len(after) == 1


async def test_an_escalated_wait_is_terminal_for_its_key(db, monkeypatch):
    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    assert (await _pass(db, task_id))["status"] == "needs_decision"

    # Владелец вернул задачу в running; ключ (поколение, ревизия) тот же.
    await repo.update_task(db, task_id, status="running")
    await db.commit()
    for _ in range(3):
        assert (await _pass(db, task_id))["status"] == "running"
    await db.execute(
        "UPDATE delivery_wait_deadlines SET started_at = '2000-01-01 00:00:00'"
    )
    await db.commit()
    for _ in range(2):
        await _pass(db, task_id)

    assert await _needs_decision_events(db, task_id) == 1


async def test_a_pass_of_a_replaced_pr_does_not_touch_the_journal(db, monkeypatch):
    from hub.services import delivery_wait

    task_id = await _setup(db, monkeypatch)
    stale = dict(await repo.get_task(db, task_id))  # PR 360
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    await repo.update_task(db, task_id, pr_number=361)  # PR заменён, ключ тот же
    await db.commit()
    before = [
        dict(r)
        for r in await db.execute_fetchall("SELECT * FROM delivery_wait_deadlines")
    ]

    assert await delivery_wait.settle(db, stale, "ci_pending: x", 360) is False
    assert await delivery_wait.settle(db, stale, f"{PREFIX}: x", 360) is False

    after = [
        dict(r)
        for r in await db.execute_fetchall("SELECT * FROM delivery_wait_deadlines")
    ]
    assert after == before


async def _booking_of(pr: int):
    async def booking(db_, task_id, gen):
        await db_.execute(
            "INSERT INTO delivery_reruns (task_id, submission_generation, "
            "pr_number, state, requested_at) "
            "VALUES (?, ?, ?, 'requested', datetime('now'))",
            (task_id, gen, pr),
        )

    return booking


async def test_a_booking_of_another_pr_holds_nothing(db, monkeypatch):
    task_id = await _aged_wait(db, monkeypatch, await _booking_of(999))
    await set_policy(db, delivery_fresh_base_rerun="on")
    await _pass(db, task_id)

    assert (await repo.get_task(db, task_id))["status"] == "needs_decision"


async def test_a_booking_with_the_rerun_policy_off_holds_nothing(db, monkeypatch):
    task_id = await _aged_wait(db, monkeypatch, await _booking_of(360))
    await set_policy(db, delivery_fresh_base_rerun="off")
    await _pass(db, task_id)

    assert (await repo.get_task(db, task_id))["status"] == "needs_decision"


# --- Круг 3: данные вне транзакции перехода ---------------------------------


async def test_a_deadline_raised_before_the_lock_cancels_the_escalation(
    db, db_dsn, monkeypatch
):
    """Второе соединение сохранило срок 72 ч после чтения срока 24 ч проходом."""
    from hub.services import orchestration

    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    real = orchestration.record_delivery_outcome

    async def raise_deadline_first(db_, task_, write, **kw):
        other = await aiosqlite.connect(db_dsn, uri=True)
        try:
            await other.execute(
                "UPDATE projects SET gate_policy = ? WHERE slug = 'default'",
                ('{"base_fetch_wait_hours": 72}',),
            )
            await other.commit()
        finally:
            await other.close()
        return await real(db_, task_, write, **kw)

    monkeypatch.setattr(orchestration, "record_delivery_outcome", raise_deadline_first)

    assert (await _pass(db, task_id))["status"] == "running"
    assert await _needs_decision_events(db, task_id) == 0
    assert await _open_waits(db, task_id) == 1


async def test_a_policy_unreadable_at_the_transition_cancels_it(db, monkeypatch):
    from hub.services import orchestration

    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    real = orchestration.record_delivery_outcome

    async def corrupt_first(db_, task_, write, **kw):
        await db_.execute("UPDATE projects SET gate_policy = '{broken'")
        await db_.commit()
        return await real(db_, task_, write, **kw)

    monkeypatch.setattr(orchestration, "record_delivery_outcome", corrupt_first)

    assert (await _pass(db, task_id))["status"] == "running"
    assert await _needs_decision_events(db, task_id) == 0


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

    assert (await repo.get_task(db, task_id))["status"] == "needs_decision"
