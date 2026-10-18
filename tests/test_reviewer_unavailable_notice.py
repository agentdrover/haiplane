"""Сигнал «ревьюер недоступен» доходит до steward@<проект> (#1826).

Событие reviewer_unavailable поднимает поллер (#1262); здесь проверяется то,
что сидит поверх него: адресное уведомление в ящик стюарда каждого проекта с
ожидающими задачами, дедуп относительно ПОСЛЕДНЕГО уведомления проекта и
снятие тем же проектам. Наблюдение сторожа подменено (его сценарии держит
tests/test_poller.py), события, ключи и сообщения — настоящие.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hub import db as hub_db
from hub import poller
from hub import repository as repo
from hub.db import fetchall
from hub.services import review_availability as ra
from hub.services import reviewer_notice, watch_notices
from tests.watch_notice_support import (
    epic_of,
    events_of,
    keys,
    make_project,
    notices,
    set_watch,
)


async def _task_in(db, project_id: int | None) -> int:
    """Задача проекта (через эпик) либо без проекта (тогда — в default)."""
    parent = await epic_of(db, project_id) if project_id is not None else None
    task_id = await repo.create_task(
        db,
        title="Ждёт ревью",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="review",
        auto_review=True,
        task_type="task",
        parent_id=parent,
        priority="medium",
    )
    await db.commit()
    return task_id


def _obs(kind: str, *task_ids: int) -> ra.Observation:
    waiting = [{"task_id": t, "reason": "provider_refused"} for t in task_ids]
    return ra.Observation(
        kind,
        f"наблюдение {kind}",
        since="2026-10-17 10:00:00",
        refused_tasks=list(task_ids),
        waiting=waiting,
    )


class _Watch:
    """Подменённое наблюдение сторожа: тест называет очередь, как её видит сторож."""

    def __init__(self, monkeypatch) -> None:
        self.current = _obs("outage")

        async def observe(db, **_kw):
            return self.current

        monkeypatch.setattr(ra, "observe_outage", observe)

    def queue(self, *task_ids: int, kind: str = "outage") -> None:
        self.current = _obs(kind, *task_ids)


async def _sweep(db) -> None:
    await poller._sweep_reviewer_unavailable(db)


async def _age_signal(db) -> None:
    """Прошли сутки: суточный повтор снова разрешён."""
    await db.execute(
        "UPDATE events SET created_at = datetime('now', '-25 hours') "
        "WHERE kind IN (?, ?)",
        (ra.REVIEWER_UNAVAILABLE, ra.REVIEWER_SIGNAL_CLEARED),
    )
    await db.commit()


async def _pad_event_ids(db, up_to: int) -> None:
    """Довести максимальный id событий до ``up_to``: порядок по числу, а не по тексту."""
    while True:
        rows = await fetchall(db, "SELECT COALESCE(MAX(id), 0) AS m FROM events")
        if int(rows[0]["m"]) >= up_to:
            break
        await repo.insert_event(db, kind="padding", actor="test")
    await db.commit()


async def _signal_notes(db, slug: str) -> list[dict]:
    return [
        n["data"]
        for n in await notices(db, slug)
        if n["data"]["reason"] == "reviewer_unavailable"
    ]


async def _counts(db, *slugs: str) -> dict[str, int]:
    return {s: len(await _signal_notes(db, s)) for s in slugs}


async def _raised_ids(db) -> list[int]:
    return [e["id"] for e in await events_of(db, ra.REVIEWER_UNAVAILABLE)]


async def test_the_outage_reaches_each_waiting_project_once_per_picture(
    db, monkeypatch
):
    """AC-1: подъём → тот же набор → суточный повтор → изменения по проектам.

    Один проект получает сообщение только когда ИЗМЕНИЛСЯ его набор относительно
    его же последнего уведомления; чужие изменения и повтор события его не
    трогают.
    """
    watch = _Watch(monkeypatch)
    pa = await make_project(db, "pa")
    pb = await make_project(db, "pb")
    await make_project(db, "default")
    await _pad_event_ids(db, 96)  # ids пересекут границу 99 -> 100
    a1, a2 = await _task_in(db, pa), await _task_in(db, pa)
    b1, b2 = await _task_in(db, pb), await _task_in(db, pb)
    d1 = await _task_in(db, None)
    all_slugs = ("pa", "pb", "default")

    watch.queue(a1, a2, b1, d1)
    await _sweep(db)
    assert await _counts(db, *all_slugs) == {"pa": 1, "pb": 1, "default": 1}
    first = (await _signal_notes(db, "pa"))[0]
    assert first["v"] == 1 and first["task_id"] is None
    assert first["source_event_id"] == (await _raised_ids(db))[0]
    incident = (await _raised_ids(db))[0]

    await _sweep(db)  # тот же набор в тех же сутках
    await _age_signal(db)
    await _sweep(db)  # суточный повтор события при прежнем наборе
    assert len(await _raised_ids(db)) == 2, "событие повторено"
    assert await _counts(db, *all_slugs) == {"pa": 1, "pb": 1, "default": 1}

    watch.queue(a1, a2, b1, b2, d1)  # изменился только B
    await _sweep(db)
    assert await _counts(db, *all_slugs) == {"pa": 1, "pb": 2, "default": 1}

    watch.queue(a2, b1, b2, d1)  # ушла самая старая задача A
    await _sweep(db)
    assert await _counts(db, *all_slugs) == {"pa": 2, "pb": 2, "default": 1}

    watch.queue(a2, b1, d1)  # B вернулся к ПРЕЖНЕМУ набору (b1) — но не к последнему
    await _sweep(db)
    assert await _counts(db, *all_slugs) == {"pa": 2, "pb": 3, "default": 1}

    watch.queue(b1, d1)  # у A пусто
    await _sweep(db)
    assert await _counts(db, *all_slugs) == {"pa": 3, "pb": 3, "default": 1}
    watch.queue(a1, b1, d1)  # у A снова {1}
    await _sweep(db)
    assert await _counts(db, *all_slugs) == {"pa": 4, "pb": 3, "default": 1}
    watch.queue(a1, b1, d1)
    await _sweep(db)  # ничего не менялось
    assert await _counts(db, *all_slugs) == {"pa": 4, "pb": 3, "default": 1}

    # Идентификатор инцидента не менялся на всём пути, а ключи различимы.
    all_keys = [k for k in await keys(db) if k.startswith("reviewer_unavailable:")]
    assert {k.split(":")[1] for k in all_keys} == {str(incident)}
    assert len(all_keys) == len(set(all_keys)) == 8
    # Ревизии растут по проекту: у pa их четыре подряд.
    assert sorted(
        int(k.split(":")[3]) for k in all_keys if k.split(":")[2] == str(pa)
    ) == [
        1,
        2,
        3,
        4,
    ]


async def test_the_clearing_reaches_every_notified_project(db, monkeypatch):
    """AC-2: снятие получают все проекты инцидента; политика и соседи — учтены."""
    watch = _Watch(monkeypatch)
    for slug in ("pa", "pb", "pc", "old", "only"):
        await make_project(db, slug)
    pa, pb, pc = [
        (await repo.get_project_by_slug(db, s))["id"] for s in ("pa", "pb", "pc")
    ]
    await set_watch(db, "old", ["needs_decision", "stale"])  # старый явный список
    await set_watch(db, "only", ["reviewer_unavailable"])
    old = await _task_in(db, (await repo.get_project_by_slug(db, "old"))["id"])
    only = await _task_in(db, (await repo.get_project_by_slug(db, "only"))["id"])
    ta, tb, tc = await _task_in(db, pa), await _task_in(db, pb), await _task_in(db, pc)
    slugs = ("pa", "pb", "pc", "old", "only")

    watch.queue(ta, tb, old, only)
    await _sweep(db)
    watch.queue(tb, tc, old, only)
    await _sweep(db)
    first_incident = (await _raised_ids(db))[0]
    # Соседний инцидент: свой снятый цикл — его ключи в получатели не входят.
    await db.execute(
        "INSERT INTO watch_notices (key) VALUES (?)",
        (f"reviewer_unavailable:{first_incident + 1000}:{pa}:1:abc",),
    )
    await db.commit()
    assert await _counts(db, *slugs) == {"pa": 2, "pb": 1, "pc": 1, "old": 0, "only": 1}

    # Удержание too_young снятия не пишет.
    watch.queue(tc, kind="too_young")
    await _sweep(db)
    assert await events_of(db, ra.REVIEWER_SIGNAL_CLEARED) == []

    # Рестарт поллера между шагами ничего не теряет: состояние — в базе.
    watch.queue(kind="below_threshold")
    await _sweep(db)
    cleared = await events_of(db, ra.REVIEWER_SIGNAL_CLEARED)
    assert len(cleared) == 1
    after = {s: await _signal_notes(db, s) for s in slugs}
    clears = {
        s: [n for n in v if n["source_event_id"] == cleared[0]["id"]]
        for s, v in after.items()
    }
    assert {s: len(v) for s, v in clears.items()} == {
        "pa": 1,
        "pb": 1,
        "pc": 1,
        "old": 0,
        "only": 1,
    }
    ending = [k for k in await keys(db) if k.endswith(":cleared")]
    assert len(ending) == 4  # pa, pb, pc, only
    assert all(k.split(":")[1] == str(first_incident) for k in ending)
    pa_count = len(after["pa"])

    await _sweep(db)  # повторное снятие невозможно: сигнал уже снят
    assert len(await _signal_notes(db, "pa")) == pa_count


@pytest.mark.parametrize("kind", ["below_threshold", "watch_disabled"])
async def test_every_clearing_reason_notifies(db, monkeypatch, kind):
    watch = _Watch(monkeypatch)
    pid = await make_project(db, "pr")
    t1, t2 = await _task_in(db, pid), await _task_in(db, pid)
    watch.queue(t1, t2)
    await _sweep(db)
    watch.queue(kind=kind)
    await _sweep(db)
    notes = await _signal_notes(db, "pr")
    cleared = (await events_of(db, ra.REVIEWER_SIGNAL_CLEARED))[0]
    assert [n["source_event_id"] for n in notes][-1] == cleared["id"]
    assert cleared["payload"]["reason"] == kind


async def test_provider_answering_notifies_the_clearing(db, monkeypatch):
    watch = _Watch(monkeypatch)
    pid = await make_project(db, "pr")
    t1, t2 = await _task_in(db, pid), await _task_in(db, pid)
    watch.queue(t1, t2)
    await _sweep(db)

    async def answered(_db):
        return "2999-01-01 00:00:00"

    monkeypatch.setattr(ra, "_last_provider_success", answered)
    watch.queue(kind="below_threshold")
    await _sweep(db)
    cleared = (await events_of(db, ra.REVIEWER_SIGNAL_CLEARED))[0]
    assert cleared["payload"]["reason"] == "provider_answered"
    assert (await _signal_notes(db, "pr"))[-1]["source_event_id"] == cleared["id"]


async def test_incidents_do_not_leak_into_each_other(db, monkeypatch):
    """Получатели снятия — ключи ЭТОГО инцидента: префикс точный, 1 != 10."""
    watch = _Watch(monkeypatch)
    pa = await make_project(db, "pa")
    pb = await make_project(db, "pb")
    ta, tb = await _task_in(db, pa), await _task_in(db, pb)
    watch.queue(ta)
    await _sweep(db)
    watch.queue(kind="below_threshold")
    await _sweep(db)  # инцидент 1 закрыт: pa получил подъём и снятие
    watch.queue(tb)
    await _sweep(db)  # новый инцидент: только pb
    watch.queue(kind="below_threshold")
    await _sweep(db)
    assert [n["source_event_id"] for n in await _signal_notes(db, "pa")] == [
        e["id"]
        for e in (await events_of(db, ra.REVIEWER_UNAVAILABLE))[:1]
        + (await events_of(db, ra.REVIEWER_SIGNAL_CLEARED))[:1]
    ]
    assert len(await _signal_notes(db, "pb")) == 2
    # Префикс инцидента не цепляет соседа по цифрам: 1 и 12 — разные.
    await db.execute(
        "INSERT INTO watch_notices (key) VALUES ('reviewer_unavailable:12:7:5:aa')"
    )
    await db.commit()
    first = (await events_of(db, ra.REVIEWER_UNAVAILABLE))[0]["id"]
    assert set(await reviewer_notice.notified_projects(db, first)) == {pa}
    assert set(await reviewer_notice.notified_projects(db, 12)) == {7}
    assert 7 not in await reviewer_notice.notified_projects(db, 1)
    # Префикс точный: подчёркивание в имени повода — не шаблон LIKE.
    await db.execute(
        "INSERT INTO watch_notices (key) VALUES (?)",
        (f"reviewerXunavailable:{first}:9:5:aa",),
    )
    await db.commit()
    assert set(await reviewer_notice.notified_projects(db, first)) == {pa}


async def test_retention_shifts_the_incident_and_repeats_the_notice(db, monkeypatch):
    """Граница: события старше ретенции уходят — идентификатор сдвигается.

    Закрепляем как границу, а не потолок: после удаления самого раннего события
    подъёма инцидент считается по следующему сохранившемуся, и проект получает
    повтор на каждый сдвиг. Если удалены все события сигнала — как раньше.
    """
    watch = _Watch(monkeypatch)
    pa = await make_project(db, "pa")
    t1 = await _task_in(db, pa)
    watch.queue(t1)
    await _sweep(db)
    for _ in range(3):
        await _age_signal(db)
        await _sweep(db)  # суточный повтор, набор прежний
    assert len(await _signal_notes(db, "pa")) == 1
    for shift in range(1, 3):
        ids = await _raised_ids(db)
        await db.execute("DELETE FROM events WHERE id = ?", (ids[0],))
        await db.commit()
        await _age_signal(db)
        await _sweep(db)
        assert len(await _signal_notes(db, "pa")) == 1 + shift, "повтор на сдвиг"
    await db.execute("DELETE FROM events WHERE kind = ?", (ra.REVIEWER_UNAVAILABLE,))
    await db.commit()
    assert await reviewer_notice.incident_id(db) is None
    assert await ra.signal_state(db) == ""  # сигнал не поднят — как без этой задачи


async def _tally(conn) -> tuple[int, int, int]:
    """События подъёма, ключи уведомлений и сообщения хаба, как их видит соединение."""
    rows = await fetchall(
        conn,
        "SELECT (SELECT COUNT(*) FROM events WHERE kind='reviewer_unavailable') e,"
        " (SELECT COUNT(*) FROM watch_notices) k,"
        " (SELECT COUNT(*) FROM agent_messages WHERE from_agent='hub') m",
    )
    return (rows[0]["e"], rows[0]["k"], rows[0]["m"])


async def test_the_notice_commits_with_the_event_or_not_at_all(db, monkeypatch, db_dsn):
    """AC-3: сбой адресата не роняет свип; откат уносит всё; успех виден всем."""
    watch = _Watch(monkeypatch)
    pa = await make_project(db, "pa")
    pb = await make_project(db, "pb")
    ta, tb = await _task_in(db, pa), await _task_in(db, pb)
    watch.queue(ta, tb)

    real = repo.insert_agent_message

    async def flaky(conn, **kw):
        if kw.get("to_ref") == "steward@pa":
            raise RuntimeError("disk")
        return await real(conn, **kw)

    with patch.object(repo, "insert_agent_message", flaky):
        await _sweep(db)
    assert len(await _raised_ids(db)) == 1, "событие сохранено"
    failed = await events_of(db, watch_notices.FAILED_EVENT)
    assert failed[-1]["payload"]["reason"] == "reviewer_unavailable"
    assert failed[-1]["project_id"] == pa
    assert await _signal_notes(db, "pa") == []
    assert len(await _signal_notes(db, "pb")) == 1, "другой проект доставлен"

    # Откат: сбой самого commit — ни события, ни ключей, ни сообщений.
    await db.execute("DELETE FROM events")
    await db.execute("DELETE FROM watch_notices")
    await db.execute("DELETE FROM agent_messages")
    await db.commit()

    seen: list[tuple[int, int, int]] = []

    async def boom():
        # К моменту commit в ЭТОЙ транзакции уже лежат событие, ключи и сообщения.
        seen.append(await _tally(db))
        raise RuntimeError("commit failed")

    real_commit = db.commit
    monkeypatch.setattr(db, "commit", boom)
    with pytest.raises(RuntimeError, match="commit failed"):
        await _sweep(db)
    monkeypatch.setattr(db, "commit", real_commit)
    await db.rollback()
    assert seen == [(1, 2, 2)]
    assert await events_of(db, ra.REVIEWER_UNAVAILABLE) == []
    assert await keys(db) == []
    assert await _counts(db, "pa", "pb") == {"pa": 0, "pb": 0}

    # Успех: событие, ключи и сообщения видны с другого соединения вместе.
    await _sweep(db)
    other = await hub_db.connect(db_dsn)
    try:
        assert await _tally(other) == (1, 2, 2)
    finally:
        await other.close()


async def test_the_order_of_notices_follows_the_event_id(db, monkeypatch):
    """«Последнее» уведомление проекта — по числовому event_id, не по тексту ключа."""
    pa = await make_project(db, "pa")
    for event_id, digest in ((9, "aa"), (100, "bb"), (99, "cc")):
        await db.execute(
            "INSERT INTO watch_notices (key) VALUES (?)",
            (f"reviewer_unavailable:5:{pa}:{event_id}:{digest}",),
        )
    await db.commit()
    assert await reviewer_notice.notified_projects(db, 5) == {pa: (100, "bb")}


async def test_the_clearing_notice_is_in_the_transaction_of_its_event(db, monkeypatch):
    """Снятие: к моменту commit событие снятия и сообщения уже в транзакции."""
    watch = _Watch(monkeypatch)
    pa = await make_project(db, "pa")
    t1 = await _task_in(db, pa)
    watch.queue(t1)
    await _sweep(db)
    watch.queue(kind="below_threshold")
    seen: list[tuple[int, int]] = []

    async def boom():
        rows = await fetchall(
            db,
            "SELECT (SELECT COUNT(*) FROM events WHERE kind=?) c,"
            " (SELECT COUNT(*) FROM agent_messages WHERE from_agent='hub') m",
            (ra.REVIEWER_SIGNAL_CLEARED,),
        )
        seen.append((rows[0]["c"], rows[0]["m"]))
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", boom)
    with pytest.raises(RuntimeError, match="commit failed"):
        await _sweep(db)
    assert seen == [(1, 2)]  # событие снятия; сообщения подъёма и снятия


async def test_a_task_moving_between_projects_is_told_before_the_daily_repeat(
    db, monkeypatch
):
    """Тот же набор (task_id, reason), но задача сменила проект: A -> B.

    Сторож молчит (набор прежний, события моложе суток), а ящики должны
    обновиться сразу: B узнаёт о задаче, у A набор стал пустым. Нового события нет.
    """
    watch = _Watch(monkeypatch)
    pa = await make_project(db, "pa")
    pb = await make_project(db, "pb")
    t1, t2 = await _task_in(db, pa), await _task_in(db, pa)
    watch.queue(t1, t2)
    await _sweep(db)
    assert await _counts(db, "pa", "pb") == {"pa": 1, "pb": 0}
    raised = await _raised_ids(db)

    epic = (await repo.get_task(db, t1))["parent_id"]
    await repo.update_task(db, epic, project_id=pb)  # эпик перешёл в проект B
    await repo.update_task(db, t2, parent_id=await epic_of(db, pa))
    await db.commit()
    await _sweep(db)

    assert await _raised_ids(db) == raised, "нового события нет"
    assert await _counts(db, "pa", "pb") == {"pa": 2, "pb": 1}
    assert (await _signal_notes(db, "pb"))[0]["source_event_id"] == raised[-1]
    await _sweep(db)  # и повторно ничего
    assert await _counts(db, "pa", "pb") == {"pa": 2, "pb": 1}


async def test_returning_to_a_project_inside_the_day_is_told_again(db, monkeypatch):
    """A -> B -> A -> B при одном событии: на каждом шаге сообщение нужному проекту.

    Ключ несёт ревизию уведомления проекта, а не event_id: refresh пишет
    несколько картин при одном событии.
    """
    watch = _Watch(monkeypatch)
    pa = await make_project(db, "pa")
    pb = await make_project(db, "pb")
    t1 = await _task_in(db, pa)
    t2 = await _task_in(db, pa)
    epic = (await repo.get_task(db, t1))["parent_id"]
    watch.queue(t1, t2)
    await _sweep(db)
    assert await _counts(db, "pa", "pb") == {"pa": 1, "pb": 0}
    raised = await _raised_ids(db)

    expected = [
        (pb, {"pa": 2, "pb": 1}),  # A -> B: B узнал, у A набор пуст
        (pa, {"pa": 3, "pb": 2}),  # B -> A: возврат не теряется
        (pb, {"pa": 4, "pb": 3}),
    ]
    for target, counts in expected:
        await repo.update_task(db, epic, project_id=target)
        await repo.update_task(db, t2, parent_id=epic)
        await db.commit()
        await _sweep(db)
        assert await _counts(db, "pa", "pb") == counts
    assert await _raised_ids(db) == raised, "событие одно, картины разные"


async def test_a_muted_picture_is_not_replayed_when_the_reason_is_enabled(
    db, monkeypatch
):
    """Подъём при выключенном поводе; включили; тот же набор — сообщения нет."""
    watch = _Watch(monkeypatch)
    pa = await make_project(db, "pa")
    await set_watch(db, "pa", [])
    t1 = await _task_in(db, pa)
    watch.queue(t1)
    await _sweep(db)
    assert await _signal_notes(db, "pa") == []

    await set_watch(db, "pa", None)
    await _sweep(db)  # ранний выход: картина прежняя
    assert await _signal_notes(db, "pa") == [], "прошлое не доигрывается"

    t2 = await _task_in(db, pa)
    watch.queue(t1, t2)
    await _sweep(db)  # картина изменилась уже при включённом поводе
    assert len(await _signal_notes(db, "pa")) == 1
