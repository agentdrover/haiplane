"""Shared fixtures for the steward watch notices (#1679).

The notices are an effect of REAL transitions, so the helpers here drive the
same code the poller and the lifecycle run — they never call the notifier
directly except where a test is about the notifier itself.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hub import poller
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopDispatch, NoopGitOps
from hub.integrations.registry import plugins
from hub.models import TaskCreate, TaskReviewVerdict, TaskUpdateCreate


async def make_project(
    db: aiosqlite.Connection, slug: str, policy: dict[str, Any] | None = None
) -> int:
    row = await repo.get_project_by_slug(db, slug)
    if row is None:
        pid = await repo.create_project(db, slug=slug, name=slug)
    else:
        pid = int(row["id"])
    if policy is not None:
        await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    await db.commit()
    return pid


async def set_watch(
    db: aiosqlite.Connection, slug: str, watch: list[str] | None
) -> None:
    """Write (or drop, for None) the steward_watch key of a project."""
    row = await repo.get_project_by_slug(db, slug)
    policy = json.loads(row["gate_policy"] or "{}")
    if watch is None:
        policy.pop("steward_watch", None)
    else:
        policy["steward_watch"] = watch
    await repo.update_project(db, int(row["id"]), gate_policy=json.dumps(policy))
    await db.commit()


async def epic_of(db: aiosqlite.Connection, project_id: int) -> int:
    epic = await repo.create_task(
        db,
        title="epic",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="epic",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, epic, project_id=project_id)
    await db.commit()
    return epic


async def ci_check_task(
    db: aiosqlite.Connection, project_id: int | None = None, attempts: int = 2
) -> int:
    """A ci_check task one poll away from the no-PR escalation (poller.py)."""
    parent = await epic_of(db, project_id) if project_id is not None else None
    task_id = await repo.create_task(
        db,
        title="Needs a PR",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="ci_check",
        auto_review=True,
        task_type="task",
        parent_id=parent,
        priority="medium",
    )
    await repo.update_task(db, task_id, ci_no_pr_attempts=attempts)
    await db.commit()
    return task_id


async def escalate_no_pr(db: aiosqlite.Connection, task_id: int) -> None:
    """Drive the REAL poller sweep that sends a PR-less ci_check to a human."""
    plugins.git_ops = NoopGitOps()
    plugins.dispatch = NoopDispatch()
    await repo.update_task(db, task_id, status="ci_check", ci_no_pr_attempts=2)
    await db.commit()
    await poller._sweep_ci_check(db)


async def done_with_blocker(
    db: aiosqlite.Connection, project_id: int | None = None
) -> int:
    """A pair task whose done report is held by a fresh blocker (lifecycle)."""
    tv = await services.create_task(db, TaskCreate(title="Held by a blocker"))
    if project_id is not None:
        await repo.update_task(db, tv.id, parent_id=await epic_of(db, project_id))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await services.submit_for_review(db, tv.id)
    await services.record_review_verdict(
        db, tv.id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )
    await services.add_update(
        db, tv.id, TaskUpdateCreate(agent="dev", kind="blocker", content="миграция")
    )
    await services.add_update(
        db, tv.id, TaskUpdateCreate(agent="dev", kind="done", content="Готово")
    )
    return tv.id


async def notices(db: aiosqlite.Connection, slug: str) -> list[dict[str, Any]]:
    """Bodies of the hub's notices in steward@<slug>, oldest first."""
    cur = await db.execute(
        "SELECT id, kind, from_agent, from_session_id, to_kind, body "
        "FROM agent_messages WHERE to_ref = ? AND from_agent = 'hub' ORDER BY id",
        (f"steward@{slug}",),
    )
    out = []
    for row in await cur.fetchall():
        item = dict(row)
        item["data"] = json.loads(item["body"])
        out.append(item)
    return out


async def keys(db: aiosqlite.Connection) -> list[str]:
    cur = await db.execute("SELECT key FROM watch_notices ORDER BY key")
    return [r["key"] for r in await cur.fetchall()]


async def events_of(db: aiosqlite.Connection, kind: str) -> list[dict[str, Any]]:
    cur = await db.execute(
        "SELECT id, task_id, project_id, actor, payload FROM events "
        "WHERE kind = ? ORDER BY id",
        (kind,),
    )
    out = []
    for row in await cur.fetchall():
        item = dict(row)
        item["payload"] = json.loads(item["payload"] or "{}")
        out.append(item)
    return out
