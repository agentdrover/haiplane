"""OpenClaw Hub MCP server — exposes hub tools for Cursor and remote agents."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from hub import config
from hub.mcp_structured import (
    HubCreateTaskResult,
    HubCreateTaskStructured,
    HubRefineTaskResult,
    HubRefineTaskStructured,
    HubRefineTasksResult,
    HubRefineTasksStructured,
    HubTaskStatusResult,
    HubTaskStatusStructured,
    structured_tool_result,
)

# FastMCP defaults to localhost-only Host/Origin allowlists when host=127.0.0.1.
# The hub mounts streamable HTTP under the main FastAPI app, so clients send the
# public Host (e.g. agenthai.ru) — the SDK default rejects them with 421. Disable
# MCP-layer rebinding checks here; AuthMiddleware + TLS cover remote access.
mcp = FastMCP(
    "openclaw-hub",
    instructions="MCP server for OpenClaw Hub — project state, tasks, proposals, decisions",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _hub_url() -> str:
    import os

    return os.environ.get("OPENCLAW_HUB_URL", "http://127.0.0.1:8080")


def _hub_token() -> str:
    import os

    env_tok = (os.environ.get("OPENCLAW_HUB_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    # Streamable MCP mounted in the same process: reuse caller's Bearer (set by
    # AuthMiddleware via hub.mcp_internal_auth) so tools work without OPENCLAW_HUB_TOKEN.
    from hub.mcp_internal_auth import bearer_context_get

    return (bearer_context_get() or "").strip()


def _auth_headers() -> dict[str, str]:
    token = _hub_token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


async def _api_get(path: str) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{_hub_url()}{path}", headers=_auth_headers())
        resp.raise_for_status()
        return resp.json()


async def _api_post(path: str, body: dict[str, Any] | None = None) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_hub_url()}{path}", json=body or {}, headers=_auth_headers()
        )
        resp.raise_for_status()
        return resp.json()


async def _api_patch(path: str, body: dict[str, Any] | None = None) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{_hub_url()}{path}", json=body or {}, headers=_auth_headers()
        )
        resp.raise_for_status()
        return resp.json()


async def _api_put(path: str, body: Any) -> Any:
    """PUT for collection-level replace (e.g. acceptance criteria)."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{_hub_url()}{path}", json=body, headers=_auth_headers()
        )
        resp.raise_for_status()
        return resp.json()


