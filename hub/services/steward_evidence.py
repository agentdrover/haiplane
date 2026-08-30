"""The evidence packet — the steward's only input (#1074, F2 #996).

The steward judges; it does not investigate. Everything it is allowed to
reason from is assembled here, once, from sources the hub can re-check by
itself: a ground the hub cannot verify may be recorded elsewhere, but it
never enters the packet and therefore never supports a judgement.

Nothing here is a new source. The brief (#308), the machine-review report,
the CI run pinned to the submitted sha (#546/#572), the branch tip (#572),
the diff against declared areas and the recomputed class (#550/#583), the
AC locators (#505/#506), the base branch state (#921) and the dependency
edges (#483/#485) all exist already. This module puts them in one shape.

The shape is the point. Every fact carries one of two STATES:

``present``
    the hub looked and has an answer — including a NEGATIVE answer, like a
    failing CI run or a diff outside the declared areas;
``absent``
    the hub could not look, or there was nothing to look at.

They are different values, never the same one, because the mistake they
prevent has been made before: on #762 an unavailable clone read as a clean
tree, and on the harness side ``raw_count=0`` read as "no findings" rather
than "no data" (#750). A judge that cannot tell the two apart approves the
second case as if it were the first. So a caller asking "is CI green?" gets
``present`` + a status it can inspect, or ``absent`` + a reason — and the
absence is not spellable as a value of the fact.

The vocabulary of sources is the one the judgement contract already closes
over (``STEWARD_GROUND_SOURCES``, #1022). It is imported, not copied: two
lists would drift, and a ground the packet can produce but the contract
cannot name is a ground no judgement can cite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.models import STEWARD_GROUND_SOURCES, ReviewBrief, RiskClass
from hub.services.ci_report import (
    VALIDATION_FAIL,
    VALIDATION_PASS,
)

PRESENT = "present"
ABSENT = "absent"

# Reasons an absence is an absence. Codes rather than prose so the caller can
# branch on them and the digest can count them; the human-facing wording rides
# along in ``detail``.
NO_REPORT = "no_report"
REPORT_OTHER_GENERATION = "report_other_generation"
NO_PINNED_SHA = "no_pinned_sha"
NO_CI_FOR_SHA = "no_ci_for_sha"
CI_RUN_INCONCLUSIVE = "ci_run_inconclusive"
TIP_UNREADABLE = "tip_unreadable"
DIFF_UNREADABLE = "diff_unreadable"
SURFACE_UNKNOWN = "surface_unknown"
NO_STORED_CLASS = "no_stored_class"
NO_TEST_LOCATORS = "no_test_locators"
NO_PROJECT = "no_project"
BASE_UNKNOWN = "base_unknown"

# Only these two say something about the CODE. ``unknown`` and ``skipped``
# describe the RUN, and ci_report already refuses to stamp them onto a task
# for exactly that reason — carrying them here as a value would let "the run
# told us nothing" be read as "the run said something unflattering".
_CONCLUSIVE_CI = frozenset({VALIDATION_PASS, VALIDATION_FAIL})


def _check_source(source: str) -> None:
    """Refuse a source the judgement contract cannot name (#1022).

    Loud on purpose. A packet quietly carrying an unknown source would hand
    the steward a ground it can cite and the hub cannot re-check — which is
    the one thing this module exists to prevent.
    """
    if source not in STEWARD_GROUND_SOURCES:
        raise ValueError(
            f"source {source!r} is outside the closed set of steward grounds; "
            f"allowed: {', '.join(STEWARD_GROUND_SOURCES)}"
        )


@dataclass(frozen=True)
class EvidenceFact:
    """One fact, or one honest hole where a fact would have been."""

    source: str
    state: str
    detail: str
    value: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def is_present(self) -> bool:
        return self.state == PRESENT

    @property
    def is_absent(self) -> bool:
        return self.state == ABSENT


def present(source: str, detail: str, **value: Any) -> EvidenceFact:
    """The hub looked and has an answer — good, bad or indifferent."""
    _check_source(source)
    return EvidenceFact(source=source, state=PRESENT, detail=detail, value=value)


def absent(source: str, reason: str, detail: str) -> EvidenceFact:
    """The hub could not look, or there was nothing to look at."""
    _check_source(source)
    return EvidenceFact(source=source, state=ABSENT, detail=detail, reason=reason)


@dataclass(frozen=True)
class EvidencePacket:
    """Everything the steward may reason from, for ONE generation.

    ``facts`` is keyed by source and holds every source in the closed set —
    a source is never missing from the packet, only ever ``absent``. That
    difference matters to the reader: "the packet does not mention CI" and
    "the packet says CI could not be read" are not the same claim, and only
    the second one is something the hub actually knows.
    """

    task_id: int
    generation: int
    brief: ReviewBrief | None
    facts: dict[str, EvidenceFact]

    def fact(self, source: str) -> EvidenceFact:
        _check_source(source)
        return self.facts[source]

    def absent_sources(self) -> list[str]:
        return [s for s in STEWARD_GROUND_SOURCES if self.facts[s].is_absent]


def _finding_dicts(raw: str | None) -> list[dict]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [f for f in value if isinstance(f, dict)]


async def _report_fact(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> EvidenceFact:
    """The machine-review report OF THIS GENERATION, or its absence.

    A report from an earlier generation is not weaker evidence about the code
    under review — it is evidence about other code. It lands as an absence
    naming the generation it actually covers, never as a report.
    """
    source = "machine_review_report"
    row = await repo.get_latest_machine_review(db, task_id)
    if row is None:
        return absent(source, NO_REPORT, "машинного ревью по задаче нет вовсе")
    review = dict(row)
    reported_generation = review.get("submission_generation") or 0
    if reported_generation != generation:
        return absent(
            source,
            REPORT_OTHER_GENERATION,
            f"последний отчёт покрывает генерацию {reported_generation}, "
            f"судится {generation}",
        )
    confirmed = _finding_dicts(review.get("findings_confirmed"))
    unresolved = _finding_dicts(review.get("unresolved"))
    rejected = _finding_dicts(review.get("findings_rejected"))
    return present(
        source,
        f"отчёт #{review['id']} генерации {generation}: "
        f"confirmed {len(confirmed)}, unresolved {len(unresolved)}, "
        f"raw {review.get('raw_count')}, incomplete {bool(review.get('incomplete'))}",
        review_id=review["id"],
        generation=generation,
        raw_count=review.get("raw_count") or 0,
        confirmed=confirmed,
        unresolved=unresolved,
        rejected=rejected,
        incomplete=bool(review.get("incomplete")),
        tokens_spent=review.get("tokens_spent"),
        self_reviewed=bool(review.get("self_reviewed")),
    )


async def _ci_fact(
    db: aiosqlite.Connection, task_id: int, pinned_sha: str
) -> EvidenceFact:
    """CI as recorded for the PINNED sha (#546/#572), or its absence."""
    source = "ci_pinned_sha"
    if not pinned_sha:
        return absent(
            source, NO_PINNED_SHA, "сдача не закрепила sha — не о чем спрашивать CI"
        )
    row = await repo.get_ci_run_report(db, task_id, pinned_sha)
    if row is None:
        return absent(
            source,
            NO_CI_FOR_SHA,
            f"для закреплённого {pinned_sha[:12]} прогон не отчитан",
        )
    status = (dict(row).get("validation_status") or "").strip()
    if status not in _CONCLUSIVE_CI:
        return absent(
            source,
            CI_RUN_INCONCLUSIVE,
            f"прогон на {pinned_sha[:12]} отчитан как {status or 'без статуса'} — "
            "это факт о прогоне, а не о коде",
        )
    return present(
        source,
        f"CI на {pinned_sha[:12]}: {status}",
        sha=pinned_sha,
        validation_status=status,
        passed=status == VALIDATION_PASS,
    )


async def _tip_fact(
    db: aiosqlite.Connection, task: dict[str, Any], pinned_sha: str
) -> EvidenceFact:
    """Does the branch still stand where the submission pinned it (#572)?"""
    from hub.services.lifecycle import resolve_branch_tip

    source = "branch_tip"
    tip, reason = await resolve_branch_tip(db, task["id"], task.get("branch") or "")
    if not tip:
        return absent(source, TIP_UNREADABLE, reason or "вершину ветки не прочитать")
    return present(
        source,
        f"вершина {tip[:12]}, закреплено {pinned_sha[:12] or 'ничего'}",
        tip=tip,
        pinned_sha=pinned_sha,
        moved=bool(pinned_sha) and tip != pinned_sha,
    )


def _surface_fact(
    task: dict[str, Any], diff_paths: list[str] | None, diff_reason: str
) -> EvidenceFact:
    """The actual diff against the declared areas (#550)."""
    from hub.services.lifecycle import _surface_check

    source = "diff_vs_areas"
    if diff_paths is None:
        return absent(source, DIFF_UNREADABLE, diff_reason or "дифф ветки не прочитать")
    verdict, undeclared, detail = _surface_check(task, diff_paths, diff_reason)
    if verdict == "unknown":
        return absent(source, SURFACE_UNKNOWN, detail or "сверка областей не выполнена")
    return present(
        source,
        detail
        or f"дифф из {len(diff_paths)} путей, вне заявленного: {len(undeclared)}",
        paths=list(diff_paths),
        undeclared=list(undeclared),
        within_declared=verdict == "ok",
    )


async def _risk_fact(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    diff_paths: list[str] | None,
    diff_reason: str,
) -> EvidenceFact:
    """The stored class beside the one this diff implies (#550/#583).

    Both halves are required. A stored class with no recompute says nothing
    about THIS submission's blast radius, and a recompute with no stored
    class has nothing to be compared against (#838) — either way the fact is
    absent rather than half-stated.
    """
    from hub.commit_scope import ROUTINE_PATHS
    from hub.services.project_policy import risk_map_for_task
    from hub.services.risk_class import derive_risk_class

    source = "risk_class"
    stored_raw = (task.get("risk_class") or "").strip()
    if not stored_raw:
        return absent(
            source, NO_STORED_CLASS, "класс риска задачи не вычислен — сверять не с чем"
        )
    if diff_paths is None:
        return absent(
            source,
            DIFF_UNREADABLE,
            f"класс сдачи не пересчитать: {diff_reason or 'дифф не прочитан'}",
        )
    diff_class, reasons = derive_risk_class(
        [p for p in diff_paths if p not in ROUTINE_PATHS],
        await risk_map_for_task(db, task["id"]),
    )
    order = list(RiskClass)
    try:
        stored = RiskClass(stored_raw)
    except ValueError:
        return absent(
            source,
            NO_STORED_CLASS,
            f"сохранённый класс {stored_raw!r} не разобрать",
        )
    raised = diff_class is not None and order.index(diff_class) > order.index(stored)
    return present(
        source,
        f"класс задачи {stored.value}, по диффу "
        f"{diff_class.value if diff_class else 'сигнала нет'}"
        + (" — ВЫШЕ заявленного" if raised else ""),
        stored=stored.value,
        recomputed=diff_class.value if diff_class else None,
        raised=raised,
        reasons=list(reasons),
    )


def _locator_fact(brief: ReviewBrief | None) -> EvidenceFact:
    """How each test-bound AC's locator resolved (#505/#506)."""
    source = "ac_locator"
    resolutions = list(brief.locator_resolution) if brief else []
    if not resolutions:
        return absent(
            source,
            NO_TEST_LOCATORS,
            "у задачи нет критериев с verifiable_by=test — локаторов не о чем сообщать",
        )
    items = [r.model_dump() for r in resolutions]
    # ``resolvable`` is the only status that says the named test exists; the
    # other three (missing, unparseable, unknown) each say something else, and
    # #506's whole point is that they must not be collapsed into one another.
    unresolved = [i for i in items if (i.get("status") or "") != "resolvable"]
    return present(
        source,
        f"локаторов {len(items)}, не разрешилось {len(unresolved)}",
        locators=items,
        unresolved=len(unresolved),
    )


async def _base_fact(db: aiosqlite.Connection, project_row: Any | None) -> EvidenceFact:
    """Is the base branch itself green (#921)?

    Read-only on purpose: ``check_project`` announces a fresh breakage as a
    side effect, and assembling evidence must not change the world it
    describes.
    """
    from hub.integrations.registry import plugins
    from hub.services.project_policy import base_branch_of
    from hub.services.red_base import UNKNOWN, read_state

    source = "red_base"
    if project_row is None:
        return absent(source, NO_PROJECT, "проект задачи не разрешён")
    project = dict(project_row)
    branch = base_branch_of(project_row)
    try:
        runs = await plugins.git_ops.branch_ci_runs(
            branch,
            repo=(project.get("workspace_path") or "").strip() or None,
            gh_repo=(project.get("repo") or "").strip() or None,
        )
    except Exception:  # noqa: BLE001 — the packet assembles regardless
        runs = None
    state = read_state(branch, runs)
    if state.status == UNKNOWN:
        return absent(
            source, BASE_UNKNOWN, state.reason or f"состояние базы {branch} неизвестно"
        )
    return present(
        source,
        f"база {branch}: {state.status}",
        branch=branch,
        status=state.status,
        red_sha=state.red_sha,
        last_green_sha=state.last_green_sha,
    )


async def _dependency_fact(db: aiosqlite.Connection, task_id: int) -> EvidenceFact:
    """What this task waits for, judged by DELIVERY rather than status (#484/#485)."""
    source = "dependency_state"
    edges = await repo.list_task_dependencies(db, task_id)
    blocked_by = [dict(e) for e in edges.get("blocked_by", [])]
    undelivered = [e for e in blocked_by if not (e.get("merges") or 0)]
    return present(
        source,
        f"блокеров {len(blocked_by)}, не доставлено {len(undelivered)}",
        blocked_by=[
            {
                "task_id": e.get("task_id"),
                "status": e.get("status"),
                "delivered": bool(e.get("merges") or 0),
            }
            for e in blocked_by
        ],
        undelivered=len(undelivered),
    )


async def build_evidence_packet(
    db: aiosqlite.Connection, task_id: int, generation: int | None = None
) -> EvidencePacket | None:
    """Assemble the packet for one task and one generation.

    Returns ``None`` when the task does not exist — the caller decides
    whether that is a 404 or a refused run. Everything else degrades into an
    ``absent`` fact: assembling evidence must never fail because a clone is
    missing or a network call did not answer, or the steward would be blind
    exactly when the situation is unusual.
    """
    from hub.services.lifecycle import _resolve_branch_diff
    from hub.services.review_brief import build_review_brief

    row = await repo.get_task(db, task_id)
    if row is None:
        return None
    task = dict(row)
    generation = (
        generation
        if generation is not None
        else (task.get("submission_generation") or 0)
    )
    pinned_sha = (task.get("submission_sha") or "").strip()
    project_row = await repo.resolve_project_for_task(db, task_id)
    brief = await build_review_brief(db, task_id)

    # One walk of the branch feeds two facts, as in auto_verdict (#583).
    diff_paths, diff_reason = await _resolve_branch_diff(db, task)

    facts = {
        f.source: f
        for f in [
            await _report_fact(db, task_id, generation),
            await _ci_fact(db, task_id, pinned_sha),
            await _tip_fact(db, task, pinned_sha),
            _surface_fact(task, diff_paths, diff_reason),
            await _risk_fact(db, task, diff_paths, diff_reason),
            _locator_fact(brief),
            await _base_fact(db, project_row),
            await _dependency_fact(db, task_id),
        ]
    }
    return EvidencePacket(
        task_id=task_id, generation=generation, brief=brief, facts=facts
    )
