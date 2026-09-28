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
    from hub.services.prevention_gate import ROLLUP_HELD_NOTE

    parent = await _defect(db, status="running", task_type="feature")
    await _defect(db, found_in="unknown", status="completed", parent_id=parent)
    await repair_stale_parent_completions(db)
    await repair_stale_parent_completions(db)
    assert (await _row(db, parent))["status"] == "running"
    notes = await db.execute_fetchall(
        "SELECT 1 FROM task_updates WHERE task_id=? AND content=?",
        (parent, ROLLUP_HELD_NOTE),
    )
    assert len(notes) == 1


@pytest.mark.parametrize("closed", ["completed", "failed", "rejected"])
async def test_hold_leaves_a_closed_defect_closed(db, closed):
    """Only the way INTO completed is gated (review 9592c0fad3a4a46e)."""
    from hub.services.prevention_gate import hold_completion

    task_id = await _defect(db, status=closed)
    assert not await hold_completion(db, task_id, via="poller_delivery", actor="hub")
    await db.commit()
    assert (await _row(db, task_id))["status"] == closed
    assert not await _events(db, task_id, "needs_decision")


async def test_poller_sweep_leaves_a_force_completed_defect_closed(db):
    from hub import poller
    from hub.models import TaskForceComplete
    from hub.services.lifecycle import force_complete_task

    task_id = await _defect(db, status="running")
    await force_complete_task(db, task_id, TaskForceComplete(comment="stuck"))
    stop = AsyncMock(side_effect=RuntimeError("past the gate"))
    with patch("hub.services.resolve_delivery_pr", stop):
        with pytest.raises(RuntimeError, match="past the gate"):
            await poller._deliver_pair_task(db, await _row(db, task_id))
    assert (await _row(db, task_id))["status"] == "completed"
    assert not await _events(db, task_id, "needs_decision")


async def test_hold_does_not_reopen_a_close_that_raced_it(db):
    """Review e04e9496c2bd7c28: closed between the read and the write."""
    from hub.services import prevention_gate

    task_id = await _defect(db, status="review")
    real_get = repo.get_task

    async def read_then_close(conn, tid):
        row = await real_get(conn, tid)
        await conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (tid,))
        return row

    with patch.object(prevention_gate.repo, "get_task", read_then_close):
        held = await prevention_gate.hold_completion(
            db, task_id, via="poller_delivery", actor="hub"
        )
    await db.commit()
    assert not held
    assert (await _row(db, task_id))["status"] == "completed"
    assert not await _events(db, task_id, "needs_decision")


async def test_refusal_names_the_field_of_its_own_door(client, db):
    """Review d1c616745c743d85: submit is not told to use the done report."""
    done_id = await _defect(db)
    done = (await _done(client, done_id)).text
    assert "hub_report_done(prevention)" in done
    assert "hub_submit_for_review" not in done

    pair_id = await _pair_defect(db)
    submit = (
        await client.post(
            f"/api/tasks/{pair_id}/submit-review",
            json={"agent": "dev", "branch": "task-1/fix"},
        )
    ).text
    assert "hub_submit_for_review(prevention)" in submit
    assert "submit-review --prevention" in submit
    assert "hub_report_done" not in submit


async def test_held_rollup_says_so_once(db):
    """Review daf11404103bda70: a skipped rollup leaves one line, not none."""
    from hub.services.lifecycle import maybe_rollup_parent
    from hub.services.prevention_gate import ROLLUP_HELD_NOTE

    parent = await _defect(db, status="running", task_type="feature")
    first = await _defect(db, found_in="unknown", status="completed", parent_id=parent)
    second = await _defect(db, found_in="unknown", status="completed", parent_id=parent)
    await maybe_rollup_parent(db, first)
    await maybe_rollup_parent(db, second)
    await db.commit()
    notes = await db.execute_fetchall(
        "SELECT 1 FROM task_updates WHERE task_id=? AND content=?",
        (parent, ROLLUP_HELD_NOTE),
    )
    assert len(notes) == 1


async def _pair_defect(db) -> int:
    task_id = await _defect(db, status="running")
    await db.execute(
        "UPDATE tasks SET branch='task-1/fix', git_mode='remote', auto_review=1 "
        "WHERE id=?",
        (task_id,),
    )
    await db.commit()
    return task_id


