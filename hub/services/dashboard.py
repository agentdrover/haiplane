"""Dashboard aggregation and task listing services."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiosqlite

from hub import config
from hub import db as db_module
from hub import repository as repo
from hub.integrations.registry import plugins
from hub.models import (
    ActivityItem,
    DashboardData,
    TaskChildSummary,
    TaskProgress,
    TaskView,
)
from hub.services.lifecycle import row_to_task

log = logging.getLogger("hub")


async def get_dashboard_data(db: aiosqlite.Connection) -> DashboardData:
    """Aggregate all data for the main dashboard."""
    commits_t = asyncio.create_task(plugins.github.recent_commits(8))
    prs_t = asyncio.create_task(plugins.github.open_prs())
    decisions_t = asyncio.create_task(plugins.notes.recent_decisions(limit=8))

    active_rows = await repo.list_tasks_by_statuses(
        db,
        ["open", "running", "fix_requested", "ci_check"],
        limit=20,
    )
    draft_rows = await repo.list_tasks_by_status(db, "draft", limit=20)
    needs_info_rows = await repo.list_tasks_by_status(db, "needs_info", limit=20)
    review_rows = await repo.list_tasks_by_status(db, "review", limit=20)
    needs_decision_rows = await repo.list_tasks_by_status(
        db, "needs_decision", limit=20
    )
    pending_report_rows = await repo.list_tasks_by_status(
        db,
        "pending_report",
        order_by="updated_at ASC",
        limit=20,
    )
    stale_rows = await repo.list_stale_running(db, config.STALE_THRESHOLD_MINUTES)
    epic_rows = await repo.list_active_epics(db, limit=20)
    activity_rows = await repo.list_activity(db, limit=15)

    commits = await commits_t
    prs = await prs_t
    decisions = await decisions_t

    recent_activity = _parse_activity_rows(activity_rows)

    vast_info = await plugins.vast.vast_status()
    if vast_info.get("managed"):
        vast_label = vast_info.get("instance", {}).get("label", "running")
    else:
        vast_label = "no instance"

    return DashboardData(
        recent_commits=commits,
        open_prs=prs,
        active_tasks=[row_to_task(r) for r in active_rows],
        draft_tasks=[row_to_task(r) for r in draft_rows],
        needs_info_tasks=[row_to_task(r) for r in needs_info_rows],
        review_tasks=[row_to_task(r) for r in review_rows],
        needs_decision_tasks=[row_to_task(r) for r in needs_decision_rows],
        pending_report_tasks=[row_to_task(r) for r in pending_report_rows],
        stale_tasks=[row_to_task(r) for r in stale_rows],
        epics=[row_to_task(r) for r in epic_rows],
        recent_decisions=decisions,
        recent_activity=recent_activity,
        vast_status=vast_label,
    )


async def get_inbox_data(db: aiosqlite.Connection) -> dict[str, Any]:
    """Gather inbox items: drafts, questions, decisions, pending reports, stale."""
    draft_rows = await repo.list_tasks_by_status(db, "draft", limit=20)
    needs_info_rows = await repo.list_tasks_by_status(db, "needs_info", limit=20)
    needs_decision_rows = await repo.list_tasks_by_status(
        db, "needs_decision", limit=20
    )
    pending_report_rows = await repo.list_tasks_by_status(
        db,
        "pending_report",
        order_by="updated_at ASC",
        limit=20,
    )
    stale_rows = await repo.list_stale_running(db, config.STALE_THRESHOLD_MINUTES)

    questions: list[dict[str, Any]] = []
    for r in needs_info_rows:
        tv = row_to_task(r)
        d = tv.model_dump()
        update_rows = await repo.get_task_updates(db, tv.id)
        d["updates"] = [dict(u) for u in update_rows]
        questions.append(d)

    return {
        "drafts": [row_to_task(r) for r in draft_rows],
        "questions": questions,
        "decisions": [row_to_task(r) for r in needs_decision_rows],
        "pending_reports": [row_to_task(r) for r in pending_report_rows],
        "stale_tasks": [row_to_task(r) for r in stale_rows],
    }


async def get_epics_enriched(db: aiosqlite.Connection) -> list[TaskView]:
    """Get active epics with children and progress."""
    rows = await repo.list_active_epics(db, limit=20)
    epics: list[TaskView] = []
    for r in rows:
        tv = row_to_task(r)
        children = await db_module.get_children(db, tv.id)
        if children:
            tv.children = [
                TaskChildSummary(
                    id=c["id"],
                    title=c["title"],
                    task_type=c["task_type"],
                    status=c["status"],
                    priority=c.get("priority", "medium"),
                )
                for c in children
            ]
            progress_data = await db_module.get_progress(db, tv.id)
            tv.progress = TaskProgress(**progress_data)
        epics.append(tv)
    return epics


async def list_tasks(
    db: aiosqlite.Connection,
    *,
    status: str | None = None,
    task_type: str | None = None,
    priority: str | None = None,
    source: str | None = None,
    parent_id: int | None = None,
    limit: int = 50,
) -> list[TaskView]:
    """List tasks with optional filters, returning TaskView models."""
    rows = await repo.list_tasks_filtered(
        db,
        status=status,
        task_type=task_type,
        priority=priority,
        source=source,
        parent_id=parent_id,
        limit=limit,
    )
    return [row_to_task(r) for r in rows]


def _parse_activity_rows(
    activity_rows: list[aiosqlite.Row],
) -> list[ActivityItem]:
    """Parse raw activity rows into ActivityItem models."""
    result: list[ActivityItem] = []
    for r in activity_rows:
        d = dict(r)
        detail = None
        if d.get("detail"):
            try:
                detail = json.loads(d["detail"])
            except json.JSONDecodeError:
                detail = {"raw": d["detail"]}
        result.append(
            ActivityItem(
                kind=d["kind"],
                summary=d["summary"],
                detail=detail,
                timestamp=d["timestamp"],
            ),
        )
    return result


async def list_activity(
    db: aiosqlite.Connection,
    *,
    limit: int = 30,
) -> list[ActivityItem]:
    """List recent activity items."""
    rows = await repo.list_activity(db, limit=limit)
    return _parse_activity_rows(rows)
