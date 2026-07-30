"""Actionable error payloads for Hub API and MCP (#172)."""

from __future__ import annotations

import re
from typing import Any

from hub.models import HIERARCHY_RULES, TaskType
from hub.mcp_envelope import enrich_error_payload

_PERMISSION_HINTS: dict[str, dict[str, str | None]] = {
    "tasks.archive": {
        "required_role": "human",
        "suggested_tool": "hub_withdraw_own_draft",
        "hint": (
            "Agent tokens cannot archive tasks. For your own mistaken drafts, "
            "use hub_withdraw_own_draft (narrow scope). Otherwise ask a human "
            "with tasks.archive."
        ),
    },
    "tasks.delete": {
        "required_role": "human",
        "suggested_tool": "hub_withdraw_own_draft",
        "hint": (
            "Agent tokens cannot delete tasks. For your own draft proposals, "
            "use hub_withdraw_own_draft instead. Permanent delete requires a human."
        ),
    },
    "tasks.human_gate": {
        "required_role": "human",
        "suggested_tool": None,
        "hint": "This gate requires a human or admin token.",
    },
    "tasks.decision": {
        "required_role": "human",
        "suggested_tool": "hub_decide_task",
        "hint": "Decision Gate requires hub_decide_task with a human or admin token.",
    },
}

_DONE_SUGGESTED_TOOLS: dict[str, str | None] = {
    "pair_start_required": "hub_pair_start",
    "human_decision_required": "hub_decide_task",
    "awaiting_ci_conveyor": "hub_task_status",
    "task_already_terminal": "hub_task_status",
    "invalid_status_for_done": "hub_pair_start",
}

# Who acts next per done-report reason. Two of these are emphatically not the
# agent's to resolve, and the old silent default called them all "agent" (#548).
_DONE_ACTORS: dict[str, str] = {
    "pair_start_required": "agent",
    "human_decision_required": "human",
    "awaiting_ci_conveyor": "ci",
    "task_already_terminal": "none",
    "invalid_status_for_done": "agent",
}

_HIERARCHY_PARENT_RE = re.compile(r"requires parent of type (\w+), got (\w+)")
_HIERARCHY_NEEDS_PARENT_RE = re.compile(r"requires a parent of type (\w+)")


def permission_denied_detail(permission: str) -> dict[str, Any]:
    meta = _PERMISSION_HINTS.get(
        permission,
        {
            "required_role": "human",
            "suggested_tool": None,
            "hint": (
                f"This operation requires permission '{permission}' on a human or admin token."
            ),
        },
    )
    return enrich_error_payload(
        {
            "reason": "permission_denied",
            "actor_hint": "human",
            "message": f"missing permission: {permission}",
            "hint": meta["hint"],
            "required_role": meta["required_role"],
            "suggested_tool": meta.get("suggested_tool"),
            "required_permission": permission,
        }
    )


def withdraw_agent_only_detail() -> dict[str, Any]:
    return enrich_error_payload(
        {
            "reason": "withdraw_agent_only",
            # Raised by require_agent_caller, i.e. the refused caller is a
            # human/admin, and hub_archive_task is theirs as well (#548).
            "actor_hint": "human",
            "message": "withdraw is only for agent tokens",
            "hint": (
                "hub_withdraw_own_draft is agent-only. Humans and admins should use "
                "hub_archive_task or POST /api/tasks/{id}/archive."
            ),
            "required_role": "agent",
            "suggested_tool": "hub_archive_task",
        }
    )


def withdraw_own_draft_error_detail(
    *,
    reason: str,
    message: str,
    hint: str,
    current_status: str | None = None,
    required_status: str | None = None,
    suggested_tool: str | None = "hub_withdraw_own_draft",
    required_role: str = "agent",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reason": reason,
        "message": message,
        "hint": hint,
        "required_role": required_role,
        "suggested_tool": suggested_tool,
    }
    if current_status is not None:
        payload["current_status"] = current_status
    if required_status is not None:
        payload["required_status"] = required_status
    return enrich_error_payload(payload)


