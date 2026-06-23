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
            "message": f"missing permission: {permission}",
            "hint": meta["hint"],
            "required_role": meta["required_role"],
            "suggested_tool": meta.get("suggested_tool"),
            "required_permission": permission,
        }
    )


def human_only_gate_detail(message: str | None = None) -> dict[str, Any]:
    return enrich_error_payload(
        {
            "reason": "human_only_gate",
            "message": message or "this operation requires human or admin role",
            "hint": "This operation requires a human or admin token, not an agent token.",
            "required_role": "human",
            "suggested_tool": None,
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
                "required_role": "human",
                "suggested_tool": None,
            }
        )
    return enrich_error_payload(
        {
            "reason": "api_error",
            "message": msg,
            "hint": msg,
            "suggested_tool": None,
            "status_code": status_code,
        }
    )
