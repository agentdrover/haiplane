"""Risk class derivation from observable facts (#582, epic #578).

The class is computed from what a task DECLARES it will touch — never from
anything the author says about risk itself. A self-assessed class would be
R0 across the board: not from malice, that is simply what optimisation does.
So the create/refine models carry no class field at all (#581), and this
module looks only at ``affected_areas``.

Shadow-mode honesty rules:

- No declared areas → no facts → the class stays "not computed" (``None``),
  which is deliberately distinct from R0 ("computed and found harmless").
- ANY migration is at least R3 — the owner's rule of 2026-07-31. Splitting
  additive (R2) from destructive (R4) migrations starts only after enough
  statistics exist; starting with the split would let the agent judge
  whether its own migration is safe.
- Every triggered feature is returned as a human-readable reason:
  «R3, потому что миграция» can be argued with; a bare «R3» cannot.

Known limitation (recorded in the task): ``affected_areas`` is filled by the
task author, so the SOURCE of these facts is still partly self-reported.
That is cured by recomputing from the actual diff at submission — #583, the
next task of this feature — not by trusting the declaration less here.
"""

from __future__ import annotations

from hub.models import RiskClass

# Files whose change means a public-contract change: API request/response
# shapes, MCP tool surface, CLI. Matched by exact repo-relative path.
_CONTRACT_SURFACES: frozenset[str] = frozenset(
    {
        "hub/models.py",
        "hub/app.py",
        "hub/mcp_server.py",
        "hub/cli.py",
    }
)

# Authentication and role checks: changes here move who is allowed to do
# what, and a mistake is an open door — no cheap rollback.
_AUTH_SURFACES: frozenset[str] = frozenset(
    {
        "hub/auth.py",
        "hub/mcp_internal_auth.py",
    }
)

# Where migrations live. Touching this file does not PROVE a migration, but
# shadow mode judges declarations; the diff-based recompute (#583) will see
# the actual ALTER/CREATE. Erring upward is the point of the owner's rule.
_MIGRATION_SURFACE = "hub/db.py"

_ORDER: list[RiskClass] = [
    RiskClass.r0,
    RiskClass.r1,
    RiskClass.r2,
    RiskClass.r3,
    RiskClass.r4,
    RiskClass.r5,
]


def _at_least(current: RiskClass, floor: RiskClass) -> RiskClass:
    return floor if _ORDER.index(floor) > _ORDER.index(current) else current


def _is_docs(area: str) -> bool:
    return area.startswith("docs/") or area.endswith(".md")


def _is_tests(area: str) -> bool:
    return area.startswith("tests/") or "/tests/" in area


def _is_presentation(area: str) -> bool:
    return area.startswith("hub/templates/") or area.startswith("hub/static/")


def derive_risk_class(
    affected_areas: list[str] | None,
) -> tuple[RiskClass | None, list[str]]:
    """Compute (class, reasons) from declared areas; (None, []) without facts.

    The class is the MAXIMUM over triggered features, so adding a harmless
    path to a risky change can only keep or raise the class, never lower it.
    """
    areas = [a.strip() for a in (affected_areas or []) if a and a.strip()]
    if not areas:
        return None, []

    result = RiskClass.r0
    reasons: list[str] = []

    migration_hits = sorted(a for a in areas if a == _MIGRATION_SURFACE)
    auth_hits = sorted(a for a in areas if a in _AUTH_SURFACES)
    contract_hits = sorted(a for a in areas if a in _CONTRACT_SURFACES)
    code_hits = sorted(
        a
        for a in areas
        if a.startswith("hub/")
        and a not in _CONTRACT_SURFACES
        and a not in _AUTH_SURFACES
        and a != _MIGRATION_SURFACE
        and not _is_presentation(a)
        and not _is_tests(a)
        and not _is_docs(a)
    )
    presentation_hits = sorted(a for a in areas if _is_presentation(a))
    tests_hits = sorted(a for a in areas if _is_tests(a))
    docs_hits = sorted(a for a in areas if _is_docs(a))
    known = (
        set(migration_hits)
        | set(auth_hits)
        | set(contract_hits)
        | set(code_hits)
        | set(presentation_hits)
        | set(tests_hits)
        | set(docs_hits)
    )
    unknown_hits = sorted(a for a in areas if a not in known)

    if migration_hits:
        result = _at_least(result, RiskClass.r3)
        reasons.append(
            "R3: затронут hub/db.py — возможна миграция; любая миграция — "
            "минимум R3 (правило владельца 31.07.2026)"
        )
    if auth_hits:
        result = _at_least(result, RiskClass.r3)
        reasons.append(
            "R3: аутентификация/ролевые проверки ("
            + ", ".join(auth_hits)
            + ") — операции без дешёвого отката"
        )
    if contract_hits:
        result = _at_least(result, RiskClass.r2)
        reasons.append(
            "R2: меняется публичный контракт (" + ", ".join(contract_hits) + ")"
        )
    if code_hits:
        result = _at_least(result, RiskClass.r2)
        reasons.append("R2: изменяется код хаба (" + ", ".join(code_hits) + ")")
    if presentation_hits:
        result = _at_least(result, RiskClass.r1)
        reasons.append("R1: шаблоны/статика (" + ", ".join(presentation_hits) + ")")
    if tests_hits:
        result = _at_least(result, RiskClass.r1)
        reasons.append("R1: тесты (" + ", ".join(tests_hits) + ")")
    if unknown_hits:
        # A path the map does not know is not therefore safe — CI configs
        # and deploy scripts live exactly here. Unknown costs MORE than
        # known-benign, never less.
        result = _at_least(result, RiskClass.r2)
        reasons.append(
            "R2: области вне известной карты (" + ", ".join(unknown_hits) + ")"
        )
    if docs_hits and not reasons:
        # Only docs — say WHY the class is low rather than staying silent:
        # a bare R0 is as unarguable as a bare R3.
        reasons.append("R0: только документация/тексты (" + ", ".join(docs_hits) + ")")

    return result, reasons
