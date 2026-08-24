"""Defect passport: the stage a defect was caught at, and what caused it (#909).

The metric that answers "what leaks to production" currently reconstructs the
answer from the ``completed_at`` of a feature ancestor. These tests pin the
opposite property: the stage is a recorded fact, the default is an honest
``unknown``, and a causal link that does not resolve is refused rather than
stored.
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub.db import _MIGRATIONS, _SCHEMA, _migrate, validate_caused_by
from hub.models import DefectFoundIn
from hub.repository import DefectPassportError, set_defect_passport

PASSPORT_COLUMNS = ("found_in", "caused_by_task_id", "detected_at", "resolved_at")


async def _table_columns(conn: aiosqlite.Connection, table: str) -> dict[str, dict]:
    rows = await conn.execute_fetchall(f"PRAGMA table_info({table})")
    return {row["name"]: dict(row) for row in rows}


async def _insert_task(conn: aiosqlite.Connection, title: str) -> int:
    cur = await conn.execute(
        "INSERT INTO tasks (title, description, status) VALUES (?, '', 'open')",
        (title,),
    )
    return cur.lastrowid


async def _make_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.executescript(_SCHEMA)
    await _migrate(conn)
    return conn


async def test_passport_columns_present():
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        missing = set(PASSPORT_COLUMNS) - set(cols)
        assert not missing, f"missing passport columns: {missing}"
    finally:
        await conn.close()


async def test_migration_is_idempotent():
    """A second pass over the migration list must not fail or change the schema.

    The runner marks each migration applied, but a re-run against a database
    that already has the column has to be a no-op too — that is the path a
    restarted production process takes.
    """
    conn = await _make_db()
    try:
        before = await _table_columns(conn, "tasks")
        await _migrate(conn)
        await conn.execute("DELETE FROM _migrations")
        await _migrate(conn)
        after = await _table_columns(conn, "tasks")
        assert set(before) == set(after)
    finally:
        await conn.close()


async def test_migration_defaults_to_unknown():
    """Rows that predate the column read as 'unknown', never as a guess.

    Back-filling a stage from timestamps would manufacture the very number the
    passport exists to replace, so the migration states ignorance instead.
    """
    conn = await aiosqlite.connect(":memory:")
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_SCHEMA)
        # Pre-mark the passport migrations as applied so the first pass builds
        # the schema as it was BEFORE this task, then insert a row into it and
        # let the passport migration run over existing data — the production
        # path, not a fresh database.
        passport = {
            "add_found_in_column",
            "add_caused_by_task_id_column",
            "add_detected_at_column",
            "add_resolved_at_column",
            "idx_tasks_found_in",
            "idx_tasks_caused_by",
        }
        assert passport <= {name for name, _ in _MIGRATIONS}, "renamed migration?"
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations "
            "(name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
        )
        await conn.executemany(
            "INSERT OR IGNORE INTO _migrations (name) VALUES (?)",
            [(name,) for name in sorted(passport)],
        )
        await _migrate(conn)

        cols = await _table_columns(conn, "tasks")
        assert not set(PASSPORT_COLUMNS) & set(cols), (
            "setup should predate the passport"
        )
        legacy_id = await _insert_task(conn, "баг, заведённый до паспорта")

        await conn.executemany(
            "DELETE FROM _migrations WHERE name=?",
            [(name,) for name in sorted(passport)],
        )
        await _migrate(conn)

        rows = await conn.execute_fetchall(
            "SELECT found_in, caused_by_task_id, detected_at, resolved_at "
            "FROM tasks WHERE id=?",
            (legacy_id,),
        )
        row = rows[0]
        assert row["found_in"] == DefectFoundIn.unknown.value
        assert row["caused_by_task_id"] is None
        assert row["detected_at"] is None
        assert row["resolved_at"] is None
    finally:
        await conn.close()


async def test_new_task_starts_unknown(db):
    task_id = await _insert_task(db, "новый дефект")
    rows = await db.execute_fetchall(
        "SELECT found_in FROM tasks WHERE id=?", (task_id,)
    )
    assert rows[0]["found_in"] == DefectFoundIn.unknown.value


async def test_set_passport_writes_stage_and_cause(db):
    cause_id = await _insert_task(db, "изменение, которое сломало")
    defect_id = await _insert_task(db, "дефект с прода")

    applied = await set_defect_passport(
        db,
        defect_id,
        found_in=DefectFoundIn.prod.value,
        caused_by_task_id=cause_id,
        detected_at="2026-08-22 06:00:00",
    )

    assert applied == {
        "found_in": "prod",
        "caused_by_task_id": cause_id,
        "detected_at": "2026-08-22 06:00:00",
    }
    rows = await db.execute_fetchall(
        "SELECT found_in, caused_by_task_id, detected_at, resolved_at "
        "FROM tasks WHERE id=?",
        (defect_id,),
    )
    row = rows[0]
    assert row["found_in"] == "prod"
    assert row["caused_by_task_id"] == cause_id
    assert row["detected_at"] == "2026-08-22 06:00:00"
    assert row["resolved_at"] is None


async def test_partial_write_leaves_the_rest_alone(db):
    defect_id = await _insert_task(db, "дефект")
    await set_defect_passport(
        db, defect_id, found_in="ci", detected_at="2026-08-22 06:00:00"
    )

    await set_defect_passport(db, defect_id, resolved_at="2026-08-22 07:00:00")

    rows = await db.execute_fetchall(
        "SELECT found_in, detected_at, resolved_at FROM tasks WHERE id=?",
        (defect_id,),
    )
    row = rows[0]
    assert row["found_in"] == "ci"
    assert row["detected_at"] == "2026-08-22 06:00:00"
    assert row["resolved_at"] == "2026-08-22 07:00:00"


async def test_invalid_found_in_is_refused(db):
    defect_id = await _insert_task(db, "дефект")

    with pytest.raises(DefectPassportError) as exc:
        await set_defect_passport(db, defect_id, found_in="production")

    message = str(exc.value)
    assert "production" in message
    assert "staging" in message and "prod" in message, "error must list the stages"
    rows = await db.execute_fetchall(
        "SELECT found_in FROM tasks WHERE id=?", (defect_id,)
    )
    assert rows[0]["found_in"] == "unknown", "a refused write must not land"


async def test_caused_by_must_resolve(db):
    defect_id = await _insert_task(db, "дефект")

    with pytest.raises(DefectPassportError) as exc:
        await set_defect_passport(db, defect_id, caused_by_task_id=999_999)

    assert "999999" in str(exc.value).replace(" ", "")
    rows = await db.execute_fetchall(
        "SELECT caused_by_task_id FROM tasks WHERE id=?", (defect_id,)
    )
    assert rows[0]["caused_by_task_id"] is None


async def test_stage_write_is_not_applied_when_cause_is_bad(db):
    """A rejected link must not leave the stage half-written.

    ``set_defect_passport`` validates before it writes; without that ordering a
    caller passing both fields would get a stored stage and a refusal in the
    same call.
    """
    defect_id = await _insert_task(db, "дефект")

    with pytest.raises(DefectPassportError):
        await set_defect_passport(
            db, defect_id, found_in="prod", caused_by_task_id=999_999
        )

    rows = await db.execute_fetchall(
        "SELECT found_in FROM tasks WHERE id=?", (defect_id,)
    )
    assert rows[0]["found_in"] == "unknown"


async def test_task_cannot_cause_itself(db):
    defect_id = await _insert_task(db, "дефект")

    with pytest.raises(DefectPassportError):
        await set_defect_passport(db, defect_id, caused_by_task_id=defect_id)


async def test_clearing_the_cause_is_explicit(db):
    cause_id = await _insert_task(db, "изменение")
    defect_id = await _insert_task(db, "дефект")
    await set_defect_passport(db, defect_id, caused_by_task_id=cause_id)

    # Omitting the field leaves the attribution untouched...
    await set_defect_passport(db, defect_id, found_in="prod")
    rows = await db.execute_fetchall(
        "SELECT caused_by_task_id FROM tasks WHERE id=?", (defect_id,)
    )
    assert rows[0]["caused_by_task_id"] == cause_id

    # ...dropping it takes a deliberate flag.
    applied = await set_defect_passport(db, defect_id, clear_caused_by=True)
    assert applied == {"caused_by_task_id": None}
    rows = await db.execute_fetchall(
        "SELECT caused_by_task_id FROM tasks WHERE id=?", (defect_id,)
    )
    assert rows[0]["caused_by_task_id"] is None


async def test_empty_call_writes_nothing(db):
    defect_id = await _insert_task(db, "дефект")
    assert await set_defect_passport(db, defect_id) == {}


async def test_validate_caused_by_allows_none(db):
    defect_id = await _insert_task(db, "дефект")
    assert await validate_caused_by(db, defect_id, None) is None


async def test_passport_is_visible_in_task_view(db):
    """A column stored and not surfaced is a column nobody can read back."""
    from hub import repository as repo
    from hub.services import row_to_task

    cause_id = await _insert_task(db, "изменение")
    defect_id = await _insert_task(db, "дефект")
    await set_defect_passport(
        db,
        defect_id,
        found_in="prod",
        caused_by_task_id=cause_id,
        detected_at="2026-08-22 06:00:00",
        resolved_at="2026-08-22 07:30:00",
    )

    view = row_to_task(await repo.get_task(db, defect_id))

    assert view.found_in is DefectFoundIn.prod
    assert view.caused_by_task_id == cause_id
    assert view.detected_at == "2026-08-22 06:00:00"
    assert view.resolved_at == "2026-08-22 07:30:00"


async def test_task_view_defaults_to_unknown(db):
    from hub import repository as repo
    from hub.services import row_to_task

    task_id = await _insert_task(db, "обычная задача")

    view = row_to_task(await repo.get_task(db, task_id))

    assert view.found_in is DefectFoundIn.unknown
    assert view.caused_by_task_id is None


# ---------------------------------------------------------------------------
# Surfaces (#910): the passport is filled through refine, not through SQL
# ---------------------------------------------------------------------------


async def _api_task(client, **fields) -> int:
    resp = await client.post("/api/tasks", json={"title": "дефект", **fields})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def test_refine_writes_passport_fields(client):
    cause_id = await _api_task(client, title="изменение")
    defect_id = await _api_task(client, work_type="bug")

    resp = await client.post(
        f"/api/tasks/{defect_id}/refine",
        json={
            "found_in": "prod",
            "caused_by_task_id": cause_id,
            "detected_at": "2026-08-22 06:00:00",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found_in"] == "prod"
    assert body["caused_by_task_id"] == cause_id
    assert body["detected_at"] == "2026-08-22 06:00:00"


async def test_invalid_found_in_is_refused_over_api(client):
    """A bad stage is a 4xx that names the stages, never a 500.

    Deliberately NOT named like the repository-level test above: two functions
    with one name in a module leave the first silently uncollected, which is
    how a test disappears without anyone deleting it.
    """
    defect_id = await _api_task(client, work_type="bug")

    resp = await client.post(
        f"/api/tasks/{defect_id}/refine", json={"found_in": "production"}
    )

    assert resp.status_code in (400, 422), resp.text
    assert "prod" in resp.text and "staging" in resp.text


async def test_caused_by_must_resolve_over_api(client):
    defect_id = await _api_task(client, work_type="bug")

    resp = await client.post(
        f"/api/tasks/{defect_id}/refine", json={"caused_by_task_id": 999999}
    )

    assert resp.status_code == 422, resp.text
    assert "999999" in resp.text.replace(" ", "")
    read = await client.get(f"/api/tasks/{defect_id}")
    assert read.json()["caused_by_task_id"] is None


async def test_refused_passport_rolls_back_the_whole_refine(client):
    """The refine is one transaction: a refused link takes the rest with it.

    Without this the caller gets an error and a partially applied PATCH, which
    is the worst of both — the write they were told did not happen.
    """
    defect_id = await _api_task(client, work_type="bug")

    resp = await client.post(
        f"/api/tasks/{defect_id}/refine",
        json={"problem_statement": "что-то сломалось", "caused_by_task_id": 999999},
    )

    assert resp.status_code == 422, resp.text
    read = await client.get(f"/api/tasks/{defect_id}")
    assert read.json()["problem_statement"] == ""


async def test_passport_is_not_written_as_a_plain_column(client):
    """found_in must travel through the validating writer, not the column PATCH.

    If the field ever rejoins ``structured_fields_to_db``, this test still
    passes on the happy path — so it checks the property that actually breaks:
    the value lands and the causal link is still validated alongside it.
    """
    defect_id = await _api_task(client, work_type="bug")

    ok = await client.post(f"/api/tasks/{defect_id}/refine", json={"found_in": "ci"})
    assert ok.status_code == 200
    bad = await client.post(
        f"/api/tasks/{defect_id}/refine",
        json={"found_in": "review", "caused_by_task_id": 999999},
    )

    assert bad.status_code == 422
    read = await client.get(f"/api/tasks/{defect_id}")
    assert read.json()["found_in"] == "ci", "the refused call must change nothing"


async def test_clear_caused_by_over_api(client):
    cause_id = await _api_task(client, title="изменение")
    defect_id = await _api_task(client, work_type="bug")
    await client.post(
        f"/api/tasks/{defect_id}/refine", json={"caused_by_task_id": cause_id}
    )

    resp = await client.post(
        f"/api/tasks/{defect_id}/refine", json={"clear_caused_by": True}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["caused_by_task_id"] is None


async def test_refine_without_passport_keys_touches_nothing(client):
    defect_id = await _api_task(client, work_type="bug")
    await client.post(f"/api/tasks/{defect_id}/refine", json={"found_in": "test"})

    resp = await client.post(
        f"/api/tasks/{defect_id}/refine", json={"problem_statement": "уточнение"}
    )

    assert resp.status_code == 200
    assert resp.json()["found_in"] == "test"


def test_cli_refine_builds_passport_payload():
    """The CLI speaks the same PATCH as REST and MCP (#833 surface parity)."""
    import argparse

    from hub.cli import _build_refine_payload

    args = argparse.Namespace(
        found_in="prod", caused_by=42, detected_at="2026-08-22 06:00:00"
    )

    payload = _build_refine_payload(args)

    assert payload == {
        "found_in": "prod",
        "caused_by_task_id": 42,
        "detected_at": "2026-08-22 06:00:00",
    }


def test_cli_clear_caused_by_is_a_verb():
    import argparse

    from hub.cli import _build_refine_payload

    assert _build_refine_payload(argparse.Namespace(clear_caused_by=True)) == {
        "clear_caused_by": True
    }
    assert _build_refine_payload(argparse.Namespace(clear_caused_by=False)) == {}
