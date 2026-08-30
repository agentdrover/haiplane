"""Запись либо целая, либо её нет (#1065, эпик #1064).

До этой задачи хаб держал ОДНО соединение aiosqlite на процесс и делил его
между поллером, веб-роутами, REST и MCP-телеметрией. Неявная транзакция на
общем соединении означает, что ``commit()`` одной корутины фиксирует
незакоммиченное другой — включая открытый SAVEPOINT. Дыру описал сам хаб, в
docstring ``get_write_lock``: «under high cross-path concurrency a stray commit
could still flush an open SAVEPOINT», с наблюдавшимся симптомом «sporadic HTTP
500s where the write "sometimes still landed"», и отложил как «separate
hardening work».

Защитой был asyncio.Lock, но по коду на develop было 165 вызовов ``.commit()``
в 27 файлах против 13 взятий этого лока. Правило, которое держится на том,
вспомнил ли автор про ``async with``, — это не правило.

Здесь проверяется, что правило теперь держит база: соединение на запрос,
BEGIN IMMEDIATE, WAL, busy_timeout. Всё на ФАЙЛОВОЙ базе — к in-memory
journal_mode=WAL неприменим вовсе, она молча остаётся в режиме memory, и
проверять на ней режимы значило бы проверять собственную веру.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from hub import db as db_module


@pytest.fixture
async def file_db(tmp_path: Path):
    """Готовая файловая база и её DSN — то, что бывает на проде."""
    dsn = str(tmp_path / "hub.db")
    conn = await db_module.connect(dsn)
    await db_module.bootstrap(conn)
    yield dsn, conn
    await conn.close()


async def test_the_connection_opens_in_wal_with_a_busy_timeout(file_db):
    """AC-4: режимы выставлены на открытии, а не подразумеваются.

    WAL — чтобы читатель не блокировал писателя; без него соединение на запрос
    было бы медленнее общего. busy_timeout — чтобы конкуренция становилась
    ожиданием, а не ошибкой: ноль по умолчанию означает мгновенный
    "database is locked".
    """
    dsn, conn = file_db
    mode = await (await conn.execute("PRAGMA journal_mode")).fetchone()
    assert mode[0].lower() == "wal"

    timeout = await (await conn.execute("PRAGMA busy_timeout")).fetchone()
    assert timeout[0] >= 1000, "нулевой busy_timeout превращает очередь в отказ"

    foreign_keys = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
    assert foreign_keys[0] == 1


async def test_a_commit_does_not_flush_another_writer(file_db):
    """AC-1: коммит одного писателя не фиксирует незавершённое другого.

    Ровно тот отказ, что описан в docstring get_write_lock. На общем соединении
    он воспроизводился; на двух соединениях его нет по построению, и тест
    закрепляет именно это — а не то, что «мы вроде поправили».
    """
    dsn, _ = file_db
    slow = await db_module.connect(dsn)
    reader = await db_module.connect(dsn)
    try:
        # Медленный писатель начал многошаговую правку и НЕ завершил её.
        await slow.execute("BEGIN IMMEDIATE")
        await slow.execute(
            "INSERT INTO tasks (title, description, status) VALUES (?,?,?)",
            ("половина работы", "", "draft"),
        )

        # Чужая половина не видна снаружи, пока не закоммичена.
        seen = await db_module.fetchall(reader, "SELECT title FROM tasks")
        assert [r["title"] for r in seen] == []

        # И откат действительно её убирает. На общем соединении к этому
        # моменту её мог зафиксировать чужой commit — тогда rollback уже
        # ничего бы не отменил, и запись «всё-таки легла».
        await slow.rollback()
        seen = await db_module.fetchall(reader, "SELECT title FROM tasks")
        assert [r["title"] for r in seen] == []
    finally:
        await slow.close()
        await reader.close()


async def test_a_failed_write_leaves_no_partial_row(file_db):
    """AC-2: упавшая многошаговая правка не оставляет первого шага.

    Это то, ради чего нужна явная транзакция, а не только раздельные
    соединения: без неё первый шаг коммитится сам по себе и переживает падение
    второго.
    """
    dsn, conn = file_db

    with pytest.raises(RuntimeError):
        async with db_module.write_transaction(conn):
            await conn.execute(
                "INSERT INTO tasks (title, description, status) VALUES (?,?,?)",
                ("шаг один", "", "draft"),
            )
            raise RuntimeError("шаг два упал")

    rows = await db_module.fetchall(conn, "SELECT title FROM tasks")
    assert [r["title"] for r in rows] == []


async def test_a_write_transaction_commits_the_whole_block(file_db):
    """Обратная сторона: успешный блок виден целиком и другому соединению."""
    dsn, conn = file_db
    async with db_module.write_transaction(conn):
        await conn.execute(
            "INSERT INTO tasks (title, description, status) VALUES (?,?,?)",
            ("целиком", "", "draft"),
        )

    other = await db_module.connect(dsn)
    try:
        rows = await db_module.fetchall(other, "SELECT title FROM tasks")
        assert [r["title"] for r in rows] == ["целиком"]
    finally:
        await other.close()


async def test_a_nested_write_transaction_does_not_commit_its_owner(file_db):
    """Вложенный блок не коммитит чужую транзакцию.

    Иначе обёртка воспроизвела бы ровно ту проблему, ради которой заводилась:
    внутренний блок фиксировал бы незавершённую работу внешнего.
    """
    dsn, conn = file_db

    with pytest.raises(RuntimeError):
        async with db_module.write_transaction(conn):
            await conn.execute(
                "INSERT INTO tasks (title, description, status) VALUES (?,?,?)",
                ("внешний", "", "draft"),
            )
            async with db_module.write_transaction(conn):
                await conn.execute(
                    "INSERT INTO tasks (title, description, status) VALUES (?,?,?)",
                    ("внутренний", "", "draft"),
                )
            raise RuntimeError("внешний упал уже после внутреннего")

    rows = await db_module.fetchall(conn, "SELECT title FROM tasks")
    assert [r["title"] for r in rows] == [], (
        "внутренний блок закоммитил чужую транзакцию — обёртка воспроизводит "
        "ту самую дыру, которую закрывает"
    )


async def test_migrations_run_once_at_startup(tmp_path: Path):
    """AC-5: миграции и seed — работа подъёма, а не каждого соединения.

    Проверяется наблюдаемым следствием, а не счётчиком вызовов: соединение,
    открытое через connect(), не создаёт схему. Если бы bootstrap уехал в
    connect(), каждый запрос платил бы за миграции и seed.
    """
    dsn = str(tmp_path / "fresh.db")

    bare = await db_module.connect(dsn)
    try:
        with pytest.raises(aiosqlite.Error):
            await bare.execute("SELECT 1 FROM tasks")
    finally:
        await bare.close()

    ready = await db_module.connect(dsn)
    try:
        await db_module.bootstrap(ready)
        await ready.execute("SELECT 1 FROM tasks")
    finally:
        await ready.close()


async def test_concurrent_writers_queue_instead_of_failing(file_db):
    """Писатели встают в очередь, а не отказывают.

    Это то, ради чего BEGIN IMMEDIATE, а не DEFERRED: на DEFERRED-пути SQLite
    отдаёт SQLITE_BUSY немедленно и busy-handler не зовёт вовсе, потому что
    соединение апгрейдит чтение до записи. Измерено на живом коде: двенадцать
    параллельных добавлений AC падали с "database is locked" при
    busy_timeout=5000, пока транзакция была отложенной.
    """
    dsn, _ = file_db

    async def writer(n: int) -> None:
        conn = await db_module.connect(dsn)
        try:
            async with db_module.write_transaction(conn):
                await conn.execute(
                    "INSERT INTO tasks (title, description, status) VALUES (?,?,?)",
                    (f"писатель {n}", "", "draft"),
                )
        finally:
            await conn.close()

    await asyncio.gather(*(writer(i) for i in range(12)))

    reader = await db_module.connect(dsn)
    try:
        rows = await db_module.fetchall(reader, "SELECT title FROM tasks")
        assert len(rows) == 12, "ни одна запись не должна потеряться в очереди"
    finally:
        await reader.close()
