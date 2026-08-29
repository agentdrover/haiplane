"""Actionable error payloads for Hub API and MCP (#172)."""

from __future__ import annotations

import re
from typing import Any

from hub import config
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


def chat_pair_gate_forbidden_detail(method: str, path: str) -> dict[str, Any]:
    """A chat-pair session reached a route outside its allowlist (#961).

    Deny-by-default, so this refusal covers routes that do not exist yet as
    well as the ones that do — including the branches where "not an agent"
    silently means "a human" (review-verdict, pair-start, projects, threads).
    """
    return enrich_error_payload(
        {
            "reason": "chat_pair_gate_forbidden",
            "actor_hint": "human",
            "message": f"chat-pair sessions may not call {method} {path}",
            "hint": (
                "The chat-pair channel exists to post and sharpen tasks: read "
                "tasks, create one, refine it, edit its acceptance criteria and "
                "risks. Gates, decisions, admin and /mcp need the token on your "
                "laptop, not the code from the chat."
            ),
            "required_role": "human",
            "suggested_tool": None,
        }
    )


def steward_verdict_required_detail() -> dict[str, Any]:
    """422: steward judgement omitted verdict. Silent default is forbidden (#549)."""
    return enrich_error_payload(
        {
            "reason": "steward_verdict_required",
            "actor_hint": "agent",
            "message": "verdict is required and has no default",
            "hint": (
                "Send verdict as approve, changes_requested or escalate. "
                "Omitting it must not become an approve."
            ),
            "suggested_tool": "hub_submit_steward_judgement",
        }
    )


def steward_closed_vocabulary_detail(
    field: str, got: str, allowed: tuple[str, ...] | list[str]
) -> dict[str, Any]:
    """422: a closed dictionary rejected a value, including any 'unknown'."""
    allowed_list = list(allowed)
    return enrich_error_payload(
        {
            "reason": "steward_closed_vocabulary",
            "actor_hint": "agent",
            "field": field,
            "got": got,
            "allowed": allowed_list,
            "message": f"invalid {field}: {got!r}",
            "hint": (
                f"{field} must be one of: {', '.join(allowed_list)}. "
                "There is no unknown bucket — a new code is a spec change."
            ),
            "suggested_tool": "hub_submit_steward_judgement",
        }
    )


