"""Шаблон тестовой базы (#1958): копия равна полной сборке, подмена видна.

Эталон полной сборки записан здесь ДОСЛОВНО по прежнему коду фикстуры ``db`` и
не использует ``tests/template_db_support.py``: иначе шаблон и «полный путь»
были бы одной функцией и сравнение ничего не ловило бы.
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import db as hub_db
from tests import template_db_support as support


async def _reference_build(dsn: str) -> aiosqlite.Connection:
    """Прежняя сборка из фикстуры ``db``, шаг в шаг."""
    conn = await aiosqlite.connect(dsn, uri=True)
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.executescript(hub_db._SCHEMA)
    await hub_db._migrate(conn)
    await conn.execute("UPDATE plain_summary_boundary SET max_task_id=999999999")
    await conn.commit()
    if await hub_db._table_exists(conn, "roles"):
        await hub_db.seed_system_roles(conn)
    if await hub_db._table_exists(conn, "principals"):
        await hub_db.seed_chat_pair_agent(conn)
    return conn


def _normal(column: str, value):
    """Столбцы ``*_at`` заполняются ``datetime('now')``: важно наличие значения."""
    if column.endswith("_at"):
        return value is not None
    return value


async def _dump(conn: aiosqlite.Connection) -> dict:
    """Схема, PRAGMA и содержимое ВСЕХ таблиц, время нормализовано."""
    cursor = await conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    )
    master = [tuple(r) for r in await cursor.fetchall()]
    dump: dict = {"master": master}
    for pragma in ("user_version", "journal_mode", "foreign_keys", "busy_timeout"):
        cursor = await conn.execute(f"PRAGMA {pragma}")
        dump[pragma] = tuple((await cursor.fetchone()))
    for kind, name, _tbl, _sql in master:
        # sqlite_sequence — счётчики AUTOINCREMENT: от них зависит следующий id.
        if kind != "table" or (
            name.startswith("sqlite_") and name != "sqlite_sequence"
        ):
            continue
        cursor = await conn.execute(f'SELECT * FROM "{name}"')
        columns = [d[0] for d in cursor.description]
        rows = [
            tuple(_normal(c, v) for c, v in zip(columns, row))
            for row in await cursor.fetchall()
        ]
        dump[name] = sorted(rows, key=repr)
    return dump


async def test_template_copy_equals_full_build(tmp_path, tmp_path_factory):
    """AC-4: база из шаблона = база прежней полной сборки."""
    copy = await support.open_test_db(
        str(tmp_path / "copy.db"), tmp_path_factory.mktemp("tpl")
    )
    reference = await _reference_build(str(tmp_path / "reference.db"))
    try:
        got, want = await _dump(copy), await _dump(reference)
    finally:
        await copy.close()
        await reference.close()
    assert got["master"] == want["master"]
    for key in want:
        assert got[key] == want[key], key
    # Предохранитель самого сравнения: оно видит данные, а не пустые таблицы.
    assert want["roles"] and want["principals"] and want["plain_summary_boundary"]
    assert want["plain_summary_boundary"] == [(1, 999999999)]
    assert want["journal_mode"] == ("wal",)
    assert want["foreign_keys"] == (1,) and want["busy_timeout"] == (5000,)


async def test_pristine_requests_copy_one_template(
    tmp_path, tmp_path_factory, monkeypatch
):
    """Шаблон строится один раз на воркер; следующие базы — копии."""
    directory = tmp_path_factory.mktemp("tpl")
    warm = await support.open_test_db(str(tmp_path / "warm.db"), directory)
    await warm.close()
    builds: list[str] = []
    real = support.full_build

    async def counting(dsn):
        builds.append(dsn)
        return await real(dsn)

    monkeypatch.setattr(support, "full_build", counting)
    conn = await support.open_test_db(str(tmp_path / "next.db"), directory)
    await conn.close()
    assert builds == []


@pytest.fixture
def extra_migration(monkeypatch):
    """Подмена миграций ДО того, как тест запросил ``db``."""
    probe = ("zz_probe_1958", "CREATE TABLE zz_probe_1958 (x INTEGER)")
    monkeypatch.setattr(hub_db, "_MIGRATIONS", [*hub_db._MIGRATIONS, probe])


@pytest.fixture
def warm_template(tmp_path_factory):
    """Шаблон уже собран в чистом состоянии (так бывает в любом воркере)."""

    async def _warm(tmp_path):
        conn = await support.open_test_db(
            str(tmp_path / "warm.db"), tmp_path_factory.mktemp("tpl")
        )
        await conn.close()

    return _warm


async def test_substitution_before_db_takes_the_full_path(
    tmp_path, tmp_path_factory, warm_template, monkeypatch
):
    """AC-3: подмена миграций до ``db`` — база собрана заново и видит её."""
    await warm_template(tmp_path)
    probe = ("zz_probe_1958", "CREATE TABLE zz_probe_1958 (x INTEGER)")
    monkeypatch.setattr(hub_db, "_MIGRATIONS", [*hub_db._MIGRATIONS, probe])
    conn = await support.open_test_db(
        str(tmp_path / "patched.db"), tmp_path_factory.mktemp("tpl")
    )
    try:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'zz_probe_1958'"
        )
        assert await cursor.fetchone() is not None
    finally:
        await conn.close()


async def test_substitution_of_a_seed_function_is_seen(
    tmp_path, tmp_path_factory, warm_template, monkeypatch
):
    """AC-3: подмена seed до ``db`` вызывается — шаблон её не заслоняет."""
    await warm_template(tmp_path)
    calls: list[int] = []
    original = hub_db.seed_system_roles

    async def spy(db):
        calls.append(1)
        await original(db)

    monkeypatch.setattr(hub_db, "seed_system_roles", spy)
    conn = await support.open_test_db(
        str(tmp_path / "patched.db"), tmp_path_factory.mktemp("tpl")
    )
    await conn.close()
    assert calls == [1]


async def test_a_replaced_migration_inside_the_list_is_seen(
    tmp_path, tmp_path_factory, warm_template, monkeypatch
):
    """Отпечаток по содержимому: та же длина списка, другой SQL одной миграции."""
    await warm_template(tmp_path)
    migrations = list(hub_db._MIGRATIONS)
    migrations.append(("zz_swap_1958", "CREATE TABLE zz_swap_1958 (x INTEGER)"))
    monkeypatch.setattr(hub_db, "_MIGRATIONS", migrations)
    first = support.fingerprint()
    swapped = list(migrations)
    swapped[-1] = ("zz_swap_1958", "CREATE TABLE zz_swap_1958 (y INTEGER)")
    monkeypatch.setattr(hub_db, "_MIGRATIONS", swapped)
    assert len(swapped) == len(migrations)
    assert support.fingerprint() != first
    assert not support.is_pristine()


async def test_substitution_never_reaches_the_template(
    tmp_path, tmp_path_factory, monkeypatch
):
    """Шаблон строится только в чистом состоянии: подмена в файл не попадает."""
    monkeypatch.setattr(support, "_template", None)
    probe = ("zz_probe_1958", "CREATE TABLE zz_probe_1958 (x INTEGER)")
    with monkeypatch.context() as patched:
        patched.setattr(hub_db, "_MIGRATIONS", [*hub_db._MIGRATIONS, probe])
        conn = await support.open_test_db(
            str(tmp_path / "patched.db"), tmp_path_factory.mktemp("tpl")
        )
        await conn.close()
    assert support._template is None
    clean = await support.open_test_db(
        str(tmp_path / "clean.db"), tmp_path_factory.mktemp("tpl")
    )
    try:
        cursor = await clean.execute(
            "SELECT name FROM sqlite_master WHERE name = 'zz_probe_1958'"
        )
        assert await cursor.fetchone() is None
    finally:
        await clean.close()


async def test_db_fixture_after_patch_in_a_fixture_sees_the_patch(extra_migration, db):
    """Через настоящую фикстуру ``db``: подмена в фикстуре, запрошенной раньше."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE name = 'zz_probe_1958'"
    )
    assert await cursor.fetchone() is not None


