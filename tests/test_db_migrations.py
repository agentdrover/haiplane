from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

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


async def test_tasks_git_mode_column_present():
    """#975: pair-start persists hub|remote so later host git can be skipped."""
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        assert "git_mode" in cols
        assert cols["git_mode"]["notnull"] == 1
        default = cols["git_mode"]["dflt_value"]
        assert default is not None
        assert "hub" in str(default)
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


# ---------------------------------------------------------------------------
# MCP usage telemetry (#780)
# ---------------------------------------------------------------------------

MCP_CALL_EVENT_COLUMNS = {
    "id",
    "tool",
    "profile",
    "principal_id",
    "principal_role",
    "status",
    "error_reason",
    "latency_ms",
    "response_chars",
    "task_id",
    "unknown_arg_count",
    "created_at",
}


async def test_mcp_call_events_table_has_no_payload_column():
    """The privacy property of #780 is the schema, not a rule to remember.

    An argument value can only be stored where a column accepts one, so the
    column list is pinned here: adding somewhere to put a request body would
    have to fail this test first.
    """
    conn = await _make_db()
    try:
        columns = await _table_columns(conn, "mcp_call_events")
        assert set(columns) == MCP_CALL_EVENT_COLUMNS
        assert columns["task_id"]["type"] == "INTEGER"
        assert columns["latency_ms"]["notnull"] == 1
        assert columns["response_chars"]["notnull"] == 1
        # #549: pre-column rows must read as unknown, not as "discarded none".
        assert columns["unknown_arg_count"]["type"] == "INTEGER"
        assert columns["unknown_arg_count"]["notnull"] == 0
    finally:
        await conn.close()


async def test_mcp_call_events_upgrade_path_matches_a_fresh_database():
    """An existing hub upgrades into the same schema a new one is born with."""
    fresh = await _make_db()
    upgraded = await _make_db()
    try:
        # An older database: no telemetry table and no record of the migration.
        await upgraded.execute("DROP TABLE mcp_call_events")
        await upgraded.execute(
            "DELETE FROM _migrations WHERE name IN "
            "('create_mcp_call_events', 'idx_mcp_call_events_window', "
            "'add_mcp_call_events_unknown_arg_count')"
        )
        await upgraded.commit()

        await _migrate(upgraded)
        await _migrate(upgraded)  # second run must change nothing

        assert await _table_columns(upgraded, "mcp_call_events") == (
            await _table_columns(fresh, "mcp_call_events")
        )
        indexes = await upgraded.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            ("mcp_call_events",),
        )
        assert "idx_mcp_call_events_window" in {row["name"] for row in indexes}
    finally:
        await fresh.close()
        await upgraded.close()


async def test_unknown_arg_count_omitted_insert_reads_as_unmeasured():
    """#549: a telemetry row that never recorded discards is NULL, not zero."""
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA)
        await _migrate(db)
        await db.execute("INSERT INTO mcp_call_events (tool) VALUES ('hub_whoami')")
        await db.commit()
        rows = list(
            await db.execute_fetchall(
                "SELECT unknown_arg_count FROM mcp_call_events WHERE tool = 'hub_whoami'"
            )
        )
        assert rows and rows[0]["unknown_arg_count"] is None


async def test_provider_tokens_migration_is_additive():
    # AC-5 (#828): reports written before the column exists keep reading, and
    # they read as "never billed" rather than as "cost nothing" (#549).
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA)
        await _migrate(db)
        await db.execute(
            "INSERT INTO machine_reviews (task_id, submission_generation, "
            "harness_skill, raw_count) VALUES (1, 1, 'legacy', 0)"
        )
        await db.commit()

        rows = list(
            await db.execute_fetchall(
                "SELECT provider_tokens FROM machine_reviews WHERE task_id = 1"
            )
        )
        assert rows and rows[0]["provider_tokens"] is None


async def test_review_dispatch_provider_tokens_migration_is_additive():
    # #1026: a dispatch closed before the column existed reads as unknown,
    # never as a free run (#549).
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA)
        await _migrate(db)
        await db.execute(
            "INSERT INTO review_dispatches (task_id, submission_generation, "
            "agent_id, run_id, model, status) "
            "VALUES (1, 1, 'bc-legacy', 'run-legacy', 'grok-4.6', 'failed')"
        )
        await db.commit()

        rows = list(
            await db.execute_fetchall(
                "SELECT provider_tokens FROM review_dispatches WHERE agent_id = "
                "'bc-legacy'"
            )
        )
        assert rows and rows[0]["provider_tokens"] is None


