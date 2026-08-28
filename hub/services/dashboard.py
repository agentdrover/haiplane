"""Dashboard aggregation and task listing services."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, TypedDict

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
from hub.services.project_policy import clone_branch_state

log = logging.getLogger("hub")


async def _project_id_for(db: aiosqlite.Connection, project: str | None) -> int | None:
    """Resolve a project slug to its id, or None for "every project" (#627).

    An unknown slug resolves to a project id nothing matches rather than to
    None: silently widening back to every project is the failure this whole
    line of tasks is about.
    """
    slug = (project or "").strip()
    if not slug:
        return None
    row = await repo.get_project_by_slug(db, slug)
    return int(row["id"]) if row is not None else -1


async def get_dashboard_data(
    db: aiosqlite.Connection, *, project: str | None = None
) -> DashboardData:
    """Aggregate all data for the main dashboard.

    ``project`` narrows every limited list INSIDE its query (#627). It used to
    be applied by the caller afterwards, over lists already capped at 20, so a
    project whose rows were not among the newest 20 of a status disappeared
    from the board entirely.
    """
    project_id = await _project_id_for(db, project)
    commits_t = asyncio.create_task(plugins.github.recent_commits(8))
    prs_t = asyncio.create_task(plugins.github.open_prs())
    decisions_t = asyncio.create_task(plugins.notes.recent_decisions(limit=8))

    active_rows = await repo.list_tasks_by_statuses(
        db,
        ["open", "running", "fix_requested", "ci_check"],
        limit=20,
        project_id=project_id,
    )
    draft_rows = await repo.list_tasks_by_status(
        db, "draft", limit=20, project_id=project_id
    )
    needs_info_rows = await repo.list_tasks_by_status(
        db, "needs_info", limit=20, project_id=project_id
    )
    review_rows = await repo.list_tasks_by_status(
        db, "review", limit=20, project_id=project_id
    )
    needs_decision_rows = await repo.list_tasks_by_status(
        db, "needs_decision", limit=20, project_id=project_id
    )
    pending_report_rows = await repo.list_tasks_by_status(
        db,
        "pending_report",
        order_by="updated_at ASC",
        limit=20,
        project_id=project_id,
    )
    # list_stale_running carries NO limit, so narrowing it in Python cannot
    # drop a row. Kept as a post-filter deliberately, and said out loud rather
    # than left to look like an oversight (#627 AC-3).
    stale_rows = await repo.list_stale_running(db, config.STALE_THRESHOLD_MINUTES)
    if project_id is not None:
        allowed = await repo.list_task_ids_for_project(db, project_id)
        stale_rows = [r for r in stale_rows if r["id"] in allowed]
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


class _PersonFilters(TypedDict):
    """Фильтры «чьё это» — те же три поля, что принимают репо-запросы.

    Обычный dict здесь распаковывался в **kwargs с точными типами, и проверка
    типов на этом слепла: словарь со смешанными значениями подходит под любую
    сигнатуру. TypedDict делает распаковку проверяемой, ничего не меняя в
    рантайме (#847).
    """

    human_owner: str | None
    claimed_by: str | None
    mine: str | None


class _ScopedFilters(_PersonFilters):
    """То же плюс проект — набор, который принимают списки с LIMIT (#627)."""

    project_id: int | None


async def get_inbox_data(
    db: aiosqlite.Connection,
    *,
    human_owner: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Gather inbox items: drafts, questions, decisions, pending reports, stale.

    ``project`` narrows the limited lists inside their queries (#627), for the
    same reason as get_dashboard_data.
    """
    project_id = await _project_id_for(db, project)
    person: _PersonFilters = {
        "human_owner": human_owner,
        "claimed_by": claimed_by,
        "mine": mine,
    }
    scoped: _ScopedFilters = {**person, "project_id": project_id}
    draft_rows = await repo.list_tasks_by_status(
        db,
        "draft",
        order_by=repo.DRAFT_QUEUE_ORDER_BY,
        limit=20,
        **scoped,
    )
    needs_info_rows = await repo.list_tasks_by_status(
        db, "needs_info", limit=20, **scoped
    )
    needs_decision_rows = await repo.list_tasks_by_status(
        db, "needs_decision", limit=20, **scoped
    )
    pending_report_rows = await repo.list_tasks_by_status(
        db,
        "pending_report",
        order_by="updated_at ASC",
        limit=20,
        **scoped,
    )
    ci_check_rows = await repo.list_tasks_by_status(
        db,
        "ci_check",
        order_by="updated_at ASC",
        limit=20,
        **scoped,
    )
    fix_requested_rows = await repo.list_tasks_by_status(
        db,
        "fix_requested",
        order_by="updated_at ASC",
        limit=20,
        **scoped,
    )
    # No LIMIT on this one, so narrowing it in Python cannot drop a row. The
    # exception is deliberate and named rather than left looking accidental
    # (#627 AC-3): post-filtering is only safe where nothing was cut first.
    stale_rows = await repo.list_stale_running(
        db,
        config.STALE_THRESHOLD_MINUTES,
        **person,
    )
    if project_id is not None:
        allowed = await repo.list_task_ids_for_project(db, project_id)
        stale_rows = [r for r in stale_rows if r["id"] in allowed]

    # #957: the row must say what is actually known — the real age of the
    # silence, or the lapsed wait by name — instead of the constant
    # "30+ minutes" that was false for a task alerting for a week and
    # insulting for one that reported an hour ago. One query per stale row;
    # the section is short by construction (it is the list a human reads).
    stale_meta: dict[int, dict[str, str]] = {}
    for r in stale_rows:
        d = dict(r)
        waiting_for = str(d.get("waiting_for") or "")
        waiting_until = str(d.get("waiting_until") or "")
        if waiting_for and waiting_until:
            stale_meta[d["id"]] = {
                "line": (
                    f"Ожидание просрочено: ждали «{waiting_for}» "
                    f"до {waiting_until} (UTC)."
                ),
                "kind": "lapsed_wait",
            }
            continue
        last_at = await repo.last_activity_at(db, d["id"])
        stale_meta[d["id"]] = {
            "line": (
                f"Тишина с {last_at} (UTC) — ни одной записи от людей "
                "или агентов с тех пор."
                if last_at
                else "В ленте задачи нет ни одной записи."
            ),
            "kind": "silence",
        }

    questions: list[dict[str, Any]] = []
    for r in needs_info_rows:
        tv = row_to_task(r)
        d = tv.model_dump()
        update_rows = await repo.get_task_updates(db, tv.id)
        d["updates"] = [dict(u) for u in update_rows]
        questions.append(d)

    # #897: completed tasks whose PR is still open. Read from stored answers,
    # so this costs one indexed SELECT — the inbox renders constantly and a
    # provider call per render would make the whole board hostage to GitHub.
    undelivered = await repo.list_delivery_discrepancies(
        db, states=("pr_open",), project_id=project_id, limit=20
    )

    # #1038: the one section here that is NOT keyed on task status, and that is
    # the whole point. A machine-review finding is written while the task is in
    # review; by the time anyone would judge it the task is ``completed`` and
    # appears in no status-driven section at all. Over seven days that left 47
    # confirmed findings unanswered and precision uncomputable — not because
    # judging is expensive (the form has existed since #876) but because
    # nothing led to it.
    unjudged = await repo.count_unjudged_findings(db)

    return {
        "undelivered": undelivered,
        "unjudged_findings": unjudged,
        "drafts": [row_to_task(r) for r in draft_rows],
        "questions": questions,
        "decisions": [row_to_task(r) for r in needs_decision_rows],
        "pending_reports": [row_to_task(r) for r in pending_report_rows],
        "ci_check_tasks": [row_to_task(r) for r in ci_check_rows],
        "fix_requested_tasks": [row_to_task(r) for r in fix_requested_rows],
        "stale_tasks": [row_to_task(r) for r in stale_rows],
        "stale_meta": stale_meta,
        "filter_human_owner": human_owner or "",
        "filter_claimed_by": claimed_by or "",
        "filter_mine": mine or "",
        "filter_project": (project or "").strip(),
        "inbox_query": repo.inbox_query_string(
            human_owner=human_owner,
            claimed_by=claimed_by,
            mine=mine,
            project=project,
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


# How many epics fit on a card before it stops being a card. The remainder is
# NAMED rather than dropped: a silent cut reads as "this is everything", the rule
# #611 and #615 both landed on.
PROJECT_CARD_EPIC_LIMIT = 5


def humanize_since(stamp: str | None, *, now: str | None = None) -> str:
    """ "3 недели назад" instead of "2026-07-20" (#619).

    A raw date makes the reader do the arithmetic, and the question the card
    answers is "where is work happening" — a relative answer IS the answer.

    Computed on the server, in one place, because a Jinja filter would be
    rewritten differently by the next view that needs the same phrase. Today the
    project card is the only consumer; the epic list (#570) sorts by this same
    activity and is the obvious second.
    """
    if not stamp:
        return "не было"
    text = stamp.replace("T", " ").split("+", 1)[0].split(".", 1)[0].strip()
    try:
        then = datetime.fromisoformat(text)
    except ValueError:
        return text
    current = (
        datetime.fromisoformat(now) if now else datetime.now(UTC).replace(tzinfo=None)
    )
    days = (current - then).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} дн. назад"
    if days < 31:
        weeks = days // 7
        return f"{weeks} нед. назад"
    months = days // 30
    return "месяц назад" if months == 1 else f"{months} мес. назад"


async def get_project_cards(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """One card per project: live epics, three numbers, freshest first (#567).

    Live epics come from ``get_epic_board`` rather than a new query, because the
    liveness criterion must stay single (#569 put it in one place, #570 made the
    done list its exact complement). The numbers come from one aggregate query
    for all projects (#567's own risk note about N+1).

    Cards are ordered by when work last happened in the project, for the same
    reason #570 reordered the epic list: an order by id answers "which was
    created last", not "where is the work".
    """
    board = await get_epic_board(db)
    epics_by_project = {g["project"]: g["epics"] for g in board["groups"]}
    summary = await repo.project_work_summary(db)

    cards: list[dict[str, Any]] = []
    for project in await repo.list_projects(db, include_archived=True):
        p = dict(project)
        # The card gets the raw row, where gate_policy is a JSON string —
        # the template needs the dict (#743).
        try:
            p["gate_policy"] = json.loads(p.get("gate_policy") or "{}")
        except ValueError:
            p["gate_policy"] = {}
        counts = summary.get(
            int(p["id"]),
            {
                "awaiting_human": 0,
                "drafts": 0,
                "in_flight": 0,
                "queued": 0,
                "last_activity_at": None,
            },
        )
        # #887: the same reader the API uses, so the card and the API cannot
        # disagree about whether the clone protects the project's branch. Off
        # the loop: it shells out to git in a workspace that may be unreachable.
        sync = await asyncio.to_thread(clone_branch_state, project)
        epics = epics_by_project.get(p["slug"], [])
        cards.append(
            {
                "project": p,
                "live_epics": epics[:PROJECT_CARD_EPIC_LIMIT],
                "epics_hidden": max(0, len(epics) - PROJECT_CARD_EPIC_LIMIT),
                "activity_human": humanize_since(counts.get("last_activity_at")),
                "clone_branch": sync,
                **counts,
            }
        )

    # What waits for a human comes first, and only then the freshest activity
    # (#623). Ordering by activity alone answered "where did something move
    # last" — a question the feed already answers — while this list is read to
    # decide where to GO. A project holding a decision outranks a project where
    # an agent merged something an hour ago, because the human is the scarce one.
    #
    # The first key is deliberately a BOOLEAN, not the count: 28 waiting items
    # against 4 does not make the first project more urgent, it makes both
    # waiting. Sorting by the number would put the biggest backlog permanently on
    # top, which is the same defect as ordering by id — a stable answer to a
    # question nobody asked.
    #
    # Activity still breaks ties inside each group, and a project that has never
    # seen a feed entry still goes last within its group — the honest answer #570
    # settled on instead of borrowing updated_at.
    cards.sort(
        key=lambda c: (
            bool(c["awaiting_human"]),
            c["last_activity_at"] or "",
            c["project"]["id"],
        ),
        reverse=True,
    )
    return cards


async def list_tasks(
    db: aiosqlite.Connection,
    *,
    status: str | None = None,
    task_type: str | None = None,
    priority: str | None = None,
    source: str | None = None,
    parent_id: int | None = None,
    no_epic: bool = False,
    state: str | None = None,
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
        state=state,
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


# ---------------------------------------------------------------------------
# Coordination panels (#775): sessions and threads, for the owner's view
# ---------------------------------------------------------------------------


async def get_agent_sessions_panel(db: aiosqlite.Connection) -> list[dict]:
    """Registered sessions with presence, freshest first.

    Presence comes from the same place the API computes it (#771), so the badge
    on the dashboard and the answer to a caller can never disagree — and the
    age of the last sign of life travels with it, because "online" alone is a
    claim nobody can check.
    """
    from hub.services.sessions import session_view

    rows = await repo.list_agent_sessions(db, limit=50)
    return [session_view(row) for row in rows]


async def get_message_threads_panel(
    db: aiosqlite.Connection, *, limit: int = 10
) -> list[dict]:
    """Latest message of each thread with its size (#775).

    Every thread is here, including ones addressed straight from one session to
    another with no task attached: the channel is allowed to exist precisely
    because the owner sees all of it.
    """
    from hub.services.messaging import message_view

    threads: list[dict] = []
    for row in await repo.list_recent_threads(db, limit=limit):
        data = dict(row)
        view = message_view(data)
        view["messages"] = data.get("messages") or 1
        threads.append(view)
    return threads
