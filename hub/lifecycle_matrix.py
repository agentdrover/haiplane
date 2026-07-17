"""Executable ownership/deadline matrix for non-terminal lifecycle instances (#418).

The two-colour rule from #319/#393 classified by ``status`` alone, but two
statuses split by ownership: ``running`` and ``review`` each have a headless
(job-driven) and an interactive (pair / client-driven) variant that belong to
different actors. This module is the single source of truth that maps every
*lifecycle instance* — a status plus its job discriminator — to an owner, the
next actor, a deadline policy, an escalation target and an observable surface.

Invariants enforced by the coverage tests:
- every non-terminal ``TaskStatus`` (and every running/review discriminator)
  has exactly one policy — a new enum member breaks the suite until added;
- ``machine``-owned instances must carry a finite deadline and an escalation;
- ``human``/``agent_queue`` instances must name a surface and next actor and
  must NOT auto-transition (deadline is ``None``).

The poller reads :func:`machine_deadline_policies` to drive deadline
transitions; ``claimed`` (escalation ``open``) and missing-job escalation are
implemented in #417 and only documented here so the matrix stays complete.
"""

from __future__ import annotations

from dataclasses import dataclass

from hub.models import ACTIVE_STATUSES, FINAL_STATUSES, TaskStatus

OWNER_MACHINE = "machine"
OWNER_HUMAN = "human"
OWNER_AGENT_QUEUE = "agent_queue"

VALID_OWNERS = frozenset({OWNER_MACHINE, OWNER_HUMAN, OWNER_AGENT_QUEUE})


@dataclass(frozen=True)
class LifecyclePolicy:
    """Policy for one lifecycle instance (status + optional discriminator)."""

    instance: str
    status: str
    discriminator: str | None  # "headless" | "pair" | "client" | None
    owner: str
    next_actor: str
    surface: str
    # config attribute holding the deadline in minutes; None ⇒ no auto-transition
    deadline_config: str | None
    escalation: str | None  # target status when the deadline passes
    reason: str | None  # machine reason code emitted on escalation
    # SQL discriminator on the tasks row (only one is ever set)
    require_job_id: bool = False
    require_review_job_id: bool = False


def _machine(instance, status, *, deadline_config, reason, escalation="needs_decision",
             discriminator=None, require_job_id=False, require_review_job_id=False):
    return LifecyclePolicy(
        instance=instance,
        status=status,
        discriminator=discriminator,
        owner=OWNER_MACHINE,
        next_actor="hub",
        surface="watchdog",
        deadline_config=deadline_config,
        escalation=escalation,
        reason=reason,
        require_job_id=require_job_id,
        require_review_job_id=require_review_job_id,
    )


def _waiting(instance, status, *, owner, next_actor, surface, discriminator=None):
    return LifecyclePolicy(
        instance=instance,
        status=status,
        discriminator=discriminator,
        owner=owner,
        next_actor=next_actor,
        surface=surface,
        deadline_config=None,
        escalation=None,
        reason=None,
    )


LIFECYCLE_MATRIX: dict[str, LifecyclePolicy] = {
    # --- human-owned: a person must act; no auto-transition, inbox surface ---
    "draft": _waiting(
        "draft", "draft", owner=OWNER_HUMAN,
        next_actor="human", surface="inbox:drafts",
    ),
    "needs_info": _waiting(
        "needs_info", "needs_info", owner=OWNER_HUMAN,
        next_actor="human", surface="inbox:questions",
    ),
    "needs_decision": _waiting(
        "needs_decision", "needs_decision", owner=OWNER_HUMAN,
        next_actor="human", surface="inbox:decisions",
    ),
    "review:client": _waiting(
        "review:client", "review", owner=OWNER_HUMAN, discriminator="client",
        next_actor="human", surface="inbox:review",
    ),
    # --- agent_queue: waiting for an agent to pick up or drive interactively ---
    "open": _waiting(
        "open", "open", owner=OWNER_AGENT_QUEUE,
        next_actor="agent", surface="board",
    ),
    "running:pair": _waiting(
        "running:pair", "running", owner=OWNER_AGENT_QUEUE, discriminator="pair",
        next_actor="agent", surface="stale-alert",
    ),
    # --- machine-owned: the poller must auto-transition on deadline ---
    "claimed": _machine(
        "claimed", "claimed", deadline_config="CLAIM_LEASE_MINUTES",
        reason="claim_lease_expired", escalation="open",
    ),
    "running:headless": _machine(
        "running:headless", "running", deadline_config="DEADLINE_RUNNING_MINUTES",
        reason="running_deadline", discriminator="headless", require_job_id=True,
    ),
    "review:headless": _machine(
        "review:headless", "review", deadline_config="DEADLINE_REVIEW_MINUTES",
        reason="review_deadline", discriminator="headless", require_review_job_id=True,
    ),
    "fix_requested": _machine(
        "fix_requested", "fix_requested",
        deadline_config="DEADLINE_FIX_REQUESTED_MINUTES", reason="fix_deadline",
    ),
    "ci_check": _machine(
        "ci_check", "ci_check", deadline_config="DEADLINE_CI_CHECK_MINUTES",
        reason="ci_check_deadline",
    ),
    "pending_report": _machine(
        "pending_report", "pending_report",
        deadline_config="DEADLINE_PENDING_REPORT_MINUTES",
        reason="pending_report_deadline",
    ),
}


def non_terminal_statuses() -> frozenset[TaskStatus]:
    """The 10 statuses a task can sit in without being done."""
    return frozenset(s for s in TaskStatus if s not in FINAL_STATUSES)


def resolve_instance(
    status: str, *, job_id: str | None, review_job_id: str | None
) -> str:
    """Instance key for a task row — splits running/review by discriminator."""
    if status == "running":
        return "running:headless" if job_id else "running:pair"
    if status == "review":
        return "review:headless" if review_job_id else "review:client"
    return status


def policy_for(
    status: str, *, job_id: str | None = None, review_job_id: str | None = None
) -> LifecyclePolicy:
    return LIFECYCLE_MATRIX[resolve_instance(status, job_id=job_id, review_job_id=review_job_id)]


def machine_deadline_policies() -> list[LifecyclePolicy]:
    """Machine-owned instances the poller auto-transitions on deadline."""
    return [p for p in LIFECYCLE_MATRIX.values() if p.owner == OWNER_MACHINE]


__all__ = [
    "LIFECYCLE_MATRIX",
    "LifecyclePolicy",
    "OWNER_MACHINE",
    "OWNER_HUMAN",
    "OWNER_AGENT_QUEUE",
    "VALID_OWNERS",
    "ACTIVE_STATUSES",
    "non_terminal_statuses",
    "resolve_instance",
    "policy_for",
    "machine_deadline_policies",
]