async def _api_delete(path: str) -> None:
    """DELETE returning 204 / no body."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(f"{_hub_url()}{path}", headers=_auth_headers())
        resp.raise_for_status()


def _format_task(t: dict[str, Any]) -> str:
    src = (
        f" [agent:{t.get('assigned_agent', '')}]" if t.get("source") == "agent" else ""
    )
    tt = t.get("task_type", "task")
    tt_tag = f"[{tt}] " if tt != "task" else ""
    parent = f" (parent #{t['parent_id']})" if t.get("parent_id") else ""
    owner = f" [owner:{t['human_owner']}]" if t.get("human_owner") else ""
    reviewer = f" [reviewer:{t['human_reviewer']}]" if t.get("human_reviewer") else ""
    arch = " [archived]" if t.get("archived") else ""
    return (
        f"#{t['id']} {tt_tag}[{t['status']}] ({t.get('runtime', 'auto')})"
        f"{arch}{src}{owner}{reviewer}{parent} {t['title']}"
    )


# ---------------------------------------------------------------------------
# Dashboard / Overview
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_project_status() -> str:
    """Get project overview: active tasks, drafts needing approval, tasks with questions, open PRs, recent commits, decisions."""
    data = await _api_get("/api/dashboard")
    parts: list[str] = []

    drafts = data.get("draft_tasks", [])
    if drafts:
        parts.append("## Drafts (need approval)")
        for t in drafts:
            parts.append(f"- {_format_task(t)}")

    needs_info = data.get("needs_info_tasks", [])
    if needs_info:
        parts.append("\n## Needs Info (agent asked a question)")
        for t in needs_info:
            parts.append(f"- {_format_task(t)}")

    review = data.get("review_tasks", [])
    if review:
        parts.append("\n## Under Review")
        for t in review:
            cycle = t.get("review_cycle", 0)
            parts.append(f"- {_format_task(t)} (review cycle {cycle + 1})")

    needs_decision = data.get("needs_decision_tasks", [])
    if needs_decision:
        parts.append("\n## Needs Decision (arbiter report ready)")
        for t in needs_decision:
            parts.append(
                f"- {_format_task(t)} — ARBITER REPORT READY, human must decide"
            )

    tasks = data.get("active_tasks", [])
    if tasks:
        parts.append("\n## Active Tasks (open / running)")
        for t in tasks:
            parts.append(f"- {_format_task(t)}")

    prs = data.get("open_prs", [])
    if prs:
        parts.append("\n## Open PRs")
        for pr in prs:
            parts.append(
                f"- #{pr['number']} {pr['title']} ({pr.get('headRefName', '')})"
            )

    commits = data.get("recent_commits", [])
    if commits:
        parts.append("\n## Recent Commits")
        for c in commits[:5]:
            msg = c.get("message", "").split("\n")[0][:80]
            parts.append(f"- {c.get('sha', '')} {msg}")

    decisions = data.get("recent_decisions", [])
    if decisions:
        parts.append("\n## Recent Decisions")
        for d in decisions[:5]:
            title = d.get("title", "Decision")
            parts.append(f"- {title}")

    return "\n".join(parts) if parts else "No activity found."


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_create_task(
    title: str,
    description: str = "",
    task_type: str = "task",
    parent_id: int | None = None,
    priority: str = "medium",
    runtime: str = "auto",
    run_immediately: bool = False,
    human_owner: str = "",
    human_reviewer: str = "",
) -> HubCreateTaskResult:
    """Create a new task, epic, feature, or subtask.

    Args:
        title: Short title (required)
        description: Detailed description of what needs to be done
        task_type: 'epic', 'feature', 'task', or 'subtask'
        parent_id: Parent task ID (required for feature/subtask, optional for task)
        priority: 'critical', 'high', 'medium', or 'low'
        runtime: 'auto' or 'openrouter'
        run_immediately: If True, dispatch immediately (not applicable for epic/feature)
        human_owner: Person who owns / is accountable for this task
        human_reviewer: Person who will review and accept the result
    """
    body: dict[str, Any] = {
        "title": title,
        "description": description,
        "task_type": task_type,
        "priority": priority,
        "runtime": runtime,
        "source": "human",
        "run_immediately": run_immediately,
        "human_owner": human_owner,
        "human_reviewer": human_reviewer,
    }
    if parent_id is not None:
        body["parent_id"] = parent_id
    result = await _api_post("/api/tasks", body)
    status = result.get("status", "?")
    summary = f"{task_type.capitalize()} #{result['id']} created (status: {status})."
    return structured_tool_result(summary, HubCreateTaskStructured(task=result))


@mcp.tool()
async def hub_create_subtasks(
    parent_id: int,
    items: list[dict[str, Any]],
    task_type: str = "subtask",
    source: str = "agent",
    agent: str = "",
) -> str:
    """Create multiple child tasks under one parent in a single atomic call.

    Args:
        parent_id: Parent task ID (must match hierarchy rules for task_type).
        items: List of dicts with title, optional description, priority, and
            optional acceptance_criteria (list of Given/When/Then dicts) and
            risks (list of risk dicts) so a child is born closer to DoR.
        task_type: task or subtask (default subtask).
        source: agent (draft) or human (open).
        agent: Assigned agent name when source is agent.
    """
    if not items:
        return "Nothing to create: items list is empty."
    body: dict[str, Any] = {
        "items": items,
        "task_type": task_type,
        "source": source,
        "agent": agent,
    }
    created = await _api_post(f"/api/tasks/{parent_id}/subtasks", body)
    if not created:
        return f"No subtasks created under #{parent_id}."
    lines = [
        f"Created {len(created)} {task_type}(s) under #{parent_id}:",
        *[f"  #{t['id']} [{t['status']}] {t['title']}" for t in created],
    ]
    return "\n".join(lines)


@mcp.tool()
async def hub_list_tasks(
    status: str = "",
    task_type: str = "",
    parent_id: int | None = None,
    human_owner: str = "",
    human_reviewer: str = "",
    claimed_by: str = "",
    mine: str = "",
    limit: int = 20,
    include_archived: bool = False,
) -> str:
    """List tasks with optional filters.

    Args:
        status: Filter by status: draft, open, running, needs_info, review, fix_requested, needs_decision, completed, failed, rejected. Empty for all.
        task_type: Filter by type: epic, feature, task, subtask. Empty for all.
        parent_id: Filter by parent task ID. None for all.
        human_owner: Filter by human_owner (exact match). Empty for all.
        human_reviewer: Filter by human_reviewer (exact match). Empty for all.
        claimed_by: Filter by claim holder (exact match). Empty for all.
        mine: Shorthand for human_owner OR claimed_by (same person). Empty for all.
        limit: Max number of tasks to return
        include_archived: When True, include archived tasks (hidden from boards by default).
    """
    from urllib.parse import urlencode

    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    if task_type:
        params["type"] = task_type
    if parent_id is not None:
        params["parent_id"] = parent_id
    if human_owner:
        params["human_owner"] = human_owner
    if human_reviewer:
        params["human_reviewer"] = human_reviewer
    if claimed_by:
        params["claimed_by"] = claimed_by
    if mine:
        params["mine"] = mine
    if include_archived:
        params["include_archived"] = "true"
    tasks = await _api_get(f"/api/tasks?{urlencode(params)}")
    if not tasks:
        return "No tasks found."
    lines = [_format_task(t) for t in tasks]
    return "\n".join(lines)


@mcp.tool()
async def hub_task_status(task_id: int) -> HubTaskStatusResult:
    """Get detailed status of a specific task including updates and log tail.

    Args:
        task_id: The task ID number
    """
    await _api_post(f"/api/tasks/{task_id}/refresh")
    task = await _api_get(f"/api/tasks/{task_id}")
    parts = [
        f"Task #{task['id']}: {task['title']}",
        f"Status: {task['status']}",
        f"Source: {task.get('source', 'human')}",
        f"Runtime: {task.get('runtime', 'auto')}",
        f"Agent: {task.get('assigned_agent', '-')}",
        f"Job ID: {task.get('job_id', '-')}",
        f"Exit code: {task.get('exit_code', '-')}",
        f"Review: {'enabled' if task.get('auto_review', True) else 'disabled'}, cycle {task.get('review_cycle', 0)}",
        f"Created: {task['created_at']}",
    ]
    if task.get("updates"):
        parts.append("\nUpdates:")
        for u in task["updates"]:
            parts.append(
                f"  [{u['created_at']}] ({u['kind']}) {u.get('agent', '')}: {u['content']}"
            )
    if task.get("result_text"):
        parts.append(f"\nResult:\n{task['result_text']}")
    if task.get("log_tail"):
        parts.append("\nLog tail:\n" + "\n".join(task["log_tail"][-20:]))
    summary = "\n".join(parts)
    return structured_tool_result(summary, HubTaskStatusStructured(task=task))


@mcp.tool()
async def hub_task_update(
    task_id: int, content: str, agent: str = "", kind: str = "status"
) -> str:
    """Add a status update or report to a task.

    Args:
        task_id: The task ID to update
        content: Update text — status report, blocker description, or completion report
        agent: Name of the agent posting the update
        kind: Type of update: 'status', 'report', 'blocker', 'done', 'review', or 'arbitration'
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/updates",
        {
            "agent": agent,
            "kind": kind,
            "content": content,
        },
    )
    return f"Update #{result['id']} added to task #{task_id}."


def _format_hub_report_done_message(task_id: int, report_id: int, status: str) -> str:
    """Return MCP text that reflects the actual post-report task status."""
    base = (
        f"Done report #{report_id} submitted for task #{task_id}. "
        f"Task status: {status}."
    )
    if status == "completed":
        return f"{base} Task completed."
    if status == "pending_report":
        return f"{base} Awaiting human review before completion."
    if status in ("ci_check", "review", "needs_decision"):
        return f"{base} Task entered {status}."
    if status in ("open", "running"):
        return (
            f"{base} Status unchanged for this report "
            "(pair/open path; use pair-start or done conveyor as applicable)."
        )
    return base


