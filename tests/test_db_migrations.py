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
    "review_checklist",
    "risks",
    "readiness_score",
    "dor_passed",
    "ready_at",
    "started_at",
    "completed_at",
    "prepared_by",
    "prepared_at",
    "human_owner",
    "human_reviewer",
    "risk_class",
    "risk_class_reasons",
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


async def test_tasks_archived_column_present():
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        assert "archived" in cols
        assert (
            cols["archived"]["dflt_value"] is not None
            or cols["archived"]["notnull"] == 1
        )
    finally:
        await conn.close()


async def test_claim_columns_present():
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        for name in ("claimed_by", "claim_session_id", "claimed_at"):
            assert name in cols, f"missing column {name}"
    finally:
        await conn.close()


async def test_task_idempotency_keys_table_present():
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "task_idempotency_keys")
        assert "client_request_id" in cols
        assert "task_id" in cols
        assert "request_hash" in cols
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
        ("review_checklist", "'[]'"),
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
        "prepared_at",
    ],
)
async def test_optional_columns_are_nullable(column: str):
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        assert cols[column]["notnull"] == 0
    finally:
        await conn.close()


async def test_prepared_by_column_present_with_default():
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        assert "prepared_by" in cols
        assert cols["prepared_by"]["dflt_value"] == "''"
        assert cols["prepared_by"]["notnull"] == 1
    finally:
        await conn.close()


@pytest.mark.parametrize(
    "column",
    ["human_owner", "human_reviewer"],
)
async def test_human_owner_reviewer_columns_present_with_defaults(column: str):
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        assert column in cols
        assert cols[column]["dflt_value"] == "''"
        assert cols[column]["notnull"] == 1
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


async def test_review_generation_columns_present():
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        gen = cols.get("submission_generation")
        assert gen is not None
        assert gen["notnull"] == 1
        assert gen["dflt_value"] == "0"
        verdict = cols.get("review_verdict")
        assert verdict is not None
        assert verdict["notnull"] == 0
        verdict_gen = cols.get("review_verdict_generation")
        assert verdict_gen is not None
        assert verdict_gen["notnull"] == 0
    finally:
        await conn.close()


async def test_review_generation_defaults_for_existing_rows():
    conn = await _make_db()
    try:
        await conn.execute(
            "INSERT INTO tasks (title, description, status) VALUES ('t', '', 'open')"
        )
        row = (
            await conn.execute_fetchall(
                "SELECT submission_generation, review_verdict, "
                "review_verdict_generation FROM tasks"
            )
        )[0]
        assert row["submission_generation"] == 0
        assert row["review_verdict"] is None
        assert row["review_verdict_generation"] is None
    finally:
        await conn.close()


async def test_implementer_principal_id_column_present():
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        col = cols.get("implementer_principal_id")
        assert col is not None
        assert col["notnull"] == 0  # nullable: legacy tasks use name fallback
    finally:
        await conn.close()


async def test_projects_table_and_project_id_column():
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "projects")
        for col in (
            "slug",
            "name",
            "repo",
            "workspace_path",
            "default_branch",
            "default_branch_policy",
            "archived",
        ):
            assert col in cols, col
        task_cols = await _table_columns(conn, "tasks")
        assert "project_id" in task_cols
        assert task_cols["project_id"]["notnull"] == 0
    finally:
        await conn.close()


async def test_seed_default_project_idempotent_and_backfills_epics():
    from hub.db import seed_default_project

    conn = await _make_db()
    try:
        await conn.execute(
            "INSERT INTO tasks (title, description, status, task_type) "
            "VALUES ('E', '', 'open', 'epic')"
        )
        await conn.commit()
        await seed_default_project(conn)
        await seed_default_project(conn)  # idempotent

        projects = await conn.execute_fetchall(
            "SELECT id, slug FROM projects WHERE slug='default'"
        )
        assert len(projects) == 1
        epics = await conn.execute_fetchall(
            "SELECT project_id FROM tasks WHERE task_type='epic'"
        )
        assert all(e["project_id"] == projects[0]["id"] for e in epics)
    finally:
        await conn.close()


# ---- Durable poller state (#416) ----


