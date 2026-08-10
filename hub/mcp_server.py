"""OpenClaw Hub MCP server — exposes hub tools for Cursor and remote agents."""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from hub import config
from hub.actionable_errors import normalize_api_error_detail
from hub.services.tree_output import (
    TreeOutputOptions,
    render_task_tree,
    truncate_text,
    TRUNCATION_NOTICE,
)
from hub.hub_instance import instance_echo_fields, with_instance_echo
from hub.mcp_envelope import (
    build_mutation_envelope,
    enrich_error_payload,
    format_echo_response,
    merge_mutation_response,
)
from hub.workflow_reference import build_mcp_instructions, lifecycle_map_lines
from mcp.types import CallToolResult

from hub.mcp_structured import (
    HubCreateTaskResult,
    HubCreateTaskStructured,
    HubReadinessTreeResult,
    HubReadinessTreeStructured,
    HubRefineTaskResult,
    HubRefineTaskStructured,
    HubRefineTasksResult,
    HubRefineTasksStructured,
    HubTaskStatusResult,
    HubTaskStatusStructured,
    structured_echo_result,
    structured_tool_result,
)

# FastMCP defaults to localhost-only Host/Origin allowlists when host=127.0.0.1.
# The hub mounts streamable HTTP under the main FastAPI app, so clients send the
# public Host (e.g. agenthai.ru) — the SDK default rejects them with 421. Disable
# MCP-layer rebinding checks here; AuthMiddleware + TLS cover remote access.
mcp = FastMCP(
    "openclaw-hub",
    instructions=build_mcp_instructions(),
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


class HubApiError(Exception):
    """Structured Hub REST error for MCP consumers."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__(payload.get("message", "hub api error"))

    def as_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False)


def _strip_internal_urls(text: str) -> str:
    import re

    cleaned = re.sub(r"https?://127\.0\.0\.1:\d+[^\s]*", "", text)
    cleaned = re.sub(r"for url '[^']*'", "", cleaned)
    return cleaned.strip()


def _parse_api_error(resp: Any, status_code: int) -> dict[str, Any]:
    detail: Any = None
    try:
        body = resp.json()
        detail = body.get("detail", body)
    except Exception:
        detail = getattr(resp, "text", "") or ""

    payload = normalize_api_error_detail(detail, status_code=status_code)
    msg = _strip_internal_urls(str(payload.get("message", payload.get("hint", ""))))
    if msg:
        payload["message"] = msg
    return payload


async def _api_get(path: str, *, timeout: float = 15) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{_hub_url()}{path}", headers=_auth_headers())
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HubApiError(
                _parse_api_error(exc.response, exc.response.status_code)
            ) from exc
        return resp.json()


async def _api_post(
    path: str,
    body: dict[str, Any] | None = None,
    *,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    import httpx

    headers = _auth_headers()
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_hub_url()}{path}", json=body or {}, headers=headers
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HubApiError(
                _parse_api_error(exc.response, exc.response.status_code)
            ) from exc
        return resp.json()


async def _api_patch(path: str, body: dict[str, Any] | None = None) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{_hub_url()}{path}", json=body or {}, headers=_auth_headers()
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HubApiError(
                _parse_api_error(exc.response, exc.response.status_code)
            ) from exc
        return resp.json()


async def _api_put(path: str, body: Any) -> Any:
    """PUT for collection-level replace (e.g. acceptance criteria)."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{_hub_url()}{path}", json=body, headers=_auth_headers()
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HubApiError(
                _parse_api_error(exc.response, exc.response.status_code)
            ) from exc
        return resp.json()


async def _api_delete(path: str) -> None:
    """DELETE returning 204 / no body."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(f"{_hub_url()}{path}", headers=_auth_headers())
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HubApiError(
                _parse_api_error(exc.response, exc.response.status_code)
            ) from exc


async def _api_post_with_status(
    path: str, body: dict[str, Any] | None = None
) -> tuple[Any, int]:
    """POST that also returns the HTTP status (e.g. 201 created vs 200 existing)."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_hub_url()}{path}", json=body or {}, headers=_auth_headers()
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HubApiError(
                _parse_api_error(exc.response, exc.response.status_code)
            ) from exc
        return resp.json(), resp.status_code


async def _api_put_with_status(path: str, body: Any) -> tuple[Any, int]:
    """PUT that also returns the HTTP status (201 created vs 200 updated)."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{_hub_url()}{path}", json=body, headers=_auth_headers()
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HubApiError(
                _parse_api_error(exc.response, exc.response.status_code)
            ) from exc
        return resp.json(), resp.status_code


def _finding_line(finding: dict[str, Any]) -> str:
    """One-line rendering of a review finding with its scope marker (#435)."""
    scope_mark = ""
    if finding.get("scope") == "out_of_scope":
        linked = finding.get("linked_task_id")
        scope_mark = f" [out-of-scope → #{linked}]" if linked else " [out-of-scope]"
    return (
        f"  {finding.get('id', '?')}. [{finding.get('severity', '?')}]"
        f"{scope_mark} {finding.get('message', '')}"
    )


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
async def hub_project_status() -> CallToolResult:
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

    return structured_echo_result(
        "\n".join(parts) if parts else "No activity found.",
        dashboard=data,
    )


def _tree_query_string(
    *,
    depth: int | None = None,
    max_nodes: int | None = None,
    max_chars: int | None = None,
    mode: str = "full",
) -> str:
    params: dict[str, str] = {}
    if depth is not None:
        params["depth"] = str(depth)
    if max_nodes is not None:
        params["max_nodes"] = str(max_nodes)
    if max_chars is not None:
        params["max_chars"] = str(max_chars)
    if mode and mode != "full":
        params["mode"] = mode
    if not params:
        return ""
    return "?" + urllib.parse.urlencode(params)


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
    client_request_id: str = "",
) -> HubCreateTaskResult:
    """Create a new task, epic, feature, or subtask. HUMAN-ONLY (#360).

    Creates work that is already approved, so an agent token gets 403
    ``agent_create_forbidden`` — use ``hub_propose_task`` instead, which
    creates a draft for human approval. The refusal is enforced by the API, not
    here, so it also holds for a token calling POST /api/tasks directly.

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
        client_request_id: Optional idempotency key; safe to retry on timeout
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
    extra_headers: dict[str, str] = {}
    idem_key = client_request_id.strip()
    if idem_key:
        body["client_request_id"] = idem_key
        extra_headers["X-Client-Request-Id"] = idem_key
    result = await _api_post(
        "/api/tasks",
        body,
        extra_headers=extra_headers or None,
    )
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
        source: agent (draft) or human (open). ``human`` is human-only (#360):
            an agent token asking for it gets 403 agent_create_forbidden.
        agent: Assigned agent name when source is agent.
    """
    if not items:
        return format_echo_response("Nothing to create: items list is empty.")
    body: dict[str, Any] = {
        "items": items,
        "task_type": task_type,
        "source": source,
        "agent": agent,
    }
    created = await _api_post(f"/api/tasks/{parent_id}/subtasks", body)
    if not created:
        return format_echo_response(f"No subtasks created under #{parent_id}.")
    lines = [
        f"Created {len(created)} {task_type}(s) under #{parent_id}:",
        *[f"  #{t['id']} [{t['status']}] {t['title']}" for t in created],
    ]
    return format_echo_response("\n".join(lines))


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
    after_id: int | None = None,
    mode: str = "full",
    project: str = "",
) -> CallToolResult:
    """List tasks with optional filters.

    Pagination (#254): pass after_id=0 to start a paged walk (mode=summary
    for compact fields), then repeat with the returned next_cursor until it
    is null.

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
    if after_id is not None:
        params["after_id"] = after_id
    if mode and mode != "full":
        params["mode"] = mode
    if project:
        params["project"] = project
    result = await _api_get(f"/api/tasks?{urlencode(params)}")
    if isinstance(result, dict):
        # Paged/summary envelope (#254).
        tasks = result.get("tasks", [])
        next_cursor = result.get("next_cursor")
        if not tasks:
            return structured_echo_result("No tasks found.", tasks=[], next_cursor=None)
        lines = [_format_task(t) for t in tasks]
        if next_cursor is not None:
            lines.append(f"… more: pass after_id={next_cursor}")
        return structured_echo_result(
            "\n".join(lines), tasks=tasks, next_cursor=next_cursor
        )
    if not result:
        return structured_echo_result("No tasks found.", tasks=[])
    lines = [_format_task(t) for t in result]
    return structured_echo_result("\n".join(lines), tasks=result)


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
    if task.get("description"):
        parts.append(f"\nDescription:\n{task['description']}")
    if task.get("technical_hints"):
        parts.append(f"\nTechnical hints:\n{task['technical_hints']}")
    scope_in = task.get("scope_in") or []
    scope_out = task.get("scope_out") or []
    if scope_in or scope_out:
        parts.append("\nScope:")
        if scope_in:
            parts.append("  In: " + "; ".join(scope_in))
        if scope_out:
            parts.append("  Out: " + "; ".join(scope_out))
    validation = task.get("validation_commands") or []
    if validation:
        parts.append("\nValidation commands:")
        for cmd in validation:
            parts.append(f"  - {cmd}")
    if task.get("lifecycle_hint"):
        parts.append(f"\nLifecycle: {task['lifecycle_hint']}")
    latest_review = task.get("latest_review")
    if latest_review:
        freshness = (
            "current" if latest_review.get("is_current") else "stale — work resubmitted"
        )
        solo = (
            " [SELF-APPROVED: solo mode, not independent]"
            if latest_review.get("self_approved")
            else ""
        )
        parts.append(
            f"\nLatest review: {(latest_review.get('verdict') or '?').upper()} "
            f"for submission #{latest_review.get('submission_generation', 0)} "
            f"({freshness}){solo}"
        )
        for finding in (latest_review.get("findings") or [])[:10]:
            parts.append(_finding_line(finding))
    acs = task.get("acceptance_criteria") or []
    if acs:
        parts.append("\nAcceptance criteria:")
        for ac in acs:
            parts.append(
                f"  {ac.get('id', '?')} [{ac.get('verifiable_by', '?')}]\n"
                f"    Given: {ac.get('given', '')}\n"
                f"    When: {ac.get('when', '')}\n"
                f"    Then: {ac.get('then', '')}"
            )
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
        kind: Type of update: 'status', 'report', 'blocker', 'done', 'review', or 'arbitration'.
            Prefer hub_report_done for completion (kind='done' is a deprecated alias with
            the same validator and response envelope).
    """
    prior_status: str | None = None
    try:
        prior_task = await _api_get(f"/api/tasks/{task_id}")
        prior_status = prior_task.get("status")
    except HubApiError:
        prior_task = None
    try:
        result = await _api_post(
            f"/api/tasks/{task_id}/updates",
            {
                "agent": agent,
                "kind": kind,
                "content": content,
            },
        )
        task = await _api_get(f"/api/tasks/{task_id}")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    status = task.get("status", "?")
    if kind == "done":
        message = _format_hub_report_done_message(task_id, result["id"], status)
    else:
        message = f"Update #{result['id']} added to task #{task_id}."
    if task.get("lifecycle_hint"):
        message += f"\nLifecycle: {task['lifecycle_hint']}"
    response = _format_mutation_success(message, task, transition_from=prior_status)
    if kind == "done":
        # ADR-0002 Stage 1 (#325): kind=done is the deprecated alias of
        # hub_report_done.
        response = await _mark_deprecated(
            "hub_task_update kind=done", "hub_report_done", response
        )
    return response


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
    if status == "review":
        return (
            f"{base} Universal Review Gate: the done report was routed to "
            "review, not completion. A reviewer must run hub_get_review_brief "
            "and hub_submit_review; after an APPROVED verdict, report done again."
        )
    if status in ("ci_check", "needs_decision"):
        return f"{base} Task entered {status}."
    if status in ("open", "running"):
        return (
            f"{base} Status unchanged for this report "
            "(pair/open path; use pair-start or done conveyor as applicable)."
        )
    return base


