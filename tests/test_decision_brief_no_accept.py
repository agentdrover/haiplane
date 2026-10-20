"""Где справка «Принять» НЕ выдаёт и где выдаёт только по доказательству (#1912).

В #1810 «Принять» было выключено целиком; #1912 вернуло его через граф
происхождения отчётов (``tests/test_accept_provenance.py``). Здесь остаются
границы: задача-состояние, суждение стюарда, неизвестная доставка.
"""

from __future__ import annotations

import json

import aiosqlite

from hub import repository as repo
from hub.services import decision_brief
from tests.state_support import drive_to_review, make_state_task
from tests.test_decision_brief_whitelist import _task

ACCEPT = "Принять"


async def _build(db, task_id: int, ctx=None) -> dict:
    task = dict(await repo.get_task(db, task_id) or {})
    return await decision_brief.build(db, task, ctx)


async def test_a_steward_approve_adds_nothing_to_the_proof(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, _ = await _task(client, db, monkeypatch, "na-steward", 1)
    task = dict(await repo.get_task(db, task_id) or {})
    await repo.insert_steward_judgement(
        db,
        task_id=task_id,
        generation=int(task["submission_generation"]),
        kind="verdict",
        submitted_verdict="approve",
        verdict="approve",
        confidence="high",
        grounds=json.dumps([{"source": "ci_report", "detail": ""}]),
    )
    await db.commit()
    got = await _build(db, task_id)
    # Доказательство есть — «Принять» даёт оно, а не суждение стюарда.
    assert got["accept_proven"] is True
    assert got["recommend"]["action"] == ACCEPT


async def test_a_complete_state_task_is_still_only_check(
    client, db: aiosqlite.Connection
):
    task_id = await make_state_task(db)
    await drive_to_review(client, db, task_id)
    got = await _build(db, task_id)
    assert got["accept_proven"] is True
    assert got["recommend"]["action"] == "Проверить"
    assert decision_brief.STATE_NOT_ACCEPTED in got["recommend"]["why"]


async def test_the_inbox_line_says_accept_only_with_a_proof(
    client, db: aiosqlite.Connection, monkeypatch
):
    from hub import services

    task_id, _ = await _task(client, db, monkeypatch, "na-inbox", 1)
    inbox = await services.get_inbox_data(db)
    (row,) = [r for r in inbox["decision_queue"] if r["task_id"] == task_id]
    assert row["brief_line"].startswith(ACCEPT)


async def test_unknown_delivery_is_check_and_never_merged(db: aiosqlite.Connection):
    from tests.test_decision_brief_fresh_review import _completed

    got = await _build(
        db,
        await _completed(db),
        {"delivery": {"state": "unknown", "reason": "хаб не мержил эту задачу"}},
    )
    assert got["recommend"]["action"] == "Проверить"
    assert "Влито" not in got["recommend"]["action"]
    assert "влит" not in got["happened"].lower()
    merged = await _build(
        db, await _completed(db), {"delivery": {"state": "not_in_prod", "reason": "r"}}
    )
    assert merged["recommend"]["action"] == "Влито, выкат не подтверждён"