async def test_durable_poller_columns_present_with_defaults():
    # AC-1: a fresh database exposes the three durable columns with valid
    # defaults — a NOT NULL zero retry budget and nullable clocks.
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        for name in (
            "status_entered_at",
            "ci_check_started_at",
            "ci_no_pr_attempts",
        ):
            assert name in cols, f"missing column {name}"
        assert cols["ci_no_pr_attempts"]["notnull"] == 1
        assert cols["ci_no_pr_attempts"]["dflt_value"] == "0"
        assert cols["status_entered_at"]["notnull"] == 0
        assert cols["ci_check_started_at"]["notnull"] == 0
    finally:
        await conn.close()


async def test_status_entered_at_backfilled_for_existing_rows(monkeypatch):
    # AC-2: an existing database with active rows that predate the new columns
    # gets safe defaults on migration — status is preserved, the retry budget
    # starts at 0, and status_entered_at is backfilled to migration time so the
    # row receives a full grace window, never an instant escalation.
    from hub import db as db_module

    original = list(db_module._MIGRATIONS)
    new_names = {
        "add_status_entered_at_column",
        "add_ci_check_started_at_column",
        "add_ci_no_pr_attempts_column",
        "backfill_status_entered_at",
    }
    legacy = [m for m in original if m[0] not in new_names]

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_SCHEMA)
        monkeypatch.setattr(db_module, "_MIGRATIONS", legacy)
        await _migrate(conn)
        cols = await _table_columns(conn, "tasks")
        assert "status_entered_at" not in cols

        await conn.execute(
            "INSERT INTO tasks (title, description, status) "
            "VALUES ('legacy', '', 'ci_check')"
        )
        await conn.commit()

        monkeypatch.setattr(db_module, "_MIGRATIONS", original)
        await _migrate(conn)

        row = (
            await conn.execute_fetchall(
                "SELECT status, status_entered_at, ci_no_pr_attempts FROM tasks"
            )
        )[0]
        assert row["status"] == "ci_check"
        assert row["status_entered_at"] is not None
        assert row["ci_no_pr_attempts"] == 0
    finally:
        await conn.close()


async def test_machine_review_incompleteness_columns_present():
    # #549: incomplete is NULLABLE on purpose. A NOT NULL DEFAULT 0 would make
    # every report written before this column assert a completeness it never
    # claimed — the same substitution the column exists to prevent.
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "machine_reviews")
        incomplete = cols.get("incomplete")
        assert incomplete is not None
        assert incomplete["notnull"] == 0, "must stay nullable: NULL means 'not stated'"
        assert incomplete["dflt_value"] is None
        for name in ("unresolved", "lost_dimensions"):
            col = cols.get(name)
            assert col is not None
            assert col["notnull"] == 1
            assert col["dflt_value"] == "'[]'"
    finally:
        await conn.close()


async def test_machine_review_incompleteness_migration_preserves_rows():
    # AC-4: reports written before the migration keep their findings and read
    # back as "completeness not stated", not as complete.
    conn = await _make_db()
    try:
        await conn.execute(
            "INSERT INTO machine_reviews (task_id, submission_generation, "
            "raw_count, findings_confirmed, findings_rejected, submitted_by) "
            "VALUES (1, 1, 5, '[{\"title\": \"old finding\"}]', '[]', 'pda_claude')"
        )
        row = (
            await conn.execute_fetchall(
                "SELECT raw_count, findings_confirmed, incomplete, unresolved, "
                "lost_dimensions FROM machine_reviews"
            )
        )[0]
        assert row["raw_count"] == 5
        assert "old finding" in row["findings_confirmed"]
        assert row["incomplete"] is None, "pre-migration rows claim nothing"
        assert row["unresolved"] == "[]"
        assert row["lost_dimensions"] == "[]"
    finally:
        await conn.close()