def _format_hub_api_error(err: HubApiError) -> str:
    return json.dumps(enrich_error_payload(err.payload), ensure_ascii=False)


def _format_mutation_success(
    message: str,
    task: dict[str, Any],
    *,
    transition_from: str | None = None,
) -> str:
    envelope = build_mutation_envelope(
        task,
        transition_from=transition_from,
        transition_to=task.get("status"),
    )
    return merge_mutation_response(message, envelope)


async def _read_task(task_id: int) -> dict[str, Any] | None:
    try:
        return await _api_get(f"/api/tasks/{task_id}")
    except HubApiError:
        return None


async def _prior_status(task_id: int) -> str | None:
    """Read a task's status before a mutation, for ``transition.from``.

    Returns None when the task cannot be read, which the envelope renders as
    an unknown origin — honest, and better than naming the destination as the
    origin.
    """
    task = await _read_task(task_id)
    return task.get("status") if task else None


async def _task_mutation_response(
    task_id: int,
    message: str,
    *,
    prior_status: str | None,
    task: dict[str, Any] | None,
    fallback_status: str | None = None,
) -> str:
    body = task or {"id": task_id, "status": fallback_status or "?"}
    return _format_mutation_success(message, body, transition_from=prior_status)


@mcp.tool()
async def hub_report_done(task_id: int, summary: str, agent: str = "") -> str:
    """Submit a done report and return the task's actual status after lifecycle handling.

    Universal Review Gate (#306): a done report completes a task only when
    the current submission already carries an APPROVED review (or the task
    explicitly opted out via auto_review=false). Otherwise the report is
    treated as a submission — the task routes to ``review`` (client-driven)
    or ``ci_check`` (branch conveyor) and the response names the next
    action. The response always states the real status and never implies
    ``completed`` unless the task is actually completed.

    Args:
        task_id: The task ID to report on
        summary: What was changed and how it was validated
        agent: Name of the agent submitting the report
    """
    prior_status: str | None = None
    try:
        prior_task = await _api_get(f"/api/tasks/{task_id}")
        prior_status = prior_task.get("status")
    except HubApiError:
        prior_status = None
    try:
        result = await _api_post(
            f"/api/tasks/{task_id}/updates",
            {
                "agent": agent,
                "kind": "done",
                "content": summary,
            },
        )
        task = await _api_get(f"/api/tasks/{task_id}")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    status = task.get("status", "?")
    msg = _format_hub_report_done_message(task_id, result["id"], status)
    if task.get("lifecycle_hint"):
        msg += f"\nLifecycle: {task['lifecycle_hint']}"
    return _format_mutation_success(msg, task, transition_from=prior_status)


# ---------------------------------------------------------------------------
# Hierarchy: tree, context
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_task_tree(
    task_id: int,
    depth: int | None = None,
    max_nodes: int | None = None,
    max_chars: int | None = None,
    mode: str = "full",
) -> CallToolResult:
    """Get the hierarchy tree for a task/epic/feature with all descendants and progress.

    Without limit parameters the full tree is returned (backward compatible).
    ``mode=summary`` applies defaults ``depth=2`` and ``max_nodes=50``.
    When output is cut, the text ends with ``[truncated]``.

    Args:
        task_id: The root task ID to build tree from
        depth: Maximum depth from root (0 = root only)
        max_nodes: Maximum number of nodes to include
        max_chars: Maximum UTF-8 character length of rendered text
        mode: ``full`` (default) or ``summary`` (soft caps for large epics)
    """
    query = _tree_query_string(
        depth=depth,
        max_nodes=max_nodes,
        mode=mode,
    )
    tree = await _api_get(f"/api/tasks/{task_id}/tree{query}")
    options = TreeOutputOptions(
        depth=depth,
        max_nodes=max_nodes,
        max_chars=max_chars,
        mode=mode if mode in ("full", "summary") else "full",
    )
    rendered = render_task_tree(tree, options)
    return structured_echo_result(rendered.text, tree=tree)


_CONTEXT_MODES = ("full", "summary")
# Agents routinely guess ``brief`` for a shorter digest; accept it as an alias
# of ``summary`` instead of failing with a raw pattern-mismatch error (#454).
_CONTEXT_MODE_ALIASES = {"brief": "summary"}


def _normalize_context_mode(mode: str) -> str:
    """Coerce a context ``mode`` to a supported value or raise a clear error."""
    normalized = (mode or "full").strip().lower()
    normalized = _CONTEXT_MODE_ALIASES.get(normalized, normalized)
    if normalized not in _CONTEXT_MODES:
        allowed = ", ".join(_CONTEXT_MODES)
        raise ValueError(
            f"Invalid mode {mode!r}. Allowed values: {allowed} "
            f"('brief' is accepted as an alias of 'summary')."
        )
    return normalized


