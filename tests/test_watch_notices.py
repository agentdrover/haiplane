"""The hub puts watch notices into steward@<project> in the moment of the event (#1679).

Every test here drives a real transition (poller sweep, lifecycle report,
machine-review intake, release/red-base sweeps). The notice is written in the
transaction of its source event, behind a SAVEPOINT, so the tests also pin what
must NOT happen: a failing notice breaks nothing, and a repeated source event
never doubles a message.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
from unittest.mock import AsyncMock, patch

import aiosqlite
from httpx import AsyncClient

from hub import db as hub_db
from hub import poller
from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services import red_base, watch_notices
from tests.test_api import _reviewable_task
from tests.test_messaging import _auth, _register_role
from tests.test_poller import (
    _age_task_feed,
    _make_draft,
    _make_stale_task,
    _project_task,
)
from tests.watch_notice_support import (
    ci_check_task,
    done_with_blocker,
    escalate_no_pr,
    events_of,
    keys,
    make_project,
    notices,
)

CLOSED_FIELDS = {
    "v",
    "reason",
    "project_slug",
    "task_id",
    "source_event_id",
    "source_key",
    "review_id",
}


async def test_each_needs_decision_entry_is_announced_to_its_project(
    db: aiosqlite.Connection,
):
    await make_project(db, "default")
    pa = await make_project(db, "a")
    pb = await make_project(db, "b")

    first = await ci_check_task(db, pa)
    await escalate_no_pr(db, first)  # poller.py: no PR after the attempt limit
    await escalate_no_pr(db, first)  # the same generation, a second entry
    held = await done_with_blocker(db, pa)  # lifecycle.py: done under a blocker
    other = await ci_check_task(db, pb)
    await escalate_no_pr(db, other)
    orphan = await ci_check_task(db, None)
    await escalate_no_pr(db, orphan)

    in_a = await notices(db, "a")
    assert [n["data"]["task_id"] for n in in_a] == [first, first, held]
    assert len({n["data"]["source_event_id"] for n in in_a}) == 3
    for n in in_a:
        data = n["data"]
        assert set(data) == CLOSED_FIELDS
        assert (data["v"], data["reason"], data["project_slug"]) == (
            1,
            "needs_decision",
            "a",
        )
        assert data["source_key"] == f"needs_decision:{data['source_event_id']}"
        assert data["review_id"] is None
        assert (n["kind"], n["from_agent"], n["from_session_id"]) == ("note", "hub", "")
        assert n["to_kind"] == "role"
    # The events the notices point at are the real needs_decision events.
    ids = {e["id"] for e in await events_of(db, "needs_decision")}
    assert {n["data"]["source_event_id"] for n in in_a} <= ids

    assert [n["data"]["task_id"] for n in await notices(db, "b")] == [other]
    assert [n["data"]["task_id"] for n in await notices(db, "default")] == [orphan]
    for task_id in (first, held, other, orphan):
        status = (await repo.get_task(db, task_id))["status"]
        assert status == "needs_decision", (task_id, status)


async def test_two_review_reports_give_two_notices(client: AsyncClient, db):
    await make_project(db, "default")
    task_id = await _reviewable_task(client, db)
    report = {
        "raw_count": 1,
        "incomplete": False,
        "agent_count": 1,
        "tokens_spent": 10,
        "duration_ms": 10,
        "harness_skill": "multi-agent-review",
        "harness_version": 1,
        "orchestrator": "claude-code-workflow",
        "findings_confirmed": [],
        "findings_rejected": [],
        "agent": "claude-code",
    }
    for _ in range(2):
        resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=report)
        assert resp.status_code == 200, resp.text

    cur = await db.execute(
        "SELECT id FROM machine_reviews WHERE task_id = ? ORDER BY id", (task_id,)
    )
    review_ids = [r["id"] for r in await cur.fetchall()]
    assert len(review_ids) == 2

    got = [n["data"] for n in await notices(db, "default") if n["data"]["review_id"]]
    assert [d["review_id"] for d in got] == review_ids
    assert [d["source_key"] for d in got] == [f"machine_review:{i}" for i in review_ids]
    assert {d["reason"] for d in got} == {"machine_review"}
    completed = await events_of(db, "machine_review_completed")
    assert [e["payload"]["review_id"] for e in completed] == review_ids
    assert [d["source_event_id"] for d in got] == [e["id"] for e in completed]


async def _release_sweep(db, merged: bool, reason: str) -> None:
    with patch(
        "hub.services.release.merge_ready_release",
        AsyncMock(return_value=(merged, reason)),
    ):
        await poller._sweep_release_policy(db)


RED_RUNS = [
    {"status": "completed", "conclusion": "failure", "sha": "deadbeef" * 5},
]


async def test_release_red_base_and_key_namespaces(db: aiosqlite.Connection):
    await make_project(db, "a")
    await make_project(db, "b")
    poller._release_notices.clear()
    poller._release_stalls.clear()

    blocked = "релизный PR #42 не смержен: ci_fail (mypy)"
    await _release_sweep(db, False, blocked)  # release_blocked
    await _release_sweep(db, True, "релиз PR #42 смержен в main")  # unblocked
    await _release_sweep(db, False, blocked)  # blocked again
    release = [n["data"] for n in await notices(db, "a")]
    assert [d["reason"] for d in release] == ["release"] * 3
    pid_a = (await repo.get_project_by_slug(db, "a"))["id"]
    event_ids = [
        e["id"]
        for kind in ("release_blocked", "release_unblocked")
        for e in await events_of(db, kind)
        if e["project_id"] == pid_a
    ]
    assert sorted(d["source_event_id"] for d in release) == sorted(event_ids)
    assert {d["source_key"] for d in release} == {f"release:{i}" for i in event_ids}

    plugins.git_ops.branch_ci_runs = AsyncMock(return_value=RED_RUNS)
    await red_base.check_all_projects(db)
    assert len(await events_of(db, "base_branch_red")) == 2  # one per project
    # The same red SHA again, with the repeat guard gone (feed retention):
    await db.execute("DELETE FROM events WHERE kind = 'base_branch_red'")
    await db.commit()
    await red_base.check_all_projects(db)
    assert (
        len(await events_of(db, "base_branch_red")) == 2
    )  # the event repeats, the notice does not
    pid = {s: (await repo.get_project_by_slug(db, s))["id"] for s in ("a", "b")}
    sha = RED_RUNS[0]["sha"]
    for slug in ("a", "b"):
        red = [
            n["data"]
            for n in await notices(db, slug)
            if n["data"]["reason"] == "base_branch_red"
        ]
        assert [d["source_key"] for d in red] == [f"base_branch_red:{pid[slug]}:{sha}"]
        assert red[0]["task_id"] is None

    # Equal numeric ids of different reasons do not suppress each other.
    async with hub_db.write_transaction(db):
        for reason, ns in (
            ("needs_decision", "needs_decision"),
            ("release", "release"),
        ):
            assert await watch_notices.notify_steward(
                db, reason, project_id=pid["a"], source_key=f"{ns}:9001"
            )
        assert not await watch_notices.notify_steward(
            db, "release", project_id=pid["a"], source_key="release:9001"
        )
    assert {"needs_decision:9001", "release:9001"} <= set(await keys(db))


async def test_stale_rungs_and_draft_windows_are_announced_by_their_own_rules(
    db: aiosqlite.Connection,
):
    from hub.poller import _sweep_stale_statuses, _sweep_unrefined_drafts

    silent = await _make_stale_task(db, status="ci_check", minutes=0)
    await _project_task(db, "pj", silent)
    await repo.add_task_update(db, silent, "dev", "status", "Plan: ждать")
    await _age_task_feed(db, silent, 90)

    await _sweep_stale_statuses(db)
    await _sweep_stale_statuses(db)  # the same rung: nothing new
    stale = [n["data"] for n in await notices(db, "pj")]
    assert len(stale) == 1 and stale[0]["reason"] == "stale"
    assert stale[0]["task_id"] == silent
    assert stale[0]["source_key"].startswith(f"stale:{silent}:ci_check:")
    first_key = stale[0]["source_key"]
    # task_update, task_stale event and notice are one transaction: 1 of each.
    alerts = [
        u for u in await repo.get_task_updates(db, silent) if u["kind"] == "alert"
    ]
    events = [e for e in await events_of(db, "task_stale") if e["task_id"] == silent]
    assert len(alerts) == 1 and len(events) == 1
    assert events[0]["payload"]["status"] == "ci_check"
    assert stale[0]["source_event_id"] == events[0]["id"]

    await _age_task_feed(db, silent, 25 * 60)
    await _sweep_stale_statuses(db)  # the next rung
    keys_now = [n["data"]["source_key"] for n in await notices(db, "pj")]
    assert len(keys_now) == 2 and keys_now[0] == first_key and keys_now[1] != first_key

    # A declared wait that has lapsed: its own rung, announced once.
    lapsed = await _make_stale_task(db, status="ci_check", minutes=0)
    await _project_task(db, "pl", lapsed)
    await db.execute(
        "UPDATE tasks SET updated_at=datetime('now', '-999 minutes'), "
        "waiting_for='вердикт', waiting_until=datetime('now', '-45 minutes') "
        "WHERE id=?",
        (lapsed,),
    )
    await db.commit()
    await _sweep_stale_statuses(db)
    await _sweep_stale_statuses(db)
    wait = [n["data"] for n in await notices(db, "pl")]
    assert [d["source_key"] for d in wait] == [f"stale:{lapsed}:ci_check:просрочка"]
    assert wait[0]["task_id"] == lapsed

    draft = await _make_draft(db)
    await _project_task(db, "pd", draft)
    await _sweep_unrefined_drafts(db)
    await _sweep_unrefined_drafts(db)
    drafts = [n["data"] for n in await notices(db, "pd")]
    assert len(drafts) == 1 and drafts[0]["reason"] == "stale"
    assert drafts[0]["source_key"].startswith("stale_draft:")
    # Real activity opens a new window; silence again gives a new notice.
    await repo.add_task_update(db, draft, "dev", "status", "работаю над DoR")
    await db.commit()
    await _sweep_unrefined_drafts(db)
    drafts = [n["data"]["source_key"] for n in await notices(db, "pd")]
    assert len(drafts) == 2 and len(set(drafts)) == 2


async def test_notice_failure_never_breaks_its_transition(
    db: aiosqlite.Connection, db_dsn: str
):
    pa = await make_project(db, "a")
    real_insert = repo.insert_agent_message

    async def after_message(*args, **kwargs):
        await real_insert(*args, **kwargs)  # key and message are in
        raise RuntimeError("boom after the message")

    async def before_message(*args, **kwargs):
        raise RuntimeError("boom before the message")  # only the key is in

    for fault, label in ((after_message, "after"), (before_message, "before")):
        task_id = await ci_check_task(db, pa)
        with patch.object(repo, "insert_agent_message", fault):
            await escalate_no_pr(db, task_id)
        row = await repo.get_task(db, task_id)
        assert row["status"] == "needs_decision", label  # the transition stands
        assert any(
            e["task_id"] == task_id for e in await events_of(db, "needs_decision")
        )
        assert await keys(db) == [], label  # no key
        assert await notices(db, "a") == [], label  # no message
        failed = [
            e
            for e in await events_of(db, "watch_notice_failed")
            if e["payload"].get("reason") == "needs_decision"
        ]
        assert failed[-1]["payload"]["error"] == "RuntimeError"
        assert failed[-1]["payload"]["source_key"].startswith("needs_decision:")
        assert failed[-1]["task_id"] == task_id

    # The next pass is clean: no leftovers from the failed ones.
    task_id = await ci_check_task(db, pa)
    await escalate_no_pr(db, task_id)
    assert len(await keys(db)) == 1
    assert [n["data"]["task_id"] for n in await notices(db, "a")] == [task_id]

    # Two connections racing for one key leave exactly one message.
    async def contender(source_event_id: int) -> bool:
        conn = await hub_db.connect(db_dsn)
        try:
            await conn.execute("BEGIN IMMEDIATE")
            sent = await watch_notices.notify_steward(
                conn,
                "release",
                project_id=pa,
                source_key="release:777",
                source_event_id=source_event_id,
            )
            await asyncio.sleep(0.05)
            await conn.commit()
            return sent
        finally:
            await conn.close()

    failed_before = len(await events_of(db, "watch_notice_failed"))
    results = await asyncio.gather(contender(1), contender(2))
    assert sorted(results) == [False, True]
    # The loser is turned away by the key, not by an error caught on the way.
    assert len(await events_of(db, "watch_notice_failed")) == failed_before
    again = [n for n in await notices(db, "a") if n["data"]["reason"] == "release"]
    assert len(again) == 1


async def test_notices_wait_for_a_steward_who_comes_later(
    client: AsyncClient, monkeypatch, db: aiosqlite.Connection
):
    auth = _auth(monkeypatch)
    pa = await make_project(db, "a")
    task_id = await ci_check_task(db, pa)
    await escalate_no_pr(db, task_id)  # nobody is online
    await db.commit()

    await _register_role(client, auth["alpha"], "late", "steward", ["a"])
    inbox = await client.get("/api/messages?session_id=late", headers=auth["alpha"])
    assert inbox.status_code == 200, inbox.text
    bodies = [json.loads(m["body"]) for m in inbox.json()]
    assert [b["task_id"] for b in bodies] == [task_id]
    assert [m["matched_by"] for m in inbox.json()] == ["role"]
    assert [m["to_kind"] for m in inbox.json()] == ["role"]
    assert bodies[0]["reason"] == "needs_decision"


def _string_constants() -> dict[str, set[str]]:
    """Every module-level ``NAME = "text"`` in hub, by name (imports share the name)."""
    found: dict[str, set[str]] = {}
    for path in pathlib.Path("hub").rglob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found.setdefault(target.id, set()).add(node.value.value)
    return found


def test_no_needs_decision_event_bypasses_the_notice_wrapper():
    """Every place that writes the event goes through the one wrapper.

    A literal and a NAME bound to the string (``ESCALATED_TO_HUMAN``, or
    ``mod.NAME``) are the same event: both are resolved to their value.
    """
    consts = _string_constants()

    def values(node: ast.expr) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name):
            return consts.get(node.id, set())
        if isinstance(node, ast.Attribute):
            return consts.get(node.attr, set())
        return set()

    offenders = []
    for path in pathlib.Path("hub").rglob("*.py"):
        if path.name == "watch_notices.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "insert_event"
            ):
                continue
            for kw in node.keywords:
                if kw.arg == "kind" and "needs_decision" in values(kw.value):
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


async def test_steward_escalation_after_the_review_budget_is_announced(
    db: aiosqlite.Connection,
):
    from hub.services import steward_applied

    await make_project(db, "default")
    task_id = await ci_check_task(db, None)
    await repo.update_task(db, task_id, status="review")
    await db.commit()

    await steward_applied._hand_to_the_human(db, task_id, 99, 0)

    assert (await repo.get_task(db, task_id))["status"] == "needs_decision"
    events = [
        e for e in await events_of(db, "needs_decision") if e["task_id"] == task_id
    ]
    assert len(events) == 1
    assert events[0]["payload"] == {"reason": "review_cycle_limit"}
    got = [n["data"] for n in await notices(db, "default")]
    assert [d["task_id"] for d in got] == [task_id]
    assert got[0]["source_event_id"] == events[0]["id"]


async def test_unknown_task_gives_no_notice(db: aiosqlite.Connection):
    async with hub_db.write_transaction(db):
        sent = await watch_notices.notify_steward(
            db, "needs_decision", task_id=987654, source_key="needs_decision:1"
        )
    assert sent is False
    assert await keys(db) == [] and await notices(db, "default") == []
