"""Accept the run evidence CI produced, and decide what it may speak for (#546).

The hub used to be the only thing that could write a run result, and it wrote
none: the runners existed but nothing called them, so the two gates that read
recorded results (#508, #510) could never be satisfied. Running them on the
production machine was rejected on 31.07.2026 — that would make the box a
second place where task-supplied commands execute. CI already runs the suite in
a disposable runner, so execution stays there and the hub becomes the party that
*checks and keeps* the fact instead of the party that produces it.

Two rules follow from that split, and they are the whole module:

1. Evidence is keyed by COMMIT, not by submission number. CI usually runs when
   the PR opens — before any submission exists, when the generation is still 0 —
   and again after a resubmission. The commit is the only identifier present in
   both moments, and the only one the reporter cannot choose for itself: the hub
   pins the tip at submission (#572) and compares.

2. Silence is never success and never failure. A missing report answers
   ``unknown`` with a named reason. A false ``fail`` would block a verdict for a
   reason unrelated to the work — the same mistake #506 made when it read an
   unavailable environment as an absence of problems.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from hub import repository as repo
from hub.services.ac_tests import (
    FAIL as AC_FAIL,
    NOT_FOUND as AC_NOT_FOUND,
    PASS as AC_PASS,
    record_ac_test_results,
    test_ac_nodeids,
)
from hub.services.validation_run import (
    FAIL as VALIDATION_FAIL,
    PASS as VALIDATION_PASS,
    SKIPPED as VALIDATION_SKIPPED,
    UNKNOWN as VALIDATION_UNKNOWN,
    record_validation_result,
)

log = logging.getLogger("hub")

AC_STATUSES = frozenset({AC_PASS, AC_FAIL, AC_NOT_FOUND})
VALIDATION_STATUSES = frozenset(
    {VALIDATION_PASS, VALIDATION_FAIL, VALIDATION_UNKNOWN, VALIDATION_SKIPPED}
)
# Only these two are facts about the code. ``unknown`` and ``skipped`` say
# something about the run, so they are kept in the report row and deliberately
# NOT written onto the task: an "unknown" stamped for the current generation
# would read as "ran, did not pass" in the gate's wording (#510) — a false
# accusation. Leaving the fields untouched keeps the honest gap message
# "not run for this generation".
VALIDATION_WRITABLE = frozenset({VALIDATION_PASS, VALIDATION_FAIL})

STATE_CURRENT = "current"
STATE_UNKNOWN = "unknown"


def _pinned_sha(task: dict) -> str:
    return (task.get("submission_sha") or "").strip()


async def accept_ci_run_report(
    db: Any,
    task_id: int,
    *,
    head_sha: str,
    ac_results: dict[str, str],
    validation_status: str = "",
    validation_log: str = "",
    reason: str = "",
    reported_by: str = "",
) -> dict:
    """Store a CI run report and stamp it if it covers the pinned commit.

    Returns a dict describing what was done, including why it was NOT applied
    when that is the case. Raises ValueError for a malformed report (the caller
    turns it into a 4xx) — a report the hub cannot understand is refused rather
    than half-stored.
    """
    head_sha = (head_sha or "").strip()
    if not head_sha:
        raise ValueError("head_sha is required: a report must name the commit it ran")

    row = await repo.get_task(db, task_id)
    if row is None:
        raise LookupError("task not found")
    task = dict(row)

    validation_status = (validation_status or "").strip()
    if validation_status and validation_status not in VALIDATION_STATUSES:
        raise ValueError(
            f"unknown validation status {validation_status!r}; "
            f"expected one of {sorted(VALIDATION_STATUSES)}"
        )

    known = await test_ac_nodeids(db, task_id)
    accepted: dict[str, str] = {}
    ignored: list[str] = []
    for ac_id, status in (ac_results or {}).items():
        status = (status or "").strip()
        if status not in AC_STATUSES:
            raise ValueError(
                f"unknown AC status {status!r} for {ac_id}; "
                f"expected one of {sorted(AC_STATUSES)}"
            )
        # A report may only speak for AC the hub itself considers machine
        # verifiable. Anything else is recorded as ignored and named in the
        # response: dropping it silently is how a report starts inventing AC.
        if ac_id in known:
            accepted[ac_id] = status
        else:
            ignored.append(ac_id)

    generation = task.get("submission_generation") or 0
    pinned = _pinned_sha(task)
    applied = False
    if not pinned:
        applied_reason = (
            "не применён: коммит сдачи не закреплён "
            "(задача ещё не сдавалась или вершину не удалось определить)"
        )
    elif pinned != head_sha:
        applied_reason = (
            f"не применён: отчёт о коммите {head_sha[:12]}, "
            f"а на ревью закреплён {pinned[:12]}"
        )
    elif generation <= 0:
        applied_reason = "не применён: у задачи нет ни одной сдачи"
    else:
        applied = True
        applied_reason = f"применён к сдаче #{generation} ({pinned[:12]})"

    await repo.upsert_ci_run_report(
        db,
        task_id=task_id,
        head_sha=head_sha,
        ac_results=json.dumps(accepted, ensure_ascii=False, sort_keys=True),
        validation_status=validation_status,
        validation_log=validation_log or "",
        reason=reason or "",
        reported_by=reported_by or "",
    )

    recorded: list[dict] = []
    if applied:
        recorded = await _stamp(
            db,
            task_id,
            generation=generation,
            ac_results=accepted,
            validation_status=validation_status,
            validation_log=validation_log or "",
        )
    await db.commit()

    return {
        "applied": applied,
        "reason": applied_reason,
        "head_sha": head_sha,
        "submission_generation": generation if applied else None,
        "ac_recorded": recorded,
        "ac_ignored": ignored,
        "validation_status": validation_status,
    }


async def _stamp(
    db: Any,
    task_id: int,
    *,
    generation: int,
    ac_results: dict[str, str],
    validation_status: str,
    validation_log: str,
) -> list[dict]:
    """Write the report's facts through the shared record paths. No commit."""
    recorded: list[dict] = []
    if ac_results:
        recorded = await record_ac_test_results(db, task_id, ac_results, generation)
    if validation_status in VALIDATION_WRITABLE:
        await record_validation_result(
            db,
            task_id,
            generation=generation,
            status=validation_status,
            log_tail=validation_log,
        )
    return recorded


