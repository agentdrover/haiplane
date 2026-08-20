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

log = logging.getLogger(__name__)

# Surfaces that ARE the ladder and the gates: the risk derivation, this very
# module, the lifecycle transitions, the switch itself, auth, and the process
# rules the gates enforce. A task declaring any of these is a change to the
# oversight machinery and stays with the owner even at a low class — matched
# by exact path or prefix against declared affected_areas.
LADDER_SURFACES: tuple[str, ...] = (
    "hub/services/risk_class.py",
    "hub/services/auto_approve.py",
    "hub/services/lifecycle.py",
    "hub/config.py",
    "hub/auth.py",
    "hub/mcp_internal_auth.py",
    "docs/agent-context/",
    "docs/repository-rules.md",
    ".github/",
)

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
    ceiling = _AUTO_BAND.get(mode)
    if ceiling is None:
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
    if order.index(risk) > order.index(ceiling):
        return False

    areas = deserialize_str_list(row["affected_areas"])
    ladder = _touches_ladder(areas)
    if ladder:
        # The system does not simplify its own rules: gate/ladder changes
        # wait for the owner at ANY class.
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
            f"Автоодобрено политикой: класс {risk.value} не выше порога "
            f"{ceiling.value} (выключатель OPENCLAW_AUTO_APPROVE_MAX_CLASS="
            f"{mode}). Признаки: {'; '.join(reasons) or '—'}."
        ),
        author_kind="hub",
    )
    await repo.insert_event(
        db,
        kind="task_approved",
        task_id=task_id,
        actor="hub",
        payload={"auto": True, "risk_class": risk.value, "ceiling": ceiling.value},
    )
    log.info("auto-approved draft #%s at class %s", task_id, risk.value)
    return True
