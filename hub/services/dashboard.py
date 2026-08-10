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
    TaskProjectRef,
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
    # #569: one liveness criterion for every consumer of "which epics are
    # alive" — this call and get_epics_enriched below. A project card that
    # grows an epic list later must call the same function, or the three
    # places the task feared will drift apart.
    epic_rows = await repo.list_live_epics(db)
    activity_rows = await repo.list_activity(db, limit=15)

    commits = await commits_t
    prs = await prs_t
    decisions = await decisions_t

    recent_activity = _parse_activity_rows(activity_rows)

    if config.VAST_ENABLED:
        vast_info = await plugins.vast.vast_status()
        if vast_info.get("managed"):
            vast_label = vast_info.get("instance", {}).get("label", "running")
        else:
            vast_label = "no instance"
    else:
        vast_label = None

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


async def get_inbox_data(
    db: aiosqlite.Connection,
    *,
    human_owner: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = None,
) -> dict[str, Any]:
    """Gather inbox items: drafts, questions, decisions, pending reports, stale."""
    person = {
        "human_owner": human_owner,
        "claimed_by": claimed_by,
        "mine": mine,
    }
    draft_rows = await repo.list_tasks_by_status(
        db,
        "draft",
        order_by=repo.DRAFT_QUEUE_ORDER_BY,
        limit=20,
        **person,
    )
    needs_info_rows = await repo.list_tasks_by_status(
        db, "needs_info", limit=20, **person
    )
    needs_decision_rows = await repo.list_tasks_by_status(
        db, "needs_decision", limit=20, **person
    )
    pending_report_rows = await repo.list_tasks_by_status(
        db,
        "pending_report",
        order_by="updated_at ASC",
        limit=20,
        **person,
    )
    ci_check_rows = await repo.list_tasks_by_status(
        db,
        "ci_check",
        order_by="updated_at ASC",
        limit=20,
        **person,
    )
    fix_requested_rows = await repo.list_tasks_by_status(
        db,
        "fix_requested",
        order_by="updated_at ASC",
        limit=20,
        **person,
    )
    stale_rows = await repo.list_stale_running(
        db,
        config.STALE_THRESHOLD_MINUTES,
        **person,
    )

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
        "ci_check_tasks": [row_to_task(r) for r in ci_check_rows],
        "fix_requested_tasks": [row_to_task(r) for r in fix_requested_rows],
        "stale_tasks": [row_to_task(r) for r in stale_rows],
        "filter_human_owner": human_owner or "",
        "filter_claimed_by": claimed_by or "",
        "filter_mine": mine or "",
        "inbox_query": repo.inbox_query_string(
            human_owner=human_owner,
            claimed_by=claimed_by,
            mine=mine,
        ),
    }


async def _enrich_epics(
    db: aiosqlite.Connection,
    rows: list[Any],
    project_slugs: dict[int, str] | None = None,
) -> list[TaskView]:
    """Attach children, progress and the project ref to epic rows, in order.

    ``row_to_task`` does NOT fill ``project`` — only three call sites do, by
    hand (app.py and lifecycle.py). Grouping by ``tv.project`` without setting
    it here would have put every epic into "no project" while looking like it
    worked: the mechanism-right-path-not-wired trap, caught by checking the
    value instead of assuming it (#570).
    """
    epics: list[TaskView] = []
    for r in rows:
        tv = row_to_task(r)
        project_id = dict(r).get("project_id")
        if project_id is not None and project_slugs:
            slug = project_slugs.get(int(project_id))
            if slug:
                tv.project = TaskProjectRef(id=int(project_id), slug=slug)
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


async def get_epics_enriched(db: aiosqlite.Connection) -> list[TaskView]:
    """Get active epics with children and progress."""
    return await _enrich_epics(db, await repo.list_live_epics(db))


# A project with no slug of its own: an epic may carry project_id = NULL (the
# column has no NOT NULL constraint), and such an epic must land in a NAMED
# group rather than drop out of a grouped view (#570 AC-1).
UNASSIGNED_PROJECT = "без проекта"


