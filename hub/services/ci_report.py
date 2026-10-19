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
from hub.services import ci_contract, ci_trust, red_test_gate
from hub.services.ac_tests import (
    FAIL as AC_FAIL,
    NOT_FOUND as AC_NOT_FOUND,
    PASS as AC_PASS,
    test_ac_nodeids,
)
from hub.services.validation_run import (
    FAIL as VALIDATION_FAIL,
    PASS as VALIDATION_PASS,
    SKIPPED as VALIDATION_SKIPPED,
    UNKNOWN as VALIDATION_UNKNOWN,
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

# How a deterministic check can end (#875). "skipped" is a real outcome, not a
# missing one: a step the workflow chose not to run proves nothing, and saying
# so is different from not mentioning it.
CHECK_PASS = "pass"  # nosec B105 - an outcome name, not a credential
CHECK_FAIL = "fail"
CHECK_SKIPPED = "skipped"
CHECK_OUTCOMES = frozenset({CHECK_PASS, CHECK_FAIL, CHECK_SKIPPED})

# The mutation report (#1270) is stored verbatim; the reporter trims the survivor
# list well below this, so hitting it means a reporter that forgot to.
MUTATIONS_MAX_CHARS = 32_000
# The red-test baseline (#913): the reporter keeps the AC tests and trims the
# rest well below this, so hitting it means a reporter that forgot to.
BASELINE_MAX_CHARS = 32_000


def _pinned_sha(task: dict) -> str:
    return (task.get("submission_sha") or "").strip()


async def store_ci_run_report(
    db: Any,
    task_id: int,
    *,
    head_sha: str,
    ac_results: dict[str, str],
    validation_status: str = "",
    validation_log: str = "",
    reason: str = "",
    reported_by: str = "",
    checks: dict[str, str] | None = None,
    mutations: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
    provenance: dict[str, str] | None = None,
) -> dict:
    """Store what a CI run claims about one attempt. No commit, no GitHub.

    First half of accepting a report (#1687). The report is a claim: it is kept
    as the row of its attempt, unverified, and nothing is written onto the task.
    ``settle_ci_run_report`` asks GitHub and decides what the claim is worth.
    Raises ValueError for a malformed report (the caller turns it into a 4xx) —
    a report the hub cannot understand is refused rather than half-stored.
    """
    head_sha = (head_sha or "").strip()
    if not head_sha:
        raise ValueError("head_sha is required: a report must name the commit it ran")

    row = await repo.get_task(db, task_id)
    if row is None:
        raise LookupError("task not found")

    validation_status = (validation_status or "").strip()
    if validation_status and validation_status not in VALIDATION_STATUSES:
        raise ValueError(
            f"unknown validation status {validation_status!r}; "
            f"expected one of {sorted(VALIDATION_STATUSES)}"
        )

    # #875: the same enumeration discipline the AC statuses get. A check whose
    # outcome the hub cannot name is refused rather than stored as something
    # the prepass block would then have to interpret.
    for name, outcome in (checks or {}).items():
        if (outcome or "").strip() not in CHECK_OUTCOMES:
            raise ValueError(
                f"unknown check outcome {outcome!r} for {name!r}; "
                f"expected one of {sorted(CHECK_OUTCOMES)}"
            )

    # #1270: stored as evidence under its own key, never interpreted into a
    # gate. Bounded, because it is free text from CI landing in a table row.
    # #1606: ``None`` is "the report carried no such key" — the step did not
    # run — and is NOT stored as {}: the repository keeps what is already
    # there. A dict, even an empty one or a state=error, replaces.
    mutations_json = None
    if mutations is not None:
        mutations_json = json.dumps(mutations, ensure_ascii=False, sort_keys=True)
        if len(mutations_json) > MUTATIONS_MAX_CHARS:
            raise ValueError(
                f"mutations report is {len(mutations_json)} chars, the limit is "
                f"{MUTATIONS_MAX_CHARS}: trim the survivor list before sending"
            )

    baseline_json = None if baseline is None else _checked_baseline(baseline)

    known = await test_ac_nodeids(db, task_id)
    accepted: dict[str, str] = {}
    ignored: list[str] = []
    sent: dict[str, str] = {}
    for ac_id, status in (ac_results or {}).items():
        status = (status or "").strip()
        if status not in AC_STATUSES:
            raise ValueError(
                f"unknown AC status {status!r} for {ac_id}; "
                f"expected one of {sorted(AC_STATUSES)}"
            )
        sent[ac_id] = status
        # A report may only speak for AC the hub itself considers machine
        # verifiable. Anything else is recorded as ignored and named in the
        # response: dropping it silently is how a report starts inventing AC.
        if ac_id in known:
            accepted[ac_id] = status
        else:
            ignored.append(ac_id)

    claimed = {k: v for k, v in (provenance or {}).items() if v}
    await repo.upsert_ci_report_attempt(
        db,
        task_id=task_id,
        head_sha=head_sha,
        run_id=claimed.get("run_id", ""),
        run_attempt=claimed.get("run_attempt", ""),
        ac_results=json.dumps(accepted, ensure_ascii=False, sort_keys=True),
        checks=json.dumps(
            {k: (v or "").strip() for k, v in (checks or {}).items()},
            ensure_ascii=False,
            sort_keys=True,
        ),
        validation_status=validation_status,
        validation_log=validation_log or "",
        reason=reason or "",
        reported_by=reported_by or "",
        mutations=mutations_json,
        baseline=baseline_json,
        provenance=json.dumps(claimed, ensure_ascii=False, sort_keys=True),
        submitted=json.dumps({"ac_results": sent}, ensure_ascii=False, sort_keys=True),
    )
    # #1687: a new claim makes the standing UNCONFIRMED until it is settled
    # against GitHub — in the same transaction, so no reader in between sees the
    # previous confirmation (or the evidence it wrote) as current.
    await ci_trust.invalidate(db, dict(row), head_sha)
    return {
        "head_sha": head_sha,
        "ac_ignored": ignored,
        "validation_status": validation_status,
    }


def _applied_reason(
    task: dict, head_sha: str, standing: ci_trust.Standing | None
) -> tuple[bool, str]:
    """Whether the verified report counts for this submission, and the words for it."""
    if standing is None or not standing.verified:
        why = ci_trust.unverified_note(standing.reason if standing else "pending")
        return False, f"не применён: {why}"
    generation = task.get("submission_generation") or 0
    pinned = _pinned_sha(task)
    if not pinned:
        return False, (
            "не применён: коммит сдачи не закреплён "
            "(задача ещё не сдавалась или вершину не удалось определить)"
        )
    if pinned != head_sha:
        return False, (
            f"не применён: отчёт о коммите {head_sha[:12]}, "
            f"а на ревью закреплён {pinned[:12]}"
        )
    if generation <= 0:
        return False, "не применён: у задачи нет ни одной сдачи"
    return True, f"применён к сдаче #{generation} ({pinned[:12]})"


async def settle_ci_run_report(db: Any, task_id: int, stored: dict) -> dict:
    """Second half (#1687): ask GitHub, project the current attempt, describe it.

    Runs outside any transaction (it downloads); the writes it makes are one
    short transaction of their own. Returns the intake answer.
    """
    head_sha = stored["head_sha"]
    standing = await ci_trust.refresh_quietly(db, task_id, head_sha)
    task = dict(await repo.get_task(db, task_id) or {})
    applied, applied_reason = _applied_reason(task, head_sha, standing)
    current = dict(await repo.get_ci_run_report(db, task_id, head_sha) or {})
    generation = task.get("submission_generation") or 0
    trust, why = (
        ci_trust.trust_of(current, await ci_contract.fingerprint_for_task(db, task_id))
        if current
        else (ci_trust.UNVERIFIED, "pending")
    )
    return {
        "applied": applied,
        "reason": applied_reason,
        "head_sha": head_sha,
        "submission_generation": generation if applied else None,
        "ac_recorded": list(standing.ac_recorded) if standing and applied else [],
        "ac_ignored": stored["ac_ignored"],
        "validation_status": stored["validation_status"],
        "mutations_state": _stored_state(current.get("mutations")),
        "baseline_state": _stored_state(current.get("baseline")),
        "provenance": _stored_provenance(current.get("provenance")),
        "trust": trust,
        "trust_reason": why,
    }


async def accept_ci_run_report(
    db: Any, task_id: int, *, commit: bool = True, **report: Any
) -> dict:
    """Store a CI run report, check it against GitHub, and describe the outcome.

    The request handler does the two halves itself: it owns the write
    transaction for the first (#1644: the key's scope, the generation and the
    write stay together) and runs the second after it, because the second talks
    to GitHub and must not hold the database for that long.
    """
    stored = await store_ci_run_report(db, task_id, **report)
    if commit:
        await db.commit()
    return await settle_ci_run_report(db, task_id, stored)


PROVENANCE_NOTE = "claimed by the reporter; see trust for what GitHub confirmed (#1687)"


def stored_provenance(report_row: dict) -> dict[str, str]:
    """Provenance of a stored report row, for the brief (#1686)."""
    return _stored_provenance(report_row.get("provenance"))


def _stored_provenance(raw: str | None) -> dict[str, str]:
    """The run provenance stored with a report (#1686); {} when none was sent."""
    try:
        block = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(block, dict):
        return {}
    return {str(k): str(v) for k, v in block.items()}


def _stored_state(raw: str | None) -> str:
    """``state`` of a stored evidence block, ``not_reported`` when it holds none."""
    try:
        block = json.loads(raw or "{}")
    except ValueError:
        return "not_reported"
    if not isinstance(block, dict):
        return "not_reported"
    return str(block.get("state") or "not_reported")


def _checked_baseline(baseline: dict[str, Any] | None) -> str:
    """The baseline (#913) as stored text, or ValueError for one we cannot read.

    Stored verbatim next to ``mutations``; interpreted only by the red-test
    gate at submission. Statuses are enumerated like AC statuses: a test the
    hub cannot classify is refused, never guessed into "failed".
    """
    red_test_gate.validate_baseline(baseline or {})
    text = json.dumps(baseline or {}, ensure_ascii=False, sort_keys=True)
    if len(text) > BASELINE_MAX_CHARS:
        raise ValueError(
            f"baseline report is {len(text)} chars, the limit is "
            f"{BASELINE_MAX_CHARS}: trim the non-AC tests before sending"
        )
    return text


async def adopt_ci_run_report(db: Any, task_id: int, head_sha: str, generation: int):
    """Write an already-stored report when submission pins its commit (#546).

    The usual order is CI first, submission second: the run finishes when the PR
    opens, long before the hub pins anything. Without this the evidence would
    sit in the table forever while the gate reported "never ran". Called inside
    the submission write lock, so it must not commit and does not ask GitHub:
    the check ran just before (``ci_trust.refresh``), and a report it did not
    confirm is adopted as nothing (#1687) — the same outcome as one that arrives
    after the submission.
    """
    head_sha = (head_sha or "").strip()
    if not head_sha or generation <= 0:
        return None
    adopted = await ci_trust.adopt(db, task_id, head_sha, generation)
    if adopted is None:
        return None
    trust, reason, recorded = adopted
    stored = dict(await repo.get_ci_run_report(db, task_id, head_sha) or {})
    out: dict[str, Any] = {
        "head_sha": head_sha,
        "submission_generation": generation,
        "ac_recorded": recorded,
        "validation_status": (stored.get("validation_status") or "").strip()
        if trust == ci_trust.VERIFIED
        else "",
    }
    if trust != ci_trust.VERIFIED:
        out["not_adopted"] = ci_trust.unverified_note(reason)
    return out


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
    report = await repo.get_ci_run_report(db, task_id, pinned)
    if report:
        fp = await ci_contract.fingerprint_for_task(db, task_id)
        trust, reason = ci_trust.trust_of(dict(report), fp)
        if trust != ci_trust.VERIFIED:
            return STATE_UNKNOWN, ci_trust.unverified_note(reason)
        return STATE_CURRENT, ""
    latest = await repo.latest_ci_run_report(db, task_id)
    if latest is not None:
        other = (dict(latest).get("head_sha") or "")[:12]
        return STATE_UNKNOWN, (
            f"CI отчитался о коммите {other}, а на ревью закреплён {pinned[:12]}"
        )
    return STATE_UNKNOWN, (f"CI не присылал отчёт о прогоне для коммита {pinned[:12]}")