@mcp.tool()
async def hub_report_done(task_id: int, summary: str, agent: str = "") -> str:
    """Submit a done report and return the task's actual status after lifecycle handling.

    From ``pending_report``, a valid done report typically moves the task to
    ``completed``. In pair mode (``open``/``running`` without ``job_id``), the
    same tool may leave the task unchanged or advance it to ``ci_check`` — the
    response always states the real status and never implies ``completed`` unless
    the task is actually completed.

    Args:
        task_id: The task ID to report on
        summary: What was changed and how it was validated
        agent: Name of the agent submitting the report
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/updates",
        {
            "agent": agent,
            "kind": "done",
            "content": summary,
        },
    )
    task = await _api_get(f"/api/tasks/{task_id}")
    status = task.get("status", "?")
    return _format_hub_report_done_message(task_id, result["id"], status)


# ---------------------------------------------------------------------------
# Hierarchy: tree, context
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_task_tree(task_id: int) -> str:
    """Get the hierarchy tree for a task/epic/feature with all descendants and progress.

    Args:
        task_id: The root task ID to build tree from
    """
    tree = await _api_get(f"/api/tasks/{task_id}/tree")

    def _fmt(node: dict[str, Any], indent: int = 0) -> list[str]:
        prefix = "  " * indent
        tt = node.get("task_type", "task")
        progress = node.get("progress")
        prog_str = ""
        if progress and progress.get("total", 0) > 0:
            prog_str = f" ({progress['completed']}/{progress['total']} = {progress['percent']}%)"
        lines = [
            f"{prefix}[{tt}] #{node['id']} {node['title']} — {node['status']}{prog_str}"
        ]
        for child in node.get("children", []):
            lines.extend(_fmt(child, indent + 1))
        return lines

    return "\n".join(_fmt(tree))


@mcp.tool()
async def hub_my_context(task_id: int) -> str:
    """Get full work context for an agent: hierarchy breadcrumb, siblings, progress, children.

    Use this before starting work on a task to understand its place in the project.

    Args:
        task_id: The task ID to get context for
    """
    ctx = await _api_get(f"/api/tasks/{task_id}/context")
    return ctx.get("context_text", f"Context for task #{task_id} not available.")


# ---------------------------------------------------------------------------
# Lifecycle: approve, reject, start
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_approve_task(
    task_id: int,
    comment: str = "",
    run: bool = False,
    runtime: str = "",
    force: bool = False,
) -> str:
    """Approve a draft task after DoR. Force approval is audited by the API.

    Args:
        task_id: The draft task ID to approve
        comment: Optional reviewer comment
        run: If True, also dispatch the task immediately after approval
        runtime: Override runtime: 'auto' or 'openrouter'. Empty to keep existing.
        force: If True, override failed DoR checks. Use only as a human decision.
    """
    body: dict[str, Any] = {"comment": comment, "run": run, "force": force}
    if runtime:
        body["runtime"] = runtime
    result = await _api_post(f"/api/tasks/{task_id}/approve", body)
    status = result.get("status", "?")
    return f"Task #{task_id} approved (status: {status})."


@mcp.tool()
async def hub_reject_task(task_id: int, comment: str = "") -> str:
    """Reject a draft task (proposed by agent).

    Args:
        task_id: The draft task ID to reject
        comment: Reason for rejection
    """
    await _api_post(f"/api/tasks/{task_id}/reject", {"comment": comment})
    return f"Task #{task_id} rejected."


@mcp.tool()
async def hub_start_task(task_id: int, plan: str = "", runtime: str = "") -> str:
    """Dispatch an open task to an agent. Requires a plan.

    A plan is required before starting. Either pass it here or create
    an update with kind='status' and content starting with 'Plan:' beforehand.

    Args:
        task_id: The open task ID to start
        plan: Work plan (what will be done and how). Required if no plan update exists.
        runtime: Override runtime: 'auto' or 'openrouter'. Empty to keep existing.
    """
    body: dict[str, Any] = {}
    if plan:
        body["plan"] = plan
    if runtime:
        body["runtime"] = runtime
    result = await _api_post(f"/api/tasks/{task_id}/start", body)
    status = result.get("status", "?")
    job_id = result.get("job_id", "-")
    return f"Task #{task_id} dispatched (status: {status}, job: {job_id})."


@mcp.tool()
async def hub_pair_start(
    task_id: int,
    plan: str = "",
    assigned_agent: str = "",
    branch_slug: str = "",
) -> str:
    """Start pair mode: move an open task to running without headless dispatch.

    Use this when a human works with a Cursor agent locally instead of
    ``hub_start_task``, which always calls oc-dev-dispatch.

    Args:
        task_id: The open task ID to pair-start
        plan: Work plan if none exists yet (kind='status' content starting with 'Plan:')
        assigned_agent: Agent name to record on the task. Empty uses caller identity.
        branch_slug: Optional branch slug (task-<id>/<slug>). Empty uses title slug.
    """
    body: dict[str, Any] = {}
    if plan:
        body["plan"] = plan
    if assigned_agent:
        body["assigned_agent"] = assigned_agent
    if branch_slug:
        body["branch_slug"] = branch_slug
    result = await _api_post(f"/api/tasks/{task_id}/pair-start", body or None)
    status = result.get("status", "?")
    branch = result.get("branch") or "-"
    agent = result.get("assigned_agent") or "-"
    job_id = result.get("job_id")
    job_note = "no dispatch job" if not job_id else f"job: {job_id}"
    return (
        f"Task #{task_id} pair-started (status: {status}, branch: {branch}, "
        f"agent: {agent}, {job_note})."
    )


@mcp.tool()
async def hub_claim_task(
    task_id: int,
    agent: str,
    session_id: str = "",
) -> str:
    """Claim an open task for one Cursor agent/session (409 if already claimed).

    Args:
        task_id: The open task ID
        agent: Agent name taking the claim
        session_id: Optional Cursor session id for conflict detection
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/claim",
        {"agent": agent, "session_id": session_id},
    )
    status = result.get("status", "?")
    holder = result.get("claimed_by") or agent
    return f"Task #{task_id} claimed (status: {status}, claimed_by: {holder})."


@mcp.tool()
async def hub_release_task(
    task_id: int,
    agent: str,
    session_id: str = "",
) -> str:
    """Release a claimed task back to open.

    Args:
        task_id: The claimed task ID
        agent: Agent that holds the claim
        session_id: Optional session id that must match the claim
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/release",
        {"agent": agent, "session_id": session_id},
    )
    status = result.get("status", "?")
    return f"Task #{task_id} claim released (status: {status})."


@mcp.tool()
async def hub_force_complete_task(task_id: int, comment: str = "") -> str:
    """Human force-completes a pending_report task without an agent done report.

    Use this only when a human has inspected the result and intentionally accepts
    responsibility for completing a task that lacks a normal done report. The
    comment is recorded as the audit-trail message on the task update.

    Args:
        task_id: The pending_report task ID to complete
        comment: Reason for the override; recorded as the audit-trail message
    """
    body = {"comment": comment} if comment else None
    result = await _api_post(f"/api/tasks/{task_id}/force-complete", body)
    status = result.get("status", "?")
    return f"Task #{task_id} force-completed (status: {status})."


@mcp.tool()
async def hub_archive_task(task_id: int, cascade: bool = True) -> str:
    """Hide a task from default lists (optional subtree cascade).

    Args:
        task_id: Task to archive
        cascade: If True, archive the whole subtree. If False, only this row.
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/archive",
        {"cascade": cascade},
    )
    st = result.get("status", "?")
    return f"Task #{task_id} archived (status in response: {st})."