# --- First-class task dependencies (#482, epic #478) -------------------------
#
# The order of work has lived outside the hub since the beginning: in a chat,
# in an agent's memory, in a sentence inside somebody's constraints. This is
# the table that lets it live in the system.


@asynccontextmanager
async def _deps_db():
    """A migrated in-memory database with foreign keys ON.

    The runtime enables them (hub/db.py); a cascade test with them off would
    prove the cascade works where nothing enforces it.
    """
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(_SCHEMA)
        await _migrate(db)
        yield db


async def _task_row(db: aiosqlite.Connection, title: str) -> int:
    cur = await db.execute(
        "INSERT INTO tasks (title, description, status) VALUES (?, '', 'open')",
        (title,),
    )
    return cur.lastrowid


async def test_task_dependencies_table_present():
    # AC-1 (#482): the table exists with an index on BOTH ends of the edge —
    # "who blocks me" is walked at start, "whom do I unblock" at finish.
    async with _deps_db() as db:
        names = {
            r["name"]
            for r in await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        assert "task_dependencies" in names
        assert "idx_task_dependencies_task" in names
        assert "idx_task_dependencies_depends_on" in names


async def test_duplicate_dependency_edge_is_refused():
    # AC-2 (#482): an edge is a fact, not a tally. Two identical rows would
    # make "how many things block me" depend on how often somebody clicked.
    async with _deps_db() as db:
        a = await _task_row(db, "blocked")
        b = await _task_row(db, "blocker")
        await db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
            (a, b),
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id) "
                "VALUES (?, ?)",
                (a, b),
            )


async def test_self_dependency_is_refused_by_schema():
    # AC-3 (#482): a task blocking itself is a cycle of length one. Cycle
    # detection proper is #483, but this case needs no graph walk, so it is
    # closed in the schema — unwritable even from a SQL prompt.
    async with _deps_db() as db:
        a = await _task_row(db, "lonely")
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id) "
                "VALUES (?, ?)",
                (a, a),
            )


async def test_dependency_edges_die_with_their_task():
    # AC-4 (#482): an edge pointing at a task that no longer exists would
    # read as a blocker nobody can ever satisfy.
    async with _deps_db() as db:
        a = await _task_row(db, "blocked")
        b = await _task_row(db, "blocker")
        c = await _task_row(db, "dependent")
        await db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
            (a, b),
        )
        await db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
            (c, a),
        )
        await db.commit()

        await db.execute("DELETE FROM tasks WHERE id = ?", (a,))
        await db.commit()

        left = list(
            await db.execute_fetchall(
                "SELECT task_id, depends_on_task_id FROM task_dependencies"
            )
        )
        assert left == [], "edges on both sides of the deleted task must be gone"


async def test_outcome_answer_snapshot_migration_preserves_legacy_rows():
    """#576 AC-5. Answers written before the snapshot column stay readable
    and do not become 'revised' just because the new column is empty."""
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "outcome_answers")
        assert "hypothesis_snapshot" in cols
        assert cols["hypothesis_snapshot"]["notnull"] == 0

        await conn.execute(
            "INSERT INTO tasks (id, title, description) VALUES (1, 't', '')"
        )
        await conn.execute(
            "INSERT INTO outcome_answers (task_id, verdict, measured_value, "
            "note, answered_by) VALUES (1, 'moved', '0 → 5', '', 'owner')"
        )
        row = (
            await conn.execute_fetchall(
                "SELECT verdict, measured_value, hypothesis_snapshot "
                "FROM outcome_answers"
            )
        )[0]
        assert row["verdict"] == "moved"
        assert row["measured_value"] == "0 → 5"
        assert row["hypothesis_snapshot"] is None
    finally:
        await conn.close()


