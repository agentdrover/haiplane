"""Тестовая база: собирается один раз на воркер, в тесте — копия файла (#1958).

Фикстура ``db`` в каждом тесте строила схему, прогоняла миграции и два seed: в
среднем 1,4 с setup против 0,1 с самого теста. Здесь та же сборка делается один
раз, а тест получает копию готового файла (несколько миллисекунд).

Сборка шаблона и полный путь — ОДНА функция (``apply_build``): два пути не могут
разойтись. Она зовёт функции через ``hub.db.<имя>``, а не через импорт в
conftest, поэтому подмена ``hub_db.seed_*`` или ``hub_db._MIGRATIONS`` видна.

Шаблон годится только пока всё, из чего он собран, такое же, как при загрузке
этого модуля (``pristine``). Тест, подменивший схему, миграции или функции
сборки ДО запроса ``db``, получает прежний полный путь и видит подмену.
Шаблон строится только в «чистом» состоянии: подменённое состояние никогда не
попадает в файл шаблона.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import aiosqlite

from hub import db as hub_db

_TEST_BOUNDARY = 999999999


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint() -> tuple:
    """Всё, из чего собирается база, по СОДЕРЖИМОМУ (не по id контейнера).

    Замена одной миграции внутри списка меняет хеш; id и длина списка её бы не
    заметили. Функции сравниваются по тождеству: подмена ``hub_db.seed_*``
    даёт другой объект.
    """
    return (
        _digest(hub_db._SCHEMA),
        _digest(repr([(name, sql) for name, sql in hub_db._MIGRATIONS])),
        # Входы seed: имя агента чат-пары и системные роли.
        hub_db.CHAT_PAIR_AGENT,
        _digest(repr(hub_db.SYSTEM_ROLES)),
        tuple(
            id(f)
            for f in (
                hub_db._migrate,
                hub_db._table_exists,
                hub_db.seed_system_roles,
                hub_db.seed_chat_pair_agent,
            )
        ),
    )


_PRISTINE = fingerprint()

# Путь к шаблону этого процесса (воркера xdist). None — ещё не собран.
_template: Path | None = None


def is_pristine() -> bool:
    return fingerprint() == _PRISTINE


async def open_connection(dsn: str) -> aiosqlite.Connection:
    """Соединение теста: те же PRAGMA и row_factory, что у фикстуры всегда."""
    conn = await aiosqlite.connect(dsn, uri=True)
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    return conn


async def apply_build(conn: aiosqlite.Connection) -> None:
    """Схема, миграции, водяной знак и seed — шаг в шаг как раньше в ``db``."""
    await conn.executescript(hub_db._SCHEMA)
    await hub_db._migrate(conn)
    # #1811: задачи тестов - «старые» для has_plain_summary, иначе каждое
    # одобрение в наборе требовало бы нового поля. Тесты правила сами
    # возвращают водяной знак (см. test_readiness).
    await conn.execute(
        "UPDATE plain_summary_boundary SET max_task_id=?", (_TEST_BOUNDARY,)
    )
    await conn.commit()
    if await hub_db._table_exists(conn, "roles"):
        await hub_db.seed_system_roles(conn)
    if await hub_db._table_exists(conn, "principals"):
        await hub_db.seed_chat_pair_agent(conn)


async def full_build(dsn: str) -> aiosqlite.Connection:
    """Полный путь без шаблона: открыть соединение и собрать базу."""
    conn = await open_connection(dsn)
    await apply_build(conn)
    return conn


async def _build_template(directory: Path) -> Path:
    path = directory / "template.db"
    conn = await full_build(str(path))
    await conn.commit()
    # Всё содержимое WAL — в основной файл: копируется один файл.
    await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await conn.close()
    return path


async def open_test_db(dsn: str, template_dir: Path) -> aiosqlite.Connection:
    """База теста: копия шаблона, а при подмене сборки — полный путь."""
    global _template
    if not is_pristine():
        return await full_build(dsn)
    if _template is None:
        _template = await _build_template(template_dir)
    shutil.copyfile(_template, dsn)
    return await open_connection(dsn)