async def adopt_ci_run_report(db: Any, task_id: int, head_sha: str, generation: int):
    """Stamp an already-stored report when submission pins its commit (#546).

    The usual order is CI first, submission second: the run finishes when the PR
    opens, long before the hub pins anything. Without this the evidence would
    sit in the table forever while the gate reported "never ran". Called inside
    the submission write lock, so it must not commit.
    """
    head_sha = (head_sha or "").strip()
    if not head_sha or generation <= 0:
        return None
    row = await repo.get_ci_run_report(db, task_id, head_sha)
    if row is None:
        return None
    stored = dict(row)
    try:
        ac_results = json.loads(stored.get("ac_results") or "{}")
    except (ValueError, TypeError):
        log.warning("CI report for #%s has unreadable ac_results", task_id)
        ac_results = {}
    if not isinstance(ac_results, dict):
        ac_results = {}
    recorded = await _stamp(
        db,
        task_id,
        generation=generation,
        ac_results={str(k): str(v) for k, v in ac_results.items()},
        validation_status=(stored.get("validation_status") or "").strip(),
        validation_log=stored.get("validation_log") or "",
    )
    return {
        "head_sha": head_sha,
        "submission_generation": generation,
        "ac_recorded": recorded,
        "validation_status": (stored.get("validation_status") or "").strip(),
    }


async def ci_report_state(db: Any, task: dict) -> tuple[str, str]:
    """(state, reason) for the review brief — ``current`` or ``unknown`` (#546).

    Never ``fail``: this answers "do we have run evidence for the code under
    review", and the absence of evidence is not evidence of failure. The reason
    is always populated for ``unknown`` so a reviewer reads a cause instead of
    an empty field.
    """
    task_id = task["id"]
    pinned = _pinned_sha(task)
    if not pinned:
        return STATE_UNKNOWN, ("коммит сдачи не закреплён — отчёт CI не с чем сверить")
    if await repo.get_ci_run_report(db, task_id, pinned):
        return STATE_CURRENT, ""
    latest = await repo.latest_ci_run_report(db, task_id)
    if latest is not None:
        other = (dict(latest).get("head_sha") or "")[:12]
        return STATE_UNKNOWN, (
            f"CI отчитался о коммите {other}, а на ревью закреплён {pinned[:12]}"
        )
    return STATE_UNKNOWN, (f"CI не присылал отчёт о прогоне для коммита {pinned[:12]}")
