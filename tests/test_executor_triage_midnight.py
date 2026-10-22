"""Суточный замер дайджеста по времени ДЕЙСТВИЯ, а не свипа (#1943, круг 2).

Действие владельца у полуночи не должно менять день от того, когда прошёл свип:
``followup_action_at`` - метка самого долговечного действия (заказ, сдача, срок),
``followup_at`` остаётся временем обработки. Опоздавший исход (действие в уже
собранном дне, обработка позже) не теряется: он идёт отдельной строкой
«досчитано позже» в дайджесте дня обработки.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from hub.services import digest, triage_inbox
from hub.services import executor_triage as triage
from tests import test_executor_orders as _eo
from tests.test_executor_orders import _task
from tests.test_executor_triage import CLASS1, _fail, _pin_old_activation, _relaunch
from tests.test_executor_triage_inbox import (
    DAY,
    _digests,
    _rows,
    _stamp,
)

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud

START = f"{DAY} 00:00:00"
END = "2026-10-21 00:00:00"
LAST_SECOND = f"{DAY} 23:59:58"


async def _acted(world) -> int:
    """Рекомендация повтора, за которой в LAST_SECOND выпущен новый заказ."""
    await _pin_old_activation(world)
    task = await _task(world, "действие у полуночи")
    first = await _fail(world, task, CLASS1)
    await _stamp(world, first, f"{DAY} 10:00:00")
    later = await _relaunch(world, task)
    await world.db.execute(
        "UPDATE executor_orders SET created_at = ? WHERE id = ?", (LAST_SECOND, later)
    )
    await world.db.commit()
    return first


async def _row_of(world, order_id: int) -> dict:
    sql = "SELECT * FROM executor_triage_recommendations WHERE order_id = ?"
    return (await _rows(world.db, sql, (order_id,)))[0]


async def test_outcome_stores_the_time_of_the_action(world):
    """Метка действия пишется там же, где followup, и равна времени самого действия."""
    first = await _acted(world)
    await triage.sweep(world.db)
    await world.db.commit()
    row = await _row_of(world, first)
    assert row["followup"] == "new_order"
    assert row["followup_action_at"] == LAST_SECOND
    assert row["followup_at"] != row["followup_action_at"]


async def test_day_does_not_depend_on_when_the_sweep_ran(world):
    """Свип до и после полуночи: действие в одном и том же дне."""
    first = await _acted(world)
    await triage.sweep(world.db)
    await world.db.commit()
    counted = []
    for swept_at in (f"{DAY} 23:59:59", "2026-10-21 00:00:01"):
        await world.db.execute(
            "UPDATE executor_triage_recommendations SET followup_at = ? "
            "WHERE order_id = ?",
            (swept_at, first),
        )
        await world.db.commit()
        sections = await triage_inbox.digest_sections(world.db, START, END)
        counted.append([(s["resolved"], s["matched"]) for s in sections.values()])
    assert counted[0] == counted[1] == [(1, 1)]


async def test_late_outcome_is_reported_not_lost(world):
    """День уже собран, исход обработан позже: строка «досчитано позже» дня обработки."""
    first = await _acted(world)
    built = await _digests(world, now=datetime(2026, 10, 21, 0, 0, 0, tzinfo=UTC))
    assert built[0]["data"]["triage"]["created"] == 1
    assert built[0]["data"]["triage"]["resolved"] == 0
    await world.db.execute(
        "UPDATE autopilot_digests SET created_at = '2026-10-21 00:00:00'"
    )
    await triage.sweep(world.db)
    await world.db.execute(
        "UPDATE executor_triage_recommendations SET followup_at = ? WHERE order_id = ?",
        ("2026-10-21 00:00:01", first),
    )
    await world.db.commit()
    nxt = await digest.generate_due_digests(
        world.db, now=datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
    )
    await world.db.commit()
    assert nxt >= 1
    rows = await _rows(
        world.db,
        "SELECT payload FROM autopilot_digests WHERE digest_date = '2026-10-21'",
    )
    section = json.loads(rows[0]["payload"])["triage"]
    assert (section["resolved"], section["late"]) == (0, 1)
    assert "досчитано позже 1" in triage_inbox.section_line(section)
    # День действия не пересчитывается.
    old = await _rows(
        world.db,
        "SELECT payload FROM autopilot_digests WHERE digest_date = ?",
        (DAY,),
    )
    assert json.loads(old[0]["payload"])["triage"]["resolved"] == 0


async def test_migration_adds_the_column_and_keeps_recommendations(monkeypatch):
    """Вся цепочка steps на базе с данными: колонка есть, строки целы, повтор пуст."""
    import aiosqlite

    import hub.db as db_module
    from hub.migrations import FROZEN_MIGRATIONS
    from hub.migrations.steps import load_steps
    from tests.test_db_migrations import _SCHEMA

    steps = load_steps()
    mine = [(n, q) for n, q in steps if "followup_action_at" in n]
    assert mine, "нет шага миграции #1943"
    before = [(n, q) for n, q in steps if (n, q) not in mine]
    monkeypatch.setattr(db_module, "_MIGRATIONS", [*FROZEN_MIGRATIONS, *before])
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(_SCHEMA)
        await db_module._migrate(conn)
        await conn.execute("PRAGMA foreign_keys = OFF")
        await conn.execute(
            "INSERT INTO tasks (id, title, description) VALUES (7, 't', '')"
        )
        await conn.execute(
            "INSERT INTO executor_hosts (name, principal_id, public_key_pem) "
            "VALUES ('k8', 1, 'pem')"
        )
        await conn.execute("INSERT INTO executor_runs (id, task_id) VALUES (9, 7)")
        await conn.execute(
            "INSERT INTO executor_orders (host, task_id, generation, executor_run_id, "
            "state, expires_at) VALUES ('k8', 7, 1, 9, 'closed', '2030-01-01')"
        )
        await conn.execute(
            "INSERT INTO executor_triage_recommendations (order_id, task_id, "
            "triage_class, action, followup, followup_at) "
            "VALUES (1, 7, 1, 'to_owner', 'new_order', '2026-10-20 01:00:00')"
        )
        await conn.commit()
        await conn.execute("PRAGMA foreign_keys = ON")
        monkeypatch.setattr(db_module, "_MIGRATIONS", [*FROZEN_MIGRATIONS, *steps])
        await db_module._migrate(conn)
        await db_module._migrate(conn)
        rows = await conn.execute_fetchall(
            "SELECT order_id, followup, followup_at, followup_action_at "
            "FROM executor_triage_recommendations"
        )
        assert [tuple(r) for r in rows] == [
            (1, "new_order", "2026-10-20 01:00:00", None)
        ]
    finally:
        await conn.close()