async def test_task_update_principal_migration_preserves_rows():
    """#559 AC-2. Rows written before the field keep their content and are
    distinguishable from "written by nobody we can name".

    The DB default is 'legacy' precisely so history says what it is. A single
    NULL would have meant three things at once — predates the field, written
    by the hub, written without authentication — and that collapse is what
    this task removes.
    """
    conn = await _make_db()
    try:
        await conn.execute(
            "INSERT INTO tasks (id, title, description) VALUES (1, 't', '')"
        )
        # A row as it would have been inserted before the migration.
        await conn.execute(
            "INSERT INTO task_updates (task_id, agent, kind, content) "
            "VALUES (1, 'pda_claude', 'status', 'written before the field')"
        )
        row = (
            await conn.execute_fetchall(
                "SELECT agent, content, principal_id, author_kind FROM task_updates"
            )
        )[0]
        assert row["content"] == "written before the field", "no data lost"
        assert row["agent"] == "pda_claude"
        assert row["principal_id"] is None
        assert row["author_kind"] == "legacy", (
            "history must say it predates the field, not impersonate the hub"
        )
    finally:
        await conn.close()


async def test_hub_written_update_is_not_confused_with_history():
    """The other half of AC-2: absence of a principal has distinct reasons."""
    from hub import repository as repo

    conn = await _make_db()
    try:
        await conn.execute(
            "INSERT INTO tasks (id, title, description) VALUES (1, 't', '')"
        )
        await repo.add_task_update(conn, 1, "hub", "alert", "conveyor said so")
        row = (
            await conn.execute_fetchall(
                "SELECT principal_id, author_kind FROM task_updates"
            )
        )[0]
        assert row["principal_id"] is None
        assert row["author_kind"] == "hub", (
            "the hub has no principal by nature; that is not the same as legacy"
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Risk class column (#581)
# ---------------------------------------------------------------------------


async def test_risk_class_column_nullable_without_default():
    # NULL is the only honest "not computed": NOT NULL or a default would
    # collapse absence into a value the moment anything trusts the class.
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        assert "risk_class" in cols
        assert cols["risk_class"]["notnull"] == 0
        assert cols["risk_class"]["dflt_value"] is None
    finally:
        await conn.close()


async def test_risk_class_migration_preserves_rows(monkeypatch):
    # AC-3 (#581): rows written before the migration survive it unchanged
    # and read back as "not computed" (NULL) — never as R0.
    import hub.db as db_module

    trimmed = [m for m in _MIGRATIONS if m[0] != "add_risk_class_column"]
    assert len(trimmed) == len(_MIGRATIONS) - 1, (
        "the migration under test must exist in _MIGRATIONS"
    )

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_SCHEMA)
        monkeypatch.setattr(db_module, "_MIGRATIONS", trimmed)
        await _migrate(conn)
        await conn.execute(
            "INSERT INTO tasks (title, description) VALUES ('legacy row', 'kept')"
        )
        await conn.commit()

        monkeypatch.setattr(db_module, "_MIGRATIONS", _MIGRATIONS)
        await _migrate(conn)

        rows = await conn.execute_fetchall(
            "SELECT title, description, risk_class FROM tasks"
        )
        assert len(rows) == 1
        assert rows[0]["title"] == "legacy row"
        assert rows[0]["description"] == "kept"
        assert rows[0]["risk_class"] is None
    finally:
        await conn.close()


async def test_gate_policy_migration_preserves_projects(monkeypatch):
    # AC-5 (#743): project rows written before the migration survive it and
    # read back with an empty policy — every gate human by default.
    import hub.db as db_module

    trimmed = [m for m in _MIGRATIONS if m[0] != "add_projects_gate_policy"]
    assert len(trimmed) == len(_MIGRATIONS) - 1

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_SCHEMA)
        monkeypatch.setattr(db_module, "_MIGRATIONS", trimmed)
        await _migrate(conn)
        await conn.execute(
            "INSERT INTO projects (slug, name, repo, workspace_path, "
            "default_branch, default_branch_policy, status) "
            "VALUES ('legacy', 'Legacy', '', '', 'develop', '{}', 'active')"
        )
        await conn.commit()

        monkeypatch.setattr(db_module, "_MIGRATIONS", _MIGRATIONS)
        await _migrate(conn)

        rows = await conn.execute_fetchall(
            "SELECT slug, name, gate_policy FROM projects WHERE slug='legacy'"
        )
        assert len(rows) == 1
        assert rows[0]["name"] == "Legacy"
        assert rows[0]["gate_policy"] == "{}"
    finally:
        await conn.close()
