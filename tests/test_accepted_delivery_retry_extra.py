"""Автоповтор доставки принятой задачи (#1999): входы, схема, соседи.

Дополняет ``test_accepted_delivery_retry.py``: каждый вход, где доставка
принятой задачи получает base_updated, миграция на свежей и старой базе,
согласование с #1956, отказы, которые не про базу.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hub import db as hub_db
from hub import repository as repo
from hub.services.delivery_state import scan_completed_deliveries
from tests.accepted_delivery_retry_support import (
    RetryRig,
    accepted_task,
    approved_task,
    armed_rig,
)
from tests.delivery_rerun_support import POLICY_KEY, set_policy

TABLE = "accepted_delivery_retries"


async def _columns(db) -> set[str]:
    return {
        dict(r)["name"]
        for r in await db.execute_fetchall(f"PRAGMA table_info({TABLE})")
    }


async def test_schema_on_fresh_and_old_base(db):
    fresh = await _columns(db)
    assert {
        "task_id",
        "submission_generation",
        "pr_number",
        "attempts",
        "armed_at",
    } <= fresh

    # старая база: шага ещё нет в _migrations и таблицы нет - запуск миграций её создаёт
    await db.execute(f"DROP TABLE {TABLE}")
    await db.execute(
        "DELETE FROM _migrations WHERE name LIKE 'create_accepted_delivery_retries%'"
    )
    await db.commit()
    assert not await _columns(db)
    await hub_db._migrate(db)
    assert await _columns(db) == fresh
    await hub_db._migrate(db)  # повторный запуск терпит готовую схему

    # ключ (задача, поколение) и каскад от задачи
    await db.execute("INSERT INTO tasks (title) VALUES ('t')")
    task_id = (await db.execute_fetchall("SELECT max(id) FROM tasks"))[0][0]
    insert = f"INSERT INTO {TABLE} (task_id, submission_generation) VALUES (?, 1)"
    await db.execute(insert, (task_id,))
    with pytest.raises(Exception, match="UNIQUE"):
        await db.execute(insert, (task_id,))
    await db.rollback()
    await db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    await db.commit()
    assert not await db.execute_fetchall(f"SELECT 1 FROM {TABLE}")


async def test_decide_accept_and_force_complete_arm_the_retry(client, db, monkeypatch):
    decided = await approved_task(db)
    forced = await approved_task(db)
    await repo.update_task(db, decided, status="needs_decision")
    await db.commit()
    rigs = {
        "decide": await accepted_task(
            db, monkeypatch, status="needs_decision", task_id=decided
        ),
        "force": await accepted_task(db, monkeypatch, status="running", task_id=forced),
    }
    rig = rigs["decide"]
    resp = await client.post(
        f"/api/tasks/{decided}/decide",
        json={"action": "accept", "pr_disposition": "deliver"},
    )
    assert resp.status_code == 200, resp.text
    assert (await rig.retry_row())["state"] == "armed"
    assert "автоповтор" in (await _feed(rig)).lower()

    forced_rig = RetryRig(db, monkeypatch, forced)
    resp = await client.post(
        f"/api/tasks/{forced}/force-complete",
        json={"comment": "принято", "pr_disposition": "deliver"},
    )
    assert resp.status_code == 200, resp.text
    assert (await forced_rig.retry_row())["state"] == "armed"


async def _feed(rig) -> str:
    rows = await repo.get_task_updates(rig.db, rig.task_id)
    return "\n".join(dict(u)["content"] or "" for u in rows)


async def test_an_unreadable_ci_probe_waits_and_never_stops(db, monkeypatch):
    rig = await armed_rig(db, monkeypatch)
    rig.git.check_pr_ci = AsyncMock(side_effect=RuntimeError("форж недоступен"))
    for _ in range(3):
        await scan_completed_deliveries(db)
    row = await rig.retry_row()
    assert (row["state"], row["attempts"]) == ("armed", 0)
    rig.git.merge_pr.assert_not_awaited()


async def test_a_refusal_that_is_not_about_the_base_stops_with_an_alert(
    db, monkeypatch
):
    rig = await armed_rig(db, monkeypatch)
    rig.git.merge_pr.return_value = False
    await scan_completed_deliveries(db)
    row = await rig.retry_row()
    assert row["state"] == "closed" and row["outcome"].startswith("merge")
    assert len(await rig.events("accepted_delivery_retry_stopped")) == 1
    await scan_completed_deliveries(db)
    assert rig.git.merge_pr.await_count == 1, "закрытая строка мержа не повторяет"


async def test_a_delivery_made_elsewhere_closes_the_row_without_a_merge(
    db, monkeypatch
):
    rig = await armed_rig(db, monkeypatch)
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (1, 77, ?, ?)",
        (rig.task_id, "f" * 40),
    )
    await db.commit()
    await scan_completed_deliveries(db)
    row = await rig.retry_row()
    assert (row["state"], row["outcome"]) == ("closed", "delivered")
    rig.git.merge_pr.assert_not_awaited()


async def test_fresh_base_rerun_is_not_doubled(db, monkeypatch):
    """Красный CI при политике #1956: повтор идёт в тот же гейт, POST один."""
    await set_policy(db, **{POLICY_KEY: "on"})
    rig = await armed_rig(db, monkeypatch)
    assert (await db.execute_fetchall("SELECT state FROM delivery_reruns"))[0][
        0
    ] == "armed"
    rig.red_first_attempt()
    for _ in range(3):
        await scan_completed_deliveries(db)
    assert rig.posts == 1, "перезапуск #1956 ровно один"
    rig.git.merge_pr.assert_not_awaited()
    row = await rig.retry_row()
    assert row["state"] == "armed" and row["attempts"] == 0, (
        "ожидание не тратит попытку"
    )