async def _general_hub_context(*, max_chars: int | None, mode: str) -> CallToolResult:
    """General Hub context for an agent with no active task (#454).

    Combines the connected instance, the caller's identity, their active
    (claimed) tasks, and the Workflow reference into one digest — the thing to
    read when onboarding a session before any task is claimed.
    """
    instance = instance_echo_fields()
    identity: dict[str, Any] = {}
    my_tasks: list[dict[str, Any]] = []
    try:
        # Diagnostics carries identity + the active workspace mode (#530), so an
        # onboarding agent sees whether worktree isolation is in effect.
        identity = await _api_get("/api/diagnostics/identity")
    except HubApiError:
        try:
            identity = await _api_get("/api/whoami")
        except HubApiError:
            identity = {}
    username = (identity.get("username") or "").strip()
    if username:
        try:
            my_tasks = await _api_get(
                f"/api/tasks?claimed_by={urllib.parse.quote(username)}&limit=50"
            )
        except HubApiError:
            my_tasks = []
        if isinstance(my_tasks, dict):  # paginated envelope shape
            my_tasks = my_tasks.get("tasks", [])

    lines = ["## Hub Context (no task)"]
    lines.append(f"Instance: {instance['instance']} ({instance['base_url']})")
    if identity:
        lines.append(
            f"Identity: {username or 'anonymous'} "
            f"(role={identity.get('role', '?')}, "
            f"principal_id={identity.get('principal_id')})"
        )
    else:
        lines.append("Identity: unavailable")
    workspace_mode = identity.get("workspace_mode") or "legacy"
    lines.append(
        f"Workspace mode: {workspace_mode}"
        + (
            " — pair-start gives each task its own git worktree; "
            "hub_pair_start returns the path"
            if workspace_mode == "worktree"
            else " — single shared working tree (branch switching)"
        )
    )
    if my_tasks:
        task_strs = [
            f"#{t.get('id')} {t.get('title', '')} ({t.get('status', '?')})"
            for t in my_tasks[:20]
        ]
        lines.append("Your claimed tasks: " + "; ".join(task_strs))
    else:
        lines.append("Your claimed tasks: none")
    lines.append("")
    lines.extend(lifecycle_map_lines())

    text = "\n".join(lines)
    effective_max = max_chars
    if mode == "summary" and effective_max is None:
        effective_max = 4000
    if effective_max is not None:
        text, truncated = truncate_text(text, effective_max)
        if truncated and TRUNCATION_NOTICE not in text:
            text = f"{text}\n{TRUNCATION_NOTICE}" if text else TRUNCATION_NOTICE
    return structured_echo_result(text, identity=identity, my_tasks=my_tasks)


@mcp.tool()
async def hub_my_context(
    task_id: int | None = None,
    max_chars: int | None = None,
    mode: str = "full",
) -> CallToolResult:
    """Get work context for an agent: hierarchy breadcrumb, siblings, progress, children.

    Call it before starting work on a task to understand its place in the project.
    Omit ``task_id`` (or pass null) to get the general Hub context instead — the
    Workflow reference plus your own active tasks and the connected instance — which
    is what to read when you have no active task yet.

    Without ``max_chars`` the full digest is returned (backward compatible).
    ``mode=summary`` caps the digest to 4000 chars unless ``max_chars`` is set
    explicitly. Truncated output ends with ``[truncated]``.

    Args:
        task_id: The task ID to get context for. Omit for general Hub context.
        max_chars: Maximum UTF-8 character length of the digest
        mode: ``full`` (default) or ``summary``. ``brief`` is accepted as an
            alias of ``summary``; any other value is rejected with the allowed set.
    """
    try:
        mode = _normalize_context_mode(mode)
    except ValueError as exc:
        return structured_echo_result(str(exc), error="invalid_mode")

    if task_id is None:
        return await _general_hub_context(max_chars=max_chars, mode=mode)

    query = _tree_query_string(max_chars=max_chars, mode=mode)
    ctx = await _api_get(f"/api/tasks/{task_id}/context{query}")
    text = ctx.get("context_text", f"Context for task #{task_id} not available.")
    effective_max = max_chars
    if mode == "summary" and effective_max is None:
        effective_max = 4000
    if effective_max is not None:
        text, truncated = truncate_text(text, effective_max)
        if truncated and TRUNCATION_NOTICE not in text:
            text = f"{text}\n{TRUNCATION_NOTICE}" if text else TRUNCATION_NOTICE
    return structured_echo_result(text, context=ctx)


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
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    body: dict[str, Any] = {"comment": comment, "run": run, "force": force}
    if runtime:
        body["runtime"] = runtime
    try:
        result = await _api_post(f"/api/tasks/{task_id}/approve", body)
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    task = await _read_task(task_id)
    status = (task or result).get("status", "?")
    message = f"Task #{task_id} approved (status: {status})."
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=task,
        fallback_status=status,
    )