async def test_disposition_migration_backfills_nothing():
    """#876 AC-3. A report written before dispositions existed keeps no
    judgement at all — silence there must not become a default verdict.

    The pairing that matters: the table appears, and the reports already in the
    database gain zero rows in it. A back-filled 'fixed' (or anything else)
    would let every legacy report read as fully judged, which is exactly the
    substitution #549 removed elsewhere.
    """
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "finding_dispositions")
        assert cols, "the table must exist after migration"
        assert cols["disposition"]["notnull"] == 1, (
            "a row without a verdict is not a row"
        )

        await conn.execute(
            "INSERT INTO tasks (id, title, description) VALUES (1, 't', '')"
        )
        await conn.execute(
            "INSERT INTO machine_reviews (task_id, submission_generation, "
            "raw_count, findings_confirmed) VALUES (1, 1, 2, ?)",
            ('[{"title": "a"}, {"title": "b"}]',),
        )
        await conn.commit()
        # Re-running migrations must not invent history for it either.
        await _migrate(conn)
        rows = await conn.execute_fetchall("SELECT * FROM finding_dispositions")
        assert rows == [], "legacy reports stay unjudged, not defaulted"

        # And the uniqueness that makes a correction a correction.
        await conn.execute(
            "INSERT INTO finding_dispositions (review_id, task_id, "
            "submission_generation, finding_index, disposition) "
            "VALUES (1, 1, 1, 0, 'fixed')"
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO finding_dispositions (review_id, task_id, "
                "submission_generation, finding_index, disposition) "
                "VALUES (1, 1, 1, 0, 'false_positive')"
            )
    finally:
        await conn.close()


async def test_category_check_needs_a_named_check():
    """#878. The table exists and refuses a second answer to "is this covered?".

    ``check_ref`` is NOT NULL by schema and non-blank by the service: a
    category closed by a tick is a category nobody covered, and the debt list
    would shrink while the token bill stayed exactly where it was.
    """
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "category_checks")
        assert cols, "the table must exist after migration"
        assert cols["check_ref"]["notnull"] == 1

        await conn.execute(
            "INSERT INTO category_checks (category, check_ref) "
            "VALUES ('timeouts', 'tests/test_x.py::test_y')"
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO category_checks (category, check_ref) "
                "VALUES ('timeouts', 'another/check')"
            )
    finally:
        await conn.close()


# --- Chat-pair: код в чате → короткая сессия (#961) --------------------------


async def test_chat_pair_tables_exist():
    """AC-19 (#961): свежая БД и БД до миграции приходят к одной схеме.

    Секрет здесь — свойство схемы, а не правило, которое надо помнить: колонок
    под plaintext кода или токена нет, есть только ``code_hash`` / ``token_hash``,
    и уникальность hash не даёт двум строкам жить под одним секретом.
    """
    fresh = await _make_db()
    upgraded = await _make_db()
    try:
        for table in ("chat_pair_codes", "chat_pair_sessions"):
            cols = await _table_columns(fresh, table)
            assert cols, f"после миграции нет таблицы {table}"
            assert not [c for c in cols if c in ("code", "token", "secret")]

        codes = await _table_columns(fresh, "chat_pair_codes")
        assert codes["code_hash"]["notnull"] == 1
        assert codes["expires_at"]["notnull"] == 1
        assert "redeemed_at" in codes
        sessions = await _table_columns(fresh, "chat_pair_sessions")
        assert sessions["token_hash"]["notnull"] == 1
        assert sessions["expires_at"]["notnull"] == 1
        assert "revoked_at" in sessions

        # Хаб, поднятый до этой миграции: таблиц нет и записи о миграции нет.
        await upgraded.execute("DROP TABLE chat_pair_codes")
        await upgraded.execute("DROP TABLE chat_pair_sessions")
        await upgraded.execute(
            "DELETE FROM _migrations WHERE name LIKE 'create_chat_pair%' "
            "OR name LIKE 'idx_chat_pair%' "
            "OR name LIKE 'add_chat_pair%'"
        )
        await upgraded.commit()

        await _migrate(upgraded)
        await _migrate(upgraded)  # второй прогон ничего не меняет

        for table in ("chat_pair_codes", "chat_pair_sessions"):
            assert await _table_columns(upgraded, table) == (
                await _table_columns(fresh, table)
            )
    finally:
        await fresh.close()
        await upgraded.close()


async def test_chat_pair_hash_is_unique():
    """Один и тот же hash кода или токена не может жить в двух строках."""
    conn = await _make_db()
    try:
        await conn.execute(
            "INSERT INTO principals (id, kind, username) VALUES (1, 'human', 'alice')"
        )
        await conn.commit()
        await conn.execute(
            "INSERT INTO chat_pair_codes (principal_id, code_hash, expires_at) "
            "VALUES (1, 'h', datetime('now', '+5 minutes'))"
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO chat_pair_codes (principal_id, code_hash, expires_at) "
                "VALUES (1, 'h', datetime('now', '+5 minutes'))"
            )
        await conn.rollback()
        await conn.execute(
            "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at) "
            "VALUES (1, 't', datetime('now', '+2 hours'))"
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at) "
                "VALUES (1, 't', datetime('now', '+2 hours'))"
            )
    finally:
        await conn.close()