async def test_pair_prod_defect_submits_with_its_prevention(client, db):
    """The pair author's path: the output rides the submission (question 3)."""
    task_id = await _pair_defect(db)
    body = {"agent": "dev", "branch": "task-1/fix"}

    refused = await client.post(f"/api/tasks/{task_id}/submit-review", json=body)
    assert refused.status_code == 422 and "prevention_required" in refused.text

    bad = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json=body | {"prevention": {"kind": "accepted_risk", "reason": "x"}},
    )
    assert bad.status_code == 422
    assert (await _row(db, task_id))["defect_prevention"] is None

    ok = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json=body | {"prevention": {"kind": "regression_test", "ref": "tests/t.py::t"}},
    )
    assert ok.status_code == 200, ok.text
    row = await _row(db, task_id)
    assert row["status"] == "review"
    assert json.loads(row["defect_prevention"])["ref"] == "tests/t.py::t"
    assert await _events(db, task_id, "defect_prevention_recorded")


async def test_submission_refused_later_leaves_no_prevention(client, db):
    """Written with the transition: a later gate's refusal records nothing."""
    task_id = await _pair_defect(db)
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "agent": "dev",
            "branch": "task-1/other",
            "prevention": {"kind": "regression_test", "ref": "tests/t.py::t"},
        },
    )
    assert resp.status_code >= 400
    assert (await _row(db, task_id))["defect_prevention"] is None


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


async def test_mcp_submit_for_review_passes_prevention():
    from hub import mcp_server

    post = AsyncMock(return_value={"id": 5, "status": "review"})
    prevention = {"kind": "regression_test", "ref": "tests/t.py::t"}
    with (
        patch.object(mcp_server, "_api_post", post),
        patch.object(mcp_server, "_read_task", AsyncMock(return_value=None)),
    ):
        await mcp_server.hub_submit_for_review(5, prevention=prevention)
    assert post.await_args.args[1]["prevention"] == prevention


def test_cli_submit_review_passes_prevention():
    from hub.cli import build_parser

    args = build_parser().parse_args(
        [
            "submit-review",
            "5",
            "--prevention",
            '{"kind": "regression_test", "ref": "tests/t.py::t"}',
        ]
    )
    with patch("hub.cli._api", return_value={"id": 5, "status": "review"}) as api:
        args.func(args)
    body = api.call_args.args[2]
    assert body["prevention"]["kind"] == "regression_test"


def test_cli_update_refuses_bad_prevention_json():
    from hub.cli import build_parser

    args = build_parser().parse_args(
        ["update", "5", "--kind", "done", "--message", "x", "--prevention", "{"]
    )
    with patch("hub.cli._api") as api:
        assert args.func(args) == 2
    api.assert_not_called()


# --- #920: the rule reaches the review brief --------------------------------
#
# A rule recorded in category_checks and named by a closed defect is worth
# something only when the next reviewer in the same area reads it. The rule
# has no area column: its area is where its class was actually met — the
# areas of the tasks whose confirmed findings carry the category, and of the
# defect that named the rule — so a rule reaches a brief whose task touches
# one of those areas, and nowhere else.

_RULE_AREA = "hub/services/lifecycle.py"


async def _with_areas(db, task_id: int, areas: list[str]) -> None:
    await db.execute(
        "UPDATE tasks SET affected_areas=? WHERE id=?", (json.dumps(areas), task_id)
    )
    await db.commit()


async def _finding_in(db, areas: list[str], category: str) -> int:
    """A task in ``areas`` whose confirmed review finding carries ``category``."""
    task_id = await _defect(db, found_in="unknown", status="completed")
    await _with_areas(db, task_id, areas)
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="multi-agent-review",
        raw_count=1,
        findings_confirmed=json.dumps(
            [{"title": "lock held", "severity": "high", "category": category}]
        ),
        incomplete=False,
    )
    await db.commit()
    return task_id


async def _brief_text(brief: dict) -> str:
    from hub import mcp_server

    with patch.object(mcp_server, "_api_get", AsyncMock(return_value=brief)):
        result = await mcp_server.hub_get_review_brief(brief["task_id"])
    return result.content[0].text


