"""Closing a production defect leaves something behind (#919, feature #908).

A defect found in prod (``found_in='prod'``) may not reach ``completed`` with
nothing after it: the close carries one of three prevention outputs — a
regression test, a rule from the category_checks catalogue (#878), or an
explicitly accepted risk with a reason and a revisit condition. The gate
stands on every door into ``completed``:

* the agent's done report (``add_update`` kind=done) — refused, 422;
* the pair submission (``submit_for_review``) — refused, 422, because after
  it the poller delivers without another word from the agent;
* the automatic completions (``transition_after_agent_done``, the poller's
  delivery sweep, the parent rollup) — held, never completed silently;
* the human overrides (decide accept, force-complete) — an emergency exit that
  stays open, but a close without a prevention output is recorded as its own
  event and alert rather than disappearing.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from hub import repository as repo
from hub.db import _MIGRATIONS, _SCHEMA, _migrate


async def _defect(
    db: aiosqlite.Connection,
    *,
    found_in: str = "prod",
    status: str = "pending_report",
    task_type: str = "task",
    parent_id: int | None = None,
) -> int:
    cur = await db.execute(
        "INSERT INTO tasks (title, description, status, found_in, auto_review, "
        "task_type, parent_id) VALUES (?, '', ?, ?, 0, ?, ?)",
        (f"defect {found_in}", status, found_in, task_type, parent_id),
    )
    await db.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


async def _row(db: aiosqlite.Connection, task_id: int) -> dict:
    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    return dict(rows[0])


async def _events(db: aiosqlite.Connection, task_id: int, kind: str) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT * FROM events WHERE task_id=? AND kind=?", (task_id, kind)
    )
    return [dict(r) for r in rows]


async def _done(client, task_id: int, prevention: dict | None = None):
    body: dict = {"agent": "dev", "kind": "done", "content": "fixed"}
    if prevention is not None:
        body["prevention"] = prevention
    return await client.post(f"/api/tasks/{task_id}/updates", json=body)


def _names_all_three(text: str) -> bool:
    return all(k in text for k in ("regression_test", "rule", "accepted_risk"))


# --- AC-1 -------------------------------------------------------------------


async def test_close_requires_test_rule_or_accepted_risk(client, db):
    task_id = await _defect(db)

    refused = await _done(client, task_id)
    assert refused.status_code == 422, refused.text
    assert "prevention_required" in refused.text
    assert _names_all_three(refused.text)
    row = await _row(db, task_id)
    assert row["status"] == "pending_report"
    assert row["defect_prevention"] is None
    # The refusal rolls the report back: no done row claims a close.
    assert not await db.execute_fetchall(
        "SELECT 1 FROM task_updates WHERE task_id=? AND kind='done'", (task_id,)
    )

    accepted = await _done(
        client,
        task_id,
        {"kind": "regression_test", "ref": "tests/test_x.py::test_y"},
    )
    assert accepted.status_code in (200, 201), accepted.text
    row = await _row(db, task_id)
    assert row["status"] == "completed"
    stored = json.loads(row["defect_prevention"])
    assert stored["kind"] == "regression_test"
    assert stored["ref"] == "tests/test_x.py::test_y"
    assert await _events(db, task_id, "defect_prevention_recorded")
    view = (await client.get(f"/api/tasks/{task_id}")).json()
    assert view["defect_prevention"]["kind"] == "regression_test"


async def test_regression_test_needs_a_locator(client, db):
    task_id = await _defect(db)
    resp = await _done(client, task_id, {"kind": "regression_test", "ref": "  "})
    assert resp.status_code == 422
    assert (await _row(db, task_id))["status"] == "pending_report"


async def test_rule_must_be_in_category_checks(client, db):
    task_id = await _defect(db)
    unknown = await _done(client, task_id, {"kind": "rule", "ref": "gate-semantics"})
    assert unknown.status_code == 422
    assert "category_checks" in unknown.text
    assert (await _row(db, task_id))["status"] == "pending_report"

    await repo.upsert_category_check(
        db, category="gate-semantics", check_ref="scripts/lint_gates.py"
    )
    await db.commit()
    ok = await _done(client, task_id, {"kind": "rule", "ref": "gate-semantics"})
    assert ok.status_code in (200, 201), ok.text
    stored = json.loads((await _row(db, task_id))["defect_prevention"])
    assert stored["kind"] == "rule"
    assert stored["check_ref"] == "scripts/lint_gates.py"


async def test_prevention_only_rides_a_done_report(client, db):
    task_id = await _defect(db, status="running")
    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={
            "agent": "dev",
            "kind": "status",
            "content": "x",
            "prevention": {"kind": "regression_test", "ref": "t"},
        },
    )
    assert resp.status_code == 422


async def test_pair_submission_refused_without_prevention(client, db):
    task_id = await _defect(db, status="running")
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={"agent": "dev"}
    )
    assert resp.status_code == 422, resp.text
    assert "prevention_required" in resp.text
    assert _names_all_three(resp.text)
    assert (await _row(db, task_id))["status"] == "running"


async def test_automatic_completion_is_held(db):
    from hub.services.orchestration import transition_after_agent_done

    task_id = await _defect(db, status="running")
    task = await _row(db, task_id)
    result = await transition_after_agent_done(db, task, has_done=True)
    await db.commit()
    assert result == "needs_decision"
    assert (await _row(db, task_id))["status"] == "needs_decision"
    held = await _events(db, task_id, "needs_decision")
    assert held and json.loads(held[0]["payload"])["reason"] == "prevention_missing"


async def test_poller_delivery_is_held_before_the_merge(db):
    from hub import poller

    task_id = await _defect(db, status="review")
    await db.execute("UPDATE tasks SET pr_number=7 WHERE id=?", (task_id,))
    await db.commit()
    merge = AsyncMock(side_effect=AssertionError("merged a defect without output"))
    with patch("hub.services.merge_before_completion", merge):
        await poller._deliver_pair_task(db, await _row(db, task_id))
    assert (await _row(db, task_id))["status"] == "needs_decision"
    merge.assert_not_called()


async def test_parent_rollup_is_held(db):
    from hub.services.lifecycle import maybe_rollup_parent

    parent = await _defect(db, status="running", task_type="feature")
    child = await _defect(db, found_in="unknown", status="completed", parent_id=parent)
    await maybe_rollup_parent(db, child)
    await db.commit()
    assert (await _row(db, parent))["status"] == "running"


async def test_stale_parent_repair_is_held(db):
    from hub.services.lifecycle import repair_stale_parent_completions

    parent = await _defect(db, status="running", task_type="feature")
    await _defect(db, found_in="unknown", status="completed", parent_id=parent)
    await repair_stale_parent_completions(db)
    assert (await _row(db, parent))["status"] == "running"


async def test_human_accept_closes_but_records_the_missing_output(db):
    from hub.models import TaskDecide
    from hub.services.lifecycle import decide_task

    task_id = await _defect(db, status="needs_decision")
    await decide_task(db, task_id, TaskDecide(action="accept"))
    assert (await _row(db, task_id))["status"] == "completed"
    assert await _events(db, task_id, "prod_defect_closed_without_prevention")


async def test_force_complete_closes_but_records_the_missing_output(db):
    from hub.models import TaskForceComplete
    from hub.services.lifecycle import force_complete_task

    task_id = await _defect(db, status="running")
    await force_complete_task(db, task_id, TaskForceComplete(comment="stuck"))
    assert (await _row(db, task_id))["status"] == "completed"
    [event] = await _events(db, task_id, "prod_defect_closed_without_prevention")
    assert json.loads(event["payload"])["via"] == "force_complete"


# --- AC-2 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "prevention",
    [
        {"kind": "accepted_risk", "reason": "", "revisit": "after 2026-12-01"},
        {"kind": "accepted_risk", "reason": "rare path", "revisit": ""},
        {"kind": "accepted_risk", "reason": "  ", "revisit": "  "},
        {"kind": "accepted_risk"},
    ],
)
async def test_accepted_risk_needs_reason_and_revisit(client, db, prevention):
    task_id = await _defect(db)
    resp = await _done(client, task_id, prevention)
    assert resp.status_code == 422, resp.text
    assert "accepted_risk" in resp.text
    row = await _row(db, task_id)
    assert row["status"] == "pending_report"
    assert row["defect_prevention"] is None


async def test_accepted_risk_with_reason_and_revisit_counts(client, db):
    task_id = await _defect(db)
    resp = await _done(
        client,
        task_id,
        {
            "kind": "accepted_risk",
            "reason": "one customer, manual workaround",
            "revisit": "if it recurs or by 2026-12-01",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    stored = json.loads((await _row(db, task_id))["defect_prevention"])
    assert stored["kind"] == "accepted_risk"
    assert stored["revisit"] == "if it recurs or by 2026-12-01"


# --- AC-3 -------------------------------------------------------------------


@pytest.mark.parametrize("found_in", ["unknown", "review", "ci", "test", "staging"])
async def test_gate_scoped_to_prod_defects(client, db, found_in):
    task_id = await _defect(db, found_in=found_in)
    resp = await _done(client, task_id)
    assert resp.status_code in (200, 201), resp.text
    assert (await _row(db, task_id))["status"] == "completed"

    from hub.models import TaskForceComplete
    from hub.services.lifecycle import force_complete_task

    other = await _defect(db, found_in=found_in, status="running")
    await force_complete_task(db, other, TaskForceComplete(comment="stuck"))
    assert not await _events(db, other, "prod_defect_closed_without_prevention")


# --- Schema -----------------------------------------------------------------


async def test_migration_on_clean_and_populated_base():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(_SCHEMA)
        # A base with data, migrated up to just before this column.
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, "
            "applied_at TEXT DEFAULT (datetime('now')))"
        )
        for name, sql in _MIGRATIONS:
            if name == "add_defect_prevention_column":
                break
            try:
                await conn.execute(sql)
            except Exception:  # noqa: BLE001 - column already in _SCHEMA
                pass
            await conn.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))
        await conn.execute(
            "INSERT INTO tasks (title, description, status) VALUES ('old', '', 'completed')"
        )
        await conn.commit()
        await _migrate(conn)
        await _migrate(conn)
        rows = await conn.execute_fetchall(
            "SELECT defect_prevention FROM tasks WHERE title='old'"
        )
        assert rows[0]["defect_prevention"] is None
    finally:
        await conn.close()


# --- Contract surfaces ------------------------------------------------------


async def test_mcp_report_done_passes_prevention():
    from hub import mcp_server

    post = AsyncMock(return_value={"id": 1})
    get = AsyncMock(return_value={"id": 5, "status": "completed"})
    prevention = {"kind": "regression_test", "ref": "tests/t.py::t"}
    with (
        patch.object(mcp_server, "_api_post", post),
        patch.object(mcp_server, "_api_get", get),
    ):
        await mcp_server.hub_report_done(5, "fixed", prevention=prevention)
    assert post.await_args.args[1]["prevention"] == prevention


def test_cli_update_passes_prevention():
    from hub.cli import build_parser

    args = build_parser().parse_args(
        [
            "update",
            "5",
            "--kind",
            "done",
            "--message",
            "fixed",
            "--prevention",
            '{"kind": "rule", "ref": "gate-semantics"}',
        ]
    )
    with patch("hub.cli._api", return_value={"id": 1}) as api:
        assert args.func(args) == 0
    body = api.call_args.args[2]
    assert body["prevention"] == {"kind": "rule", "ref": "gate-semantics"}


def test_cli_update_refuses_bad_prevention_json():
    from hub.cli import build_parser

    args = build_parser().parse_args(
        ["update", "5", "--kind", "done", "--message", "x", "--prevention", "{"]
    )
    with patch("hub.cli._api") as api:
        assert args.func(args) == 2
    api.assert_not_called()