async def test_steward_judgements_table_and_unique_triple():
    """#1022: judgements persist, and (task, generation, kind) is at-most-once."""
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "steward_judgements")
        assert "verdict" in cols
        assert "submitted_verdict" in cols
        await conn.execute(
            "INSERT INTO tasks (id, title, description) VALUES (1, 't', '')"
        )
        await conn.execute(
            "INSERT INTO steward_judgements (task_id, generation, kind, "
            "submitted_verdict, verdict) VALUES (1, 1, 'verdict', 'approve', "
            "'approve')"
        )
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO steward_judgements (task_id, generation, kind, "
                "submitted_verdict, verdict) VALUES (1, 1, 'verdict', "
                "'escalate', 'escalate')"
            )
    finally:
        await conn.close()


# --- #1343: pipeline_merges rekeyed from the PR number to the merge ----------

_REKEY_PREFIX = "rekey_pipeline_merges_"


def _before_rekey() -> list[tuple[str, str]]:
    trimmed = [m for m in _MIGRATIONS if not m[0].startswith(_REKEY_PREFIX)]
    assert len(trimmed) < len(_MIGRATIONS), (
        "the rekey migrations under test must exist in _MIGRATIONS"
    )
    return trimmed


_OLD_MERGE_ROWS = [
    # id, project_id, pr_number, task_id, merged_at, merge_sha,
    # released_pr, released_sha
    (5, 1, 406, 816, "2026-08-21 10:00:00", "0e785d04c0", 83, "rel0083"),
    (88, 1, 438, None, "2026-08-21 11:00:00", "backmerge1", None, None),
    (89, None, 12, 900, "2026-08-22 09:00:00", None, None, ""),
    (90, 1, 471, 950, "2026-08-22 10:00:00", "", 85, "rel0085"),
]


async def _pipeline_merges_rows(conn: aiosqlite.Connection) -> list[tuple]:
    rows = await conn.execute_fetchall(
        "SELECT id, project_id, pr_number, task_id, merged_at, merge_sha, "
        "released_pr, released_sha FROM pipeline_merges ORDER BY id"
    )
    return [tuple(r) for r in rows]


async def _pipeline_merges_schema(conn: aiosqlite.Connection) -> list[tuple]:
    rows = await conn.execute_fetchall(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE tbl_name = 'pipeline_merges' ORDER BY type, name"
    )
    return [tuple(r) for r in rows]


async def test_pipeline_merges_rekey_preserves_rows(monkeypatch):
    """#1343 AC-3. The table is rebuilt to drop UNIQUE (project_id,
    pr_number); every row keeps its id and every column, released_* included,
    and a second run changes nothing."""
    import hub.db as db_module

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_SCHEMA)
        monkeypatch.setattr(db_module, "_MIGRATIONS", _before_rekey())
        await _migrate(conn)
        await conn.executemany(
            "INSERT INTO pipeline_merges (id, project_id, pr_number, task_id, "
            "merged_at, merge_sha, released_pr, released_sha) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            _OLD_MERGE_ROWS,
        )
        # A deleted row: its id must not be handed out again.
        await conn.execute(
            "INSERT INTO pipeline_merges (id, project_id, pr_number) VALUES (95, 1, 1)"
        )
        await conn.execute("DELETE FROM pipeline_merges WHERE id = 95")
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO pipeline_merges (project_id, pr_number, merge_sha) "
                "VALUES (1, 438, 'probe')"
            )
        await conn.rollback()

        monkeypatch.setattr(db_module, "_MIGRATIONS", _MIGRATIONS)
        await _migrate(conn)

        assert await _pipeline_merges_rows(conn) == _OLD_MERGE_ROWS
        tables = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE name LIKE 'pipeline_merges%' "
            "AND type = 'table'"
        )
        assert [r[0] for r in tables] == ["pipeline_merges"], "no leftover copy"

        # The (project_id, pr_number) constraint is gone...
        cur = await conn.execute(
            "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
            "VALUES (1, 438, 1281, 'new0438')"
        )
        assert cur.lastrowid == 96, "ids continue past the old sequence"
        # ...and the merge itself is the key now.
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO pipeline_merges (project_id, pr_number, merge_sha) "
                "VALUES (1, 999, 'new0438')"
            )
        await conn.rollback()

        schema = await _pipeline_merges_schema(conn)
        await _migrate(conn)
        await _migrate(conn)
        assert await _pipeline_merges_rows(conn) == _OLD_MERGE_ROWS
        assert await _pipeline_merges_schema(conn) == schema
    finally:
        await conn.close()


