"""Находки свежего взгляда (#1810, правило 270): показ без сети, state completed, обрезка."""

from __future__ import annotations

import json

import aiosqlite
import pytest

from hub import repository as repo
from hub.services import decision_brief, inbox_decisions
from tests.state_support import drive_to_review, make_state_task
from tests.test_decision_brief_whitelist import ACCEPT, _action, _task


async def _build(db, task_id: int, ctx=None) -> dict:
    task = dict(await repo.get_task(db, task_id) or {})
    return await decision_brief.build(db, task, ctx)


async def _completed(db) -> int:
    task_id = await repo.create_task(
        db,
        title="done",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="bot",
        rationale="",
        status="completed",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()
    return task_id


async def test_completed_brief_without_delivery_in_ctx_never_asks_git(
    db: aiosqlite.Connection, monkeypatch
):
    """P2-1: доставку считает вызывающий; сама справка в git не ходит."""
    from hub.services import delivery_state

    async def boom(*_a, **_k):
        raise AssertionError("справка зовёт delivery_state (git)")

    monkeypatch.setattr(delivery_state, "delivery_state", boom)
    got = await _build(db, await _completed(db))
    assert got["recommend"]["action"] == "Проверить"
    assert "Работа влита" not in got["happened"], "факт мержа не доказан"


async def test_completed_state_task_without_approval_is_not_called_accepted(
    client, db: aiosqlite.Connection
):
    """P2-2: «принято» — по state_accepted, а не по статусу completed."""
    task_id = await make_state_task(db)
    await drive_to_review(client, db, task_id)
    await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
    await db.commit()
    got = await _build(db, task_id)
    assert got["recommend"]["action"] != "Доставлено"
    assert "принято" not in got["happened"].lower()


async def test_inbox_builds_briefs_only_for_the_rows_it_shows(
    client, db: aiosqlite.Connection, monkeypatch
):
    """P2-3: дорогая справка — после обрезки DECISION_CAP, а не до."""
    from hub import services

    monkeypatch.setattr(inbox_decisions, "DECISION_CAP", 1)
    for n in range(3):
        await _task(client, db, monkeypatch, f"fr-cap-{n}", 1)
    built: list[int] = []
    real = decision_brief.build

    async def spy(db_, task, ctx=None):
        built.append(int(task["id"]))
        return await real(db_, task, ctx)

    monkeypatch.setattr(decision_brief, "build", spy)
    inbox = await services.get_inbox_data(db)
    assert len([r for r in inbox["decision_queue"] if r["action"] == "verdict"]) == 1
    assert len(built) == 1, f"справки собраны для скрытых строк: {built}"


@pytest.mark.parametrize(
    "finding",
    [
        {"id": 1, "severity": "high", "message": "x", "scope": None},
        {"id": 1, "severity": "high", "message": "x", "scope": "IN_SCOPE"},
        {"id": 1, "severity": "high", "message": "x", "scope": "что-то"},
        "не словарь",
    ],
)
async def test_unknown_finding_scope_blocks_accept(
    client, db: aiosqlite.Connection, monkeypatch, finding
):
    """P3: блокируем всё, что не названо out_of_scope, а не только известное плохое."""
    task_id, _ = await _task(
        client, db, monkeypatch, f"fr-scope-{abs(hash(str(finding)))}", 1
    )
    task = dict(await repo.get_task(db, task_id) or {})
    await db.execute(
        "UPDATE tasks SET review_verdict='approved', review_verdict_generation=?, "
        "review_findings=? WHERE id=?",
        (task["submission_generation"], json.dumps([finding]), task_id),
    )
    await db.commit()
    assert await _action(db, task_id) != ACCEPT


async def test_state_task_keeps_the_rework_level_of_a_standing_verdict(
    client, db: aiosqlite.Connection
):
    """P3: уровень блокера не теряется и в ветке state."""
    task_id = await make_state_task(db)
    await drive_to_review(client, db, task_id)
    task = dict(await repo.get_task(db, task_id) or {})
    await db.execute(
        "UPDATE tasks SET review_verdict='changes_requested', "
        "review_verdict_generation=? WHERE id=?",
        (task["submission_generation"], task_id),
    )
    await db.commit()
    got = await _build(db, task_id)
    assert got["recommend"]["action"] == "На доработку"
