"""#1997: учёт исхода разбора k8 - отдельная связь «исход -> дайджест», а не id в JSON.

``triage_digest_outcomes(recommendation_id UNIQUE)`` пишется в той же транзакции,
что сбор раздела и INSERT дайджеста. Исход учтён ровно одним дайджестом, legacy без
``outcome_ids`` ничего не подтверждает, а история не перечитывается.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import aiosqlite
import pytest

import hub.db as db_module
from hub import repository as repo
from hub.services import digest, triage_inbox
from tests import test_executor_orders as _eo
from tests.test_executor_orders import _task
from tests.test_executor_triage import CLASS1, _fail, _pin_old_activation
from tests.test_executor_triage_inbox import DAY, _rows, _stamp

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud

LINKS = "triage_digest_outcomes"
STEP_TOKENS = (
    "triage_digest_outcomes",
    "triage_outcome_queued",
    "queue_triage_outcomes",
    "triage_outcome_dequeued",
    "recommendations_created",
)


def is_step_1997(name: str) -> bool:
    return any(token in name for token in STEP_TOKENS)


async def _outcome(world, title: str = "исход") -> tuple[int, int]:
    """Рекомендация, решённая в DAY: (id рекомендации, id задачи)."""
    await _pin_old_activation(world)
    task = await _task(world, title)
    order = await _fail(world, task, CLASS1)
    await _stamp(world, order, f"{DAY} 10:00:00", "new_order", f"{DAY} 12:00:00")
    row = (
        await _rows(
            world.db,
            "SELECT id FROM executor_triage_recommendations WHERE order_id = ?",
            (order,),
        )
    )[0]
    return int(row["id"]), task


async def _counted_links(db) -> list[dict]:
    return await _rows(db, f"SELECT * FROM {LINKS} WHERE digest_id IS NOT NULL")


async def _generate(conn, now: datetime) -> int:
    created = await digest.generate_due_digests(conn, now=now)
    await conn.commit()
    return created


def _day(n: int) -> datetime:
    return datetime(2026, 10, 20 + n, 1, 0, tzinfo=UTC)


async def _taken(db) -> int:
    """Сколько раз исходы вошли в разделы ВСЕХ сохранённых дайджестов."""
    total = 0
    for row in await _rows(db, "SELECT payload FROM autopilot_digests"):
        section = json.loads(row["payload"]).get("triage") or {}
        total += int(section.get("resolved", 0)) + int(section.get("unmeasured", 0))
        total += int(section.get("late", 0))
    return total


async def test_an_outcome_is_counted_exactly_once(world):
    """AC-1: перенос задачи между проектами и две конкурентные генерации."""
    rec_id, task_id = await _outcome(world)
    await _generate(world.db, _day(1))
    assert await _taken(world.db) == 1
    other = await repo.create_project(world.db, slug="moved-1997", name="moved-1997")
    await repo.update_task(world.db, task_id, project_id=other)
    await world.db.commit()
    # суточная ветка: тот же день, другой проект, дайджеста у него ещё нет
    await _generate(world.db, _day(1))
    await _generate(world.db, _day(2))
    assert await _taken(world.db) == 1
    links = await _rows(world.db, f"SELECT * FROM {LINKS}")
    assert [(r["recommendation_id"], r["kind"]) for r in links] == [(rec_id, "day")]
    with pytest.raises(aiosqlite.IntegrityError):
        await world.db.execute(
            f"INSERT INTO {LINKS} (recommendation_id, digest_id, kind) "
            "VALUES (?, 999, 'late')",
            (rec_id,),
        )
    await world.db.rollback()


async def test_an_outcome_is_counted_once_by_concurrent_digests(
    world, db_dsn, monkeypatch
):
    """AC-1: два соединения, разные даты; сбор и запись не должны разойтись."""
    await _outcome(world, "гонка")
    other = await db_module.connect(db_dsn)
    real = triage_inbox.project_section
    calls: list[int] = []

    async def slow(db, *args, **kwargs):
        calls.append(1)
        collected = await real(db, *args, **kwargs)
        await asyncio.sleep(0.3)  # окно между сбором и записью
        return collected

    monkeypatch.setattr(triage_inbox, "project_section", slow)
    try:
        await asyncio.gather(_generate(world.db, _day(1)), _generate(other, _day(2)))
    finally:
        await other.close()
    assert len(calls) >= 2, "окно между сбором и записью не создано"
    assert await _taken(world.db) == 1
    links = await _rows(world.db, f"SELECT * FROM {LINKS}")
    assert len(links) == 1


async def test_legacy_and_history_do_not_hide_or_slow(world, monkeypatch):
    """AC-2: legacy без outcome_ids не скрывает исход; история не перечитывается."""
    rec_id, task_id = await _outcome(world, "legacy")
    project = await repo.resolve_project_for_task(world.db, task_id)
    legacy = {"triage": {"state": "measured", "created": 0, "resolved": 0}}
    await world.db.execute(
        "INSERT INTO autopilot_digests (project_id, digest_date, payload, created_at) "
        "VALUES (?, '2026-10-21', ?, '2026-10-21 12:00:00')",
        (project["id"], json.dumps(legacy)),
    )
    # 100 000 учтённых исторических исходов и старые дайджесты с маркером
    await _history(world, task_id, 100_000, 1_000_000)
    await world.db.execute("PRAGMA foreign_keys = OFF")
    for day in range(1, 20):
        await world.db.execute(
            "INSERT INTO autopilot_digests (project_id, digest_date, payload) "
            "VALUES (?, ?, ?)",
            (
                project["id"],
                f"2026-02-{day:02d}",
                json.dumps({"triage": {"state": "measured", "mark": "OLDPAYLOAD"}}),
            ),
        )
    await world.db.commit()
    await world.db.execute("PRAGMA foreign_keys = ON")

    parsed: list[str] = []
    real_loads = json.loads

    def spy(text, *a, **k):
        parsed.append(str(text))
        return real_loads(text, *a, **k)

    monkeypatch.setattr(json, "loads", spy)
    await _generate(world.db, _day(3))
    monkeypatch.setattr(json, "loads", real_loads)
    assert not [p for p in parsed if "OLDPAYLOAD" in p]
    late = [
        json.loads(r["payload"])["triage"]
        for r in await _rows(
            world.db,
            "SELECT payload FROM autopilot_digests WHERE digest_date = '2026-10-22'",
        )
    ]
    assert [s["late"] for s in late] == [1]
    await _generate(world.db, _day(4))
    mine = await _rows(
        world.db, f"SELECT kind FROM {LINKS} WHERE recommendation_id = ?", (rec_id,)
    )
    assert [r["kind"] for r in mine] == ["late"]
    plan = await _rows(
        world.db, "EXPLAIN QUERY PLAN " + triage_inbox.OUTCOME_ROWS_SQL, ("2030",)
    )
    # очередь неучтённых - частичный индекс, а не проход по истории
    assert "idx_triage_digest_outcomes_pending" in " ".join(r["detail"] for r in plan)


async def test_migration_links_only_provably_counted_outcomes(monkeypatch):
    """Вся цепочка steps на заполненной базе: связи только по outcome_ids измеренных."""
    from hub.migrations import FROZEN_MIGRATIONS
    from hub.migrations.steps import load_steps
    from tests.test_db_migrations import _SCHEMA

    steps = load_steps()
    mine = [(n, q) for n, q in steps if is_step_1997(n)]
    assert len(mine) >= 6, "нет шагов миграции #1997"
    before = [(n, q) for n, q in steps if (n, q) not in mine]
    monkeypatch.setattr(db_module, "_MIGRATIONS", [*FROZEN_MIGRATIONS, *before])
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(_SCHEMA)
        await db_module._migrate(conn)
        await conn.execute("PRAGMA foreign_keys = OFF")
        await conn.execute("INSERT INTO projects (id, slug, name) VALUES (5, 'p', 'p')")
        await conn.execute(
            "INSERT INTO tasks (id, title, description) VALUES (7, 't', '')"
        )
        for rec in (1, 2, 3, 4):
            await conn.execute(
                "INSERT INTO executor_triage_recommendations (id, order_id, task_id, "
                "triage_class, action, followup, followup_at) "
                "VALUES (?, ?, 7, 1, 'to_owner', 'new_order', '2026-10-20 01:00:00')",
                (rec, rec + 100),
            )
        payloads = {
            "2026-10-10": {"triage": {"state": "measured", "outcome_ids": [1, 99]}},
            "2026-10-11": {"triage": {"state": "measured", "outcome_ids": [1, 2]}},
            "2026-10-12": {"triage": {"state": "unavailable", "outcome_ids": [3]}},
            "2026-10-13": {"triage": {"state": "measured"}},  # legacy
            "2026-10-14": "не json",
            "2026-10-21": {"triage": {"state": "measured", "outcome_ids": [4]}},
        }
        for day, payload in payloads.items():
            text = payload if isinstance(payload, str) else json.dumps(payload)
            await conn.execute(
                "INSERT INTO autopilot_digests (project_id, digest_date, payload) "
                "VALUES (5, ?, ?)",
                (day, text),
            )
        await conn.commit()
        await conn.execute("PRAGMA foreign_keys = ON")
        monkeypatch.setattr(db_module, "_MIGRATIONS", [*FROZEN_MIGRATIONS, *steps])
        await db_module._migrate(conn)
        await db_module._migrate(conn)
        rows = await conn.execute_fetchall(
            f"SELECT recommendation_id, digest_id, kind FROM {LINKS} "
            "WHERE digest_id IS NOT NULL ORDER BY 1"
        )
        # 1 - у более раннего дайджеста; 99 нет в таблице; 3 - дайджест unavailable
        queued = await conn.execute_fetchall(
            f"SELECT recommendation_id FROM {LINKS} WHERE digest_id IS NULL ORDER BY 1"
        )
        assert [r[0] for r in queued] == [3]  # исход без доказанного учёта - в очередь
        assert [tuple(r) for r in rows] == [
            (1, 1, "day"),
            (2, 2, "day"),
            (4, 6, "late"),
        ]
    finally:
        await conn.close()


async def test_an_outcome_after_the_day_is_left_for_its_own_day(world):
    """Исход с действием ПОСЛЕ суток дайджеста не берётся им и не связывается."""
    rec_id, _ = await _outcome(world, "будущий")
    await world.db.execute(
        "UPDATE executor_triage_recommendations SET followup_at = ? WHERE id = ?",
        ("2026-10-21 12:00:00", rec_id),
    )
    await world.db.commit()
    await _generate(world.db, _day(1))  # сутки 2026-10-20: создание есть, исхода нет
    assert await _counted_links(world.db) == []
    await _generate(world.db, _day(2))  # сутки 2026-10-21: исход в своих сутках
    links = await _counted_links(world.db)
    assert [(r["recommendation_id"], r["kind"]) for r in links] == [(rec_id, "day")]


async def test_an_unreadable_section_links_nothing(world, monkeypatch):
    """Дайджест с unavailable связей не создаёт: исход ждёт первого измеренного."""
    await _outcome(world, "сбой")
    monkeypatch.setattr(triage_inbox, "_DIGEST_ROWS_SQL", "SELECT * FROM no_such")
    await _generate(world.db, _day(1))
    states = [
        json.loads(r["payload"])["triage"]["state"]
        for r in await _rows(world.db, "SELECT payload FROM autopilot_digests")
    ]
    assert states == ["unavailable"]
    assert await _counted_links(world.db) == []


async def test_quiet_projects_do_not_rebuild_the_tail_under_the_lock(
    world, monkeypatch
):
    """Тихие проекты без делегирования не собирают раздел под write-локом."""
    await _outcome(world, "тихий")
    for i in range(5):
        await repo.create_project(world.db, slug=f"quiet-{i}", name=f"quiet-{i}")
    await world.db.commit()
    real = triage_inbox.project_section
    calls: list[int] = []

    async def counting(*args, **kwargs):
        calls.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(triage_inbox, "project_section", counting)
    await _generate(world.db, _day(1))
    # под локом собирается только единственный активный проект
    assert len(calls) == 1


async def _history(world, task_id: int, count: int, base: int = 2_000_000) -> None:
    """``count`` учтённых исторических исходов (связь с digest_id заполнена)."""
    await world.db.commit()
    await world.db.execute("PRAGMA foreign_keys = OFF")
    await world.db.execute(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
        "WHERE i < ?) "
        "INSERT INTO executor_triage_recommendations (order_id, task_id, "
        "triage_class, action, created_at, followup, followup_at) "
        "SELECT ? + i, ?, 1, 'to_owner', '2026-01-01 00:00:00', "
        "'new_order', '2026-01-02 00:00:00' FROM n",
        (count, base, task_id),
    )
    await world.db.execute(
        f"UPDATE {LINKS} SET digest_id = 1, kind = 'day' WHERE digest_id IS NULL "
        "AND recommendation_id IN (SELECT id FROM executor_triage_recommendations "
        "WHERE order_id >= ?)",
        (base,),
    )
    await world.db.commit()
    await world.db.execute("PRAGMA foreign_keys = ON")


async def _vm_steps(world, now: datetime) -> int:
    steps = [0]

    def tick() -> int:
        steps[0] += 1
        return 0

    await world.db.set_progress_handler(tick, 100)
    try:
        await _generate(world.db, now)
    finally:
        await world.db.set_progress_handler(None, 0)
    return steps[0]


async def test_work_does_not_grow_with_the_counted_history(world):
    """AC-2: объём работы (шаги VM) не растёт с учтённым хвостом."""
    _, task_id = await _outcome(world, "объём")
    await _history(world, task_id, 1_000)
    small = await _vm_steps(world, _day(1))
    # те же выборки на хвосте в 20 раз длиннее: свежий день, один свежий исход
    await _history(world, task_id, 20_000, 3_000_000)
    await world.db.execute(
        "UPDATE executor_triage_recommendations SET followup_at = '2026-10-21 12:00:00' "
        "WHERE order_id < 2000000"
    )
    await world.db.commit()
    large = await _vm_steps(world, _day(2))
    assert large < small * 2 + 50, (small, large)


async def test_work_under_the_lock_does_not_depend_on_other_projects(
    world, monkeypatch
):
    """Класс «глобальное под локом»: под write-локом проекта определения проекта
    считаются только для ЕГО исходов, а не для всех проектов."""
    await _pin_old_activation(world)
    total = 0
    for slug in ("a-1997", "b-1997", "c-1997"):
        pid = await repo.create_project(world.db, slug=slug, name=slug)
        for i in range(10):
            task = await _task(world, f"{slug}-{i}")
            order = await _fail(world, task, CLASS1)
            await repo.update_task(world.db, task, project_id=pid)
            await _stamp(
                world, order, f"{DAY} 10:00:00", "new_order", f"{DAY} 12:00:00"
            )
            total += 1
    await world.db.commit()
    real = repo.resolve_project_for_task
    under_lock = [0]

    async def counting(db, task_id, *args, **kwargs):
        if db.in_transaction:
            under_lock[0] += 1
        return await real(db, task_id, *args, **kwargs)

    monkeypatch.setattr(repo, "resolve_project_for_task", counting)
    await _generate(world.db, _day(1))
    # каждый исход определяется под локом не более одного раза (старо: проекты x исходы)
    assert 0 < under_lock[0] <= total, under_lock[0]


async def test_a_recommendation_inserted_with_an_outcome_is_queued(world):
    """Триггер заводит очередь и при INSERT рекомендации с готовым исходом."""
    _, task_id = await _outcome(world, "вставка")
    await world.db.execute("PRAGMA foreign_keys = OFF")
    await world.db.execute(
        "INSERT INTO executor_triage_recommendations (order_id, task_id, "
        "triage_class, action, followup, followup_at) "
        "VALUES (777777, ?, 1, 'to_owner', 'new_order', '2026-10-20 05:00:00')",
        (task_id,),
    )
    await world.db.commit()
    await world.db.execute("PRAGMA foreign_keys = ON")
    queued = await _rows(
        world.db,
        f"SELECT l.recommendation_id FROM {LINKS} l JOIN "
        "executor_triage_recommendations r ON r.id = l.recommendation_id "
        "WHERE l.digest_id IS NULL AND r.order_id = 777777",
    )
    assert len(queued) == 1


async def test_closing_an_already_counted_outcome_aborts_the_digest(world):
    """Связь с занятым исходом не перезаписывается и валит транзакцию записи."""
    rec_id, _ = await _outcome(world, "занят")
    await _generate(world.db, _day(1))
    with pytest.raises(triage_inbox.OutcomeAlreadyCounted):
        await triage_inbox.record_links(world.db, 999, [(rec_id, "late")])
    await world.db.rollback()
    row = (await _counted_links(world.db))[0]
    assert row["digest_id"] != 999


async def test_a_candidate_moved_to_another_project_is_not_taken(world):
    """Между пробой и локом задача ушла в другой проект: старый проект её не берёт."""
    rec_id, task_id = await _outcome(world, "ушла")
    start, end = f"{DAY} 00:00:00", "2026-10-21 00:00:00"
    probe = await triage_inbox.probe_sections(world.db, start, end)
    mine = (await repo.resolve_project_for_task(world.db, task_id))["id"]
    assert probe.candidates[mine] == [rec_id]
    other = await repo.create_project(world.db, slug="away-1997", name="away-1997")
    await repo.update_task(world.db, task_id, project_id=other)
    await world.db.commit()
    section, links = await triage_inbox.project_section(
        world.db, mine, start, end, probe
    )
    assert links == [] and section["resolved"] == 0


async def test_a_deleted_recommendation_leaves_the_queue(world):
    """Удаление рекомендации не копит строки в очереди."""
    rec_id, task_id = await _outcome(world, "удалённая")
    before = await _rows(world.db, f"SELECT * FROM {LINKS} WHERE digest_id IS NULL")
    assert [r["recommendation_id"] for r in before] == [rec_id]
    await world.db.execute("PRAGMA foreign_keys = OFF")
    await world.db.execute(
        "DELETE FROM executor_triage_recommendations WHERE task_id = ?", (task_id,)
    )
    await world.db.execute("PRAGMA foreign_keys = ON")
    await world.db.commit()
    gone = await _rows(world.db, "SELECT id FROM executor_triage_recommendations")
    assert gone == []
    assert await _rows(world.db, f"SELECT * FROM {LINKS}") == []