async def test_pipeline_merges_rekey_is_all_or_nothing(monkeypatch):
    """#1343 risk: a rebuild that fails half way must leave the old table and
    its rows where they were, not a dropped table and an orphaned copy."""
    import hub.db as db_module

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_SCHEMA)
        monkeypatch.setattr(db_module, "_MIGRATIONS", _before_rekey())
        await _migrate(conn)
        await conn.executemany(
            "INSERT INTO pipeline_merges (id, project_id, pr_number, task_id, "
            "merged_at, merge_sha, released_pr, released_sha) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            _OLD_MERGE_ROWS,
        )
        await conn.commit()
        old_schema = await _pipeline_merges_schema(conn)

        rekey = [m for m in _MIGRATIONS if m[0].startswith(_REKEY_PREFIX)]
        drop_at = next(
            i for i, m in enumerate(rekey) if "DROP TABLE pipeline_merges" in m[1]
        )
        broken = rekey[: drop_at + 1] + [
            ("test_broken_after_drop", "SELECT nope FROM nowhere")
        ]
        monkeypatch.setattr(db_module, "_MIGRATIONS", _before_rekey() + broken)
        with pytest.raises(Exception):
            await _migrate(conn)

        assert await _pipeline_merges_rows(conn) == _OLD_MERGE_ROWS
        assert await _pipeline_merges_schema(conn) == old_schema
        applied = await conn.execute_fetchall(
            "SELECT name FROM _migrations WHERE name LIKE ?", (_REKEY_PREFIX + "%",)
        )
        assert applied == [], "a failed rebuild is not recorded as applied"

        # The next start completes it.
        monkeypatch.setattr(db_module, "_MIGRATIONS", _MIGRATIONS)
        await _migrate(conn)
        assert await _pipeline_merges_rows(conn) == _OLD_MERGE_ROWS
    finally:
        await conn.close()


async def test_scheduled_policy_changes_table_fresh_and_migrated():
    """#1593: таблица расписания есть на свежей схеме и доезжает миграцией."""
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "scheduled_policy_changes")
        assert {
            "id",
            "project_id",
            "at",
            "patch",
            "note",
            "created_by",
            "created_at",
            "state",
            "executed_at",
            "result",
        } <= set(cols)
        await conn.execute("INSERT INTO projects (slug, name) VALUES ('p', 'P')")
        await conn.execute(
            "INSERT INTO scheduled_policy_changes (project_id, at, patch) "
            "VALUES (1, '2026-10-13T00:00:00Z', '{}')"
        )
        row = (
            await conn.execute_fetchall("SELECT state FROM scheduled_policy_changes")
        )[0]
        assert row["state"] == "pending"
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute("UPDATE scheduled_policy_changes SET state='bogus'")
        # Путь миграции: база без записи о миграции получает таблицу при _migrate.
        await conn.execute("DROP TABLE scheduled_policy_changes")
        await conn.execute(
            "DELETE FROM _migrations WHERE name IN "
            "('create_scheduled_policy_changes', 'idx_scheduled_policy_changes_due')"
        )
        await conn.commit()
        await _migrate(conn)
        assert "state" in await _table_columns(conn, "scheduled_policy_changes")
    finally:
        await conn.close()


async def test_freeze_rationale_column_present_and_empty_for_old_rows():
    """#1594: обоснование допуска при заморозке; у старых строк оно пустое."""
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "tasks")
        assert "freeze_rationale" in cols
        assert cols["freeze_rationale"]["notnull"] == 1
        await conn.execute(
            "INSERT INTO tasks (title, description, runtime) VALUES ('t', '', 'auto')"
        )
        row = await conn.execute_fetchall("SELECT freeze_rationale FROM tasks")
        assert row[0]["freeze_rationale"] == ""
        await _migrate(conn)  # повторный проход миграций ничего не ломает
    finally:
        await conn.close()


