"""Auto-approval of low-risk drafts (#584, epic #578, F2).

The first real removal of a human gate, taken as a narrow band: R0 (docs,
texts) and R1 (tests, templates, local cleanup) — classes DERIVED from
observable facts (#582), never declared by the author. Everything here is
built around four properties the task demands:

1. THE SWITCH — ``config.AUTO_APPROVE_MAX_CLASS``: 'off' by default, and
   turning it off restores today's behavior in full. Irreversible
   delegation is not delegation, it is abandonment of control.
2. THE REASON IN THE FEED — every auto-approval records which class passed
   and on which features it was derived; without that there is nothing to
   take apart after an incident.
3. HUB AUTHORSHIP — the record is written as the hub (author_kind=hub,
   #559), never as a principal: an auto-approval must not look like a
   human's decision.
4. "NOT COMPUTED" NEVER PASSES — the absence of a class is not low risk.

And one meta-rule: tasks that touch the gates or the ladder itself are
never auto-approved, whatever their class says. The system does not get to
simplify its own rules (see ``LADDER_SURFACES``).
"""

from __future__ import annotations

import logging

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import deserialize_str_list
from hub.models import RiskClass
from hub.services.project_policy import gate_policy_of

log = logging.getLogger(__name__)

# Surfaces that ARE the ladder and the gates: the risk derivation, this very
# module, the lifecycle transitions, the switch itself, auth, and the process
# rules the gates enforce. A task declaring any of these is a change to the
# oversight machinery and stays with the owner even at a low class — matched
# by exact path or prefix against declared affected_areas.
LADDER_SURFACES: tuple[str, ...] = (
    "hub/services/risk_class.py",
    "hub/services/auto_approve.py",
    "hub/services/auto_verdict.py",
    "hub/services/lifecycle.py",
    "hub/config.py",
    "hub/auth.py",
    "hub/mcp_internal_auth.py",
    "docs/agent-context/",
    "docs/repository-rules.md",
    ".github/",
)

# The classes the switch can name. R2 stays OUT: opening it is #585, and that
# task is conditioned on a measured agreement between the agent reviewer and
# the owner (#522/#527) — a condition set on 31.07.2026 and not yet met. The
# per-project ceiling below can therefore only tighten this band, never widen
# it, which is the only safe direction while the number is missing.
_AUTO_BAND: dict[str, RiskClass] = {"r0": RiskClass.r0, "r1": RiskClass.r1}


def _touches_ladder(areas: list[str]) -> list[str]:
    return sorted(
        a for a in areas if any(a == s or a.startswith(s) for s in LADDER_SURFACES)
    )


async def maybe_auto_approve(db: aiosqlite.Connection, task_id: int) -> bool:
    """Approve a DoR-passed low-class draft when the switch allows (#584).

    Called from the readiness-recalc funnel — the only place where
    ``dor_passed`` flips to true, so every path a draft can take to
    readiness (refine, bulk refine, AC/risk mutations) arrives here.
    Runs inside the caller's transaction; returns True when the draft
    was transitioned. Every refusal is silent by design: a draft that
    does not qualify simply keeps waiting for the human, exactly as
    today.
    """
    mode = (config.AUTO_APPROVE_MAX_CLASS or "off").strip().lower()
    global_ceiling = _AUTO_BAND.get(mode)
    if global_ceiling is None:
        # 'off' — and also any unknown value: a mistyped switch must fail
        # toward the human gate, never toward silent delegation.
        return False

    row = await repo.get_task(db, task_id)
    if row is None or row["status"] != "draft" or not row["dor_passed"]:
        return False

    raw_class = row["risk_class"]
    if not raw_class:
        # "Not computed" is not low risk (#581) — never auto-approved.
        return False
    try:
        risk = RiskClass(raw_class)
    except ValueError:
        return False

    order = list(RiskClass)

    areas = deserialize_str_list(row["affected_areas"])
    ladder = _touches_ladder(areas)
    if ladder:
        # The system does not simplify its own rules: gate/ladder changes
        # wait for the owner at ANY class.
        return False

    # #744: the policy says WHERE automation is allowed, the class says WHAT
    # is safe, and the global env above stays the kill-switch and the class
    # ceiling. Resolved the same way the git conveyor resolves it — by
    # walking the hierarchy — and every failure mode (no project, unparsable
    # policy, no dor=auto) refuses toward the human gate, never toward auto.
    # The default project cannot even store 'auto' (#743), so the hub's own
    # drafts can never take this path.
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return False
    policy = gate_policy_of(project)
    if policy.get("dor") != "auto":
        return False
    project_slug = project["slug"]

    # #760: the project may lower its own ceiling, never raise it. The global
    # switch stays the upper bound and the kill-switch — a project that asks
    # for more than the env allows simply gets the env's answer, and the feed
    # line below says so, because "why did this not auto-approve" must be
    # answerable without reading two configs and a deployment.
    project_ceiling = _AUTO_BAND.get(str(policy.get("dor_max_class") or "").lower())
    ceiling = global_ceiling
    if project_ceiling is not None and order.index(project_ceiling) < order.index(
        ceiling
    ):
        ceiling = project_ceiling
    if order.index(risk) > order.index(ceiling):
        return False

    transitioned = await repo.transition_status_if(
        db, task_id, expected_from="draft", new_status="open"
    )
    if not transitioned:
        return False

    reasons = deserialize_str_list(row["risk_class_reasons"])
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        (
            f"Автоодобрено политикой проекта {project_slug} (dor=auto): класс "
            f"{risk.value} не выше действующего потолка {ceiling.value}. "
            f"Потолки: проектный "
            f"{project_ceiling.value if project_ceiling else '—'}, глобальный "
            f"{global_ceiling.value} (HAIPLANE_AUTO_APPROVE_MAX_CLASS={mode}). "
            f"Признаки: {'; '.join(reasons) or '—'}."
        ),
        author_kind="hub",
    )
    # actor=policy (#744): distinguishable from a human click AND from other
    # hub service writes — the human_gates metric (#737) already excludes it
    # by this name, so the autopilot shows up as its own line, not as noise
    # in either column.
    await repo.insert_event(
        db,
        kind="task_approved",
        task_id=task_id,
        actor="policy",
        payload={
            "auto": True,
            "risk_class": risk.value,
            "ceiling": ceiling.value,
            "project_ceiling": project_ceiling.value if project_ceiling else None,
            "global_ceiling": global_ceiling.value,
            "project": project_slug,
        },
    )
    log.info(
        "auto-approved draft #%s at class %s (project %s)",
        task_id,
        risk.value,
        project_slug,
    )
    return True
