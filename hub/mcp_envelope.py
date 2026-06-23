"""Machine-readable mutation response envelope for Hub MCP tools (#171)."""

from __future__ import annotations

from typing import Any, Literal

Awaiting = Literal["none", "human_decision", "ci", "review"]
ActorHint = Literal["agent", "human", "ci", "none"]

MUTATION_ENVELOPE_FIELDS = (
    "status",
    "awaiting",
    "transition",
    "next_action",
    "actor_hint",
)


def compute_awaiting(status: str, task: dict[str, Any] | None = None) -> Awaiting:
    """Map task status to what blocks further automated progress."""
    if status == "needs_decision":
        return "human_decision"
    if status == "ci_check":
        return "ci"
    if status in ("review", "fix_requested"):
        return "review"
    if status == "pending_report":
        return "human_decision"
    if status == "needs_info":
        return "human_decision"
    return "none"


def compute_actor_hint(awaiting: Awaiting, status: str) -> ActorHint:
    if awaiting == "ci":
        return "ci"
    if awaiting == "human_decision":
        return "human"
    if status in ("open", "running", "claimed", "draft", "needs_info"):
        return "agent"
    if status in ("completed", "failed", "rejected"):
        return "none"
    return "agent"


def compute_next_action(
    status: str,
    awaiting: Awaiting,
    *,
    reason: str | None = None,
) -> str:
    """Short agent-facing hint for the next step."""
    if reason == "human_decision_required":
        return "Task awaits human Decision Gate — call hub_decide_task with a human/admin token."
    if reason == "pair_start_required":
        return "Call hub_pair_start (or hub_claim_task then pair-start) before hub_report_done."
    if reason == "awaiting_ci_conveyor":
        return "Wait for CI poller or use hub_decide_task / human gate when stuck."
    if reason == "task_already_terminal":
        return "Task is finished; no further done report is needed."
    if reason == "invalid_status_for_done":
        return "Start work via hub_pair_start or hub_start_task before reporting done."

    if awaiting == "human_decision" and status == "needs_decision":
        return "Call hub_decide_task (human/admin token) to accept or rework."
    if awaiting == "human_decision" and status == "pending_report":
        return (
            "Await human review or use hub_report_done after approval path completes."
        )
    if awaiting == "human_decision" and status == "needs_info":
        return "Await hub_answer_question from a human."
    if awaiting == "ci":
        return "Wait for CI conveyor; poller advances to review when checks pass."
    if awaiting == "review":
        return "Await review cycle or human decision if review stalls."

    if status == "completed":
        return "No action required — task completed."
    if status in ("failed", "rejected"):
        return "Task is terminal; inspect updates or open a follow-up task."
    if status == "open":
        return "Call hub_pair_start or hub_start_task to begin work."
    if status == "claimed":
        return (
            "Call hub_pair_start to begin pair work or hub_release_task to drop claim."
        )
    if status == "running":
        return "Continue implementation; call hub_report_done when validation passes."
    if status == "draft":
        return "Refine task and await hub_approve_task from a human."
    return "Inspect hub_my_context or hub_task_status for current gates."


def build_transition(
    from_status: str | None,
    to_status: str | None,
) -> dict[str, str] | None:
    if not from_status or not to_status or from_status == to_status:
        return None
    return {"from": from_status, "to": to_status}


def build_mutation_envelope(
    task: dict[str, Any] | None,
    *,
    status: str | None = None,
    transition_from: str | None = None,
    transition_to: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build the stable mutation envelope fields for success or error payloads."""
    resolved_status = status or (task or {}).get("status") or "?"
    awaiting = compute_awaiting(resolved_status, task)
    actor_hint = compute_actor_hint(awaiting, resolved_status)
    transition = build_transition(transition_from, transition_to)
    if transition is None and transition_from and resolved_status != transition_from:
        transition = build_transition(transition_from, resolved_status)

    return {
        "status": resolved_status,
        "awaiting": awaiting,
        "transition": transition,
        "next_action": compute_next_action(resolved_status, awaiting, reason=reason),
        "actor_hint": actor_hint,
    }


def merge_mutation_response(
    message: str,
    envelope: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> str:
    """Return JSON text with human message plus envelope fields (backward-compatible parse)."""
    import json

    payload: dict[str, Any] = {"message": message, **envelope}
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def enrich_error_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add mutation envelope fields to structured API/MCP error payloads."""
    status = payload.get("current_status") or payload.get("status")
    if not status:
        return payload
    envelope = build_mutation_envelope(
        {"status": status},
        status=status,
        reason=payload.get("reason"),
    )
    merged = {**payload, **envelope}
    if "message" not in merged:
        merged["message"] = payload.get("hint") or payload.get("message") or ""
    return merged