async def test_agent_sessions_projects_column_is_idempotent(monkeypatch):
    # AC-6 (#1674): БД без колонки и свежая БД — колонка есть после миграций,
    # повторный прогон ничего не ломает, legacy '' читается как [].
    import hub.db as db_module
    from hub.services.sessions import session_view

    name = "add_agent_sessions_projects"
    trimmed = [m for m in _MIGRATIONS if m[0] != name]
    assert len(trimmed) == len(_MIGRATIONS) - 1, "миграция должна быть в списке"

    fresh = await _make_db()
    try:
        cols = await _table_columns(fresh, "agent_sessions")
        assert "projects" in cols
        assert cols["projects"]["notnull"] == 1
        await _migrate(fresh)
        await _migrate(fresh)
    finally:
        await fresh.close()

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(_SCHEMA)
        monkeypatch.setattr(db_module, "_MIGRATIONS", trimmed)
        await _migrate(conn)
        assert "projects" not in await _table_columns(conn, "agent_sessions")
        await conn.execute(
            "INSERT INTO agent_sessions (session_id, agent) VALUES ('legacy', 'bot')"
        )
        await conn.commit()

        monkeypatch.setattr(db_module, "_MIGRATIONS", _MIGRATIONS)
        await _migrate(conn)
        await _migrate(conn)
        assert "projects" in await _table_columns(conn, "agent_sessions")
        rows = await conn.execute_fetchall(
            "SELECT * FROM agent_sessions WHERE session_id = 'legacy'"
        )
        assert rows[0]["projects"] == ""
        assert session_view(rows[0])["projects"] == []
    finally:
        await conn.close()


async def test_watch_notices_table_is_idempotent_and_keys_are_unique():
    """#1679: ключи уведомлений стюарду — пожизненный дедуп, повтор ключа игнорируется."""
    conn = await _make_db()
    try:
        cols = await _table_columns(conn, "watch_notices")
        assert set(cols) == {"key", "created_at"}
        assert cols["created_at"]["notnull"] == 1
        await _migrate(conn)  # повторный проход ничего не ломает
        await _migrate(conn)
        first = await conn.execute(
            "INSERT OR IGNORE INTO watch_notices(key) VALUES ('k:1')"
        )
        again = await conn.execute(
            "INSERT OR IGNORE INTO watch_notices(key) VALUES ('k:1')"
        )
        assert (first.rowcount, again.rowcount) == (1, 0)
        rows = await conn.execute_fetchall("SELECT key, created_at FROM watch_notices")
        assert [r["key"] for r in rows] == ["k:1"] and rows[0]["created_at"]
    finally:
        await conn.close()


# --- #1809: перенос списка миграций из hub/db.py -----------------------------
# Эталоны сняты с кода ДО переноса (первый коммит задачи) и лежат в
# tests/fixtures. Перенос обязан не изменить ни имя, ни порядок, ни байт SQL.

_FIXTURES = Path(__file__).parent / "fixtures"


def _spaced(digest: str) -> str:
    # The fixture stores sha256 in groups of 8 so that detect-secrets does not
    # take 323 digests for hex secrets.
    return " ".join(digest[i : i + 8] for i in range(0, len(digest), 8))


def test_migration_list_unchanged_after_move():
    import hashlib

    import hub.db as db_module

    expected = json.loads((_FIXTURES / "migrations_baseline.json").read_text())
    actual = [
        [name, _spaced(hashlib.sha256(sql.encode()).hexdigest())]
        for name, sql in db_module._MIGRATIONS
    ]
    assert len(actual) == len(expected)
    assert [n for n, _ in actual] == [n for n, _ in expected]
    assert actual == expected


async def _schema_dump(conn: aiosqlite.Connection) -> list[list]:
    rows = await conn.execute_fetchall(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    )
    return [list(r) for r in rows]


async def test_moved_migrations_are_noop_on_migrated_db():
    conn = await _make_db()
    try:
        # Свежая база новым кодом даёт ту же схему, что дал старый код.
        expected = json.loads((_FIXTURES / "migrated_schema_baseline.json").read_text())
        assert await _schema_dump(conn) == expected

        await conn.execute(
            "INSERT INTO tasks (title, description) VALUES ('control', 'row')"
        )
        await conn.commit()
        applied_before = [
            list(r)
            for r in await conn.execute_fetchall(
                "SELECT name, applied_at FROM _migrations ORDER BY name"
            )
        ]
        data_before = [
            list(r)
            for r in await conn.execute_fetchall(
                "SELECT id, title, description FROM tasks"
            )
        ]
        baseline = json.loads((_FIXTURES / "migrations_baseline.json").read_text())
        assert {r[0] for r in applied_before} == {n for n, _ in baseline}

        # Повторная инициализация: ни одна миграция не исполняется заново.
        executed: list[str] = []
        real_execute = conn.execute

        async def spy(sql, *args, **kwargs):
            executed.append(sql)
            return await real_execute(sql, *args, **kwargs)

        conn.execute = spy  # type: ignore[method-assign]
        try:
            await _migrate(conn)
        finally:
            conn.execute = real_execute  # type: ignore[method-assign]
        migration_sql = {sql for _, sql in _MIGRATIONS}
        assert not [s for s in executed if s in migration_sql]

        assert [
            list(r)
            for r in await conn.execute_fetchall(
                "SELECT name, applied_at FROM _migrations ORDER BY name"
            )
        ] == applied_before
        assert await _schema_dump(conn) == expected
        assert [
            list(r)
            for r in await conn.execute_fetchall(
                "SELECT id, title, description FROM tasks"
            )
        ] == data_before
    finally:
        await conn.close()


