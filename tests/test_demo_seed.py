"""Демо-сид холодного старта (#944): строго opt-in и идемпотентен.

Сид наполняет пустой хаб демонстрационным набором (проект, эпик, фича,
задачи во всех ключевых статусах), чтобы docker compose up показывал
живой интерфейс, а не пустую доску. Два инварианта:

* без флага HAIPLANE_DEMO_SEED=1 сид не пишет НИЧЕГО — прод, где кто-то
  случайно прогнал скрипт, не получает игрушечных задач;
* повторный запуск ничего не дублирует — маркер идемпотентности это
  slug проекта ``demo``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import aiosqlite

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "demo_seed.py"


def _load():
    spec = importlib.util.spec_from_file_location("demo_seed", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _counts(db: aiosqlite.Connection) -> tuple[int, int]:
    tasks = list(await db.execute_fetchall("SELECT COUNT(*) FROM tasks"))[0][0]
    projects = list(await db.execute_fetchall("SELECT COUNT(*) FROM projects"))[0][0]
    return int(tasks), int(projects)


async def test_seed_is_opt_in_and_idempotent(db, monkeypatch):
    demo_seed = _load()

    # 1. Без флага — ни одной записи.
    monkeypatch.delenv("HAIPLANE_DEMO_SEED", raising=False)
    before = await _counts(db)
    assert await demo_seed.seed_demo(db) is False
    assert await _counts(db) == before

    # 2. С флагом — создаётся демо-набор.
    monkeypatch.setenv("HAIPLANE_DEMO_SEED", "1")
    assert await demo_seed.seed_demo(db) is True
    after_tasks, after_projects = await _counts(db)
    assert after_projects == before[1] + 1
    assert after_tasks > before[0]

    project = list(
        await db.execute_fetchall("SELECT id, repo FROM projects WHERE slug='demo'")
    )
    assert project, "проект demo должен существовать после сида"
    assert project[0]["repo"] == ""

    rows = list(
        await db.execute_fetchall(
            "SELECT status, task_type, user_story FROM tasks WHERE project_id=?",
            (project[0]["id"],),
        )
    )
    statuses = {r["status"] for r in rows}
    assert {"draft", "open", "running", "review", "completed"} <= statuses
    types = {r["task_type"] for r in rows}
    assert {"epic", "feature", "task"} <= types

    # Драфт приходит с заполненным DoR: поля формы и хотя бы один AC.
    draft = [r for r in rows if r["status"] == "draft"]
    assert draft and draft[0]["user_story"]
    ac_count = list(
        await db.execute_fetchall(
            "SELECT COUNT(*) FROM acceptance_criteria WHERE task_id IN "
            "(SELECT id FROM tasks WHERE project_id=? AND status='draft')",
            (project[0]["id"],),
        )
    )[0][0]
    assert int(ac_count) >= 1

    # 3. Повторный вызов — no-op: счётчики не растут.
    assert await demo_seed.seed_demo(db) is False
    assert await _counts(db) == (after_tasks, after_projects)
