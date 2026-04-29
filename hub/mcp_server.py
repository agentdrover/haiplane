"""OpenClaw Hub MCP server — exposes hub tools for Cursor and remote agents."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from hub import config

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
) -> str:
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
    return (
        f"{task_type.capitalize()} #{result['id']} created (status: {status}).\n"
        + json.dumps(result, ensure_ascii=False, indent=2)
    )


@mcp.tool()
async def hub_list_tasks(
    status: str = "",
    task_type: str = "",
    parent_id: int | None = None,
    human_owner: str = "",
    human_reviewer: str = "",
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
    if include_archived:
        params["include_archived"] = "true"
    tasks = await _api_get(f"/api/tasks?{urlencode(params)}")
    if not tasks:
        return "No tasks found."
    lines = [_format_task(t) for t in tasks]
    return "\n".join(lines)


@mcp.tool()
async def hub_task_status(task_id: int) -> str:
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
    return "\n".join(parts)


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


@mcp.tool()
async def hub_report_done(task_id: int, summary: str, agent: str = "") -> str:
    """Submit a completion report for a task — the normal agent path out of pending_report.

    After an agent finishes work it must submit a done report describing what was changed
    and how it was validated; the report transitions the task from pending_report to
    completed. This is the standard close-out path for agents.

    The explicit human exception is `hub_force_complete_task`, which is reserved for cases
    where a human has inspected the result and accepts that the agent report is missing or
    unacceptably weak. Agents should not use `hub_force_complete_task` as a fallback —
    they should always submit a real done report through this tool.

    Args:
        task_id: The task ID to report on
        summary: What was changed and how it was validated (e.g. 'Changed: X, Y. Validation: tests pass, ruff clean.')
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
    return f"Done report #{result['id']} submitted for task #{task_id}. Task should now be completed."


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
    """Agent asks a clarifying question on a running task. Task pauses until human answers.

    Args:
        task_id: The running task ID
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
    """Human answers agent's question. By default re-dispatches the task.

    Args:
        task_id: The needs_info task ID
        answer: The answer text
        resume: If True, re-dispatch the task with context. If False, just save the answer.
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


@mcp.tool()
async def hub_prepare_developer_task(
    task_id: int,
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
        acceptance_criteria: Full replacement list of AC dictionaries. Omit to keep existing ACs.
        risks: Risks to append. Omit or pass [] to add none.
        analyst: Agent name recorded in the preparation status update.
    """
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
    ):
        if val is not None:
            refine_body[key] = val

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
    for risk in risks or []:
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
        f"risks_added={risks_added}."
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
            "task_id": task_id,
            "updated_columns": updated_columns,
            "acceptance_criteria_count": ac_count,
            "risks_added": risks_added,
            "readiness_score": score,
            "dor_passed": dor_passed,
            "missing_required": missing_required,
            "recommendations_count": len(readiness.get("recommendations") or []),
            "next_action": next_action,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
async def hub_refine_task(
    task_id: int,
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
) -> str:
    """PATCH a task's structured fields (Definition of Ready inputs).

    Only fields you pass are written. Omit a parameter to leave the
    existing value untouched. Lists fully replace the existing list.

    Args:
        task_id: Task to refine.
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
    """
    body: dict[str, Any] = {}
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
    ):
        if val is not None:
            body[key] = val
    if not body:
        return (
            "Nothing to refine: pass at least one structured field. "
            "Use hub_replace_acceptance_criteria for AC changes."
        )
    result = await _api_post(f"/api/tasks/{task_id}/refine", body)
    cols = result.get("updated_columns") or {}
    if cols:
        return f"Task #{task_id} refined. Updated: {', '.join(sorted(cols))}"
    return f"Task #{task_id} refine accepted (no column changes detected)"


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