async def test_patched_migration_list_is_applied(monkeypatch):
    import hub.db as db_module

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(_SCHEMA)
        marker = (
            "test_patched_list_marker",
            "ALTER TABLE tasks ADD COLUMN zz_patched TEXT",
        )
        monkeypatch.setattr(db_module, "_MIGRATIONS", [marker])
        await _migrate(conn)
        assert "zz_patched" in await _table_columns(conn, "tasks")
        names = [
            r[0] for r in await conn.execute_fetchall("SELECT name FROM _migrations")
        ]
        assert names == ["test_patched_list_marker"]
    finally:
        await conn.close()


# ---- #1848: the CI contract fingerprint migration ----------------------------


async def _state_before_1848(tmp_path: Path) -> Path:
    """A database as it stood before #1848: attempts and projections without the column."""
    import sqlite3

    from hub import db as db_module

    path = tmp_path / "before-1848.db"
    fresh = await db_module.connect(str(path))
    await db_module.bootstrap(fresh)
    await fresh.execute(
        "INSERT INTO tasks (title, status, submission_generation, submission_sha, "
        "validation_status, validation_generation, validation_source, "
        "validation_commands) VALUES "
        "('verified', 'review', 1, 'sha-v', 'pass', 1, 'ci:11:1', '[\"x\"]'), "
        "('unverified', 'review', 1, 'sha-u', '', 0, 'manual', '[]')"
    )
    from hub import repository as repo
    from hub.models import AcceptanceCriterion

    await repo.replace_acceptance_criteria(
        fresh,
        1,
        [
            AcceptanceCriterion(
                id="AC-1",
                given="g",
                when="w",
                then="t",
                verifiable_by="test",
                test_ref="tests/test_x.py::test_a",
            )
        ],
    )
    await fresh.execute(
        "INSERT INTO ac_test_results (task_id, ac_id, submission_generation, status, "
        "source) VALUES (1, 'AC-1', 1, 'pass', 'ci:11:1')"
    )
    prov = json.dumps({"run_id": "11", "run_attempt": "1", "run_conclusion": "success"})
    checks = json.dumps(
        {"lint": "pass", "format": "pass", "types": "pass", "tests": "pass"}
    )
    for task_id, sha, trust, reason, final, run in (
        (1, "sha-v", "verified", "", 1, "11"),
        (2, "sha-u", "unverified", "legacy", 1, "12"),
    ):
        await fresh.execute(
            "INSERT INTO ci_report_attempts (task_id, head_sha, run_id, run_attempt, "
            "checks, validation_status, provenance, trust, trust_reason, trust_final) "
            "VALUES (?, ?, ?, '1', ?, 'pass', ?, ?, ?, ?)",
            (task_id, sha, run, checks, prov, trust, reason, final),
        )
        await fresh.execute(
            "INSERT INTO ci_run_reports (task_id, head_sha, validation_status, checks, "
            "provenance, trust, trust_reason, attempt_key, reported_by) "
            "VALUES (?, ?, 'pass', ?, ?, ?, ?, ?, 'ci')",
            (task_id, sha, checks, prov, trust, reason, f"ci:{run}:1"),
        )
    await fresh.commit()
    await fresh.close()
    raw = sqlite3.connect(path)
    for table in ("ci_report_attempts", "ci_run_reports"):
        raw.execute(f"ALTER TABLE {table} DROP COLUMN contract_fp")
    raw.execute(
        "DELETE FROM _migrations WHERE name IN "
        "('add_ci_report_attempts_contract_fp', 'add_ci_run_reports_contract_fp')"
    )
    raw.commit()
    raw.close()
    return path