def steward_escalate_reason_required_detail(
    allowed: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """422: escalate without a closed-set reason."""
    allowed_list = list(allowed)
    return enrich_error_payload(
        {
            "reason": "steward_escalate_reason_required",
            "actor_hint": "agent",
            "allowed": allowed_list,
            "message": "escalate_reason is required when verdict is escalate",
            "hint": f"Send escalate_reason as one of: {', '.join(allowed_list)}.",
            "suggested_tool": "hub_submit_steward_judgement",
        }
    )


def steward_unknown_finding_uid_detail(uid: str) -> dict[str, Any]:
    """422: a closure named a finding that is not in this generation's report."""
    return enrich_error_payload(
        {
            "reason": "steward_unknown_finding_uid",
            "actor_hint": "agent",
            "message": f"finding_uid {uid} is not in this generation's report",
            "hint": (
                "Closures address confirmed findings of this submission by "
                "finding_uid. A uid the hub cannot see cannot close a finding."
            ),
            "suggested_tool": "hub_submit_steward_judgement",
        }
    )


def steward_judgement_exists_detail(
    task_id: int, generation: int, kind: str
) -> dict[str, Any]:
    """409: at-most-once on (task_id, generation, kind), like the arbiter (#421)."""
    return enrich_error_payload(
        {
            "reason": "steward_judgement_exists",
            "actor_hint": "none",
            "message": (
                f"steward judgement already recorded for task #{task_id} "
                f"generation {generation} kind {kind}"
            ),
            "hint": (
                "A second judgement of the same triple is refused, not stacked. "
                "A new submission generation opens a new slot."
            ),
            "suggested_tool": "hub_submit_steward_judgement",
        }
    )


def steward_gate_forbidden_detail(method: str, path: str) -> dict[str, Any]:
    """A steward token reached a route outside its two-op allowlist (#1021)."""
    return enrich_error_payload(
        {
            "reason": "steward_gate_forbidden",
            "actor_hint": "none",
            "message": f"steward principals may not call {method} {path}",
            "hint": (
                "The steward principal may read the evidence pack and write a "
                "judgement. Everything else is refused by default, including "
                "routes that do not exist yet."
            ),
            "required_role": "steward",
            "suggested_tool": None,
        }
    )


def chat_pair_agent_missing_detail() -> dict[str, Any]:
    """503 when implementer pairing has no acting principal (#980).

    Raised on issue, not on redeem: a 503 after a code hash hit would tell
    a guesser the code existed.
    """
    return enrich_error_payload(
        {
            "reason": "chat_pair_agent_missing",
            "actor_hint": "human",
            "message": "implementer pairing has no acting agent principal",
            "hint": (
                "Create an active agent principal named "
                f"{config.CHAT_PAIR_AGENT!r} (HAIPLANE_CHAT_PAIR_AGENT), "
                "then issue the code again."
            ),
            "suggested_tool": None,
        }
    )


def chat_pair_task_not_open_detail(*, task_id: int, status: str) -> dict[str, Any]:
    """409: implementer codes are issued only for open tasks (#980)."""
    return enrich_error_payload(
        {
            "reason": "chat_pair_task_not_open",
            "actor_hint": "human",
            "message": (
                f"implementer pairing is issued only for open tasks; "
                f"#{task_id} is {status}"
            ),
            "hint": (
                "Approve or release the task to open, then issue a new "
                "implementer code from its card."
            ),
            "task_id": task_id,
            "suggested_tool": None,
        }
    )


def chat_pair_invalid_detail() -> dict[str, Any]:
    """One answer for unknown, spent and expired codes (#961).

    Three distinguishable refusals would tell somebody guessing codes which
    guesses were close, so the message says only what the operator needs.
    """
    return enrich_error_payload(
        {
            "reason": "chat_pair_invalid",
            "actor_hint": "human",
            "message": "pairing code is not valid",
            "hint": (
                "A code works once and lives about five minutes; asking for a "
                "new one also burns the previous. Take a fresh code in the hub "
                "(Web UI or POST /api/auth/chat-pair/start) and paste that."
            ),
            "required_role": None,
            "suggested_tool": None,
        }
    )


def chat_pair_rate_limited_detail() -> dict[str, Any]:
    return enrich_error_payload(
        {
            "reason": "chat_pair_rate_limited",
            "actor_hint": "human",
            "message": "too many pairing attempts from this address",
            "hint": (
                "Wait for the window to pass, then redeem a fresh code. Web "
                "login is not affected — this limiter is the pairing one."
            ),
            "required_role": None,
            "suggested_tool": None,
        }
    )


def chat_pair_auth_required_detail() -> dict[str, Any]:
    return enrich_error_payload(
        {
            "reason": "chat_pair_auth_required",
            "actor_hint": "human",
            "message": "chat pairing needs an authenticated hub",
            "hint": (
                "This hub runs in open mode (no principals or HAIPLANE_HUB_TOKENS), "
                "so there is no identity a pairing code could carry. Configure "
                "auth first; in open mode the REST API is already open."
            ),
            "required_role": None,
            "suggested_tool": None,
        }
    )


def chat_pair_run_forbidden_detail(field: str) -> dict[str, Any]:
    """Create is allowed, dispatching from it is not (#961).

    A refusal rather than a silent reset of the field: quiet degradation would
    read in the chat as "it started", and an opt-out of review is a decision
    that must not be taken from a channel living in somebody else's transcript.
    """
    return enrich_error_payload(
        {
            "reason": "chat_pair_run_forbidden",
            "actor_hint": "human",
            "message": f"chat-pair sessions may not set {field} on task creation",
            "hint": (
                "Post the task without run_immediately and without "
                "auto_review=false; start it from the hub when you are back at "
                "a machine that can watch the run."
            ),
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
            "Solo mode: set HAIPLANE_REVIEW_SELF_APPROVE=allow.",
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


def claim_without_session_detail(*, task_id: int, tool: str) -> dict[str, Any]:
    """422 when an agent takes a task without naming its session (#852).

    The claim used to accept an empty session_id, so a task could run with
    claim_session_id NULL — held by an agent NAME, which does not identify an
    executor: one agent runs several sessions at once and each of them passes
    a holder check made of names. Everything addressable routes by session
    (registry #771, messages #773, wake-up #774), so such a task cannot be
    asked a question or woken up.

    The hint also names a REST fallback (#899). A client fixes its tool
    schemas when the session starts and never refreshes them, so a session
    opened before the hub shipped this requirement has no ``session_id``
    parameter to pass: it is told to do something its own schema makes
    impossible. That happened on #498 and twice more the next day, and each
    time the work continued only because the agent happened to know REST and
    hold a token. An agent without that knowledge simply stops, and from the
    outside it reads as "the agent is broken" rather than "two versions
    disagree".

    So the refusal answers both readers. A caller that CAN pass the field is
    told to pass it — that stays the first line. A caller whose schema
    predates the field is told what actually happened and given a call it can
    make today. The fallback is called temporary on purpose: a workaround
    that reads as a normal mode of operation becomes one.
    """
    # Spelled out per tool: the two take the task through different endpoints
    # and different bodies, and a hint that leaves the reader to guess the
    # shape is the same dead end as one naming an impossible field.
    if tool == "hub_pair_start":
        rest_call = (
            f"POST /api/tasks/{task_id}/pair-start with body "
            '{"assigned_agent": "<your agent name>", '
            '"session_id": "<your session>", "plan": "Plan: ..."}'
        )
    else:
        rest_call = (
            f"POST /api/tasks/{task_id}/claim with body "
            '{"agent": "<your agent name>", "session_id": "<your session>"}'
        )
    return enrich_error_payload(
        {
            "reason": "claim_without_session",
            "actor_hint": "agent",
            "message": (
                f"Task #{task_id}: session_id is required to take a task — "
                "an agent name does not identify which session is working. "
                "If your tool schema offers no session_id, your session "
                "predates the requirement and the hint names a call that "
                "works today"
            ),
            "hint": (
                f"Pass your session id: {tool}(task_id={task_id}, "
                "session_id='<your session>'). Register it first with "
                "hub_session_register(session_id=...) so other sessions can "
                "reach you about this task. The same id must be used for "
                "hub_pair_start and hub_release_task. "
                "IF YOUR TOOL SCHEMA HAS NO session_id PARAMETER: this is a "
                "version mismatch, not your mistake — your client fixed its "
                "tool list when the session started, and the hub has shipped "
                "the field since. Nothing you can call adds the parameter. "
                "Until this session is restarted, take the task over REST "
                f"with the same token: {rest_call}. That is a way around a "
                "version gap, not the normal route — a session started now "
                "gets the field and should use the tool."
            ),
            "task_id": task_id,
            "suggested_tool": tool,
        }
    )


def pair_start_session_mismatch_detail(
    *, task_id: int, holder_session: str, caller_session: str
) -> dict[str, Any]:
    """409 when another session of the SAME agent tries to pair-start (#852).

    The name-based holder check passes here — both sessions run under one
    agent name — which is exactly the hole: two sessions of the same agent
    would both believe they hold the task.
    """
    caller_repr = caller_session or "(no session declared)"
    return enrich_error_payload(
        {
            "reason": "pair_start_session_mismatch",
            "actor_hint": "agent",
            "message": (
                f"Task #{task_id} is held by session '{holder_session}'; "
                f"pair-start denied for session '{caller_repr}'"
            ),
            "hint": (
                f"The claim belongs to session '{holder_session}', not to "
                f"'{caller_repr}' — the agent name matches, the session does "
                "not. Either continue in the holding session, or ask it to "
                "call hub_release_task first (hub_send_message reaches it: "
                f"the address is '{holder_session}'). Never pair-start a task "
                "another live session is already working."
            ),
            "task_id": task_id,
            "claim_session_id": holder_session,
            "caller_session_id": caller_session,
            "suggested_tool": "hub_pair_start",
        }
    )


def pair_branch_dirty_detail(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dirty shared hub clone: only a human on the host can clear it (#1040).

    Agents cannot reach that directory. Addressing them is the #966 class of
    refusal: an unfulfillable hint is worse than none. ``actor_hint`` is
    declared here so the envelope cannot fall back to ``agent``.
    """
    body = dict(payload or {})
    body["reason"] = "pair_branch_dirty"
    body["actor_hint"] = "human"
    body["required_role"] = "human"
    body.setdefault("suggested_tool", "hub_pair_start")
    return enrich_error_payload(body)


def pair_worktree_dirty_detail(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dirty task worktree: the agent working there can stash it (#1040).

    Same porcelain as ``pair_branch_dirty``, different place — collapsing the
    two onto one addressee would wake the wrong person. The fallback to
    ``agent`` happens to be right here; it still has to be declared, or the
    next undeclared reason inherits a coincidence.
    """
    body = dict(payload or {})
    body["reason"] = "pair_worktree_dirty"
    body["actor_hint"] = "agent"
    body["required_role"] = "agent"
    body.setdefault("suggested_tool", "hub_pair_start")
    return enrich_error_payload(body)


def session_owned_by_other_detail(*, session_id: str) -> dict[str, Any]:
    """409 when register would overwrite another principal's session row (#977).

    The other principal's name and id stay out of the payload: the caller
    learns the id is taken, not who holds it. Heartbeat of a foreign id is
    a 404 instead — that path must look like an unregistered session.
    """
    return enrich_error_payload(
        {
            "reason": "session_owned_by_other",
            "actor_hint": "agent",
            "message": (
                f"session '{session_id}' is already registered to another principal"
            ),
            "hint": (
                "Pick a new session_id and register it with "
                "hub_session_register(session_id=...) — do not reuse an id "
                "another agent already holds."
            ),
            "session_id": session_id,
            "suggested_tool": "hub_session_register",
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


def verdict_repeats_previous_detail(when: str) -> dict[str, Any]:
    """A verdict whose words are the previous verdict's, unacknowledged (#1057).

    Refused, not rewritten: repeating is legitimate when the defect is still
    there, and only the reviewer knows whether it is. What the hub can tell is
    that this exact text has already been said, which on #1042 it had — the
    complaint it carried was closed by a resubmission 18 minutes before the
    paste, and the task stood in review until someone read it again.
    """
    return enrich_error_payload(
        {
            "reason": "verdict_repeats_previous",
            "actor_hint": "agent",
            "required_role": "reviewer",
            "retry_by_same_caller": True,
            "message": (
                "this verdict repeats the text of the previous verdict on this "
                f"task ({when}) word for word"
            ),
            "hint": (
                "If the defect is still there, resend with "
                "acknowledge_repeat=true and the repeat is recorded as meant. "
                "If this was a paste of the old text, read the current "
                "submission first — it may have closed what the text asks for."
            ),
            "suggested_tool": "hub_submit_review",
        }
    )


def verdict_contradicts_its_text_detail(verdict: str, declared: str) -> dict[str, Any]:
    """The filed outcome and the body's own first word disagree (#1057).

    No acknowledgement for this one. Two outcomes in one call is not a decision
    a reviewer can confirm — it is the typo that filed #1041's 06:33 verdict as
    CHANGES_REQUESTED over a text beginning with the word APPROVED.
    """
    return enrich_error_payload(
        {
            "reason": "verdict_contradicts_its_text",
            "actor_hint": "agent",
            "required_role": "reviewer",
            "retry_by_same_caller": True,
            "message": (
                f"verdict={verdict} but the text opens by declaring {declared.upper()}"
            ),
            "hint": (
                "Send the outcome you mean: either change the verdict field or "
                "drop the opposite word from the opening line. Quoting an "
                "earlier verdict further down the text is fine — only the "
                "first meaningful line is read as a declaration."
            ),
            "suggested_tool": "hub_submit_review",
        }
    )


def changes_requested_requires_content_detail() -> dict[str, Any]:
    """A verdict that sends work back has to say what to redo (#1010).

    The next actor is whoever just called: the refusal is answered by adding a
    sentence and resending, never by handing the task to another party. No
    ActorHint literal says "the caller again", and the reviewer may be either
    kind — an agent through hub_submit_review or a human through the task
    card. So the literal is the closest one and the real constraint lives in
    ``required_role``, the same shape self_review_forbidden_detail uses for
    the same impossibility. Declaring it matters either way: undeclared, the
    envelope falls back to "agent" *with a warning*, and a caller reading that
    fallback would think the developer is on the hook for an empty verdict.
    """
    return enrich_error_payload(
        {
            "reason": "changes_requested_requires_content",
            "actor_hint": "agent",
            "required_role": "reviewer",
            "retry_by_same_caller": True,
            "message": (
                "changes_requested requires a reason: send at least one "
                "finding, or a non-empty comments text"
            ),
            "hint": (
                "Say what to redo — one sentence in `comments` is enough; "
                "structured `findings` are optional. A verdict with neither "
                "leaves the developer guessing."
            ),
            "suggested_tool": "hub_submit_review",
        }
    )


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


# What a refusal WAS, when the endpoint said it in prose instead of a slug
# (#882). Every one of these already had a name; the name was simply never
# derived, so 22 of the first telemetry window's 48 errors were filed under
# ``api_error`` — a bucket that cannot tell a caller's mistake from a gate
# refusal from an outage, which is the one distinction the report exists for.
#
# The actor differs per class and is not decoration: a 5xx is the hub failing,
# and waking the agent to fix its own arguments over an outage sends the work
# to somebody who cannot do it (#1040). ``api_error`` survives for statuses
# that genuinely do not classify — it stays honest by getting smaller, not by
# being renamed.
_STATUS_REASONS: dict[int, tuple[str, str]] = {
    404: ("not_found", "agent"),
    409: ("conflict", "agent"),
    422: ("validation_error", "agent"),
    429: ("rate_limited", "agent"),
}


def _reason_for_status(status_code: int) -> tuple[str, str]:
    """Refusal class and whose move it is, from the status alone."""
    known = _STATUS_REASONS.get(status_code)
    if known is not None:
        return known
    if 500 <= status_code <= 599:
        return "server_error", "human"
    # Malformed input is the caller's to fix, so the agent really is next.
    return "api_error", "agent"


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
    reason, actor = _reason_for_status(status_code)
    return enrich_error_payload(
        {
            "reason": reason,
            "message": msg,
            "hint": msg,
            "actor_hint": actor,
            "suggested_tool": None,
            "status_code": status_code,
        }
    )