def human_only_gate_detail(message: str | None = None) -> dict[str, Any]:
    return enrich_error_payload(
        {
            "reason": "human_only_gate",
            "actor_hint": "human",
            "message": message or "this operation requires human or admin role",
            "hint": "This operation requires a human or admin token, not an agent token.",
            "required_role": "human",
            "suggested_tool": None,
        }
    )


def agent_create_forbidden_detail() -> dict[str, Any]:
    """Agents propose, humans create (#360).

    Task creation used to trust ``source`` from the request body, so an agent
    could label its own request "human" and land a task straight in ``open`` —
    or in ``running`` with run_immediately — skipping the draft gate that
    ``hub_propose_task`` exists to enforce. Source now follows the caller's
    identity, and asking for anything else is refused rather than quietly
    downgraded: a silent draft would look like the tool worked.
    """
    return enrich_error_payload(
        {
            "reason": "agent_create_forbidden",
            "actor_hint": "human",
            "message": "agents may not create tasks directly; propose them instead",
            "hint": "Task creation with source=human is human-only. Agents use "
            "hub_propose_task (or source=agent), which creates a draft for "
            "human approval.",
            "required_role": "human",
            "suggested_tool": "hub_propose_task",
        }
    )


def self_review_forbidden_detail(agent: str) -> dict[str, Any]:
    """Universal Review Gate (#318): implementer may not review own work."""
    return enrich_error_payload(
        {
            "reason": "self_review_forbidden",
            # Another agent principal may pass this verdict (#432), so the
            # actor really is an agent — just not this one. actor_hint cannot
            # express "a different principal of the same kind", so the
            # constraint is a field of its own: an orchestrator dispatching on
            # actor_hint alone would re-send the very agent it just refused.
            "actor_hint": "agent",
            "same_principal_forbidden": True,
            "message": f"agent '{agent}' implemented this task and cannot review it",
            "hint": "The Universal Review Gate requires an independent reviewer: "
            "another agent principal or a human token must submit the verdict. "
            "Solo mode: set OPENCLAW_REVIEW_SELF_APPROVE=allow.",
            "required_role": "independent_reviewer",
            "suggested_tool": None,
        }
    )


def pair_start_claim_mismatch_detail(
    *, task_id: int, holder: str, caller: str
) -> dict[str, Any]:
    """409 for hub_pair_start when the claim holder ≠ resolved caller name (#453).

    The name in hub_claim_task(agent=...) must match assigned_agent in
    hub_pair_start; a mismatch here is almost always a wrong/missing
    assigned_agent argument, so spell out both the holder and the caller
    identity the server actually resolved, plus the two ways to recover.
    """
    caller_repr = caller or "(unresolved: token identity matched no agent name)"
    return enrich_error_payload(
        {
            "reason": "pair_start_claim_mismatch",
            "actor_hint": "agent",
            "message": (
                f"Task #{task_id} is claimed by '{holder}'; "
                f"pair-start denied for '{caller_repr}'"
            ),
            "hint": (
                f"The claim is held by '{holder}', but pair-start resolved your "
                f"identity as '{caller_repr}'. Either call "
                f"hub_pair_start(assigned_agent='{holder}') to continue as the "
                "holder, or hub_release_task first and re-claim under your name. "
                "The name in hub_claim_task(agent=...) must equal assigned_agent "
                "in hub_pair_start (the same authenticated principal is accepted "
                "even when the presentational name differs)."
            ),
            "claimed_by": holder,
            "caller_identity": caller,
            "task_id": task_id,
            "suggested_tool": "hub_pair_start",
        }
    )