@mcp.tool()
async def hub_unarchive_task(task_id: int, cascade: bool = True) -> str:
    """Restore archived tasks (optional subtree cascade).

    Args:
        task_id: Task to unarchive
        cascade: If True, unarchive the whole subtree. If False, only this row.
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/unarchive",
        {"cascade": cascade},
    )
    st = result.get("status", "?")
    return f"Task #{task_id} unarchived (status in response: {st})."


@mcp.tool()
async def hub_delete_task(task_id: int) -> str:
    """Permanently delete a task and all descendants (irreversible).

    Args:
        task_id: Root of the subtree to remove from the database.
    """
    await _api_delete(f"/api/tasks/{task_id}")
    return f"Task #{task_id} and its descendants were deleted."


# ---------------------------------------------------------------------------
# Q&A: question / answer
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_ask_question(task_id: int, question: str, agent: str = "") -> str:
    """Agent asks a clarifying question. Task moves to needs_info until human answers.

    Allowed when the task is ``running``, or ``open`` with no ``job_id`` (pair path
    before ``hub_pair_start``). Headless ``running`` tasks with a ``job_id`` are
    unchanged.

    Args:
        task_id: The task ID
        question: The question text
        agent: Name of the agent asking
    """
    await _api_post(
        f"/api/tasks/{task_id}/question",
        {
            "agent": agent,
            "question": question,
        },
    )
    return f"Question posted on task #{task_id}. Task is now paused (needs_info). Waiting for human answer."


@mcp.tool()
async def hub_answer_question(task_id: int, answer: str, resume: bool = True) -> str:
    """Human answers agent's question on a needs_info task.

    For pair tasks (no ``job_id``), ``resume=true`` returns to ``open`` or ``running``
    without headless dispatch. Headless tasks with a ``job_id`` re-dispatch when
    ``resume=true``.

    Args:
        task_id: The needs_info task ID
        answer: The answer text
        resume: If True, resume work after the answer. Pair: no dispatch; headless: re-dispatch.
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/answer",
        {
            "answer": answer,
            "resume": resume,
        },
    )
    status = result.get("status", "?")
    return f"Answer posted on task #{task_id} (status: {status})."


# ---------------------------------------------------------------------------
# Decide (after arbiter)
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_decide_task(
    task_id: int,
    action: str,
    instructions: str = "",
    decision_summary: str = "",
    record_decision: bool = False,
) -> str:
    """Human decision after arbiter review — the Decision Gate.

    When a task reaches needs_decision (review ambiguity, CI/review cycle
    limit, or arbiter escalation), a human must explicitly accept or rework
    it. This tool records the human decision and optionally persists it as
    a reusable decision record through the notes integration.

    The decision_summary is always written into the task update log so the
    reasoning is visible even without a notes backend. When record_decision
    is True the summary is additionally saved through the notes plugin (if
    configured); if notes are unavailable, core flow continues unaffected.

    Args:
        task_id: The needs_decision task ID
        action: 'accept' to complete, 'rework' to send back for fixes
        instructions: When action='rework', what needs to be fixed
        decision_summary: Short summary/reason for the decision (recorded in task updates)
        record_decision: If True, also persist the decision through the notes integration
    """
    body: dict[str, Any] = {
        "action": action,
        "instructions": instructions,
        "decision_summary": decision_summary,
        "record_decision": record_decision,
    }
    result = await _api_post(f"/api/tasks/{task_id}/decide", body)
    status = result.get("status", "?")
    suffix = ""
    if decision_summary:
        suffix = " (decision recorded)"
    return f"Task #{task_id}: decision '{action}' applied (status: {status}).{suffix}"


# ---------------------------------------------------------------------------
# Proposals (backward compat)
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_propose_task(
    title: str,
    description: str,
    agent: str = "",
    rationale: str = "",
    parent_id: int | None = None,
    human_owner: str = "",
    human_reviewer: str = "",
) -> str:
    """Propose a new task for human approval (used by agents). Creates a draft task.

    Args:
        title: Short title of the proposed task
        description: What needs to be done and why
        agent: Name of the proposing agent
        rationale: Why this task is needed
        parent_id: Optional parent task ID (to propose subtask or task within a feature)
        human_owner: Person who owns / is accountable for this task
        human_reviewer: Person who will review and accept the result
    """
    body: dict[str, Any] = {
        "title": title,
        "description": description,
        "source": "agent",
        "agent": agent,
        "rationale": rationale,
        "human_owner": human_owner,
        "human_reviewer": human_reviewer,
    }
    if parent_id is not None:
        body["parent_id"] = parent_id
    result = await _api_post("/api/tasks", body)
    return f"Draft task #{result['id']} created. Awaiting human approval."


@mcp.tool()
async def hub_list_proposals(status: str = "draft") -> str:
    """List agent proposals (draft tasks).

    Args:
        status: Filter: draft, open, rejected. Default: draft.
    """
    tasks = await _api_get(f"/api/tasks?status={status}&limit=50")
    agent_tasks = [t for t in tasks if t.get("source") == "agent"]
    if not agent_tasks:
        return f"No {status} proposals."
    lines = [_format_task(t) for t in agent_tasks]
    return "\n".join(lines)


# Deprecated aliases
@mcp.tool()
async def hub_approve_proposal(proposal_id: int, comment: str = "") -> str:
    """Deprecated: use hub_approve_task instead. Approves and dispatches."""
    return await hub_approve_task(proposal_id, comment=comment, run=True)


