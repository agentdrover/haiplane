"""Справка #1810 НИКОГДА не выдаёт «Принять» (решение владельца, 20.10).

Максимум — «Проверить» с причиной «доказательство принятия — отдельная задача»,
иначе «На доработку». Предикат ``accept_proof`` остаётся, его итог —
``brief["accept_proven"]``; в рекомендацию он не попадает.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from hub import repository as repo
from hub.services import decision_brief
from tests.state_support import drive_to_review, make_state_task
from tests.test_accept_proof import _copy
from tests.test_decision_brief_whitelist import _task

FORBIDDEN = "Принять"


def _never_accept(brief: dict) -> None:
    rec = brief["recommend"]
    assert rec["action"] != FORBIDDEN
    assert rec["verdict"] != "approved"
    assert not rec["action"].startswith(FORBIDDEN)


async def _build(db, task_id: int, ctx=None) -> dict:
    task = dict(await repo.get_task(db, task_id) or {})
    return await decision_brief.build(db, task, ctx)


async def test_a_fully_proven_submission_is_still_only_check(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, _ = await _task(client, db, monkeypatch, "na-proven", 1)
    got = await _build(db, task_id)
    assert got["accept_proven"] is True, "контроль: условия доказаны"
    _never_accept(got)
    assert got["recommend"]["action"] == "Проверить"
    assert "доказательство принятия — отдельная задача" in got["recommend"]["why"]
    assert "Принять" not in decision_brief.recommend_line(got).split(":")[0]


async def test_a_steward_approve_does_not_become_accept(
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
    _never_accept(got)
    assert got["recommend"]["action"] == "Проверить"


async def test_a_complete_state_task_is_still_only_check(
    client, db: aiosqlite.Connection
):
    task_id = await make_state_task(db)
    await drive_to_review(client, db, task_id)
    got = await _build(db, task_id)
    assert got["accept_proven"] is True
    _never_accept(got)
    assert got["recommend"]["action"] == "Проверить"


async def test_the_inbox_line_never_says_accept(
    client, db: aiosqlite.Connection, monkeypatch
):
    from hub import services

    task_id, _ = await _task(client, db, monkeypatch, "na-inbox", 1)
    inbox = await services.get_inbox_data(db)
    (row,) = [r for r in inbox["decision_queue"] if r["task_id"] == task_id]
    assert not row["brief_line"].startswith(FORBIDDEN)


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


@pytest.mark.parametrize("nearest_is_independent", [True])
async def test_a_copy_does_not_stop_at_the_nearest_independent_report(
    client, db: aiosqlite.Connection, monkeypatch, nearest_is_independent: bool
):
    """Класс A: gen1 #1 unresolved + #2 чистый; gen2 #4 чистый; gen3 #3 = перенос #2."""
    task_id, ids = await _task(client, db, monkeypatch, "na-origin", 3)
    first, second, fourth = ids
    await db.execute(
        "UPDATE machine_reviews SET submission_generation=1, unresolved=? WHERE id=?",
        ('[{"title":"не проверено","why":"x"}]', first),
    )
    await db.execute(
        "UPDATE machine_reviews SET submission_generation=1 WHERE id=?", (second,)
    )
    await db.execute(
        "UPDATE machine_reviews SET submission_generation=2 WHERE id=?", (fourth,)
    )
    await _copy(db, task_id, 3, second)
    await db.execute("UPDATE tasks SET submission_generation=3 WHERE id=?", (task_id,))
    await db.commit()
    got = await _build(db, task_id)
    assert got["accept_proven"] is False
    _never_accept(got)