async def test_rule_reaches_review_brief(client, db):
    # AC-1: the class was met in this area, a prod defect was closed with it as
    # its rule; the next brief in the area carries the rule, its check and the
    # defect that bought it.
    await _finding_in(db, [_RULE_AREA], "timeouts")
    recorded = await client.post(
        "/api/metrics/category-checks",
        json={"category": "timeouts", "check_ref": "tests/test_poller.py::test_ttl"},
    )
    assert recorded.status_code == 200, recorded.text
    defect = await _defect(db)
    await _with_areas(db, defect, [_RULE_AREA])
    closed = await _done(client, defect, {"kind": "rule", "ref": "timeouts"})
    assert closed.status_code in (200, 201), closed.text

    task_id = await _defect(db, found_in="unknown", status="review")
    await _with_areas(db, task_id, [_RULE_AREA, "tests/test_poller.py"])
    resp = await client.get(f"/api/tasks/{task_id}/review-brief")
    assert resp.status_code == 200, resp.text
    brief = resp.json()

    rules = brief["catalogue_rules"]
    assert [r["category"] for r in rules] == ["timeouts"]
    rule = rules[0]
    assert rule["check_ref"] == "tests/test_poller.py::test_ttl"
    assert [d["task_id"] for d in rule["source_defects"]] == [defect]
    assert rule["matched_areas"] == [_RULE_AREA]
    assert rule["created_at"], "the rule says since when it stands"

    text = await _brief_text(brief)
    assert "category_checks" in text
    assert "tests/test_poller.py::test_ttl" in text
    assert f"#{defect}" in text


async def test_no_rules_no_section(client, db):
    # AC-2: a catalogue with no rule for the task's area leaves no trace in the
    # brief — no header over an empty list, which would read as "checked, the
    # area is clean".
    task_id = await _defect(db, found_in="unknown", status="review")
    await _with_areas(db, task_id, ["hub/web.py"])

    empty = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert empty["catalogue_rules"] == []
    assert "category_checks" not in await _brief_text(empty)

    # A rule elsewhere is still not a rule of this area.
    await _finding_in(db, [_RULE_AREA], "timeouts")
    await repo.upsert_category_check(
        db, category="timeouts", check_ref="tests/test_poller.py::test_ttl"
    )
    await db.commit()
    other = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert other["catalogue_rules"] == []
    assert "category_checks" not in await _brief_text(other)


async def test_rule_area_is_where_the_class_was_met(client, db):
    # The rule's area has three sources, each enough on its own: the areas of
    # a task whose finding carried the class, the file the finding named, and
    # the areas of the defect that named the rule. A rule no defect bought
    # says so rather than leaving the link blank.
    await _finding_in(db, ["hub/web.py"], "naming")
    await repo.upsert_category_check(db, category="naming", check_ref="ruff N")
    file_task = await _defect(db, found_in="unknown", status="completed")
    await repo.insert_machine_review(
        db,
        task_id=file_task,
        submission_generation=1,
        harness_skill="multi-agent-review",
        raw_count=1,
        findings_confirmed=json.dumps(
            [{"title": "t", "category": "styling", "file": "./hub/cli.py:12"}]
        ),
        incomplete=False,
    )
    await repo.upsert_category_check(db, category="styling", check_ref="ruff E")
    await repo.upsert_category_check(db, category="locks", check_ref="t::locks")
    await db.commit()
    defect = await _defect(db)
    await _with_areas(db, defect, ["hub/poller.py"])
    closed = await _done(client, defect, {"kind": "rule", "ref": "locks"})
    assert closed.status_code in (200, 201), closed.text

    briefs: list[dict] = []

    async def rules_for(areas: list[str]) -> dict[str, dict]:
        task_id = await _defect(db, found_in="unknown", status="review")
        await _with_areas(db, task_id, areas)
        brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
        briefs.append(brief)
        return {r["category"]: r for r in brief["catalogue_rules"]}

    by_task_area = await rules_for(["hub/web.py"])
    assert set(by_task_area) == {"naming"}
    assert by_task_area["naming"]["source_defects"] == []
    assert "no defect recorded" in await _brief_text(briefs[-1])
    assert set(await rules_for(["hub/cli.py"])) == {"styling"}
    assert set(await rules_for(["hub/poller.py"])) == {"locks"}
    # A directory area holds the files under it.
    assert set(await rules_for(["hub"])) == {"naming", "styling", "locks"}