@mcp.tool()
async def hub_reject_proposal(proposal_id: int, comment: str = "") -> str:
    """Deprecated: use hub_reject_task instead."""
    return await hub_reject_task(proposal_id, comment=comment)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_list_decisions(limit: int = 10) -> str:
    """List recent architectural/development decisions from notesforllm.

    Args:
        limit: Max decisions to return
    """
    data = await _api_get("/api/dashboard")
    decisions = data.get("recent_decisions", [])
    if not decisions:
        return "No decisions recorded."
    lines = []
    for d in decisions[:limit]:
        title = d.get("title", "Decision")
        content = d.get("content", d.get("decision", ""))
        lines.append(f"- {title}")
        if content:
            lines.append(f"  {content[:200]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Vast.ai instance management — registered only when OPENCLAW_VAST_ENABLED=1
# ---------------------------------------------------------------------------

if config.VAST_ENABLED:

    @mcp.tool()
    async def hub_vast_up() -> str:
        """Create or reuse a Vast.ai GPU instance with vLLM model.

        Provisions a GPU instance, bootstraps vLLM with Qwen3-Coder, and waits
        until the model is healthy. Takes 2-15 minutes depending on whether an
        instance already exists.

        Returns the OpenAI-compatible API endpoint. After this tool completes,
        write the returned base_url to ~/.openclaw/vast-upstream.json on Mac
        so the local proxy picks it up automatically.
        """
        import httpx

        async with httpx.AsyncClient(timeout=1200) as client:
            resp = await client.post(f"{_hub_url()}/api/vast/up")
            resp.raise_for_status()
            result = resp.json()

        if result.get("error"):
            return f"Failed to create Vast instance: {result['error']}"

        public_ip = result.get("public_ip")
        api_port = result.get("api_port")
        model_id = result.get("model_id", "")
        base_url = result.get("base_url") or (
            f"http://{public_ip}:{api_port}/v1" if public_ip and api_port else "unknown"
        )
        proxy_upstream = base_url.rstrip("/")
        if proxy_upstream.endswith("/v1"):
            proxy_upstream = proxy_upstream[:-3]
        hourly = result.get("hourly_rate", "?")

        parts = [
            "Vast.ai instance is UP and healthy.",
            f"  Instance:  #{result.get('instance_id', '?')}",
            f"  Rate:      ${hourly}/hr",
            f"  Model:     {model_id}",
            f"  Endpoint:  {base_url}",
            "",
            "UPDATE LOCAL PROXY by running this command on Mac:",
            f'  echo \'{{"base_url":"{proxy_upstream}"}}\' > ~/.openclaw/vast-upstream.json',
            "",
            "Local proxy → http://localhost:8741/v1",
            "Cursor model ready to use.",
        ]
        return "\n".join(parts)

    @mcp.tool()
    async def hub_vast_status() -> str:
        """Check the status of the current Vast.ai GPU instance."""
        result = await _api_get("/api/vast/status")

        if not result.get("managed"):
            return "No active Vast.ai instance."

        parts = [
            f"Vast instance #{result.get('instance_id')} — {result.get('status', 'unknown')}",
            f"  Hourly rate: ${result.get('hourly_rate', '?')}/hr",
            f"  Base URL:    {result.get('base_url', 'N/A')}",
            f"  Public IP:   {result.get('public_ip', 'N/A')}",
            f"  Last used:   {result.get('last_used_at', 'N/A')}",
        ]
        if result.get("degraded"):
            parts.append(
                "  WARNING: Status degraded (API lookup failed, using cached state)"
            )
        return "\n".join(parts)

    @mcp.tool()
    async def hub_vast_down() -> str:
        """Destroy the active Vast.ai GPU instance to stop billing."""
        import httpx

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{_hub_url()}/api/vast/down")
            resp.raise_for_status()
            result = resp.json()

        if result.get("destroyed"):
            return f"Vast instance #{result.get('instance_id', '?')} destroyed. Billing stopped."
        return (
            f"No instance to destroy. {result.get('reason', result.get('error', ''))}"
        )


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_dispatch_jobs(limit: int = 15) -> str:
    """List recent oc-dev-dispatch jobs (raw dispatch state).

    Args:
        limit: Max jobs to return
    """
    jobs = await _api_get(f"/api/dispatch/jobs?limit={limit}")
    if not jobs:
        return "No dispatch jobs found."
    lines = []
    for j in jobs:
        lines.append(
            f"{j.get('job_id', '?')} [{j.get('status', '?')}] "
            f"runtime={j.get('runtime', '?')} exit={j.get('exit_code', '-')} "
            f"session={j.get('session_id', '')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Structured task form (#43): refine, ACs, risks, readiness
# ---------------------------------------------------------------------------
#
# Design (agreed in #46 follow-up): one MCP tool per business operation,
# mirroring the REST API one-to-one. CLI (#42) is a separate UX surface;
# both reuse the same backend services (services.refinement.*) so the
# behavior never drifts.


def _format_ac(ac: dict[str, Any]) -> str:
    test = f"\n   Test: {ac['test_ref']}" if ac.get("test_ref") else ""
    return (
        f"{ac['id']} [{ac.get('verifiable_by', '?')}]\n"
        f"  Given: {ac.get('given', '')}\n"
        f"   When: {ac.get('when', '')}\n"
        f"   Then: {ac.get('then', '')}" + test
    )


def _format_readiness(report: dict[str, Any], task_id: int) -> str:
    parts = [
        f"Task #{task_id} readiness: score={report['score']} "
        f"dor_passed={'yes' if report['dor_passed'] else 'no'}"
    ]
    missing = report.get("missing_required") or []
    if missing:
        parts.append("  Missing required: " + ", ".join(missing))
    risks = report.get("risks") or []
    if risks:
        risk_brief = ", ".join(
            f"{r.get('kind')}:{r.get('severity')}" for r in risks[:5]
        )
        parts.append(f"  Risks ({len(risks)}): {risk_brief}")
    recs = report.get("recommendations") or []
    blocking = [r for r in recs if r.get("severity") == "blocking"]
    if blocking:
        parts.append("  Blocking recommendations:")
        for r in blocking[:5]:
            parts.append(f"    - {r.get('field')}: {r.get('message')}")
    elif recs:
        parts.append(
            f"  ({len(recs)} non-blocking suggestions; call hub_get_readiness with explain=true for full JSON)"
        )
    return "\n".join(parts)


def _prepare_quality_warnings(
    acceptance_criteria: list[dict[str, Any]] | None,
    risks: list[dict[str, Any]] | None,
    duplicate_risks: list[dict[str, Any]] | None,
    affected_areas: list[str] | None,
    validation_commands: list[str] | None,
    risk_mode: str,
) -> list[str]:
    warnings: list[str] = []
    if acceptance_criteria is not None:
        warnings.append(
            "acceptance_criteria replace existing criteria; review before apply"
        )
        for ac in acceptance_criteria:
            if ac.get("verifiable_by") == "test" and not ac.get("test_ref"):
                warnings.append(
                    f"acceptance criterion {ac.get('id', '<unknown>')} has no test_ref"
                )
    if risks:
        if risk_mode == "append":
            warnings.append("risks are appended; repeated apply can duplicate risks")
        elif risk_mode == "dedupe":
            warnings.append("risks are deduped by kind/severity/description/mitigation")
        elif risk_mode == "replace":
            warnings.append("risks replace existing risk list")
    for risk in duplicate_risks or []:
        warnings.append(
            "duplicate risk skipped: "
            f"{risk.get('kind')}:{risk.get('severity')} {risk.get('description')}"
        )
    if affected_areas == []:
        warnings.append("affected_areas is empty")
    if validation_commands == []:
        warnings.append("validation_commands is empty")
    return warnings


def _developer_handoff_text(
    task_id: int,
    *,
    problem_statement: str | None,
    business_value: str | None,
    scope_in: list[str] | None,
    scope_out: list[str] | None,
    affected_areas: list[str] | None,
    validation_commands: list[str] | None,
    acceptance_criteria: list[dict[str, Any]] | None,
    risks: list[dict[str, Any]] | None,
    review_checklist: list[str] | None,
) -> str:
    lines = [f"Developer handoff for task #{task_id}"]
    if problem_statement:
        lines.append(f"Problem: {problem_statement}")
    if business_value:
        lines.append(f"Value: {business_value}")
    if scope_in:
        lines.append("Scope in:")
        lines.extend(f"- {item}" for item in scope_in)
    if scope_out:
        lines.append("Scope out:")
        lines.extend(f"- {item}" for item in scope_out)
    if affected_areas:
        lines.append("Affected areas: " + ", ".join(affected_areas))
    if acceptance_criteria:
        lines.append("Acceptance criteria:")
        lines.extend(
            f"- {ac.get('id', '?')}: Given {ac.get('given', '')}; "
            f"When {ac.get('when', '')}; Then {ac.get('then', '')}"
            for ac in acceptance_criteria
        )
    if risks:
        lines.append("Risks:")
        lines.extend(
            f"- {risk.get('kind', '?')}:{risk.get('severity', '?')} — "
            f"{risk.get('description', '')}; mitigation: {risk.get('mitigation', '')}"
            for risk in risks
        )
    if validation_commands:
        lines.append("Validation:")
        lines.extend(f"- {cmd}" for cmd in validation_commands)
    if review_checklist:
        lines.append("Review checklist:")
        lines.extend(f"- {item}" for item in review_checklist)
    return "\n".join(lines)


def _risk_key(risk: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(risk.get("kind", "")),
        str(risk.get("severity", "")),
        str(risk.get("description", "")),
        str(risk.get("mitigation", "")),
    )


@mcp.tool()
async def hub_prepare_developer_task(
    task_id: int,
    mode: str = "apply",
    risk_mode: str = "dedupe",
    work_type: str | None = None,
    class_of_service: str | None = None,
    size: str | None = None,
    wip_tag: str | None = None,
    due_date: str | None = None,
    user_story: str | None = None,
    problem_statement: str | None = None,
    business_value: str | None = None,
    technical_hints: str | None = None,
    scope_in: list[str] | None = None,
    scope_out: list[str] | None = None,
    affected_areas: list[str] | None = None,
    validation_commands: list[str] | None = None,
    constraints: list[str] | None = None,
    assumptions: list[str] | None = None,
    out_of_scope_for_review: list[str] | None = None,
    review_checklist: list[str] | None = None,
    acceptance_criteria: list[dict[str, Any]] | None = None,
    risks: list[dict[str, Any]] | None = None,
    human_owner: str | None = None,
    human_reviewer: str | None = None,
    analyst: str = "analyst-agent",
) -> str:
    """Prepare a raw task for developer handoff in one analyst operation.

    This combines the existing REST semantics:
    refine structured DoR fields, atomically replace acceptance criteria,
    append risks, compute readiness, and write an analyst status update.

    Args:
        task_id: Target task.
        mode: apply writes changes; preview returns planned operations without writes.
        risk_mode: dedupe skips existing identical risks; append always appends; replace replaces the full risk list through refine.
        acceptance_criteria: Full replacement list of AC dictionaries. Omit to keep existing ACs.
        risks: Risks to append. Omit or pass [] to add none.
        analyst: Agent name recorded in the preparation status update.
    """
    if mode not in {"apply", "preview"}:
        raise ValueError("mode must be 'apply' or 'preview'")
    if risk_mode not in {"dedupe", "append", "replace"}:
        raise ValueError("risk_mode must be 'dedupe', 'append', or 'replace'")

    if wip_tag is None and (work_type is None or work_type == "feature"):
        wip_tag = "feature_work"
    prepared_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    refine_body: dict[str, Any] = {}
    for key, val in (
        ("work_type", work_type),
        ("class_of_service", class_of_service),
        ("size", size),
        ("wip_tag", wip_tag),
        ("due_date", due_date),
        ("user_story", user_story),
        ("problem_statement", problem_statement),
        ("business_value", business_value),
        ("technical_hints", technical_hints),
        ("scope_in", scope_in),
        ("scope_out", scope_out),
        ("affected_areas", affected_areas),
        ("validation_commands", validation_commands),
        ("constraints", constraints),
        ("assumptions", assumptions),
        ("out_of_scope_for_review", out_of_scope_for_review),
        ("review_checklist", review_checklist),
        ("human_owner", human_owner),
        ("human_reviewer", human_reviewer),
        ("prepared_by", analyst),
        ("prepared_at", prepared_at),
    ):
        if val is not None:
            refine_body[key] = val

    current_task = await _api_get(f"/api/tasks/{task_id}")
    existing_acs: list[dict[str, Any]] = []
    if acceptance_criteria is not None:
        existing_acs = await _api_get(f"/api/tasks/{task_id}/acceptance_criteria")

    existing_risks = current_task.get("risks") or []
    existing_risk_keys = {_risk_key(risk) for risk in existing_risks}
    incoming_risks = risks or []
    duplicate_risks = [
        risk for risk in incoming_risks if _risk_key(risk) in existing_risk_keys
    ]
    if risk_mode == "dedupe":
        risks_to_add = [
            risk for risk in incoming_risks if _risk_key(risk) not in existing_risk_keys
        ]
    elif risk_mode == "append":
        risks_to_add = incoming_risks
    else:
        risks_to_add = []
        if risks is not None:
            refine_body["risks"] = risks

    structured_fields_to_change = [
        key for key, val in refine_body.items() if current_task.get(key) != val
    ]

    handoff_text = _developer_handoff_text(
        task_id,
        problem_statement=problem_statement,
        business_value=business_value,
        scope_in=scope_in,
        scope_out=scope_out,
        affected_areas=affected_areas,
        validation_commands=validation_commands,
        acceptance_criteria=acceptance_criteria,
        risks=risks,
        review_checklist=review_checklist,
    )
    quality_warnings = _prepare_quality_warnings(
        acceptance_criteria,
        risks,
        duplicate_risks if risk_mode == "dedupe" else [],
        affected_areas,
        validation_commands,
        risk_mode,
    )
    planned_operations: list[str] = []
    if refine_body:
        planned_operations.append("refine_task")
    if acceptance_criteria is not None:
        planned_operations.append("replace_acceptance_criteria")
    if risk_mode == "replace" and risks is not None:
        planned_operations.append("replace_risks")
    elif risks_to_add:
        planned_operations.extend("add_risk" for _ in risks_to_add)
    planned_operations.append("write_analyst_update")
    diff = {
        "structured_fields_to_change": sorted(structured_fields_to_change),
        "will_replace_acceptance_criteria": acceptance_criteria is not None,
        "existing_acceptance_criteria_count": len(existing_acs),
        "new_acceptance_criteria_count": len(acceptance_criteria)
        if acceptance_criteria is not None
        else None,
        "risk_mode": risk_mode,
        "existing_risks_count": len(existing_risks),
        "incoming_risks_count": len(incoming_risks),
        "duplicate_risks_count": len(duplicate_risks),
        "risks_to_add_count": len(risks_to_add),
    }

    if mode == "preview":
        return json.dumps(
            {
                "mode": "preview",
                "task_id": task_id,
                "planned_operations": planned_operations,
                "diff": diff,
                "quality_warnings": quality_warnings,
                "developer_handoff_text": handoff_text,
                "next_action": "preview_only",
            },
            ensure_ascii=False,
            indent=2,
        )

    updated_columns: list[str] = []
    if refine_body:
        refine_result = await _api_post(f"/api/tasks/{task_id}/refine", refine_body)
        cols = refine_result.get("updated_columns") or []
        updated_columns = sorted(cols) if isinstance(cols, list) else sorted(cols)

    ac_count: int | None = None
    if acceptance_criteria is not None:
        ac_result = await _api_put(
            f"/api/tasks/{task_id}/acceptance_criteria",
            acceptance_criteria,
        )
        ac_count = (
            len(ac_result) if isinstance(ac_result, list) else len(acceptance_criteria)
        )

    risks_added = 0
    for risk in risks_to_add:
        await _api_post(f"/api/tasks/{task_id}/risks", risk)
        risks_added += 1

    readiness = await _api_get(f"/api/tasks/{task_id}/readiness")
    dor_passed = bool(readiness.get("dor_passed"))
    missing_required = readiness.get("missing_required") or []
    next_action = "ready_for_developer" if dor_passed else "needs_analyst_followup"
    score = readiness.get("score")

    update_message = (
        "Analyst preparation complete: "
        f"readiness score={score}, dor_passed={'yes' if dor_passed else 'no'}, "
        f"acceptance_criteria={'unchanged' if ac_count is None else ac_count}, "
        f"risks_added={risks_added}, duplicate_risks={len(duplicate_risks)}.\n\n"
        f"Developer handoff:\n{handoff_text}"
    )
    if missing_required:
        update_message += " Missing required: " + ", ".join(missing_required) + "."
    await _api_post(
        f"/api/tasks/{task_id}/updates",
        {
            "agent": analyst,
            "kind": "status",
            "content": update_message,
        },
    )

    return json.dumps(
        {
            "mode": "apply",
            "task_id": task_id,
            "updated_columns": updated_columns,
            "acceptance_criteria_count": ac_count,
            "risks_added": risks_added,
            "duplicate_risks_count": len(duplicate_risks),
            "readiness_score": score,
            "dor_passed": dor_passed,
            "missing_required": missing_required,
            "recommendations_count": len(readiness.get("recommendations") or []),
            "diff": diff,
            "quality_warnings": quality_warnings,
            "developer_handoff_text": handoff_text,
            "next_action": next_action,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
async def hub_refine_task(
    task_id: int,
    title: str | None = None,
    work_type: str | None = None,
    class_of_service: str | None = None,
    size: str | None = None,
    wip_tag: str | None = None,
    due_date: str | None = None,
    user_story: str | None = None,
    problem_statement: str | None = None,
    business_value: str | None = None,
    technical_hints: str | None = None,
    scope_in: list[str] | None = None,
    scope_out: list[str] | None = None,
    affected_areas: list[str] | None = None,
    validation_commands: list[str] | None = None,
    constraints: list[str] | None = None,
    assumptions: list[str] | None = None,
    out_of_scope_for_review: list[str] | None = None,
    review_checklist: list[str] | None = None,
    human_owner: str | None = None,
    human_reviewer: str | None = None,
    acceptance_criteria: list[dict[str, Any]] | None = None,
    risks: list[dict[str, Any]] | None = None,
) -> HubRefineTaskResult:
    """PATCH a task's structured fields (Definition of Ready inputs).

    Only fields you pass are written. Omit a parameter to leave the
    existing value untouched. Lists fully replace the existing list.
    ``acceptance_criteria`` and ``risks`` mirror POST /api/tasks/{id}/refine:
    AC list replaces all criteria; risks list replaces the JSON risks column.

    Args:
        task_id: Task to refine.
        title: New task title (1–500 chars). Omit to leave unchanged.
        work_type: feature | bug | refactor | chore | docs | spike | incident
        class_of_service: standard | expedite | fixed_date | intangible
        size: XS | S | M | L | XL
        wip_tag: feature_work | bugfix | tech_debt | support
        due_date: ISO date string (YYYY-MM-DD) for fixed_date COS.
        user_story: "As a <role>, I want <X> so that <Y>".
        problem_statement: What's broken / why this work exists.
        business_value: Outcome / why it matters.
        technical_hints: Hints, references, suggested approach.
        scope_in: In-scope items (REPLACES the list).
        scope_out: Out-of-scope items (REPLACES the list).
        affected_areas: Modules/paths impacted (REPLACES).
        validation_commands: Commands proving it works (REPLACES).
        constraints: Hard constraints (REPLACES).
        assumptions: Assumptions made (REPLACES).
        out_of_scope_for_review: Things the reviewer should ignore (REPLACES).
        review_checklist: Reviewer checklist — what to verify in diff (REPLACES).
        human_owner: Person who owns / is accountable for this task.
        human_reviewer: Person who will review and accept the result.
        acceptance_criteria: Full AC replacement list (same shape as REST refine).
        risks: Full risks list replacement (same shape as REST refine / TaskRisk).
    """
    body: dict[str, Any] = {}
    for key, val in (
        ("title", title),
        ("work_type", work_type),
        ("class_of_service", class_of_service),
        ("size", size),
        ("wip_tag", wip_tag),
        ("due_date", due_date),
        ("user_story", user_story),
        ("problem_statement", problem_statement),
        ("business_value", business_value),
        ("technical_hints", technical_hints),
        ("scope_in", scope_in),
        ("scope_out", scope_out),
        ("affected_areas", affected_areas),
        ("validation_commands", validation_commands),
        ("constraints", constraints),
        ("assumptions", assumptions),
        ("out_of_scope_for_review", out_of_scope_for_review),
        ("review_checklist", review_checklist),
        ("human_owner", human_owner),
        ("human_reviewer", human_reviewer),
    ):
        if val is not None:
            body[key] = val
    if acceptance_criteria is not None:
        body["acceptance_criteria"] = acceptance_criteria
    if risks is not None:
        body["risks"] = risks
    if not body:
        summary = (
            "Nothing to refine: pass at least one structured field, "
            "acceptance_criteria, or risks."
        )
        return structured_tool_result(
            summary,
            HubRefineTaskStructured(task_id=task_id, no_op=True),
        )
    # REST /refine returns the full TaskView. We report what changed from the
    # PATCH keys we actually sent (not a column diff), and surface AC/risks
    # counts + readiness from the returned task so the summary never claims
    # "no changes" when acceptance_criteria or risks were replaced.
    result = await _api_post(f"/api/tasks/{task_id}/refine", body)
    fields_set = sorted(body.keys())
    ac_count = (
        len(result.get("acceptance_criteria") or [])
        if "acceptance_criteria" in body
        else None
    )
    risks_count = len(result.get("risks") or []) if "risks" in body else None
    readiness_score = result.get("readiness_score")
    dor_passed = result.get("dor_passed")

    parts = [f"Set: {', '.join(fields_set)}"]
    if ac_count is not None:
        parts.append(f"{ac_count} acceptance criteria")
    if risks_count is not None:
        parts.append(f"{risks_count} risks")
    if readiness_score is not None:
        parts.append(f"readiness {readiness_score}")
    summary = f"Task #{task_id} refined. " + "; ".join(parts) + "."

    return structured_tool_result(
        summary,
        HubRefineTaskStructured(
            task_id=task_id,
            fields_set=fields_set,
            acceptance_criteria_count=ac_count,
            risks_count=risks_count,
            readiness_score=readiness_score,
            dor_passed=dor_passed,
            task=result,
        ),
    )


@mcp.tool()
async def hub_refine_tasks(items: list[dict[str, Any]]) -> HubRefineTasksResult:
    """Bulk-refine many tasks in ONE atomic call (replaces N hub_refine_task).

    Either every item lands or none does. Use this to bring a batch of tasks
    to DoR without a request per task.

    Args:
        items: List of dicts, each with ``task_id`` plus any TaskRefine fields
            (e.g. work_type, scope_in, problem_statement, size,
            acceptance_criteria, risks). ``acceptance_criteria``/``risks``
            replace the full list for that task.
    """
    if not items:
        return structured_tool_result(
            "Nothing to refine: items list is empty.",
            HubRefineTasksStructured(no_op=True),
        )
    result = await _api_post("/api/tasks/refine-bulk", {"items": items})
    results = result.get("results") or []
    lines = [f"Refined {len(results)} task(s):"]
    for r in results:
        detail = [f"set {', '.join(r.get('fields_set') or []) or '-'}"]
        if r.get("acceptance_criteria_count") is not None:
            detail.append(f"{r['acceptance_criteria_count']} AC")
        if r.get("risks_count") is not None:
            detail.append(f"{r['risks_count']} risks")
        if r.get("readiness_score") is not None:
            dor = " DoR✓" if r.get("dor_passed") else ""
            detail.append(f"readiness {r['readiness_score']}{dor}")
        lines.append(f"  #{r.get('task_id')}: " + "; ".join(detail))
    return structured_tool_result(
        "\n".join(lines),
        HubRefineTasksStructured(results=results),
    )


@mcp.tool()
async def hub_list_acceptance_criteria(task_id: int) -> str:
    """List all acceptance criteria (Given/When/Then scenarios) for a task."""
    items = await _api_get(f"/api/tasks/{task_id}/acceptance_criteria")
    if not items:
        return f"Task #{task_id} has no acceptance criteria."
    return "\n\n".join(_format_ac(ac) for ac in items)


@mcp.tool()
async def hub_add_acceptance_criterion(
    task_id: int,
    ac_id: str,
    given: str,
    when: str,
    then: str,
    verifiable_by: str = "test",
    test_ref: str = "",
) -> str:
    """Add a single Given/When/Then acceptance criterion to a task.

    Args:
        task_id: Target task.
        ac_id: Stable identifier for this AC (e.g. "AC-1"). Must be
            unique within the task — duplicate ids return HTTP 409.
        given: Precondition / context.
        when: Action / event.
        then: Observable outcome.
        verifiable_by: How the AC is verified: test | manual | log_check | ui_check.
        test_ref: Optional pointer to the test (e.g. tests/x.py::test_y).
    """
    body: dict[str, Any] = {
        "id": ac_id,
        "given": given,
        "when": when,
        "then": then,
        "verifiable_by": verifiable_by,
    }
    if test_ref:
        body["test_ref"] = test_ref
    await _api_post(f"/api/tasks/{task_id}/acceptance_criteria", body)
    return f"Added {ac_id} to task #{task_id}"


@mcp.tool()
async def hub_replace_acceptance_criteria(
    task_id: int,
    items: list[dict[str, Any]],
) -> str:
    """Atomically replace ALL acceptance criteria for a task.

    Pass an empty list to clear them. Each item must have id, given,
    when, then, verifiable_by; test_ref is optional. The whole replace
    is one transaction — partial application is impossible.

    Args:
        task_id: Target task.
        items: New acceptance criteria. Empty list clears them.
    """
    result = await _api_put(f"/api/tasks/{task_id}/acceptance_criteria", items)
    count = len(result) if isinstance(result, list) else len(items)
    return f"Task #{task_id} now has {count} acceptance criteria"


@mcp.tool()
async def hub_delete_acceptance_criterion(task_id: int, ac_id: str) -> str:
    """Delete a single acceptance criterion by its id."""
    import urllib.parse

    safe_id = urllib.parse.quote(ac_id, safe="")
    await _api_delete(f"/api/tasks/{task_id}/acceptance_criteria/{safe_id}")
    return f"Deleted {ac_id} from task #{task_id}"


@mcp.tool()
async def hub_add_risk(
    task_id: int,
    kind: str,
    severity: str,
    description: str,
    mitigation: str,
) -> str:
    """Append a risk to a task through the atomic dedicated endpoint.

    Args:
        task_id: Target task.
        kind: ambiguous_requirements | large_scope | external_dependency |
            data_migration | breaking_change | security | performance |
            unknown_unknowns
        severity: low | medium | high
        description: One-line risk description.
        mitigation: How we plan to handle / reduce it.
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/risks",
        {
            "kind": kind,
            "severity": severity,
            "description": description,
            "mitigation": mitigation,
        },
    )
    total = len(result.get("risks") or []) if isinstance(result, dict) else 0
    suffix = f" (total: {total})" if total else ""
    return f"Risk '{kind}:{severity}' added to task #{task_id}{suffix}"


@mcp.tool()
async def hub_get_readiness(task_id: int, explain: bool = False) -> str:
    """Get the Definition of Ready report and readiness score for a task.

    Returns a compact human-readable summary. Set explain=true to receive
    the full ReadinessReport JSON including the per-component score
    breakdown — useful when debugging why a task isn't approving.

    Args:
        task_id: Target task.
        explain: If true, dump the full JSON report instead of a summary.
    """
    path = f"/api/tasks/{task_id}/readiness"
    if explain:
        path += "?explain=true"
    report = await _api_get(path)
    if explain:
        return json.dumps(report, ensure_ascii=False, indent=2)
    return _format_readiness(report, task_id)


# ---------------------------------------------------------------------------
# Admin: read-only identity diagnostic (Stage 4)
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_admin_my_identity() -> str:
    """Diagnostic: show the current identity, roles, and permissions of the caller.

    This is a read-only tool that helps agents verify their identity and
    understand what operations they are authorized to perform.
    """
    try:
        await _api_get("/api/tasks?limit=1")
        return (
            "Identity check: API access confirmed. "
            "Use the Hub Web UI or CLI for detailed identity info."
        )
    except Exception as e:
        return f"Identity check failed: {e}"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
