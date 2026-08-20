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

import fnmatch

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


# The class each bucket carries — the same floors the feature branches below
# apply, kept in one place so a project map cannot mean something different
# from the built-in derivation.
_BUCKET_CLASS: dict[str, RiskClass] = {
    "docs": RiskClass.r0,
    "tests": RiskClass.r1,
    "presentation": RiskClass.r1,
    "code": RiskClass.r2,
    "contract": RiskClass.r2,
    "auth": RiskClass.r3,
    "migration": RiskClass.r3,
}


def _at_least(current: RiskClass, floor: RiskClass) -> RiskClass:
    return floor if _ORDER.index(floor) > _ORDER.index(current) else current


def _is_docs(area: str) -> bool:
    return area.startswith("docs/") or area.endswith(".md")


def _is_tests(area: str) -> bool:
    return area.startswith("tests/") or "/tests/" in area


def _is_presentation(area: str) -> bool:
    return area.startswith("hub/templates/") or area.startswith("hub/static/")


def _map_bucket(area: str, project_map: dict[str, str] | None) -> str | None:
    """The bucket a project's own map assigns to ``area``, if any (#760).

    Three matching forms, kept few so a rule stays readable at a glance:
    an exact path, a directory prefix (``src/``), or a glob (``src/**``,
    ``*.md``) — where ``*`` crosses separators, which is what makes the
    two-star form the natural way to name a subtree.
    """
    if not project_map:
        return None
    for pattern, bucket in project_map.items():
        if area == pattern:
            return bucket
        if pattern.endswith("/") and area.startswith(pattern):
            return bucket
        if fnmatch.fnmatchcase(area, pattern):
            return bucket
    return None


def derive_risk_class(
    affected_areas: list[str] | None,
    project_map: dict[str, str] | None = None,
) -> tuple[RiskClass | None, list[str]]:
    """Compute (class, reasons) from declared areas; (None, []) without facts.

    The class is the MAXIMUM over triggered features, so adding a harmless
    path to a risky change can only keep or raise the class, never lower it.

    ``project_map`` (#760) lets a project describe its OWN paths, because the
    built-in map above knows exactly one repository — the hub's. For a
    satellite repository every source file lands in "outside the known map"
    and costs R2, which is a correct default and a useless permanent state.

    The map only ADDS. A mapped path is classified by its bucket IN ADDITION
    to whatever the built-in map already sees in it, and the class is still
    the maximum — so a rule saying ``hub/db.py: docs`` does not demote a
    migration, it merely adds a docs feature nobody needed. What a map can do
    is stop a path from being unknown, and that is the whole point of it.
    """
    areas = [a.strip() for a in (affected_areas or []) if a and a.strip()]
    if not areas:
        return None, []

    result = RiskClass.r0
    reasons: list[str] = []

    # What the BUILT-IN map sees. These sets drive the reason texts, which are
    # written in the hub's own vocabulary ("код хаба") and would be a lie
    # about a satellite's file; paths the project map rescued get their own
    # line below, naming the bucket their owner assigned.
    migration_native = sorted(a for a in areas if a == _MIGRATION_SURFACE)
    auth_native = sorted(a for a in areas if a in _AUTH_SURFACES)
    contract_native = sorted(a for a in areas if a in _CONTRACT_SURFACES)
    code_native = sorted(
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
    presentation_native = sorted(a for a in areas if _is_presentation(a))
    tests_native = sorted(a for a in areas if _is_tests(a))
    docs_native = sorted(a for a in areas if _is_docs(a))
    native = (
        set(migration_native)
        | set(auth_native)
        | set(contract_native)
        | set(code_native)
        | set(presentation_native)
        | set(tests_native)
        | set(docs_native)
    )

    # What the project's own map adds, for the paths the built-in map missed.
    rescued: dict[str, list[str]] = {}
    for area in areas:
        if area in native:
            continue
        bucket = _map_bucket(area, project_map)
        if bucket:
            rescued.setdefault(bucket, []).append(area)
    rescued_all = {a for paths in rescued.values() for a in paths}

    if migration_native or "migration" in rescued:
        result = _at_least(result, RiskClass.r3)
        if migration_native:
            reasons.append(
                "R3: затронут hub/db.py — возможна миграция; любая миграция — "
                "минимум R3 (правило владельца 31.07.2026)"
            )
    if auth_native or "auth" in rescued:
        result = _at_least(result, RiskClass.r3)
        if auth_native:
            reasons.append(
                "R3: аутентификация/ролевые проверки ("
                + ", ".join(auth_native)
                + ") — операции без дешёвого отката"
            )
    if contract_native or "contract" in rescued:
        result = _at_least(result, RiskClass.r2)
        if contract_native:
            reasons.append(
                "R2: меняется публичный контракт (" + ", ".join(contract_native) + ")"
            )
    if code_native or "code" in rescued:
        result = _at_least(result, RiskClass.r2)
        if code_native:
            reasons.append("R2: изменяется код хаба (" + ", ".join(code_native) + ")")
    if presentation_native or "presentation" in rescued:
        result = _at_least(result, RiskClass.r1)
        if presentation_native:
            reasons.append(
                "R1: шаблоны/статика (" + ", ".join(presentation_native) + ")"
            )
    if tests_native or "tests" in rescued:
        result = _at_least(result, RiskClass.r1)
        if tests_native:
            reasons.append("R1: тесты (" + ", ".join(tests_native) + ")")

    unknown_hits = sorted(a for a in areas if a not in native and a not in rescued_all)
    if unknown_hits:
        # A path the map does not know is not therefore safe — CI configs
        # and deploy scripts live exactly here. Unknown costs MORE than
        # known-benign, never less.
        result = _at_least(result, RiskClass.r2)
        reasons.append(
            "R2: области вне известной карты (" + ", ".join(unknown_hits) + ")"
        )

    if rescued:
        # One line, sorted by class descending, so the heaviest rule the owner
        # wrote is the first thing read: "R2: … src/** → code" argues for
        # itself the way a bare class never could.
        described = ", ".join(
            f"{area} → {bucket}"
            for bucket in sorted(
                rescued, key=lambda b: _ORDER.index(_BUCKET_CLASS[b]), reverse=True
            )
            for area in sorted(rescued[bucket])
        )
        top = max(_BUCKET_CLASS[b] for b in rescued)
        reasons.append(f"{top.value}: пути описаны картой проекта ({described})")

    if docs_native and not reasons:
        # Only docs — say WHY the class is low rather than staying silent:
        # a bare R0 is as unarguable as a bare R3.
        reasons.append(
            "R0: только документация/тексты (" + ", ".join(docs_native) + ")"
        )

    return result, reasons
