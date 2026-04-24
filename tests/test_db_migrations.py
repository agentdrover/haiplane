from __future__ import annotations

import aiosqlite
import pytest

from hub.db import (
    _MIGRATIONS,
    _SCHEMA,
    _migrate,
    deserialize_risks,
    deserialize_str_list,
    serialize_risks,
    serialize_str_list,
)

STRUCTURED_TASK_COLUMNS = {
    "work_type",
    "class_of_service",
    "size",
    "wip_tag",
    "due_date",
    "user_story",
    "problem_statement",
    "business_value",
    "scope_in",
    "scope_out",
    "affected_areas",
    "technical_hints",
    "constraints",
    "assumptions",
    "validation_commands",
    "out_of_scope_for_review",
    "risks",
    "readiness_score",
    "dor_passed",
    "ready_at",
    "started_at",
    "completed_at",
}


async def _table_columns(conn: aiosqlite.Connection, table: str) -> dict[str, dict]:
    rows = await conn.execute_fetchall(f"PRAGMA table_info({table})")
    return {row["name"]: dict(row) for row in rows}


async def _make_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.executescript(_SCHEMA)
    await _migrate(conn)
    return conn


async def test_structured_task_columns_present():
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        missing = STRUCTURED_TASK_COLUMNS - set(cols)
        assert not missing, f"missing columns after migration: {missing}"
    finally:
        await conn.close()


@pytest.mark.parametrize(
    "column, expected_default",
    [
        ("work_type", "'feature'"),
        ("class_of_service", "'standard'"),
        ("user_story", "''"),
        ("problem_statement", "''"),
        ("business_value", "''"),
        ("scope_in", "'[]'"),
        ("scope_out", "'[]'"),
        ("affected_areas", "'[]'"),
        ("technical_hints", "''"),
        ("constraints", "'[]'"),
        ("assumptions", "'[]'"),
        ("validation_commands", "'[]'"),
        ("out_of_scope_for_review", "'[]'"),
        ("risks", "'[]'"),
    ],
)
async def test_structured_columns_have_safe_defaults(
    column: str, expected_default: str
):
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        assert cols[column]["dflt_value"] == expected_default
        assert cols[column]["notnull"] == 1
    finally:
        await conn.close()


@pytest.mark.parametrize(
    "column",
    [
        "size",
        "wip_tag",
        "due_date",
        "readiness_score",
        "dor_passed",
        "ready_at",
        "started_at",
        "completed_at",
    ],
)
async def test_optional_columns_are_nullable(column: str):
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        assert cols[column]["notnull"] == 0
    finally:
        await conn.close()


async def test_acceptance_criteria_table_created():
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "acceptance_criteria")
        expected = {
            "id",
            "task_id",
            "ac_id",
            "given",
            "when_clause",
            "then_clause",
            "verifiable_by",
            "test_ref",
            "position",
            "created_at",
        }
        assert expected <= set(cols)
    finally:
        await conn.close()


async def test_acceptance_criteria_unique_per_task():
    conn = await _make_db()
    try:
        await conn.execute("INSERT INTO tasks (title, description) VALUES ('t', '')")
        await conn.commit()
        await conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, ac_id, given, when_clause, then_clause, verifiable_by) "
            "VALUES (1, 'AC-1', 'g', 'w', 't', 'test')"
        )
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO acceptance_criteria "
                "(task_id, ac_id, given, when_clause, then_clause, verifiable_by) "
                "VALUES (1, 'AC-1', 'g2', 'w2', 't2', 'manual')"
            )
            await conn.commit()
    finally:
        await conn.close()


async def test_acceptance_criteria_cascade_on_task_delete():
    conn = await _make_db()
    try:
        await conn.execute("INSERT INTO tasks (title, description) VALUES ('t', '')")
        await conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, ac_id, given, when_clause, then_clause, verifiable_by) "
            "VALUES (1, 'AC-1', 'g', 'w', 't', 'test')"
        )
        await conn.commit()
        await conn.execute("DELETE FROM tasks WHERE id=1")
        await conn.commit()
        rows = await conn.execute_fetchall(
            "SELECT id FROM acceptance_criteria WHERE task_id=1"
        )
        assert rows == []
    finally:
        await conn.close()


async def test_failed_migration_is_not_marked_as_applied(monkeypatch):
    """Regression for review I2: if a migration's SQL fails, _migrate
    must raise and must NOT record the migration as applied. Otherwise
    the next start silently skips a broken step and the schema diverges
    forever."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.executescript(_SCHEMA)
    # Inject a deliberately-broken migration.
    from hub import db as db_module

    bad_migration = (
        "test_broken_migration",
        "ALTER TABLE tasks ADD COLUMN ; -- syntax error",
    )
    original = list(db_module._MIGRATIONS)
    monkeypatch.setattr(db_module, "_MIGRATIONS", original + [bad_migration])
    try:
        with pytest.raises(Exception):
            await _migrate(conn)
        rows = await conn.execute_fetchall(
            "SELECT name FROM _migrations WHERE name=?", (bad_migration[0],)
        )
        assert rows == [], "broken migration must not be recorded as applied"
    finally:
        await conn.close()


async def test_migrations_are_idempotent():
    conn = await _make_db()
    try:
        before = await conn.execute_fetchall(
            "SELECT name FROM _migrations ORDER BY name"
        )
        await _migrate(conn)
        await _migrate(conn)
        after = await conn.execute_fetchall(
            "SELECT name FROM _migrations ORDER BY name"
        )
        assert [r[0] for r in before] == [r[0] for r in after]
        expected_names = {name for name, _ in _MIGRATIONS}
        assert expected_names <= {r[0] for r in after}
    finally:
        await conn.close()


def test_serialize_str_list_roundtrip():
    items = ["alpha", "beta", "юникод"]
    raw = serialize_str_list(items)
    assert deserialize_str_list(raw) == items


def test_serialize_str_list_empty_inputs():
    assert serialize_str_list(None) == "[]"
    assert serialize_str_list([]) == "[]"
    assert deserialize_str_list("[]") == []
    assert deserialize_str_list(None) == []
    assert deserialize_str_list("") == []


def test_deserialize_str_list_invalid_json_returns_empty():
    assert deserialize_str_list("{not json") == []
    assert deserialize_str_list('"a string, not a list"') == []
    assert deserialize_str_list("123") == []


def test_deserialize_str_list_coerces_items_to_str():
    assert deserialize_str_list('[1, 2, "x"]') == ["1", "2", "x"]


def test_serialize_risks_roundtrip():
    risks = [
        {
            "kind": "external_dependency",
            "severity": "high",
            "description": "Vast API may be unavailable",
            "mitigation": "Add retry with backoff",
        }
    ]
    raw = serialize_risks(risks)
    assert deserialize_risks(raw) == risks


def test_serialize_risks_empty_inputs():
    assert serialize_risks(None) == "[]"
    assert serialize_risks([]) == "[]"
    assert deserialize_risks(None) == []
    assert deserialize_risks("") == []


def test_deserialize_risks_drops_non_dict_items():
    assert deserialize_risks('[{"k": 1}, "bad", 42, null]') == [{"k": 1}]


def test_deserialize_risks_invalid_json_returns_empty():
    assert deserialize_risks("not-json") == []
    assert deserialize_risks('"some string"') == []