async def _answers(conn, task_id: int, sha: str) -> dict:
    from hub import config
    from hub import repository as repo
    from hub.services import ci_trust, lifecycle
    from hub.services.ac_tests import ac_tests_gap

    task = dict(await repo.get_task(conn, task_id))
    return {
        "pass_gap": await ci_trust.pass_gap(conn, task_id, sha),
        "ac_gap": await ac_tests_gap(conn, task),
        "validation_gap": await lifecycle._validation_gap_of_the_work(conn, task),
        "sdd": config.SDD_VALIDATION,
    }


async def test_ci_contract_fingerprint_migration_keeps_trust(tmp_path):
    from hub import db as db_module
    from hub import repository as repo
    from hub.services import ci_contract, ci_trust

    # The literal in the migration and in the repository is the hub contract's.
    assert repo.HUB_CONTRACT_FP == ci_contract.HUB_FINGERPRINT
    sql = dict(db_module._MIGRATIONS)["add_ci_report_attempts_contract_fp"]
    assert ci_contract.HUB_FINGERPRINT in sql

    path = await _state_before_1848(tmp_path)
    conn = await db_module.connect(str(path))
    try:
        await db_module.bootstrap(conn)
        first = await _answers(conn, 1, "sha-v"), await _answers(conn, 2, "sha-u")
        await db_module.bootstrap(conn)  # a repeated bootstrap changes nothing
        assert (
            await _answers(conn, 1, "sha-v"),
            await _answers(conn, 2, "sha-u"),
        ) == first

        fp = ci_contract.HUB_FINGERPRINT
        for task_id, sha, trust in (
            (1, "sha-v", "verified"),
            (2, "sha-u", "unverified"),
        ):
            report = dict(await repo.get_ci_run_report(conn, task_id, sha))
            attempt = dict((await repo.list_ci_report_attempts(conn, task_id, sha))[0])
            # The attempt and the projection carry the same fingerprint: the hub's.
            assert report["contract_fp"] == attempt["contract_fp"] == fp
            # The hub's verified stays verified; unverified/legacy never becomes it.
            assert ci_trust.trust_of(report, fp)[0] == trust

        # The answers of the gates are those of a verified green hub report, and
        # the evidence CI wrote (the AC result and its source) is still counted.
        verified, unverified = first
        assert verified["pass_gap"] is None
        assert verified["ac_gap"] is None
        assert verified["validation_gap"] is None
        assert unverified["pass_gap"] is not None
        results = [dict(r) for r in await repo.list_ac_test_results(conn, 1)]
        assert [(r["ac_id"], r["status"], r["source"]) for r in results] == [
            ("AC-1", "pass", "ci:11:1")
        ]
        assert dict(await repo.get_task(conn, 1))["validation_source"] == "ci:11:1"

        # A claim that arrives after the upgrade has no fingerprint until checked.
        await repo.upsert_ci_report_attempt(
            conn,
            task_id=1,
            head_sha="sha-new",
            run_id="13",
            run_attempt="1",
            ac_results="{}",
            checks="{}",
            validation_status="pass",
            validation_log="",
            reason="",
            reported_by="ci",
            mutations=None,
            baseline=None,
            provenance="{}",
            submitted="{}",
        )
        fresh_attempt = dict(
            (await repo.list_ci_report_attempts(conn, 1, "sha-new"))[0]
        )
        assert fresh_attempt["contract_fp"] == ci_contract.NO_FINGERPRINT == ""
        assert ci_trust.trust_of({**fresh_attempt, "trust": "verified"}, fp)[0] == (
            "unverified"
        )
        # A verdict stamps the attempt; a repeated claim of that attempt takes the
        # stamp away again - it is a new claim.
        await repo.set_ci_attempt_trust(
            conn,
            fresh_attempt["id"],
            trust="verified",
            trust_reason="",
            final=True,
            body_version=fresh_attempt["body_version"],
            contract_fp=fp,
        )
        stamped = dict((await repo.list_ci_report_attempts(conn, 1, "sha-new"))[0])
        assert stamped["contract_fp"] == fp
        await repo.upsert_ci_report_attempt(
            conn,
            task_id=1,
            head_sha="sha-new",
            run_id="13",
            run_attempt="1",
            ac_results="{}",
            checks="{}",
            validation_status="pass",
            validation_log="",
            reason="",
            reported_by="ci",
            mutations=None,
            baseline=None,
            provenance="{}",
            submitted="{}",
        )
        again = dict((await repo.list_ci_report_attempts(conn, 1, "sha-new"))[0])
        assert again["contract_fp"] == "" and again["trust"] == "unverified"
    finally:
        await conn.close()