def hierarchy_error_detail(
    raw_message: str,
    *,
    task_type: str | None = None,
    parent_id: int | None = None,
) -> dict[str, Any]:
    hint = raw_message
    match = _HIERARCHY_PARENT_RE.search(raw_message)
    if match:
        required_type, got_type = match.group(1), match.group(2)
        child = task_type or "child"
        hint = (
            f"Cannot attach {child} under parent type '{got_type}'. "
            f"Create or select a parent of type '{required_type}' "
            f"(hub_create_task(task_type='{required_type}', parent_id=<id>) "
            f"or hub_propose_task with parent_id of a {required_type})."
        )
    else:
        needs = _HIERARCHY_NEEDS_PARENT_RE.search(raw_message)
        if needs:
            required_type = needs.group(1)
            child = task_type or "child"
            hint = (
                f"{child} requires parent_id of a {required_type}. "
                f"Use hub_create_task(task_type='{child}', parent_id=<{required_type}_id>)."
            )
        elif "cannot have a parent" in raw_message:
            hint = (
                f"Top-level {task_type or 'task'} must omit parent_id. "
                "Only feature/subtask always need parents per HIERARCHY_RULES."
            )
        elif "Epics cannot have a parent" in raw_message:
            hint = "Epics are roots — omit parent_id when calling hub_create_task."

    allowed_parent: str | None = None
    if task_type:
        try:
            rule = HIERARCHY_RULES[TaskType(task_type)]
            allowed_parent = rule.value if rule else None
        except ValueError:
            allowed_parent = None

    payload: dict[str, Any] = {
        "reason": "invalid_hierarchy",
        # The caller passed a parent the hierarchy rules reject — its own to fix.
        "actor_hint": "agent",
        "message": raw_message,
        "hint": hint,
        "suggested_tool": "hub_create_task",
        "task_type": task_type,
        "parent_id": parent_id,
    }
    if allowed_parent:
        payload["required_parent_type"] = allowed_parent
    return enrich_error_payload(payload)


def done_report_error_detail(
    task: dict[str, Any],
    *,
    reason: str,
    hint: str,
    required_status: str,
) -> dict[str, Any]:
    return enrich_error_payload(
        {
            "reason": reason,
            "message": hint,
            "hint": hint,
            "required_status": required_status,
            "current_status": task["status"],
            "suggested_tool": _DONE_SUGGESTED_TOOLS.get(reason),
            "actor_hint": _DONE_ACTORS.get(reason, "agent"),
        }
    )


def normalize_api_error_detail(detail: Any, *, status_code: int) -> dict[str, Any]:
    """Turn plain-string FastAPI details into actionable payloads."""
    if isinstance(detail, dict) and detail.get("reason"):
        payload = dict(detail)
        if "message" not in payload:
            payload["message"] = payload.get("hint") or str(detail)
        if payload.get("reason") in _DONE_SUGGESTED_TOOLS and not payload.get(
            "suggested_tool"
        ):
            payload["suggested_tool"] = _DONE_SUGGESTED_TOOLS[payload["reason"]]
        if payload.get("reason") in _DONE_ACTORS and not payload.get("actor_hint"):
            payload["actor_hint"] = _DONE_ACTORS[payload["reason"]]
        return enrich_error_payload(payload)

    if isinstance(detail, str):
        if detail.startswith("missing permission:"):
            permission = detail.split(":", 1)[1].strip()
            return permission_denied_detail(permission)
        if status_code == 403 and "human or admin" in detail.lower():
            return human_only_gate_detail(detail)
        if any(
            token in detail
            for token in (
                "requires parent",
                "cannot have a parent",
                "Epics cannot",
                "Parent task #",
            )
        ):
            return hierarchy_error_detail(detail)

    msg = str(detail)
    if status_code == 403:
        return enrich_error_payload(
            {
                "reason": "forbidden",
                "message": msg,
                "hint": msg,
                "actor_hint": "human",
                "required_role": "human",
                "suggested_tool": None,
            }
        )
    return enrich_error_payload(
        {
            "reason": "api_error",
            "message": msg,
            "hint": msg,
            # Malformed input is the caller's to fix, so the agent really is next.
            "actor_hint": "agent",
            "suggested_tool": None,
            "status_code": status_code,
        }
    )
