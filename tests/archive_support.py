"""Подставки тестов архива задачи в работе (#1673).

Ни одного импорта из нового кода хаба: файл обязан загружаться и на кодовой базе
ДО задачи, чтобы тесты падали на поведении, а не на ImportError.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.integrations.registry import plugins


async def mk(
    db: aiosqlite.Connection,
    title: str,
    status: str = "open",
    *,
    parent: int | None = None,
    kind: str = "task",
    **cols: Any,
) -> int:
    """Задача с заданным статусом; остальные колонки — прямой UPDATE."""
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status=status,
        auto_review=True,
        task_type=kind,
        parent_id=parent,
        priority="medium",
    )
    for col, value in cols.items():
        await db.execute(f"UPDATE tasks SET {col}=? WHERE id=?", (value, task_id))  # noqa: S608
    await db.commit()
    return task_id


async def principal(db: aiosqlite.Connection, kind: str, username: str) -> int:
    cur = await db.execute(
        "INSERT INTO principals (kind, username) VALUES (?, ?)", (kind, username)
    )
    await db.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


async def say(
    db: aiosqlite.Connection,
    task_id: int,
    kind: str,
    content: str,
    *,
    by: int | None = None,
    author_kind: str = "hub",
) -> None:
    await repo.add_task_update(
        db, task_id, "x", kind, content, principal_id=by, author_kind=author_kind
    )
    await db.commit()


async def slot(
    db: aiosqlite.Connection, task_id: int, channel: str, *, closed: bool = False
) -> None:
    await db.execute(
        "INSERT INTO executor_slots (task_id, channel, slot, released_at) "
        "VALUES (?, ?, 's1', ?)",
        (task_id, channel, "2026-10-01 00:00:00" if closed else None),
    )
    await db.commit()


def jobs(monkeypatch, table: dict[str, str]) -> None:
    """Слово реестра о каждом job; отсутствующего в таблице реестр не знает."""
    monkeypatch.setattr(
        plugins.dispatch,
        "get_job",
        lambda ref: {"id": ref, "status": table[ref]} if ref in table else None,
    )


def human(monkeypatch, name: str = "denis") -> dict[str, str]:
    """Закрытый режим с одним человеком; вернуть его заголовки."""
    monkeypatch.setattr(config, "HUB_TOKENS", {"h-token": TokenIdentity(name, "human")})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {"Authorization": "Bearer h-token"}


async def footprint(db: aiosqlite.Connection) -> tuple[int, int, int]:
    """(записей ленты, записей activity, архивных задач): всё, что пишет архив."""
    out = []
    for sql in (
        "SELECT COUNT(*) FROM task_updates",
        "SELECT COUNT(*) FROM activity_log",
        "SELECT COUNT(*) FROM tasks WHERE archived=1",
    ):
        out.append((await (await db.execute(sql)).fetchone())[0])
    return out[0], out[1], out[2]


async def archived(db: aiosqlite.Connection, task_id: int) -> bool:
    row = await repo.get_task(db, task_id)
    return bool(row["archived"])