@mcp.tool()
async def hub_reject_task(task_id: int, comment: str = "") -> str:
    """Reject a draft task (proposed by agent).

    Args:
        task_id: The draft task ID to reject
        comment: Reason for rejection
    """
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    try:
        await _api_post(f"/api/tasks/{task_id}/reject", {"comment": comment})
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    task = await _read_task(task_id)
    message = f"Task #{task_id} rejected."
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=task,
        fallback_status="rejected",
    )


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
    prior_status: str | None = None
    try:
        prior_task = await _api_get(f"/api/tasks/{task_id}")
        prior_status = prior_task.get("status")
    except HubApiError:
        prior_status = None
    body: dict[str, Any] = {}
    if plan:
        body["plan"] = plan
    if runtime:
        body["runtime"] = runtime
    try:
        result = await _api_post(f"/api/tasks/{task_id}/start", body)
        task = await _api_get(f"/api/tasks/{task_id}")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    status = task.get("status", result.get("status", "?"))
    job_id = task.get("job_id", result.get("job_id", "-"))
    message = f"Task #{task_id} dispatched (status: {status}, job: {job_id})."
    return _format_mutation_success(message, task, transition_from=prior_status)


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

    If the task is already claimed, ``assigned_agent`` must match the claim
    holder's name — i.e. the ``agent`` you passed to hub_claim_task. A mismatch
    returns a structured ``pair_start_claim_mismatch`` error naming the holder
    and the identity the server resolved. Pair-start by the same authenticated
    principal is accepted even when the presentational name differs.

    Workspace mode (#530/#459): when the server runs with
    OPENCLAW_WORKTREE_PER_TASK=1 the response names your task's isolated git
    worktree path — work THERE, not in the shared clone (the main clone stays
    on the base branch). In legacy mode the response is unchanged.

    Args:
        task_id: The open task ID to pair-start
        plan: Work plan if none exists yet (kind='status' content starting with 'Plan:')
        assigned_agent: Agent name to record on the task; for a claimed task it
            must equal the claim holder (hub_claim_task agent). Empty uses caller
            identity, which may not match the holder — pass it explicitly.
        branch_slug: Optional branch slug (task-<id>/<slug>). Empty uses title slug.
    """
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    body: dict[str, Any] = {}
    if plan:
        body["plan"] = plan
    if assigned_agent:
        body["assigned_agent"] = assigned_agent
    if branch_slug:
        body["branch_slug"] = branch_slug
    try:
        result = await _api_post(f"/api/tasks/{task_id}/pair-start", body or None)
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    task = await _read_task(task_id)
    status = (task or result).get("status", "?")
    branch = (task or result).get("branch") or "-"
    agent_name = (task or result).get("assigned_agent") or "-"
    job_id = (task or result).get("job_id")
    job_note = "no dispatch job" if not job_id else f"job: {job_id}"
    message = (
        f"Task #{task_id} pair-started (status: {status}, branch: {branch}, "
        f"agent: {agent_name}, {job_note})."
    )
    # Worktree isolation (#530): the mode/path live on the pair-start response
    # (not the DB re-read), so read them from `result` and tell the agent where
    # its isolated tree is — otherwise it would keep working in the shared clone.
    # #615: print what the server computed about the statement's age. Rendering
    # only — the computation is server-side so CLI and REST see it too.
    freshness = (result or {}).get("statement_freshness")
    if freshness:
        from hub.services.statement_freshness import render_freshness

        block = render_freshness(freshness)
        if block:
            message += f"\n{block}"
    workspace_mode = (result or {}).get("workspace_mode") or "legacy"
    worktree_path = (result or {}).get("worktree_path") or ""
    if workspace_mode == "worktree" and worktree_path:
        message += (
            f"\nWorkspace mode: worktree — your isolated working tree is at "
            f"{worktree_path}. Work THERE; the main clone stays on the base branch."
        )
    # The branch name is an obligation, not a note (#533). It used to appear
    # only inside the summary line above, which reads as "here is what we
    # recorded" — and the policy document said the local name MAY differ. When
    # it does, the task points at one branch while the work happens in
    # another, so CI and the reviewer look at code nobody wrote.
    if branch and branch != "-" and not job_id:
        message += (
            f"\nCanonical branch: {branch}. Create or switch to exactly this "
            "name locally — submit_for_review compares what you report against "
            "it and refuses a mismatch."
        )
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=task,
        fallback_status=status,
    )


@mcp.tool()
async def hub_submit_for_review(
    task_id: int,
    agent: str = "",
    summary: str = "",
    branch: str = "",
) -> str:
    """Submit the current work of a pair task for client-driven review (#307).

    Moves a running pair task (no dispatch job) into status=review and bumps
    the submission generation, which invalidates any earlier APPROVED
    verdict. This does NOT complete the task: after review, an APPROVED
    verdict returns it to running for the normal done path, and
    CHANGES_REQUESTED returns it to running for fixes.

    Args:
        task_id: The running pair task ID
        agent: Name of the submitting agent (empty uses task's assigned agent)
        summary: Short note on what is being submitted
        branch: The branch you actually worked in. Compared against the
            canonical name pair-start gave you; a mismatch is refused with
            both names and a way to fix it. Omitting it skips the check —
            the hub cannot see your working copy, so this is your report,
            not its observation (#533).
    """
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    body: dict[str, Any] = {}
    if agent:
        body["agent"] = agent
    if summary:
        body["summary"] = summary
    if branch:
        body["branch"] = branch
    try:
        task = await _api_post(f"/api/tasks/{task_id}/submit-review", body or None)
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    generation = task.get("submission_generation", 0)
    message = (
        f"Task #{task_id} submitted for review (submission #{generation}, "
        f"status: {task.get('status', '?')}). Awaiting reviewer verdict via "
        "hub_submit_review."
    )
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=task,
    )


@mcp.tool()
async def hub_get_review_brief(task_id: int) -> CallToolResult:
    """Get the full review brief for a task: everything a reviewer needs (#308).

    Returns acceptance criteria, scope, validation commands, review
    checklist, branch/PR metadata with an advisory diff command, the latest
    submission summary, and the latest recorded verdict with findings.

    Fail-fast self-review check (#433): if YOU implemented this task, the
    response starts with a self_review_warning — stop and hand the review
    to an independent reviewer instead of running it (hub_submit_review
    would reject your verdict).

    Args:
        task_id: The task ID to review
    """
    try:
        brief = await _api_get(f"/api/tasks/{task_id}/review-brief")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    parts = []
    # #433: fail-fast self-review notice goes FIRST so the reviewer stops
    # before spending review effort — hub_submit_review would reject anyway.
    warning = brief.get("self_review_warning")
    if warning:
        parts.append(
            f"WARNING [{warning.get('reason', 'self_review_forbidden')}]: "
            f"{warning.get('message', '')}\n{warning.get('hint', '')}\n"
        )
    parts.extend(
        [
            f"Review brief for task #{brief['task_id']}: {brief['title']}",
            f"Status: {brief['status']} | submission #{brief.get('submission_generation', 0)} "
            f"| review cycle {brief.get('review_cycle', 0)}",
        ]
    )
    if brief.get("description"):
        parts.append(f"\nDescription:\n{brief['description']}")
    acs = brief.get("acceptance_criteria") or []
    if acs:
        parts.append("\nAcceptance criteria:")
        for ac in acs:
            parts.append(
                f"  {ac.get('id', '?')} [{ac.get('verifiable_by', '?')}] "
                f"Given: {ac.get('given', '')} | When: {ac.get('when', '')} "
                f"| Then: {ac.get('then', '')}"
            )
    if brief.get("scope_in"):
        parts.append("\nIn scope: " + "; ".join(brief["scope_in"]))
    if brief.get("scope_out"):
        parts.append("Out of scope: " + "; ".join(brief["scope_out"]))
    if brief.get("out_of_scope_for_review"):
        parts.append(
            "Out of scope for review: " + "; ".join(brief["out_of_scope_for_review"])
        )
    if brief.get("review_checklist"):
        parts.append("\nReview checklist:")
        for item in brief["review_checklist"]:
            parts.append(f"  - {item}")
    if brief.get("validation_commands"):
        parts.append("\nValidation commands:")
        for cmd in brief["validation_commands"]:
            parts.append(f"  - {cmd}")
    if brief.get("constraints"):
        parts.append("\nConstraints: " + "; ".join(brief["constraints"]))
    if brief.get("technical_hints"):
        parts.append(f"\nTechnical hints:\n{brief['technical_hints']}")
    if brief.get("branch"):
        pr = f" | PR #{brief['pr_number']}" if brief.get("pr_number") else ""
        parts.append(f"\nBranch: {brief['branch']}{pr}")
        if brief.get("diff_command"):
            parts.append(f"Diff: {brief['diff_command']}")
    if brief.get("stacking_warning"):
        parts.append(f"\n{brief['stacking_warning']}")
    if brief.get("latest_submission_summary"):
        parts.append(f"\nLatest submission:\n{brief['latest_submission_summary']}")
    latest_review = brief.get("latest_review")
    if latest_review:
        freshness = (
            "current" if latest_review.get("is_current") else "stale — work resubmitted"
        )
        solo = (
            " [SELF-APPROVED: solo mode, not independent]"
            if latest_review.get("self_approved")
            else ""
        )
        parts.append(
            f"\nLatest verdict: {(latest_review.get('verdict') or '?').upper()} "
            f"for submission #{latest_review.get('submission_generation', 0)} "
            f"({freshness}){solo}"
        )
        for finding in (latest_review.get("findings") or [])[:20]:
            parts.append(_finding_line(finding))
    parts.append(
        "\nSubmit the verdict with hub_submit_review "
        "(verdict=approved|changes_requested, findings for changes_requested; "
        "changes_requested needs at least one scope=in_scope finding)."
    )
    return structured_echo_result("\n".join(parts), brief=brief)


@mcp.tool()
async def hub_submit_review(
    task_id: int,
    verdict: str,
    comments: str = "",
    agent: str = "",
    findings: list[dict[str, Any]] | None = None,
    create_tasks_for_out_of_scope: bool = False,
) -> str:
    """Submit a review verdict for the current submission of a task (#307).

    Records the verdict bound to the current submission generation. This
    does NOT complete the task: for client-driven review the task returns
    to running — with APPROVED the developer proceeds to the normal done
    path, with CHANGES_REQUESTED the developer fixes the findings and
    resubmits via hub_submit_for_review.

    Finding scope (#435): every finding carries scope
    (in_scope|out_of_scope, default in_scope). changes_requested with
    findings requires at least one in_scope finding — if everything is out
    of scope, submit approved and keep out-of-scope findings as
    recommendations linked to follow-up tasks. Out-of-scope findings
    without linked_task_id get a non-blocking warning.

    Auto-drafts (#436): create_tasks_for_out_of_scope=true auto-creates a
    DRAFT follow-up task for every out_of_scope finding without
    linked_task_id (same feature parent as the reviewed task when
    applicable) and stamps the created id into the stored finding. Drafts
    still need human DoR approval. Idempotent on resubmit.

    Args:
        task_id: The task under review
        verdict: 'approved' or 'changes_requested'
        comments: Free-text review summary
        agent: Reviewer agent name
        findings: For changes_requested — list of dicts with id (int, stable
            within this submission), severity (high|medium|low), message,
            and optional file, line, recommendation,
            scope (in_scope|out_of_scope, default in_scope),
            linked_task_id (int — follow-up task for out_of_scope findings).
        create_tasks_for_out_of_scope: Auto-create draft follow-up tasks
            for unlinked out_of_scope findings (default false).
    """
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    body: dict[str, Any] = {"verdict": verdict}
    if comments:
        body["comments"] = comments
    if agent:
        body["agent"] = agent
    if findings:
        body["findings"] = findings
    if create_tasks_for_out_of_scope:
        body["create_tasks_for_out_of_scope"] = True
    try:
        task = await _api_post(f"/api/tasks/{task_id}/review-verdict", body)
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    n_findings = len(findings or [])
    findings_note = f", {n_findings} finding(s)" if n_findings else ""
    message = (
        f"Review verdict {verdict.upper()} recorded for task #{task_id}"
        f"{findings_note} (status: {task.get('status', '?')})."
    )
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=task,
    )


@mcp.tool()
async def hub_claim_task(
    task_id: int,
    agent: str,
    session_id: str = "",
) -> str:
    """Claim an open task for one Cursor agent/session (409 if already claimed).

    Remember the ``agent`` name you pass here: a later hub_pair_start must use
    the same value as its ``assigned_agent`` (the name is how the holder is
    matched), unless you pair-start under the same authenticated principal.

    Args:
        task_id: The open task ID
        agent: Agent name taking the claim; reuse it as assigned_agent in hub_pair_start
        session_id: Optional Cursor session id for conflict detection
    """
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    try:
        result = await _api_post(
            f"/api/tasks/{task_id}/claim",
            {"agent": agent, "session_id": session_id},
        )
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    task = await _read_task(task_id)
    status = (task or result).get("status", "?")
    holder = (task or result).get("claimed_by") or agent
    message = f"Task #{task_id} claimed (status: {status}, claimed_by: {holder})."
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=task,
        fallback_status=status,
    )


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
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    try:
        result = await _api_post(
            f"/api/tasks/{task_id}/release",
            {"agent": agent, "session_id": session_id},
        )
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    task = await _read_task(task_id)
    status = (task or result).get("status", "?")
    message = f"Task #{task_id} claim released (status: {status})."
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=task,
        fallback_status=status,
    )


@mcp.tool()
async def hub_force_complete_task(task_id: int, comment: str = "") -> str:
    """Human force-completes a stuck task without an agent done report.

    Audited override for any non-terminal ``task`` or ``subtask`` when no *active*
    dispatch job backs ``job_id`` or ``review_job_id`` (409 if active). Missing
    or terminal dispatch jobs are allowed and noted in the audit trail. A
    non-empty ``comment`` is required for active lifecycle states other than
    ``pending_report`` and ``claimed``; those two may use the default message.
    Rejects terminal tasks and ``epic``/``feature`` rows with incomplete
    descendants.

    Args:
        task_id: Task or subtask to complete
        comment: Audit-trail reason; required for most active lifecycle states
    """
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    body = {"comment": comment} if comment else None
    try:
        result = await _api_post(f"/api/tasks/{task_id}/force-complete", body)
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    task = await _read_task(task_id)
    status = (task or result).get("status", "?")
    message = f"Task #{task_id} force-completed (status: {status})."
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=task,
        fallback_status=status,
    )


@mcp.tool()
async def hub_archive_task(task_id: int, cascade: bool = True) -> str:
    """Hide a task from default lists (optional subtree cascade).

    Args:
        task_id: Task to archive
        cascade: If True, archive the whole subtree. If False, only this row.
    """
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    try:
        result = await _api_post(
            f"/api/tasks/{task_id}/archive",
            {"cascade": cascade},
        )
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    task = await _read_task(task_id)
    st = (task or result).get("status", "?")
    message = f"Task #{task_id} archived (status in response: {st})."
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=task,
        fallback_status=st,
    )


@mcp.tool()
async def hub_withdraw_own_draft(task_id: int) -> str:
    """Withdraw (archive) your own agent draft without children.

    Narrow agent-only path — does not replace hub_archive_task for humans.

    Args:
        task_id: Draft task you created (source=agent, assigned to you).
    """
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    try:
        result = await _api_post(f"/api/tasks/{task_id}/withdraw")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    task = await _read_task(task_id)
    st = (task or result).get("status", "?")
    archived = (task or result).get("archived", True)
    message = f"Draft task #{task_id} withdrawn (archived={archived}, status: {st})."
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=task,
        fallback_status=st,
    )


@mcp.tool()
async def hub_unarchive_task(task_id: int, cascade: bool = True) -> str:
    """Restore archived tasks (optional subtree cascade).

    Args:
        task_id: Task to unarchive
        cascade: If True, unarchive the whole subtree. If False, only this row.
    """
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    try:
        result = await _api_post(
            f"/api/tasks/{task_id}/unarchive",
            {"cascade": cascade},
        )
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    task = await _read_task(task_id)
    st = (task or result).get("status", "?")
    message = f"Task #{task_id} unarchived (status in response: {st})."
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=task,
        fallback_status=st,
    )


@mcp.tool()
async def hub_delete_task(task_id: int) -> str:
    """Permanently delete a task and all descendants (irreversible).

    Args:
        task_id: Root of the subtree to remove from the database.
    """
    prior_task = await _read_task(task_id)
    prior_status = prior_task.get("status") if prior_task else None
    try:
        await _api_delete(f"/api/tasks/{task_id}")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    message = f"Task #{task_id} and its descendants were deleted."
    return await _task_mutation_response(
        task_id,
        message,
        prior_status=prior_status,
        task=prior_task,
        fallback_status=prior_status or "?",
    )


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
    # Read the status BEFORE the call: afterwards the task has already moved,
    # and transition.from would report the destination as the origin (#369).
    prior_status = await _prior_status(task_id)
    try:
        await _api_post(
            f"/api/tasks/{task_id}/question",
            {
                "agent": agent,
                "question": question,
            },
        )
        task = await _api_get(f"/api/tasks/{task_id}")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    message = (
        f"Question posted on task #{task_id}. Task is now paused (needs_info). "
        "Waiting for human answer."
    )
    return _format_mutation_success(message, task, transition_from=prior_status)


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
    prior_status = await _prior_status(task_id)
    try:
        result = await _api_post(
            f"/api/tasks/{task_id}/answer",
            {
                "answer": answer,
                "resume": resume,
            },
        )
        task = await _api_get(f"/api/tasks/{task_id}")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    status = task.get("status", result.get("status", "?"))
    message = f"Answer posted on task #{task_id} (status: {status})."
    return _format_mutation_success(message, task, transition_from=prior_status)


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
    prior_status: str | None = None
    try:
        prior_task = await _api_get(f"/api/tasks/{task_id}")
        prior_status = prior_task.get("status")
    except HubApiError:
        prior_status = None
    try:
        result = await _api_post(f"/api/tasks/{task_id}/decide", body)
        task = await _api_get(f"/api/tasks/{task_id}")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    status = task.get("status", result.get("status", "?"))
    suffix = ""
    if decision_summary:
        suffix = " (decision recorded)"
    message = (
        f"Task #{task_id}: decision '{action}' applied (status: {status}).{suffix}"
    )
    return _format_mutation_success(message, task, transition_from=prior_status)


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
    task_type: str = "task",
    project: str = "",
) -> str:
    """Propose new work for human approval (used by agents). Creates a DRAFT.

    Since #323 agents can propose the full decomposition: task, subtask,
    feature (parent = epic), or epic. Everything an agent proposes starts
    as a draft — the human approval gate owns the hierarchy.

    Args:
        title: Short title of the proposed work
        description: What needs to be done and why
        agent: Name of the proposing agent
        rationale: Why this work is needed
        parent_id: Parent task ID (feature → epic, task → feature, subtask → task)
        human_owner: Person who owns / is accountable for this work
        human_reviewer: Person who will review and accept the result
        task_type: task (default), subtask, feature, or epic
        project: Project slug — only when proposing an epic (#346)
    """
    if task_type not in ("task", "subtask", "feature", "epic"):
        return format_echo_response(
            f"Invalid task_type {task_type!r}: use task, subtask, feature, or epic."
        )
    body: dict[str, Any] = {
        "title": title,
        "description": description,
        "source": "agent",
        "agent": agent,
        "rationale": rationale,
        "human_owner": human_owner,
        "human_reviewer": human_reviewer,
        "task_type": task_type,
    }
    if project:
        body["project"] = project
    if parent_id is not None:
        body["parent_id"] = parent_id
    result = await _api_post("/api/tasks", body)
    return format_echo_response(
        f"Draft {task_type} #{result['id']} created. Awaiting human approval."
    )


@mcp.tool()
async def hub_list_projects(include_archived: bool = False) -> CallToolResult:
    """List projects (#338): slug, repo, workspace, base branch.

    Args:
        include_archived: Include archived projects.
    """
    query = "?include_archived=true" if include_archived else ""
    projects = await _api_get(f"/api/projects{query}")
    if not projects:
        return structured_echo_result("No projects.", projects=[])
    lines = [
        f"{p['slug']}: {p['name']} | repo={p.get('repo') or '-'} "
        f"| base={p.get('default_branch', 'develop')}"
        + (" [archived]" if p.get("archived") else "")
        for p in projects
    ]
    return structured_echo_result("\n".join(lines), projects=projects)


@mcp.tool()
async def hub_submit_machine_review(
    task_id: int,
    raw_count: int,
    incomplete: bool,
    findings_confirmed: list[dict[str, Any]] | None = None,
    findings_rejected: list[dict[str, Any]] | None = None,
    unresolved: list[dict[str, Any]] | None = None,
    lost_dimensions: list[str] | None = None,
    harness_skill: str = "multi-agent-review",
    harness_version: int | None = None,
    agent_count: int | None = None,
    tokens_spent: int | None = None,
    duration_ms: int | None = None,
    orchestrator: str = "",
    model: str = "",
    agent: str = "",
) -> CallToolResult:
    """Submit a structured multi-agent review report (#381).

    Bound to the task's current submission generation — resubmitting work
    makes the report stale (like human verdicts). Metrics fields (#384)
    are optional but strongly encouraged: tokens_spent/duration_ms feed
    the practice economics.

    Honest incompleteness (#549) is not optional: ``incomplete`` is REQUIRED
    and has no default. A missing voice never equals a missing defect, so
    "0 confirmed" only means anything next to ``incomplete=False``. A finding
    nobody managed to judge belongs in ``unresolved``, NOT in
    ``findings_rejected`` — "nobody voted" and "someone refuted it" are
    opposite outcomes.

    Args:
        task_id: Reviewed task.
        raw_count: Findings before adversarial verification.
        incomplete: True when any agent died, any dimension was lost, context
            was truncated, or a budget ran out. No default on purpose: a
            silently-defaulted False is how a run with dead agents reads clean.
        findings_confirmed: [{title, severity, category?, file?, line?, detail?}]
        findings_rejected: [{title, category?, reason?}] — actually refuted.
        unresolved: [{title, why}] — nobody could judge these. Never rejected.
        lost_dimensions: names of dimensions that returned nothing.
        harness_skill: Skill name used (hub_get_skill source).
        harness_version: Skill version executed.
        agent_count: Total subagents in the run.
        tokens_spent: Tokens consumed by the run.
        duration_ms: Wall-clock duration.
        orchestrator: Client/orchestrator name (e.g. claude-code-workflow).
        model: Model id used by review agents.
        agent: Submitting agent name.
    """
    body: dict[str, Any] = {
        "raw_count": raw_count,
        "findings_confirmed": findings_confirmed or [],
        "findings_rejected": findings_rejected or [],
        "incomplete": incomplete,
        "unresolved": unresolved or [],
        "lost_dimensions": lost_dimensions or [],
        "harness_skill": harness_skill,
        "orchestrator": orchestrator,
        "model": model,
        "agent": agent,
    }
    for key, value in (
        ("harness_version", harness_version),
        ("agent_count", agent_count),
        ("tokens_spent", tokens_spent),
        ("duration_ms", duration_ms),
    ):
        if value is not None:
            body[key] = value
    try:
        result = await _api_post(f"/api/tasks/{task_id}/machine-review", body)
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    confirmed = len(result.get("findings_confirmed") or [])
    rejected = len(result.get("findings_rejected") or [])
    # Every number in this line comes from what was STORED, not from what was
    # sent. Intake normalises raw_count upward when a report claims fewer raw
    # findings than it lists (#519), and echoing the input here would confirm
    # a number the record does not hold — which the agent then quotes into the
    # task log. A task about trustworthy numbers cannot ship a receipt that
    # disagrees with the row it describes.
    stored_raw = result.get("raw_count", raw_count)
    return structured_echo_result(
        f"Machine review for task #{task_id} recorded (submission "
        f"#{result.get('submission_generation')}): {stored_raw} raw → "
        f"{confirmed} confirmed / {rejected} rejected.",
        machine_review=result,
    )


@mcp.tool()
async def hub_practice_metrics(since_days: int = 90) -> CallToolResult:
    """Practice metrics (#384): machine-review economics, harness-version
    comparison, recurring finding categories, task cycle times.

    Args:
        since_days: Aggregation window in days (default 90).
    """
    try:
        data = await _api_get(f"/api/metrics/practices?since_days={since_days}")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    mr = data.get("machine_reviews", {})
    lines = [
        f"Machine reviews ({data.get('since_days')}d): {mr.get('reviews', 0)} "
        f"runs, {mr.get('raw_total', 0)} raw → {mr.get('confirmed_total', 0)} "
        f"confirmed / {mr.get('rejected_total', 0)} rejected",
        f"Tokens: {mr.get('tokens_total', 0)} total, "
        f"{mr.get('tokens_per_confirmed') or '—'} per confirmed finding",
    ]
    recurring = [c for c in data.get("recurring_categories", []) if c.get("recurring")]
    if recurring:
        lines.append(
            "Recurring categories (checklist candidates): "
            + ", ".join(f"{c['category']} ({c['tasks']} tasks)" for c in recurring[:5])
        )
    return structured_echo_result("\n".join(lines), metrics=data)


@mcp.tool()
async def hub_list_skills() -> CallToolResult:
    """List the skills library (#380): latest version per name.

    Skills are versioned prompts/checklists/workflows agents pull from
    the hub instead of carrying them in session memory.
    """
    skills = await _api_get("/api/skills")
    if not skills:
        return structured_echo_result("No skills in the library.", skills=[])
    lines = [
        f"{s['name']} v{s['version']} [{s['kind']}, {s['status']}]"
        + (f" tags={','.join(s.get('tags') or [])}" if s.get("tags") else "")
        for s in skills
    ]
    return structured_echo_result("\n".join(lines), skills=skills)


@mcp.tool()
async def hub_get_skill(name: str) -> CallToolResult:
    """Fetch the ACTIVE version of a skill — the content to execute.

    Args:
        name: Skill slug, e.g. "multi-agent-review".
    """
    try:
        skill = await _api_get(f"/api/skills/{name}")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    return structured_echo_result(
        f"{skill['name']} v{skill['version']} [{skill['kind']}]\n\n{skill['content']}",
        skill=skill,
    )


@mcp.tool()
async def hub_propose_skill(
    name: str,
    content: str,
    kind: str = "prompt",
    tags: str = "",
) -> CallToolResult:
    """Propose a new skill version (#380). Created as DRAFT — a human
    activates it via the UI or PATCH; the active version stays untouched.

    Args:
        name: Skill slug (a-z, 0-9, dashes). Existing name = next version.
        content: Full markdown content of the skill/prompt.
        kind: prompt (default) | skill | checklist | workflow.
        tags: Comma-separated tags.
    """
    body: dict[str, Any] = {"name": name, "content": content, "kind": kind}
    if tags:
        body["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    try:
        skill = await _api_post("/api/skills", body)
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    return structured_echo_result(
        f"Skill {skill['name']} v{skill['version']} proposed "
        f"(status: {skill['status']}). A human activates it.",
        skill=skill,
    )


@mcp.tool()
async def hub_provision_project(project_id: int) -> CallToolResult:
    """Clone/verify a project's workspace on the hub server (#348).

    Human-only gate (like project activation): provisioning touches the
    server filesystem and git credentials, so agent tokens get 403.
    The outcome is always readable — provision_status ok|error plus a
    detail explaining WHY (missing deploy key, wrong origin, no repo).

    Args:
        project_id: Numeric project id (see hub_list_projects).
    """
    try:
        result = await _api_post(f"/api/projects/{project_id}/provision")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    status_value = result.get("provision_status", "?")
    detail = result.get("provision_detail", "")
    project = result.get("project") or {}
    return structured_echo_result(
        f"Provision {project.get('slug', project_id)}: {status_value} — {detail}",
        provision_status=status_value,
        provision_detail=detail,
        project=project,
    )


@mcp.tool()
async def hub_wait_events(
    since: int = 0,
    wait: int = 30,
    kinds: str = "",
) -> CallToolResult:
    """Wait for typed hub events past a cursor (#349) — the agent half of
    the «human pressed a button → agent continues» loop.

    Long-polls GET /api/events: returns immediately when events with
    id > ``since`` exist, otherwise blocks up to ``wait`` seconds (server
    caps at 60). An empty result is normal — repeat with the same cursor.

    Args:
        since: Last seen event id (0 starts from the whole feed).
        wait: Long-poll seconds, 0 returns immediately.
        kinds: Comma-separated filter, e.g. "review_verdict_recorded,task_approved".
    """
    from urllib.parse import urlencode

    params = {"since": since, "wait": wait}
    if kinds:
        params["kinds"] = kinds
    result = await _api_get(
        f"/api/events?{urlencode(params)}", timeout=min(wait, 60) + 20
    )
    events = result.get("events", [])
    next_cursor = result.get("next_cursor", since)
    if not events:
        return structured_echo_result(
            f"No events (cursor {next_cursor}). Repeat hub_wait_events with "
            f"since={next_cursor}.",
            events=[],
            next_cursor=next_cursor,
        )
    lines = []
    for e in events:
        target = f"task #{e['task_id']}" if e.get("task_id") else ""
        if e.get("project_id"):
            target = (target + f" project #{e['project_id']}").strip()
        lines.append(f"[{e['id']}] {e['kind']} {target} {e.get('payload') or ''}")
    return structured_echo_result(
        "\n".join(lines), events=events, next_cursor=next_cursor
    )


@mcp.tool()
async def hub_create_project(
    slug: str,
    name: str,
    repo: str = "",
    workspace_path: str = "",
    default_branch: str = "develop",
) -> str:
    """Create a project (#338). HUMAN-ONLY: projects define git routing;
    agent tokens receive human_only_gate.

    Args:
        slug: URL-safe unique slug (lowercase, digits, dashes)
        name: Display name
        repo: GitHub owner/repo for PRs
        workspace_path: Server workspace clone path
        default_branch: Integration branch (default develop)
    """
    body = {
        "slug": slug,
        "name": name,
        "repo": repo,
        "workspace_path": workspace_path,
        "default_branch": default_branch,
    }
    try:
        result = await _api_post("/api/projects", body)
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    return format_echo_response(f"Project {result['slug']} (#{result['id']}) created.")


@mcp.tool()
async def hub_propose_project(
    slug: str,
    name: str,
    repo: str = "",
    workspace_path: str = "",
    default_branch: str = "develop",
) -> str:
    """Propose a project (#345): created as PENDING until a human
    activates it — pending projects stay out of git routing.

    Args:
        slug: URL-safe unique slug
        name: Display name
        repo: GitHub owner/repo
        workspace_path: Server workspace clone path
        default_branch: Integration branch
    """
    body = {
        "slug": slug,
        "name": name,
        "repo": repo,
        "workspace_path": workspace_path,
        "default_branch": default_branch,
    }
    try:
        result = await _api_post("/api/projects", body)
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    return format_echo_response(
        f"Project {result['slug']} (#{result['id']}) proposed "
        f"(status: {result.get('status', 'pending')}). "
        "A human activates it via PATCH /api/projects or the UI."
    )


@mcp.tool()
async def hub_list_proposals(status: str = "draft") -> CallToolResult:
    """List agent proposals (draft tasks).

    Args:
        status: Filter: draft, open, rejected. Default: draft.
    """
    tasks = await _api_get(f"/api/tasks?status={status}&limit=50")
    agent_tasks = [t for t in tasks if t.get("source") == "agent"]
    if not agent_tasks:
        return structured_echo_result(f"No {status} proposals.", proposals=[])
    # Draft queue ranking (#253): DoR-ready first, then readiness, then age.
    agent_tasks.sort(
        key=lambda t: (
            not bool(t.get("dor_passed")),
            -(t.get("readiness_score") or 0),
            t.get("created_at") or "",
            t.get("id") or 0,
        )
    )
    lines = []
    for t in agent_tasks:
        t["ready_to_approve"] = bool(t.get("dor_passed")) and not any(
            r.get("severity") == "high" for r in (t.get("risks") or [])
        )
        score = t.get("readiness_score")
        marks = [
            f"score={score}" if score is not None else "score=?",
            "READY" if t["ready_to_approve"] else "not-ready",
        ]
        if any(r.get("severity") == "high" for r in (t.get("risks") or [])):
            marks.append("HIGH-RISK")
        if t.get("prepared_by"):
            marks.append(f"prep:{t['prepared_by']}")
        if t.get("created_at"):
            marks.append(f"created:{str(t['created_at'])[:10]}")
        lines.append(f"{_format_task(t)}  [{', '.join(marks)}]")
    return structured_echo_result("\n".join(lines), proposals=agent_tasks)


# Deprecated aliases (ADR-0002 Stage 1: warning + telemetry, #325)
async def _mark_deprecated(tool: str, replacement: str, result: str) -> str:
    """Count the alias call and stamp the response with a migration hint."""
    try:
        await _api_post(
            "/api/telemetry/deprecated-tool",
            {"tool": tool, "replacement": replacement},
        )
    except HubApiError:
        pass  # telemetry must never break the aliased operation
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return result
    payload["deprecated"] = True
    payload["next_action"] = f"Deprecated alias: use {replacement} instead."
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
async def hub_approve_proposal(proposal_id: int, comment: str = "") -> str:
    """Deprecated: use hub_approve_task instead. Approves and dispatches."""
    result = await hub_approve_task(proposal_id, comment=comment, run=True)
    return await _mark_deprecated("hub_approve_proposal", "hub_approve_task", result)


@mcp.tool()
async def hub_reject_proposal(proposal_id: int, comment: str = "") -> str:
    """Deprecated: use hub_reject_task instead."""
    result = await hub_reject_task(proposal_id, comment=comment)
    return await _mark_deprecated("hub_reject_proposal", "hub_reject_task", result)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_list_decisions(limit: int = 10) -> CallToolResult:
    """List recent architectural/development decisions from notesforllm.

    Args:
        limit: Max decisions to return
    """
    data = await _api_get("/api/dashboard")
    decisions = data.get("recent_decisions", [])
    if not decisions:
        # Distinguish "no decisions" from "integration broken" (#251).
        health = await _api_get("/api/integrations/notes")
        available = health.get("status") == "available"
        if available:
            message = "No decisions recorded."
        else:
            message = (
                "Notes integration unavailable "
                f"({health.get('status')}): {health.get('detail', '')}"
            )
        return structured_echo_result(
            message,
            decisions=[],
            notes_available=available,
            notes_status=health.get("status"),
            notes_detail=health.get("detail", ""),
        )
    lines = []
    for d in decisions[:limit]:
        title = d.get("title", "Decision")
        content = d.get("content", d.get("decision", ""))
        lines.append(f"- {title}")
        if content:
            lines.append(f"  {content[:200]}")
    return structured_echo_result(
        "\n".join(lines),
        decisions=decisions[:limit],
        notes_available=True,
        notes_status="available",
    )


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
            return format_echo_response(
                f"Failed to create Vast instance: {result['error']}"
            )

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
        return format_echo_response("\n".join(parts))

    @mcp.tool()
    async def hub_vast_status() -> str:
        """Check the status of the current Vast.ai GPU instance."""
        result = await _api_get("/api/vast/status")

        if not result.get("managed"):
            return format_echo_response("No active Vast.ai instance.")

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
        return format_echo_response("\n".join(parts))

    @mcp.tool()
    async def hub_vast_down() -> str:
        """Destroy the active Vast.ai GPU instance to stop billing."""
        import httpx

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{_hub_url()}/api/vast/down")
            resp.raise_for_status()
            result = resp.json()

        if result.get("destroyed"):
            return format_echo_response(
                f"Vast instance #{result.get('instance_id', '?')} destroyed. Billing stopped."
            )
        return format_echo_response(
            f"No instance to destroy. {result.get('reason', result.get('error', ''))}"
        )


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_dispatch_jobs(limit: int = 15) -> CallToolResult:
    """List recent oc-dev-dispatch jobs (raw dispatch state).

    Args:
        limit: Max jobs to return
    """
    jobs = await _api_get(f"/api/dispatch/jobs?limit={limit}")
    if not jobs:
        return structured_echo_result("No dispatch jobs found.", jobs=[])
    lines = []
    for j in jobs:
        lines.append(
            f"{j.get('job_id', '?')} [{j.get('status', '?')}] "
            f"runtime={j.get('runtime', '?')} exit={j.get('exit_code', '-')} "
            f"session={j.get('session_id', '')}"
        )
    return structured_echo_result("\n".join(lines), jobs=jobs)


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
    return format_echo_response("\n".join(lines))


# What a code task must prove before it is handed to a developer (#543). Kept
# in step with the CI job so a task cannot pass its own validation and then
# fail the release; see .github/workflows/ci.yml.
BASE_VALIDATION_COMMANDS = [
    "uv run ruff check hub tests",
    "uv run ruff format --check hub tests",
    "uv run pytest -q",
]


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
    outcome_metric: str | None = None,
    outcome_indicator: str | None = None,
    outcome_deadline: str | None = None,
    outcome_revisit_condition: str | None = None,
    redesign_decision: str | None = None,
    redesign_rationale: str | None = None,
    agent_fit: str | None = None,
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
        ("outcome_metric", outcome_metric),
        ("outcome_indicator", outcome_indicator),
        ("outcome_deadline", outcome_deadline),
        ("outcome_revisit_condition", outcome_revisit_condition),
        ("redesign_decision", redesign_decision),
        ("redesign_rationale", redesign_rationale),
        ("agent_fit", agent_fit),
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

    # #543: hand code tasks the base validation set when neither the caller nor
    # the task already names one. The format gate used to reach a task only if
    # the analyst happened to remember it, and CI never ran on develop — that
    # pair let the #505–#510 stack land six unformatted files. An explicit
    # argument always wins, including an explicit empty list, which means "no
    # commands" rather than "use the default". Docs tasks get nothing: there is
    # no code to lint.
    effective_work_type = work_type or current_task.get("work_type")
    if (
        validation_commands is None
        and effective_work_type != "docs"
        and not (current_task.get("validation_commands") or [])
    ):
        refine_body["validation_commands"] = list(BASE_VALIDATION_COMMANDS)

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
            with_instance_echo(
                {
                    "mode": "preview",
                    "task_id": task_id,
                    "planned_operations": planned_operations,
                    "diff": diff,
                    "quality_warnings": quality_warnings,
                    "developer_handoff_text": handoff_text,
                    "next_action": "preview_only",
                }
            ),
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
        with_instance_echo(
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
            }
        ),
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
    # #609: these seven were documented in the docstring below but absent from
    # the signature, so every value an agent passed was dropped without a word.
    # Ordered and named exactly as in hub_prepare_developer_task, which already
    # accepted them — two tools describing one PATCH should not look different.
    outcome_metric: str | None = None,
    outcome_indicator: str | None = None,
    outcome_deadline: str | None = None,
    outcome_revisit_condition: str | None = None,
    redesign_decision: str | None = None,
    redesign_rationale: str | None = None,
    agent_fit: str | None = None,
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
        outcome_metric: Which number should move once this ships, and from
            what to what — e.g. "median lead time, 3d -> 1d". This is what
            makes business_value checkable instead of merely arguable.
        outcome_indicator: A leading indicator visible before the metric
            moves — e.g. "share of tasks with a filled hypothesis".
        outcome_deadline: When the outcome will be checked (ISO date or a
            phrase like "4 weeks after release").
        outcome_revisit_condition: What would make us reopen this decision.
        redesign_decision: adapt | redesign — whether the work fits the
            current process or reshapes it.
        redesign_rationale: Why that choice. An unargued "adapt" is how an
            old process gets automated onto new technology.
        agent_fit: deterministic | assistant | sdd_native | agentic — how
            much agency the work wants.
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
        ("outcome_metric", outcome_metric),
        ("outcome_indicator", outcome_indicator),
        ("outcome_deadline", outcome_deadline),
        ("outcome_revisit_condition", outcome_revisit_condition),
        ("redesign_decision", redesign_decision),
        ("redesign_rationale", redesign_rationale),
        ("agent_fit", agent_fit),
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
        return format_echo_response(f"Task #{task_id} has no acceptance criteria.")
    return format_echo_response("\n\n".join(_format_ac(ac) for ac in items))


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

    Idempotent by ``ac_id``: re-sending the same id is a safe no-op (HTTP 200),
    not a 409. Use ``hub_upsert_acceptance_criterion`` when you want a repeat to
    overwrite the stored payload.

    Args:
        task_id: Target task.
        ac_id: Stable identifier for this AC (e.g. "AC-1"). Re-using an
            existing id returns the existing criterion unchanged.
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
    _, status_code = await _api_post_with_status(
        f"/api/tasks/{task_id}/acceptance_criteria", body
    )
    if status_code == 200:
        return format_echo_response(
            f"{ac_id} already exists on task #{task_id} (no change)"
        )
    return format_echo_response(f"Added {ac_id} to task #{task_id}")


@mcp.tool()
async def hub_upsert_acceptance_criterion(
    task_id: int,
    ac_id: str,
    given: str,
    when: str,
    then: str,
    verifiable_by: str = "test",
    test_ref: str = "",
) -> str:
    """Idempotent upsert of one acceptance criterion by ``ac_id``.

    Overwrites an existing AC with the same ``ac_id`` (a changed payload is
    applied; an identical payload is a safe no-op). ``hub_add_acceptance_criterion``
    is also idempotent now but never overwrites — use upsert when you want a
    retry to apply the latest payload.

    Args:
        task_id: Target task.
        ac_id: Stable identifier (e.g. "AC-1"). Created if new, updated if present.
        given: Precondition / context.
        when: Action / event.
        then: Observable outcome.
        verifiable_by: test | manual | log_check | ui_check.
        test_ref: Optional pointer to the test.
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
    safe_id = urllib.parse.quote(ac_id, safe="")
    _, status_code = await _api_put_with_status(
        f"/api/tasks/{task_id}/acceptance_criteria/{safe_id}", body
    )
    verb = "Created" if status_code == 201 else "Updated"
    return format_echo_response(f"{verb} {ac_id} on task #{task_id}")


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
    return format_echo_response(f"Task #{task_id} now has {count} acceptance criteria")


@mcp.tool()
async def hub_delete_acceptance_criterion(task_id: int, ac_id: str) -> str:
    """Delete a single acceptance criterion by its id."""
    import urllib.parse

    safe_id = urllib.parse.quote(ac_id, safe="")
    await _api_delete(f"/api/tasks/{task_id}/acceptance_criteria/{safe_id}")
    return format_echo_response(f"Deleted {ac_id} from task #{task_id}")


@mcp.tool()
async def hub_add_risk(
    task_id: int,
    kind: str,
    severity: str,
    description: str,
    mitigation: str = "",
) -> str:
    """Append a risk to a task through the atomic dedicated endpoint.

    Args:
        task_id: Target task.
        kind: ambiguous_requirements | large_scope | external_dependency |
            data_migration | breaking_change | security | performance |
            unknown_unknowns
        severity: low | medium | high
        description: One-line risk description.
        mitigation: How we plan to handle / reduce it. Optional (#610):
            leave it empty to record an honest "seen, no remedy yet" — that
            costs more in the readiness score than a mitigated risk, which is
            the point. Inventing filler to satisfy a required field made the
            score say less, not more.
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
    return format_echo_response(
        f"Risk '{kind}:{severity}' added to task #{task_id}{suffix}"
    )


@mcp.tool()
async def hub_get_readiness(task_id: int, explain: bool = False) -> CallToolResult:
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
        return structured_echo_result(_format_readiness(report, task_id), report=report)
    return structured_echo_result(_format_readiness(report, task_id), report=report)


@mcp.tool()
async def hub_readiness_tree(
    task_id: int, include_root: bool = False
) -> HubReadinessTreeResult:
    """DoR readiness for a whole subtree (epic/feature) in ONE call.

    Instead of calling hub_get_readiness per task, get a single report of
    which descendants of ``task_id`` are not DoR-ready and why.

    Args:
        task_id: Root task (epic/feature) whose descendants to check.
        include_root: If true, include the root task itself in the report.
    """
    path = f"/api/tasks/{task_id}/readiness-tree"
    if include_root:
        path += "?include_root=true"
    report = await _api_get(path)
    nodes = report.get("nodes") or []
    not_ready = [n for n in nodes if not n.get("dor_passed")]
    lines = [
        f"Readiness of subtree #{task_id}: "
        f"{report.get('ready', 0)}/{report.get('total', 0)} ready, "
        f"{report.get('not_ready', 0)} not ready."
    ]
    if not_ready:
        lines.append("Not ready:")
        for n in not_ready:
            reason = ", ".join(n.get("missing_required") or []) or "see recommendations"
            lines.append(
                f"  #{n['id']} [{n.get('status', '?')}] {n.get('title', '')} "
                f"(score {n.get('score', 0)}): missing {reason}"
            )
    else:
        lines.append("All tasks in the subtree pass DoR.")
    return structured_tool_result(
        "\n".join(lines),
        HubReadinessTreeStructured(report=report),
    )


# ---------------------------------------------------------------------------
# Diagnostics: identity and health (Stage 4 / epic #11)
# ---------------------------------------------------------------------------


def _format_whoami(data: dict[str, Any]) -> str:
    lines = [
        f"User: {data['username']} (role: {data['role']})",
        f"Auth source: {data['auth_source']}",
        f"Permissions ({data['permissions_count']}): "
        f"{', '.join(data.get('permissions_summary') or [])}",
        f"App version: {data['app_version']}",
    ]
    if data.get("api_key_id") is not None:
        lines.insert(2, f"API key id: {data['api_key_id']}")
    if data.get("principal_id") is not None:
        lines.insert(2, f"Principal id: {data['principal_id']}")
    return "\n".join(lines)


def _format_health(data: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Status: {data['status']}",
            f"App version: {data['app_version']}",
            f"Bind: {data['bind_host']}:{data['bind_port']}",
            f"Auth required: {data['auth_required']}",
            f"Auth disabled: {data['auth_disabled']}",
            f"Env tokens configured: {data['env_tokens_configured']}",
            f"Vast enabled: {data['vast_enabled']}",
        ]
    )


@mcp.tool()
async def hub_whoami() -> str:
    """Show the current caller identity: role, permissions summary, and auth source.

    Auth source is ``env`` for OPENCLAW_HUB_TOKENS map entries or ``db`` for
    DB-backed API keys (includes api_key_id, never the secret).
    """
    data = await _api_get("/api/whoami")
    return _format_whoami(data)


@mcp.tool()
async def hub_health() -> str:
    """Show public Hub health: bind host/port and auth/vast flags (no secrets)."""
    data = await _api_get("/health")
    return _format_health(data)


def _format_identity_diagnostics(data: dict[str, Any]) -> str:
    lines = [
        f"User: {data['username']} (role: {data['role']})",
        f"Auth source: {data['auth_source']}",
    ]
    if data.get("principal_id") is not None:
        lines.append(f"Principal id: {data['principal_id']}")
    lines.append(f"Permissions: {data.get('permissions_count', 0)}")
    lines.append(
        f"Instance: {data['instance']} "
        f"(base_url: {data['base_url']}, server_id: {data.get('server_id') or '?'})"
    )
    connected = data.get("connected_via")
    if connected:
        lines.append(f"Connected via: {connected}")
    if data.get("config_mismatch"):
        lines.append(
            "⚠ CONFIG MISMATCH: the server's configured base_url host differs "
            "from the address you actually reached — verify you are acting on "
            "the intended instance before any destructive operation."
        )
    lines.append(
        f"Workspace: {data.get('workspace_path') or '?'} "
        f"(branch: {data.get('workspace_branch') or '?'})"
    )
    lines.append(f"App version: {data['app_version']}")
    return "\n".join(lines)


@mcp.tool()
async def hub_admin_my_identity() -> str:
    """Show who you are and which instance/workspace you are really on (#452).

    One call returns caller identity, the server instance (base_url + server_id),
    the address you actually connected through, a mismatch warning when the
    server's configured URL disagrees with reality, and the workspace path and
    current branch. Read this before any destructive operation to avoid acting
    on the wrong instance.
    """
    try:
        data = await _api_get("/api/diagnostics/identity")
    except HubApiError as exc:
        return _format_hub_api_error(exc)
    return _format_identity_diagnostics(data)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
