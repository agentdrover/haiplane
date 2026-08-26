"""Machine-readable mutation response envelope for Hub MCP tools (#171)."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from hub.hub_instance import with_instance_echo

log = logging.getLogger("hub")

Awaiting = Literal["none", "human_decision", "ci", "review"]
ActorHint = Literal["agent", "human", "ci", "none"]

MUTATION_ENVELOPE_FIELDS = (
    "instance",
    "base_url",
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
    # needs_info deliberately absent: it is answered two lines up, where
    # awaiting=human_decision correctly says a human acts. Listing it here as
    # agent-actionable said the opposite, and the final fallback returns
    # "agent" anyway — so removing it changes no answer on any (awaiting,
    # status) pair, checked exhaustively (#370).
    if status in ("open", "running", "claimed", "draft"):
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
    if reason == "permission_denied":
        return "Retry with a human or admin token, or use suggested_tool if provided."
    if reason == "human_only_gate":
        return "Retry with a human or admin Bearer token."
    if reason == "agent_create_forbidden":
        return "Propose the work with hub_propose_task and let a human approve it."
    if reason == "self_review_forbidden":
        return (
            "Hand the verdict to an independent reviewer: another agent "
            "principal or a human token must call hub_submit_review."
        )
    if reason == "chat_pair_gate_forbidden":
        return (
            "This route is outside the chat-pair allowlist — post or sharpen "
            "the task from the chat, and use your laptop token for anything else."
        )
    if reason == "chat_pair_invalid":
        return "Take a fresh pairing code in the hub and redeem that one."
    if reason == "chat_pair_rate_limited":
        return "Wait out the pairing window, then redeem a fresh code."
    if reason == "chat_pair_auth_required":
        return "Configure hub auth (principals or HAIPLANE_HUB_TOKENS); open mode has no identity to pair."
    if reason == "chat_pair_run_forbidden":
        return "Create the task without run_immediately / auto_review=false, then start it from the hub."
    if reason == "forbidden":
        return "This operation is forbidden for the current token; use a human or admin token."
    if reason == "invalid_hierarchy":
        return "Fix task_type/parent_id per hint, then retry hub_create_task or hub_propose_task."

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
        return (
            "Obtain a review verdict: reviewer runs hub_get_review_brief and "
            "hub_submit_review; after APPROVED, report done again."
        )

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
    payload: dict[str, Any] = with_instance_echo({"message": message, **envelope})
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def format_echo_response(message: str, **extra: Any) -> str:
    """JSON text response for read-only MCP tools with instance echo."""
    return json.dumps(
        with_instance_echo({"message": message, **extra}), ensure_ascii=False
    )


def enrich_error_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add mutation envelope fields to structured API/MCP error payloads."""
    reason = payload.get("reason")
    status = payload.get("current_status") or payload.get("status") or "?"
    envelope = build_mutation_envelope(
        {"status": status},
        status=status,
        reason=reason,
    )
    # The call changed nothing, so it never reports a transition.
    envelope["transition"] = None
    # ``awaiting`` is NOT forced: it describes the task's gate, not this call.
    # human_decision_required legitimately awaits a human decision, and
    # awaiting_ci_conveyor awaits CI — an existing test caught this when the
    # first version of #548 zeroed it for every refusal. Force it only when the
    # payload carries no status at all, where nothing can be computed from it.
    if status == "?":
        envelope["awaiting"] = "none"

    # Who acts next is DECLARED by the refusal itself, not guessed from a status
    # that refusals do not carry (#548). The old shape was a tuple of reasons to
    # force "human", and anything absent fell through compute_actor_hint to
    # "agent" — telling the caller we had just refused that it was still the
    # responsible actor. That silent default shipped the same defect twice:
    # agent_create_forbidden (#360) and then self_review_forbidden and
    # withdraw_agent_only (#548). A default that is wrong only by omission gets
    # forgotten again, so omission is now loud.
    declared = payload.get("actor_hint")
    if declared is None:
        # Second choice, not a fallthrough: required_role is a field the refusal
        # actually carries, so deriving from it states something the payload
        # already asserts. compute_actor_hint's default is the opposite — it
        # guesses from a status refusals never set, and always lands on "agent".
        role = payload.get("required_role")
        if role in ("human", "agent"):
            declared = role
    if declared is not None:
        envelope["actor_hint"] = declared
    else:
        log.warning(
            "refusal reason %r declares neither actor_hint nor a usable "
            "required_role; falling back to %r. Declare it in the *_detail "
            "helper — see hub/actionable_errors.py.",
            reason,
            envelope.get("actor_hint"),
        )
    merged = with_instance_echo({**payload, **envelope})
    if "message" not in merged:
        merged["message"] = payload.get("hint") or payload.get("message") or ""
    return merged
