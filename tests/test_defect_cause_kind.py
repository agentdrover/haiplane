"""The kind of cause of a defect (#1791, wave 1.4).

``found_in`` says where a defect was caught; ``cause_kind`` says what it was.
Of 68 prod defects in a 30-day window only the 22 with ``caused_by_task_id``
were attributed; the rest mixed vulnerable dependencies, a spike, rework and a
duplicate with the regressions of deliveries. These tests pin:

* the passport: a kind and a link agree on the FINAL pair, whichever input
  wrote it, and a historical row (a link and no kind) is not called a regression;
* the gate: a prod defect does not close without a kind, whatever prevention
  came with the close, on every door, and a refusal leaves nothing behind;
* the report: escapes and the change failure rate do not move, the split by
  kind and the regression subset are added;
* the surfaces: REST, CLI, MCP and the page carry the same keys and numbers.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub.db import _MIGRATIONS, _SCHEMA, _migrate
from hub.repository import DefectPassportError
from hub.services import prevention_gate
from hub.services.orchestration import practice_metrics
from tests.state_support import (
    auth,
    evidence_for,
    make_state_task,
    pair_start,
    submit,
)

PREVENTION = {"kind": "regression_test", "ref": "tests/test_x.py::test_y"}
KINDS = (
    "regression",
    "latent_design",
    "dependency",
    "ops",
    "not_a_defect",
    "unknown",
)


async def _task(
    db: aiosqlite.Connection,
    *,
    found_in: str = "prod",
    status: str = "pending_report",
    cause_kind: str | None = None,
    caused_by: int | None = None,
    release_id: int | None = None,
    task_type: str = "task",
    parent_id: int | None = None,
    title: str = "defect",
) -> int:
    cur = await db.execute(
        "INSERT INTO tasks (title, description, status, found_in, auto_review, "
        "task_type, parent_id, cause_kind, caused_by_task_id, release_id, "
        "work_type) VALUES (?, '', ?, ?, 0, ?, ?, ?, ?, ?, 'bug')",
        (
            title,
            status,
            found_in,
            task_type,
            parent_id,
            cause_kind,
            caused_by,
            release_id,
        ),
    )
    await db.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


async def _row(db: aiosqlite.Connection, task_id: int) -> dict[str, Any]:
    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    return dict(rows[0])


async def _events(db: aiosqlite.Connection, task_id: int, kind: str) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT * FROM events WHERE task_id=? AND kind=?", (task_id, kind)
    )
    return [dict(r) for r in rows]


async def _snapshot(db: aiosqlite.Connection, task_id: int) -> dict[str, Any]:
    """Everything a refused close could have left behind, by the records."""
    await db.commit()
    row = await _row(db, task_id)
    tables = {
        "task_updates": "task_id",
        "events": "task_id",
        "task_submissions": "task_id",
        "task_evidence": "task_id",
    }
    counts: dict[str, int] = {}
    for table, column in tables.items():
        exists = await db.execute_fetchall(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        if exists:
            found = await db.execute_fetchall(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column}=?",  # noqa: S608
                (task_id,),
            )
            counts[table] = int(found[0]["n"])
    return {
        "status": row["status"],
        "defect_prevention": row["defect_prevention"],
        "submission_generation": row["submission_generation"],
        "submission_sha": row["submission_sha"],
        "counts": counts,
    }


async def _refine(client, task_id: int, **fields):
    return await client.post(f"/api/tasks/{task_id}/refine", json=fields)


async def _bulk(client, task_id: int, **fields):
    return await client.post(
        "/api/tasks/refine-bulk", json={"items": [{"task_id": task_id, **fields}]}
    )


async def _passport(db: aiosqlite.Connection, task_id: int, **fields):
    """The service itself, without HTTP: what every other input funnels into."""
    from hub.models import TaskRefine
    from hub.services.refinement import refine_task

    class _Resp:
        def __init__(self, status_code: int, text: str = "") -> None:
            self.status_code = status_code
            self.text = text

    try:
        await refine_task(db, task_id, TaskRefine(**fields))
    except DefectPassportError as exc:
        return _Resp(422, str(exc))
    await db.commit()
    return _Resp(200)


# --- AC-1: the final pair ---------------------------------------------------


async def test_cause_kind_and_caused_by_must_agree(client, db):
    """Every input judges the FINAL pair (stored row + this PATCH) the same way."""
    cause = await _task(db, found_in="unknown", title="the change that broke it")

    async def entries(task_id: int):
        # refine, refine-bulk and the passport writer: one behaviour.
        return {
            "refine": lambda **f: _refine(client, task_id, **f),
            "bulk": lambda **f: _bulk(client, task_id, **f),
            "passport": lambda **f: _passport(db, task_id, **f),
        }

    def verb(fields: dict) -> dict:
        # the passport writer takes cause_kind / caused_by_task_id by name
        return fields

    # --- a regression without a link, a link under another kind: refused ----
    for name in ("refine", "bulk", "passport"):
        lone = await _task(db, title=f"lone regression {name}")
        send = (await entries(lone))[name]
        resp = await send(**verb({"cause_kind": "regression"}))
        assert resp.status_code == 422, (name, resp.text)
        assert "caused_by_task_id" in resp.text, name
        assert (await _row(db, lone))["cause_kind"] is None, name

        other = await _task(db, title=f"design with a link {name}")
        send = (await entries(other))[name]
        resp = await send(
            **verb({"cause_kind": "latent_design", "caused_by_task_id": cause})
        )
        assert resp.status_code == 422, (name, resp.text)
        row = await _row(db, other)
        assert (row["cause_kind"], row["caused_by_task_id"]) == (None, None), name

        # --- agreed pairs are accepted -------------------------------------
        send = (await entries(lone))[name]
        ok = await send(
            **verb({"cause_kind": "regression", "caused_by_task_id": cause})
        )
        assert ok.status_code == 200, (name, ok.text)
        row = await _row(db, lone)
        assert (row["cause_kind"], row["caused_by_task_id"]) == ("regression", cause)

        # --- stored (regression, link): one half at a time ------------------
        # clearing the link under a regression: refused, the row is untouched
        clear = {"clear_caused_by": True}
        resp = (
            await send(**clear)
            if name != "passport"
            else await _passport(db, lone, clear_caused_by=True)
        )
        assert resp.status_code == 422, (name, resp.text)
        assert (await _row(db, lone))["caused_by_task_id"] == cause, name
        # another kind only, the link stays: refused
        resp = await send(**verb({"cause_kind": "ops"}))
        assert resp.status_code == 422, (name, resp.text)
        assert (await _row(db, lone))["cause_kind"] == "regression", name
        # a new link only, the kind stays: accepted
        other_cause = await _task(db, found_in="unknown", title=f"other cause {name}")
        resp = await send(**verb({"caused_by_task_id": other_cause}))
        assert resp.status_code == 200, (name, resp.text)
        assert (await _row(db, lone))["caused_by_task_id"] == other_cause, name
        # both at once, agreed: another kind AND the link cleared
        both = (
            {"cause_kind": "ops", "clear_caused_by": True}
            if name != "passport"
            else None
        )
        if both is None:
            resp = await _passport(db, lone, cause_kind="ops", clear_caused_by=True)
        else:
            resp = await send(**both)
        assert resp.status_code == 200, (name, resp.text)
        row = await _row(db, lone)
        assert (row["cause_kind"], row["caused_by_task_id"]) == ("ops", None), name

        # --- stored (ops, no link): a link only is refused -----------------
        resp = await send(**verb({"caused_by_task_id": cause}))
        assert resp.status_code == 422, (name, resp.text)
        assert (await _row(db, lone))["caused_by_task_id"] is None, name
        # the kind only, to regression, still without a link: refused
        resp = await send(**verb({"cause_kind": "regression"}))
        assert resp.status_code == 422, (name, resp.text)
        assert (await _row(db, lone))["cause_kind"] == "ops", name

    # --- a historical row: a link and no kind ------------------------------
    for name in ("refine", "bulk", "passport"):
        old = await _task(db, found_in="unknown", caused_by=cause, title=f"old {name}")
        send = (await entries(old))[name]
        # an unrelated PATCH is not judged and does not invent a regression
        resp = await send(**verb({"detected_at": "2026-10-01 10:00:00"}))
        assert resp.status_code == 200, (name, resp.text)
        resp = await send(**verb({"found_in": "prod"}))
        assert resp.status_code == 200, (name, resp.text)
        row = await _row(db, old)
        assert (row["cause_kind"], row["caused_by_task_id"]) == (None, cause), name
        # naming the kind over the existing link is judged on the final pair
        resp = await send(**verb({"cause_kind": "dependency"}))
        assert resp.status_code == 422, (name, resp.text)
        resp = await send(**verb({"cause_kind": "regression"}))
        assert resp.status_code == 200, (name, resp.text)
        assert (await _row(db, old))["cause_kind"] == "regression", name

    # --- a PATCH that touches neither half is never judged on the pair ------
    # (a pair broken by a path outside refine, e.g. a deleted cause, must not
    # make a date or a stage uneditable)
    broken = await _task(db, cause_kind="regression", title="pair broken outside")
    for send in (
        lambda **f: _refine(client, broken, **f),
        lambda **f: _bulk(client, broken, **f),
        lambda **f: _passport(db, broken, **f),
    ):
        resp = await send(detected_at="2026-10-02 10:00:00")
        assert resp.status_code == 200, resp.text
    assert (await _row(db, broken))["cause_kind"] == "regression"

    # --- the kind is never taken back to empty: null means "leave as is" ----
    named = await _task(db, cause_kind="ops", title="named once")
    assert (await _refine(client, named, cause_kind=None)).status_code == 200
    assert (await _bulk(client, named, cause_kind=None)).status_code == 200
    assert (await _passport(db, named, cause_kind=None)).status_code == 200
    assert (await _row(db, named))["cause_kind"] == "ops"

    # --- a value outside the list is refused by every input ----------------
    stray = await _task(db, title="stray kind")
    assert (await _refine(client, stray, cause_kind="bad")).status_code == 422
    assert (await _bulk(client, stray, cause_kind="bad")).status_code == 422
    with pytest.raises(ValueError, match="cause_kind"):
        await _passport(db, stray, cause_kind="bad")
    assert (await _row(db, stray))["cause_kind"] is None

    # --- the bulk is atomic: a bad pair in item 2 takes item 1 back --------
    first = await _task(db, title="bulk first")
    second = await _task(db, title="bulk second")
    resp = await client.post(
        "/api/tasks/refine-bulk",
        json={
            "items": [
                {"task_id": first, "cause_kind": "ops"},
                {"task_id": second, "cause_kind": "regression"},
            ]
        },
    )
    assert resp.status_code == 422
    assert (await _row(db, first))["cause_kind"] is None

    # --- registering an emergency defect without a kind still works --------
    filed = await client.post(
        "/api/prod-defects",
        json={
            "title": "Карточка отдаёт 500",
            "broken": "GET /tasks/1 падает",
            "verify": "curl /tasks/1 отвечает 200",
            "affected_areas": ["hub/app.py"],
        },
    )
    assert filed.status_code in (200, 201), filed.text
    task = filed.json()["task"]
    assert task["found_in"] == "prod" and task["cause_kind"] is None
    assert (await _row(db, task["id"]))["cause_kind"] is None


async def test_the_view_and_the_card_carry_the_kind(client, db):
    task_id = await _task(db, cause_kind="dependency", title="vulnerable package")
    view = (await client.get(f"/api/tasks/{task_id}")).json()
    assert view["cause_kind"] == "dependency"
    blank = await _task(db, title="nobody named it")
    assert (await client.get(f"/api/tasks/{blank}")).json()["cause_kind"] is None
    page = (await client.get(f"/tasks/{task_id}")).text
    assert 'data-cause-kind="dependency"' in page


async def test_a_kind_is_a_nullable_column_and_the_migration_repeats():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(_SCHEMA)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, "
            "applied_at TEXT DEFAULT (datetime('now')))"
        )
        for name, sql in _MIGRATIONS:
            if name == "add_tasks_cause_kind":
                break
            try:
                await conn.execute(sql)
            except Exception:  # noqa: BLE001 - column already in _SCHEMA
                pass
            await conn.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))
        await conn.execute(
            "INSERT INTO tasks (title, description, status, found_in, "
            "caused_by_task_id) VALUES ('old', '', 'completed', 'prod', NULL)"
        )
        await conn.commit()
        await _migrate(conn)
        await _migrate(conn)
        cols = {
            r["name"]: dict(r)
            for r in await conn.execute_fetchall("PRAGMA table_info(tasks)")
        }
        assert cols["cause_kind"]["notnull"] == 0
        assert cols["cause_kind"]["dflt_value"] is None
        rows = await conn.execute_fetchall(
            "SELECT cause_kind FROM tasks WHERE title='old'"
        )
        assert rows[0]["cause_kind"] is None
    finally:
        await conn.close()


async def test_the_fingerprint_of_a_task_without_a_kind_did_not_move(db):
    """A new empty column must not restamp every statement in the base."""
    from hub.services.statement_generation import statement_fingerprint

    task_id = await _task(db, found_in="unknown", title="a statement")
    plain = await statement_fingerprint(db, task_id)
    await db.execute("UPDATE tasks SET cause_kind='unknown' WHERE id=?", (task_id,))
    named = await statement_fingerprint(db, task_id)
    assert plain != named, "naming a cause is a recorded edit"
    await db.execute("UPDATE tasks SET cause_kind=NULL WHERE id=?", (task_id,))
    assert await statement_fingerprint(db, task_id) == plain


# --- AC-2: the kind on every door of closing --------------------------------


async def _done(client, task_id: int, prevention: dict | None = None):
    body: dict = {"agent": "dev", "kind": "done", "content": "fixed"}
    if prevention is not None:
        body["prevention"] = prevention
    return await client.post(f"/api/tasks/{task_id}/updates", json=body)


async def _pair_defect(db, **kwargs) -> int:
    task_id = await _task(db, status="running", **kwargs)
    await db.execute(
        "UPDATE tasks SET branch='task-1/fix', git_mode='remote', auto_review=1 "
        "WHERE id=?",
        (task_id,),
    )
    await db.commit()
    return task_id


async def test_closing_a_prod_defect_needs_its_cause_kind(client, db):
    # --- agent door 1: the done report, with a VALID prevention -------------
    done_id = await _task(db, title="done door")
    before = await _snapshot(db, done_id)
    refused = await _done(client, done_id, PREVENTION)
    assert refused.status_code == 422, refused.text
    assert "cause_kind_required" in refused.text
    assert "prevention_required" not in refused.text, "the prevention was valid"
    assert await _snapshot(db, done_id) == before, "no done row, no prevention"

    # without a prevention one refusal names both reasons
    both = await _done(client, done_id)
    assert both.status_code == 422
    assert "cause_kind_required" in both.text and "prevention_required" in both.text
    assert await _snapshot(db, done_id) == before

    # --- agent door 2: the pair submission, with a VALID prevention ---------
    pair_id = await _pair_defect(db, title="submit door")
    before = await _snapshot(db, pair_id)
    body = {"agent": "dev", "branch": "task-1/fix", "prevention": PREVENTION}
    refused = await client.post(f"/api/tasks/{pair_id}/submit-review", json=body)
    assert refused.status_code == 422, refused.text
    assert "cause_kind_required" in refused.text
    assert "prevention_required" not in refused.text
    assert await _snapshot(db, pair_id) == before, "no submission, no prevention"

    # --- the final state in the transaction, not the caller's dict ----------
    stale = await _task(db, title="stale dict")
    fresh_row = await _row(db, stale)
    await db.execute("UPDATE tasks SET cause_kind='ops' WHERE id=?", (stale,))
    await db.commit()
    # the caller's dict still says "no kind": the row now has one
    await prevention_gate.record_done_prevention(
        db,
        fresh_row,
        kind="done",
        prevention=prevention_gate.DefectPrevention(**PREVENTION),
        actor="dev",
    )
    claimed = await _task(db, title="dict claims a kind")
    liar = dict(await _row(db, claimed)) | {"cause_kind": "ops"}
    with pytest.raises(HTTPException) as refusal:
        await prevention_gate.record_done_prevention(
            db,
            liar,
            kind="done",
            prevention=prevention_gate.DefectPrevention(**PREVENTION),
            actor="dev",
        )
    assert refusal.value.status_code == 422

    # the same for the submission door: validated against the row, not the dict
    old_row = await _row(db, await _task(db, title="stale submission"))
    await db.execute("UPDATE tasks SET cause_kind='ops' WHERE id=?", (old_row["id"],))
    await db.commit()
    record = await prevention_gate.check_submission(
        db, old_row, prevention_gate.DefectPrevention(**PREVENTION)
    )
    assert record is not None and record["kind"] == "regression_test"
    bare_row = await _row(db, await _task(db, title="submission, dict lies"))
    with pytest.raises(HTTPException) as refusal:
        await prevention_gate.check_submission(
            db,
            bare_row | {"cause_kind": "ops"},
            prevention_gate.DefectPrevention(**PREVENTION),
        )
    assert refusal.value.status_code == 422

    # --- automatic doors hold -----------------------------------------------
    from hub import poller
    from hub.services.lifecycle import maybe_rollup_parent
    from hub.services.orchestration import transition_after_agent_done

    stored = json.dumps({"kind": "regression_test", "ref": "t"})
    auto_id = await _task(db, status="running", title="auto door")
    await db.execute(
        "UPDATE tasks SET defect_prevention=? WHERE id=?", (stored, auto_id)
    )
    await db.commit()
    result = await transition_after_agent_done(
        db, await _row(db, auto_id), has_done=True
    )
    await db.commit()
    assert result == "needs_decision"
    [held] = await _events(db, auto_id, "needs_decision")
    assert json.loads(held["payload"])["reason"] == "cause_kind_missing"

    poll_id = await _task(db, status="review", title="poller door")
    await db.execute(
        "UPDATE tasks SET pr_number=7, defect_prevention=? WHERE id=?",
        (stored, poll_id),
    )
    await db.commit()
    merge = AsyncMock(side_effect=AssertionError("merged without a cause"))
    with patch("hub.services.merge_before_completion", merge):
        await poller._deliver_pair_task(db, await _row(db, poll_id))
    assert (await _row(db, poll_id))["status"] == "needs_decision"
    merge.assert_not_called()

    parent = await _task(db, status="running", task_type="feature", title="rollup")
    await db.execute(
        "UPDATE tasks SET defect_prevention=? WHERE id=?", (stored, parent)
    )
    child = await _task(
        db, found_in="unknown", status="completed", parent_id=parent, title="child"
    )
    await maybe_rollup_parent(db, child)
    await db.commit()
    assert (await _row(db, parent))["status"] == "running"

    from hub.services.lifecycle import repair_stale_parent_completions

    swept = await _task(db, status="running", task_type="feature", title="swept")
    await db.execute("UPDATE tasks SET defect_prevention=? WHERE id=?", (stored, swept))
    await _task(
        db, found_in="unknown", status="completed", parent_id=swept, title="done child"
    )
    await repair_stale_parent_completions(db)
    await db.commit()
    assert (await _row(db, swept))["status"] == "running"

    # --- human doors: an exit with an audit, counted apart ------------------
    from hub.models import TaskDecide, TaskForceComplete
    from hub.services.lifecycle import decide_task, force_complete_task

    accept_id = await _task(db, status="needs_decision", title="accept")
    await db.execute(
        "UPDATE tasks SET defect_prevention=? WHERE id=?", (stored, accept_id)
    )
    await db.commit()
    await decide_task(db, accept_id, TaskDecide(action="accept"))
    assert (await _row(db, accept_id))["status"] == "completed"
    [bypass] = await _events(db, accept_id, "prod_defect_closed_without_cause_kind")
    assert json.loads(bypass["payload"])["via"] == "decide_accept"
    assert not await _events(db, accept_id, "prod_defect_closed_without_prevention")

    force_id = await _task(db, status="running", title="force")
    await force_complete_task(db, force_id, TaskForceComplete(comment="stuck"))
    assert (await _row(db, force_id))["status"] == "completed"
    [bypass] = await _events(db, force_id, "prod_defect_closed_without_cause_kind")
    assert json.loads(bypass["payload"])["via"] == "force_complete"
    assert await _events(db, force_id, "prod_defect_closed_without_prevention"), (
        "both gaps are counted, each under its own name"
    )

    # a defect with its kind and its output leaves no bypass record at all
    clean = await _task(db, status="running", cause_kind="ops", title="clean")
    await db.execute("UPDATE tasks SET defect_prevention=? WHERE id=?", (stored, clean))
    await db.commit()
    await force_complete_task(db, clean, TaskForceComplete(comment="stuck"))
    assert not await _events(db, clean, "prod_defect_closed_without_cause_kind")
    assert not await _events(db, clean, "prod_defect_closed_without_prevention")

    # --- unknown is an answer for the cause, not a prevention ---------------
    named = await _task(db, cause_kind="unknown", title="unknown with output")
    closed = await _done(client, named, PREVENTION)
    assert closed.status_code in (200, 201), closed.text
    assert (await _row(db, named))["status"] == "completed"

    bare = await _task(db, cause_kind="unknown", title="unknown, no output")
    refused = await _done(client, bare)
    assert refused.status_code == 422
    assert "prevention_required" in refused.text
    assert "cause_kind_required" not in refused.text
    assert (await _row(db, bare))["status"] == "pending_report"

    # a defect that is not from prod never meets the gate
    elsewhere = await _task(db, found_in="review", title="not prod")
    assert (await _done(client, elsewhere)).status_code in (200, 201)


async def test_state_task_doors_need_the_cause_kind(client, db, monkeypatch):
    """The state-task submission and its approval are doors too (#1647, #919)."""
    headers = auth(monkeypatch)
    # --- agent door 3: the state submission ---------------------------------
    state_id = await make_state_task(db, title="state door")
    await db.execute("UPDATE tasks SET found_in='prod' WHERE id=?", (state_id,))
    await db.commit()
    assert (
        await pair_start(client, state_id, headers=headers["impl"])
    ).status_code == 200
    before = await _snapshot(db, state_id)
    refused = await submit(
        client, state_id, evidence_for(), headers=headers["impl"], prevention=PREVENTION
    )
    assert refused.status_code == 422, refused.text
    assert "cause_kind_required" in refused.text
    assert await _snapshot(db, state_id) == before, "no evidence, no submission"

    # the state verdict: submitted with a kind, the kind then gone
    approved_id = await make_state_task(db, title="state verdict")
    await db.execute(
        "UPDATE tasks SET found_in='prod', cause_kind='unknown' WHERE id=?",
        (approved_id,),
    )
    await db.commit()
    assert (
        await pair_start(client, approved_id, headers=headers["impl"])
    ).status_code == 200
    sent = await submit(
        client,
        approved_id,
        evidence_for(),
        headers=headers["impl"],
        prevention=PREVENTION,
    )
    assert sent.status_code == 200, sent.text
    await db.execute("UPDATE tasks SET cause_kind=NULL WHERE id=?", (approved_id,))
    await db.commit()
    verdict = await client.post(
        f"/api/tasks/{approved_id}/review-verdict",
        json={"verdict": "approved", "expected_generation": 1},
        headers=headers["human"],
    )
    assert verdict.status_code == 200, verdict.text
    assert verdict.json()["status"] == "needs_decision"
    reasons = [
        json.loads(e["payload"]).get("reason")
        for e in await _events(db, approved_id, "needs_decision")
    ]
    assert "cause_kind_missing" in reasons


# --- AC-3: escapes and CFR stay, the split is added -------------------------


async def _deploy(db, sha: str, *, deployed: str = "-1 days") -> int:
    release_id = await repo.record_release(
        db, deployed_sha=sha, project_id=None, status="success", source="ci"
    )
    await db.execute(
        "UPDATE releases SET deployed_at=datetime('now', ?) WHERE id=?",
        (deployed, release_id),
    )
    return release_id


async def _window(db) -> dict[str, int]:
    """Prod regression, latent design, unknown, empty; review and CI defects;
    two defects on one release; one outside the window."""
    cause = await _task(db, found_in="unknown", status="completed", title="cause")
    r1, r2, r3, r4 = [await _deploy(db, f"{i:040x}") for i in range(1, 5)]
    await _task(
        db, cause_kind="regression", caused_by=cause, release_id=r1, title="regression"
    )
    await _task(db, cause_kind="latent_design", release_id=r1, title="same release")
    await _task(db, cause_kind="unknown", release_id=r2, title="unknown")
    await _task(db, release_id=None, title="empty")
    await _task(
        db,
        found_in="review",
        cause_kind="regression",
        caused_by=cause,
        release_id=r3,
        title="review",
    )
    await _task(db, found_in="ci", release_id=r3, title="ci")
    old = await _task(
        db, cause_kind="dependency", release_id=r4, title="outside the window"
    )
    await db.execute(
        "UPDATE tasks SET created_at=datetime('now', '-400 days') WHERE id=?", (old,)
    )
    await db.commit()
    return {"r1": r1, "r2": r2, "r3": r3, "r4": r4}


async def test_metrics_keep_escapes_and_cfr_and_add_the_cause_split(client, db):
    await _window(db)
    metrics = await practice_metrics(db)

    # unchanged: the numbers the same window gave before the field existed
    escaped = metrics["escaped_defects"]
    assert escaped["escaped"] == 4
    assert escaped["source"] == "found_in"
    cfr = metrics["change_failure_rate"]
    [default] = cfr["by_project"]
    assert (default["deploys"], default["failed_deploys"]) == (4, 3)
    assert default["rate"] == 0.75
    assert cfr["defects_without_release"] == 1
    by_stage = {r["stage"]: r["defects"] for r in metrics["shift_left"]["by_stage"]}
    assert by_stage["prod"] == escaped["escaped"]

    # added: the split, with the empty field and unknown as separate rows
    rows = {r["cause_kind"]: r for r in escaped["by_cause_kind"]}
    assert [r["cause_kind"] for r in escaped["by_cause_kind"]] == [*KINDS, "empty"]
    assert rows["regression"]["defects"] == 1
    assert rows["latent_design"]["defects"] == 1
    assert rows["unknown"]["defects"] == 1
    assert rows["empty"]["defects"] == 1
    assert rows["dependency"]["defects"] == 0, "outside the window"
    assert rows["regression"]["share"] == 0.25
    assert sum(r["defects"] for r in escaped["by_cause_kind"]) == escaped["escaped"]
    assert escaped["regressions"] == 1, "the subset: kind regression only"
    assert escaped["without_cause_kind"] == 1

    # an attribution without a kind is not a regression
    attributed = await _task(
        db,
        caused_by=(await _task(db, found_in="unknown", title="c2")),
        title="historical pair",
    )
    after = (await practice_metrics(db))["escaped_defects"]
    assert after["escaped"] == 5
    assert after["regressions"] == 1
    assert after["without_cause_kind"] == 2
    assert attributed

    # review_economy.escapes keeps its definition: the headline and the
    # reconstruction, nothing else
    escapes = (await practice_metrics(db))["review_economy"]["escapes"]
    assert set(escapes) == {"escaped", "reconstructed"}
    assert escapes["escaped"] == after["escaped"]


async def test_an_empty_window_splits_into_zeros_without_shares(db):
    escaped = (await practice_metrics(db))["escaped_defects"]
    assert escaped["escaped"] == 0 and escaped["regressions"] == 0
    assert all(
        r["defects"] == 0 and r["share"] is None for r in escaped["by_cause_kind"]
    )


# --- AC-4: one set of keys and numbers on every surface ---------------------


def split_of(escaped: dict) -> dict:
    keys = ("by_cause_kind", "regressions", "without_cause_kind")
    return {k: escaped[k] for k in keys}


def direct_sl(escaped: dict) -> dict:
    return split_of(escaped)


async def test_cause_split_is_the_same_on_every_surface(client, db):
    from hub import cli
    from hub.mcp_server import hub_practice_metrics

    await _window(db)
    direct = (await practice_metrics(db))["escaped_defects"]

    # REST
    rest = (await client.get("/api/metrics/practices")).json()
    split = rest["escaped_defects"]
    for key in ("by_cause_kind", "regressions", "without_cause_kind", "escaped"):
        assert split[key] == direct[key], key

    # CLI: prints the REST answer as it is
    printed: list[str] = []
    with (
        patch.object(sys, "argv", ["oc-hub", "practice-metrics"]),
        patch.object(cli, "_api", return_value=rest),
        patch.object(
            cli, "_print_json", side_effect=lambda d: printed.append(json.dumps(d))
        ),
    ):
        assert cli.main() in (0, None)
    cli_split = json.loads(printed[0])["escaped_defects"]
    assert cli_split["by_cause_kind"] == split["by_cause_kind"]
    assert cli_split["regressions"] == split["regressions"]

    # MCP: the text line carries the same numbers
    with patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get:
        get.return_value = rest
        result = await hub_practice_metrics()
    text = result.content[0].text
    expected = ", ".join(
        f"{r['cause_kind']} {r['defects']}" for r in split["by_cause_kind"]
    )
    assert f"Escaped to prod by cause_kind: {expected}; regressions 1" in text

    # shift-left carries the same split of its prod stage: REST, CLI, MCP
    sl = rest["shift_left"]
    assert sl["prod_by_cause_kind"] == split_of(split)
    assert sl["prod_by_cause_kind"] == direct_sl(direct)
    shown: list[dict] = []
    with (
        patch.object(sys, "argv", ["oc-hub", "shift-left"]),
        patch.object(cli, "_api", return_value=rest),
        patch.object(cli, "_print_json", side_effect=shown.append),
    ):
        assert cli.main() in (0, None)
    assert shown[0]["prod_by_cause_kind"] == sl["prod_by_cause_kind"]
    assert f"Shift-left prod by cause_kind: {expected}; regressions 1" in text

    # the page: a cell per row, the regression subset beside them
    page = (await client.get("/metrics")).text
    for row in split["by_cause_kind"]:
        cell = f'data-metric="escaped_defects.by_cause_kind.{row["cause_kind"]}">{row["defects"]}<'
        assert cell in page, row
    assert 'data-metric="escaped_defects.regressions">1<' in page
    assert "Вид причины" in page


def test_the_refine_surfaces_accept_the_kind():
    from hub import cli, mcp_server

    # CLI: --cause-kind rides the refine payload next to --caused-by
    ns = argparse.Namespace(cause_kind="regression", caused_by=7)
    assert cli._build_refine_payload(ns) == {
        "cause_kind": "regression",
        "caused_by_task_id": 7,
    }
    parser = cli.build_parser()
    parsed = parser.parse_args(["refine", "5", "--cause-kind", "ops"])
    assert parsed.cause_kind == "ops"
    with pytest.raises(SystemExit):
        parser.parse_args(["refine", "5", "--cause-kind", "bogus"])

    # MCP: the field is a parameter of hub_refine_task and goes out as sent
    post = AsyncMock(return_value={"id": 5, "acceptance_criteria": []})
    import asyncio

    with patch.object(mcp_server, "_api_post", post):
        asyncio.run(mcp_server.hub_refine_task(task_id=5, cause_kind="ops"))
    assert post.call_args.args[1] == {"cause_kind": "ops"}


# --- the gate is asked again where completed is written ----------------------


async def test_a_passport_changed_after_the_gate_does_not_complete(db):
    """The gate ran before the network wait; found_in turned prod under it."""
    from hub.services import delivery_writes, orchestration

    # the done-report writer
    task_id = await _task(db, found_in="unknown", status="review", title="done write")
    task = await _row(db, task_id)
    assert not await prevention_gate.hold_completion(
        db, task_id, via="report_done", actor="hub"
    ), "the gate passed: not a prod defect yet"
    await db.execute("UPDATE tasks SET found_in='prod' WHERE id=?", (task_id,))
    await db.commit()
    with patch.object(orchestration, "approval_stands", AsyncMock(return_value=True)):
        written = await orchestration._write_completed(db, task, 0, "done")
    assert written is None
    assert (await _row(db, task_id))["status"] == "needs_decision"
    assert not await _events(db, task_id, "task_completed")
    [held] = await _events(db, task_id, "needs_decision")
    assert json.loads(held["payload"])["reason"] == "prevention_missing"

    # the poller's delivery write; prevention is there, the kind is not
    poll_id = await _task(db, found_in="unknown", status="review", title="poll write")
    await db.execute(
        "UPDATE tasks SET found_in='prod', defect_prevention=? WHERE id=?",
        (json.dumps(PREVENTION), poll_id),
    )
    await db.commit()
    await delivery_writes.complete_delivered(db, poll_id, 7)
    assert (await _row(db, poll_id))["status"] == "needs_decision"
    assert not await _events(db, poll_id, "task_completed")
    [held] = await _events(db, poll_id, "needs_decision")
    assert json.loads(held["payload"])["reason"] == "cause_kind_missing"

    # a task that meets the gate still completes through both writers
    ok = await _task(db, status="review", cause_kind="ops", title="fine")
    await db.execute(
        "UPDATE tasks SET defect_prevention=? WHERE id=?", (json.dumps(PREVENTION), ok)
    )
    await db.commit()
    await delivery_writes.complete_delivered(db, ok, 8)
    assert (await _row(db, ok))["status"] == "completed"
