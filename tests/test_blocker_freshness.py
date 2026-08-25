"""Снятый блокер не уводит сдачу мимо доставки (#948).

Отчёт о готовности pair-задачи уходил к человеку, если в ЛЮБОМ месте её
истории лежал апдейт kind=blocker. Понятия «блокер снят» у проверки не было,
поэтому запись любой давности отменяла гейт доставки. 25.08.2026 так ушли #851
и #947: обе с APPROVED и зелёным CI, обе с блокером, написанным и снятым за
часы до сдачи.

Правило теперь про свежесть: значим блокер, записанный ПОСЛЕ последней сдачи
на ревью. Сдача — утверждение «работа готова», и всё, что было до неё,
ревьюер видел вместе с ней. Задача, которая ни разу не сдавалась, границы не
имеет — там значим любой блокер, как и раньше (это и держит
test_done_report_on_claimed_with_blocker_needs_decision в test_services.py).

Дороже прямого убытка был стимул, который правило создавало: единственный
способ не попасть под него — не писать блокеров, то есть молчать ровно о том,
ради чего они заведены. Поэтому тесты здесь проверяют не только куда уходит
задача, но и что человеку на гейте называют виновника поимённо.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub import repository as repo
from hub import services
from hub.models import TaskCreate, TaskReviewVerdict, TaskUpdateCreate


async def _pair_task_at_review(db: aiosqlite.Connection, title: str = "Ship me") -> int:
    """Pair-задача, доведённая до сдачи на ревью, без PR и без ветки."""
    tv = await services.create_task(db, TaskCreate(title=title))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    return tv.id


async def _blocker(db: aiosqlite.Connection, task_id: int, text: str) -> None:
    await services.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="blocker", content=text)
    )


async def _submit_and_approve(db: aiosqlite.Connection, task_id: int) -> None:
    await services.submit_for_review(db, task_id)
    await services.record_review_verdict(
        db, task_id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )


async def _done(db: aiosqlite.Connection, task_id: int) -> None:
    await services.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="Готово")
    )


async def _updates(db: aiosqlite.Connection, task_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in await repo.get_task_updates(db, task_id)]


# ---------------------------------------------------------------------------
# AC-1 — блокер до сдачи больше не отменяет доставку
# ---------------------------------------------------------------------------


async def test_a_blocker_raised_before_the_submission_does_not_hold_the_report(
    db: aiosqlite.Connection,
):
    task_id = await _pair_task_at_review(db)
    await _blocker(db, task_id, "Клон хаба не сверен, pair-start отказал")
    await _submit_and_approve(db, task_id)

    await _done(db, task_id)

    row = await repo.get_task(db, task_id)
    assert row["status"] != "needs_decision", (
        "снятое препятствие не должно отменять гейт доставки"
    )
    # Без PR и ветки путь доставки завершает задачу — важно, что она вообще на
    # него попала, а не то, чем он кончился здесь.
    assert row["status"] == "completed"


async def test_the_report_that_went_to_delivery_says_nothing_about_blockers(
    db: aiosqlite.Connection,
):
    """Тишина — тоже поведение: несостоявшийся увод не оставляет следов."""
    task_id = await _pair_task_at_review(db)
    await _blocker(db, task_id, "Протухший sha сдачи")
    await _submit_and_approve(db, task_id)

    await _done(db, task_id)

    alerts = [u for u in await _updates(db, task_id) if u["kind"] == "alert"]
    assert not any("не пошёл в доставку" in (u["content"] or "") for u in alerts)


# ---------------------------------------------------------------------------
# AC-2 — блокер после сдачи держит, как держал
# ---------------------------------------------------------------------------


async def test_a_blocker_raised_after_the_submission_still_holds_the_report(
    db: aiosqlite.Connection,
):
    task_id = await _pair_task_at_review(db)
    await _submit_and_approve(db, task_id)
    await _blocker(db, task_id, "Миграция на проде не прошла, доставлять нельзя")

    await _done(db, task_id)

    row = await repo.get_task(db, task_id)
    assert row["status"] == "needs_decision"


async def test_the_boundary_is_the_LAST_submission_not_the_first(
    db: aiosqlite.Connection,
):
    """Пересдача переносит границу: блокер между сдачами уже разобран."""
    task_id = await _pair_task_at_review(db)
    await services.submit_for_review(db, task_id)
    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict="changes_requested", agent="reviewer"),
    )
    await _blocker(db, task_id, "Блокер между первой и второй сдачей")
    await _submit_and_approve(db, task_id)

    await _done(db, task_id)

    row = await repo.get_task(db, task_id)
    assert row["status"] == "completed", (
        "граница — последняя сдача; иначе пересдача ничего не меняет"
    )


async def test_a_task_that_never_submitted_keeps_the_old_behaviour(
    db: aiosqlite.Connection,
):
    """Без сдачи границы нет — значим любой блокер, как и до правки."""
    task_id = await _pair_task_at_review(db)
    await _blocker(db, task_id, "Нечего сдавать: доступа нет")

    await _done(db, task_id)

    row = await repo.get_task(db, task_id)
    assert row["status"] == "needs_decision"


# ---------------------------------------------------------------------------
# AC-3 — человеку называют виновника, а не факт его существования
# ---------------------------------------------------------------------------


async def test_the_human_is_told_which_blocker_stopped_the_delivery(
    db: aiosqlite.Connection,
):
    task_id = await _pair_task_at_review(db)
    await _submit_and_approve(db, task_id)
    await _blocker(db, task_id, "Секреты выката протухли\nвторая строка не нужна")

    await _done(db, task_id)

    updates = await _updates(db, task_id)
    blocker = next(u for u in updates if u["kind"] == "blocker")
    alert = next(
        u
        for u in updates
        if u["kind"] == "alert" and "не пошёл в доставку" in (u["content"] or "")
    )
    assert f"#{blocker['id']}" in alert["content"], "нужен номер апдейта"
    assert "Секреты выката протухли" in alert["content"], "нужно начало текста"
    assert "вторая строка не нужна" not in alert["content"], (
        "в сообщение идёт первая строка, а не весь блокер целиком"
    )

    activity = [dict(r) for r in await repo.list_activity(db, limit=20)]
    named = [
        a
        for a in activity
        if a["kind"] == "task_needs_decision" and f"#{blocker['id']}" in (a["summary"])
    ]
    assert named, "лента активности тоже называет блокер, а не просто факт увода"


# ---------------------------------------------------------------------------
# Само правило, отдельно от лишних слоёв
# ---------------------------------------------------------------------------


def test_the_rule_reads_the_last_blocker_after_the_boundary():
    from hub.services.lifecycle import blocker_holding_the_done_report

    updates = [
        {"id": 1, "kind": "blocker", "content": "старый"},
        {"id": 2, "kind": "status", "content": "Submitted for review (submission #1)."},
        {"id": 3, "kind": "blocker", "content": "первый свежий"},
        {"id": 4, "kind": "blocker", "content": "последний свежий"},
    ]

    held = blocker_holding_the_done_report(updates)

    assert held is not None
    assert held["id"] == 4, "человеку показывают последнее препятствие, а не первое"


def test_the_rule_ignores_everything_before_the_last_boundary():
    from hub.services.lifecycle import blocker_holding_the_done_report

    updates = [
        {"id": 1, "kind": "blocker", "content": "до первой сдачи"},
        {"id": 2, "kind": "status", "content": "Submitted for review (submission #1)."},
        {"id": 3, "kind": "blocker", "content": "между сдачами"},
        {"id": 4, "kind": "status", "content": "Submitted for review (submission #2)."},
    ]

    assert blocker_holding_the_done_report(updates) is None


def test_the_note_survives_a_blocker_with_no_text():
    from hub.services.lifecycle import blocker_note

    note = blocker_note({"id": 7, "content": "", "created_at": "2026-08-25 14:00:00"})

    assert "#7" in note
    assert "2026-08-25" in note


def test_a_long_blocker_line_is_cut_rather_than_dumped():
    from hub.services.lifecycle import blocker_note

    note = blocker_note({"id": 8, "content": "я" * 400, "created_at": "2026-08-25"})

    assert len(note) < 250
    assert note.endswith("…")