async def get_epic_board(
    db: aiosqlite.Connection, allowed: set[int] | None = None
) -> dict[str, Any]:
    """Live epics grouped by project, plus the finished ones as a count (#570).

    ``allowed`` is the project scope the dashboard already applies to its other
    lists. It is a parameter rather than an afterthought because the first
    version of this function ignored it, and an epic from another project leaked
    onto a project-filtered page — caught by an existing test, not by me.

    Grouped HERE and not in the template: two views render this list — the
    dashboard and the projects page — and a grouping written in Jinja would be
    rewritten differently by the second one. That is the "same rule, two
    copies" defect this hub keeps paying for (#609, #614, #616, #571).

    Order inside a group is the order the query returned: last activity in the
    subtree, newest first. Groups themselves are ordered by their freshest
    epic, so the product where something is happening is on top.
    """
    slugs = {int(p["id"]): p["slug"] for p in await repo.list_projects(db)}
    live_rows = await repo.list_live_epics(db)
    done_rows = await repo.list_done_epics(db)
    if allowed is not None:
        live_rows = [r for r in live_rows if r["id"] in allowed]
        done_rows = [r for r in done_rows if r["id"] in allowed]
    live = await _enrich_epics(db, live_rows, slugs)

    groups: dict[str, list[TaskView]] = {}
    for tv in live:
        ref = tv.project
        groups.setdefault(ref.slug if ref else UNASSIGNED_PROJECT, []).append(tv)

    return {
        # dict preserves insertion order, and the epics arrive freshest-first,
        # so a group's place is decided by its freshest epic — the product where
        # something is happening ends up on top without a second sort.
        "groups": [{"project": slug, "epics": items} for slug, items in groups.items()],
        "live_total": len(live),
        # The finished ones are counted, not hidden: a collapsed block that lies
        # about its size is worse than no block. #571 shipped exactly that
        # mismatch this morning — 18 promised, 51 behind the link — so the count
        # and the contents come from one query here.
        "done": await _enrich_epics(db, done_rows, slugs),
        "done_total": len(done_rows),
    }


async def list_tasks(
    db: aiosqlite.Connection,
    *,
    status: str | None = None,
    task_type: str | None = None,
    priority: str | None = None,
    source: str | None = None,
    parent_id: int | None = None,
    no_epic: bool = False,
    human_owner: str | None = None,
    human_reviewer: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = None,
    limit: int = 50,
    include_archived: bool = False,
    after_id: int | None = None,
    mode: str = "full",
    project: str | None = None,
) -> list[TaskView] | dict[str, Any]:
    """List tasks with optional filters.

    Backward compatible: without ``after_id``/``mode=summary`` returns the
    plain TaskView list. A paged or summary call returns an envelope
    ``{"tasks": [...], "next_cursor": id|None}`` (#254); pass the returned
    cursor as ``after_id`` to walk the full set without gaps or duplicates.
    """
    project_id: int | None = None
    if project:
        project_row = await repo.get_project_by_slug(db, project)
        if project_row is None:
            return (
                {"tasks": [], "next_cursor": None}
                if (after_id is not None or mode == "summary")
                else []
            )
        project_id = project_row["id"]

    paged = after_id is not None or mode == "summary"
    fetch_limit = limit + 1 if paged else limit
    rows = await repo.list_tasks_filtered(
        db,
        status=status,
        task_type=task_type,
        priority=priority,
        source=source,
        parent_id=parent_id,
        no_epic=no_epic,
        human_owner=human_owner,
        human_reviewer=human_reviewer,
        claimed_by=claimed_by,
        mine=mine,
        limit=fetch_limit,
        include_archived=include_archived,
        after_id=after_id if after_id is not None else (0 if paged else None),
        project_id=project_id,
    )
    views = [row_to_task(r) for r in rows]
    if not paged:
        return views

    has_more = len(views) > limit
    views = views[:limit]
    next_cursor = views[-1].id if has_more and views else None
    if mode == "summary":
        tasks: list[dict[str, Any]] = [
            {
                "id": v.id,
                "title": v.title,
                "status": v.status.value,
                "task_type": v.task_type.value,
                "parent_id": v.parent_id,
                "priority": v.priority.value,
                "readiness_score": v.readiness_score,
                "dor_passed": v.dor_passed,
            }
            for v in views
        ]
    else:
        tasks = [v.model_dump(mode="json") for v in views]
    return {"tasks": tasks, "next_cursor": next_cursor}


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