async def test_a_replaced_chat_pair_agent_is_seen(
    tmp_path, tmp_path_factory, warm_template, monkeypatch
):
    """Вход seed (имя агента чат-пары) подменён до ``db`` — база его видит."""
    await warm_template(tmp_path)
    monkeypatch.setattr(hub_db, "CHAT_PAIR_AGENT", "zz-agent-1958")
    conn = await support.open_test_db(
        str(tmp_path / "patched.db"), tmp_path_factory.mktemp("tpl")
    )
    try:
        cursor = await conn.execute(
            "SELECT username FROM principals WHERE username = 'zz-agent-1958'"
        )
        assert await cursor.fetchone() is not None
    finally:
        await conn.close()


async def test_the_dump_sees_autoincrement_counters(tmp_path):
    """Канарейка сравнивает и счётчики ``sqlite_sequence`` (следующий id)."""
    one = await _reference_build(str(tmp_path / "one.db"))
    two = await _reference_build(str(tmp_path / "two.db"))
    try:
        await two.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES ('zz_1958', 999)"
        )
        assert await _dump(one) != await _dump(two)
    finally:
        await one.close()
        await two.close()


async def test_a_replaced_system_role_list_is_seen(
    tmp_path, tmp_path_factory, warm_template, monkeypatch
):
    """Вход seed (список системных ролей) подменён до ``db`` — база его видит."""
    await warm_template(tmp_path)
    extra = ("zz-role-1958", "ZZ", "probe", ("tasks.read",))
    monkeypatch.setattr(hub_db, "SYSTEM_ROLES", (*hub_db.SYSTEM_ROLES, extra))
    conn = await support.open_test_db(
        str(tmp_path / "patched.db"), tmp_path_factory.mktemp("tpl")
    )
    try:
        cursor = await conn.execute(
            "SELECT slug FROM roles WHERE slug = 'zz-role-1958'"
        )
        assert await cursor.fetchone() is not None
    finally:
        await conn.close()
