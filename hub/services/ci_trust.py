"""Is a CI report true? The one predicate every reader asks (#1687).

Until now the hub took a CI report on the word of whoever held the key: the
commit, the green checks and the validation result all came from the sender, one
row per commit, the last POST winning. Since #1686 the report is sent by a
trusted workflow and names the run it belongs to; this module is the other half:
the hub asks GitHub (read-only) whether that run, attempt and artifact really
exist and say what the report says.

``report_trust`` is ``verified`` or ``unverified(<reason>)``. An unverified
report is kept and shown, but it is not evidence: it writes no AC result and no
validation result, and it opens no gate. The same holds for a report whose
attempt is no longer the current one: the evidence belongs to the attempt that
produced it and is withdrawn when another attempt takes over. One exception, for
SHOWING only (#1936): the brief may name the mutations and baseline that another
verified attempt of the same commit counted (``borrowed_evidence_blocks``); the
projection and every gate stay on the current attempt.

Three things are deliberately NOT guessed:

- GitHub unreachable is ``unverified(forge_unavailable)``, retried for six
  hours and then final. It never becomes ``verified`` by default (#725).
- The current attempt is the one that STARTED last (``run_started_at``), over
  every run of ci.yml for the commit, including a rerun nobody reported.
- A skipped ``tests`` check is not a pass. A push run may skip pytest because
  the same tree was green before; the task reports come from task branches
  (pull_request / workflow_dispatch), which never skip, and the skip's source
  run is named nowhere the hub can check, so the hub does not credit it.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from hub import config
from hub import repository as repo
from hub.db import fetchall, write_transaction
from hub.integrations.registry import plugins
from hub.services import ci_artifact, ci_contract
from hub.services.ci_contract import CiContract
from hub.services.ac_tests import record_ac_test_results
from hub.services.validation_run import record_validation_result

log = logging.getLogger("hub")

VERIFIED = "verified"
UNVERIFIED = "unverified"

# Reasons. Plain tokens: they are stored, shown and matched in tests.
LEGACY = "legacy"
PENDING = "pending"
NO_PROVENANCE = "no_provenance"
RUN_NOT_FOUND = "run_not_found"
WRONG_REPOSITORY = "wrong_repository"
WRONG_WORKFLOW = "wrong_workflow"
WRONG_COMMIT = "wrong_commit"
WRONG_EVENT = "wrong_event"
RUN_IN_PROGRESS = "run_in_progress"
CONCLUSION_MISMATCH = "conclusion_mismatch"
ARTIFACT_MISSING = "artifact_missing"
ARTIFACT_MISMATCH = "artifact_mismatch"
ARTIFACT_INVALID = "artifact_invalid"
BODY_MISMATCH = "body_mismatch"
FORGE_UNAVAILABLE = "forge_unavailable"
NEWER_ATTEMPT_WITHOUT_REPORT = "newer_attempt_without_report"
CURRENT_ATTEMPT_INCOMPLETE = "current_attempt_incomplete"
CURRENT_ATTEMPT_UNKNOWN = "current_attempt_unknown"
CHECK_FAILED = "check_failed"
CHECK_SUPERSEDED = "check_superseded"
# The verdict of an attempt was made under another CI contract than the
# project's current one (#1848): it proves nothing until the attempt is checked
# again under the current one.
CONTRACT_CHANGED = "contract_changed"
# The project's contract could not be read (#1848): nothing is confirmed.
CONTRACT_UNRESOLVED = "contract_unresolved"

# The hub's own contract (#1848 made it one of many): kept as names for readers
# that mean the hub's project.
CANONICAL_WORKFLOW = ci_contract.HUB_CONTRACT.workflow_path
ALLOWED_EVENTS = frozenset({"pull_request", "push", "workflow_dispatch"})
ARTIFACT_PREFIX = "hub-ci-results-"
# A green proof names these four checks; a missing key or a skip is not a pass.
REQUIRED_CHECKS = ci_contract.CHECK_VOCABULARY
SUCCESS = "success"

RETRY_MINUTES = 5
GIVE_UP_MINUTES = 6 * 60
# The poller's share of one tick (#1687): after this many seconds no new
# verification is started (the rest waits for the next tick), and a GitHub that
# just failed is not asked again for FORGE_BACKOFF_SECONDS.
SWEEP_BUDGET_SECONDS = 20.0
FORGE_BACKOFF_SECONDS = 60.0
# A verification is repeated at most this often when the world keeps changing
# under it (a body replaced, a newer fact stored): then the answer is
# unverified(check_superseded), not a guess.
MAX_CHECK_ROUNDS = 3
# One refresh has ONE deadline, lock wait and every call to GitHub included;
# past it the standing is recomputed offline (unverified, forge_unavailable).
REFRESH_DEADLINE_SECONDS = 45.0


@dataclass(frozen=True)
class Standing:
    """What is true of a commit's CI report right now."""

    trust: str
    reason: str
    attempt_key: str = ""
    has_report: bool = False
    applied: bool = False
    ac_recorded: tuple[dict, ...] = ()
    forge_down: bool = False
    #: Версия состояния коммита, при которой записана проекция (None - не из записи).
    version: int | None = None

    @property
    def verified(self) -> bool:
        return self.trust == VERIFIED


# ---- reading the trust of a stored row --------------------------------------


def trust_of(report: Mapping[str, Any] | None, current_fp: str) -> tuple[str, str]:
    """``(trust, reason)`` of a stored report; a row without the column is legacy.

    ``current_fp`` is the fingerprint of the project's CI contract NOW (#1848):
    a verdict made under another contract (or under none) is not confirmation.
    The caller says it explicitly — this function does not guess a project.
    """
    if report is None:
        return UNVERIFIED, "no_report"
    trust = str(report.get("trust") or UNVERIFIED)
    reason = str(report.get("trust_reason") or LEGACY)
    if trust != VERIFIED:
        return UNVERIFIED, reason
    if not current_fp:
        return UNVERIFIED, CONTRACT_UNRESOLVED
    if str(report.get("contract_fp") or "") != current_fp:
        return UNVERIFIED, CONTRACT_CHANGED
    return VERIFIED, ""


def is_verified(report: Mapping[str, Any] | None, current_fp: str) -> bool:
    return trust_of(report, current_fp)[0] == VERIFIED


def unverified_note(reason: str) -> str:
    return f"CI-отчёт не подтверждён: {reason or LEGACY}"


@dataclass(frozen=True)
class Computed:
    """A projection worked out but not saved (#1975): what a gate reads in a dry run."""

    row: dict[str, Any] | None


async def report_row(
    db: Any, task_id: int, head_sha: str, computed: Computed | None = None
) -> Any:
    """The report of a commit as the gates read it: the stored row, or the computed one."""
    if computed is not None:
        return computed.row
    return await repo.get_ci_run_report(db, task_id, head_sha)


async def trusted_report(
    db: Any, task_id: int, head_sha: str, computed: Computed | None = None
) -> Any:
    """The report of a commit when GitHub confirmed it, else None.

    ``computed`` (a dry run, #1975) replaces the stored row by the projection the
    refresh would have left; the verdict on it is the same predicate.
    """
    if not head_sha:
        return None
    row = await report_row(db, task_id, head_sha, computed)
    if row is None:
        return None
    fp = await ci_contract.fingerprint_for_task(db, task_id)
    return row if is_verified(dict(row), fp) else None


async def unconfirmed_reason(db: Any, task_id: int, head_sha: str) -> str:
    """ "CI-отчёт не подтверждён: <reason>" when a report is stored but unconfirmed, else ''.

    Tells "CI never sent a report" (no row) from "CI sent one and GitHub did not
    confirm it", which readers must not blur.
    """
    row = await repo.get_ci_run_report(db, task_id, head_sha) if head_sha else None
    if row is None:
        return ""
    trust, reason = trust_of(
        dict(row), await ci_contract.fingerprint_for_task(db, task_id)
    )
    return "" if trust == VERIFIED else unverified_note(reason)


def _json_map(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def run_conclusion(report: Mapping[str, Any]) -> str:
    """How the run behind a stored report ended, as GitHub confirmed it ('' if unsaid)."""
    return str(_json_map(report.get("provenance")).get("run_conclusion") or "")


def run_succeeded(report: Mapping[str, Any]) -> bool:
    """The run behind a stored report ended in ``success`` (what GitHub confirmed)."""
    return run_conclusion(report) == SUCCESS


def can_testify_pass(report: Mapping[str, Any], current_fp: str) -> bool:
    """A report's ``pass`` counts as evidence: confirmed by GitHub AND the run succeeded.

    The ONE predicate for it: ``write_evidence`` writes a ``pass`` to the task by
    it and the completion gate accepts a tip's validation by it, so the two
    cannot drift apart.
    """
    return is_verified(report, current_fp) and run_succeeded(report)


def green_gap(report: Mapping[str, Any], contract: CiContract) -> str | None:
    """Why a verified report is not a GREEN proof; None when it is.

    Green needs the run to have ended in ``success`` and each check the project's
    CI contract requires (the hub's: lint, format, types, tests) to be ``pass`` —
    an absent key or a skip proves nothing. Which checks are required is the
    only thing the contract changes (#1848); the predicate is the same.
    """
    conclusion = run_conclusion(report)
    if conclusion != SUCCESS:
        return f"прогон закончился как {conclusion or 'неизвестно'}, нужен success"
    checks = _json_map(report.get("checks"))
    bad = [n for n in contract.ordered_checks() if str(checks.get(n) or "") != "pass"]
    if bad:
        return "проверки не pass: " + ", ".join(
            f"{n}={checks.get(n) or 'нет'}" for n in bad
        )
    return None


# ---- one attempt against GitHub --------------------------------------------


def canonical_path(path: str, contract: CiContract) -> bool:
    """The contract's workflow, with the documented ``@<ref>`` suffix of a run's path ignored."""
    return ci_contract.path_matches(contract, path)


def attempt_key(run_id: str, run_attempt: str) -> str:
    return f"ci:{run_id}:{run_attempt}" if run_id and run_attempt else ""


async def _forge_args(db: Any, task_id: int) -> dict[str, Any]:
    """The forge keyword arguments of the task's project (clone, repository, forge)."""
    from hub.services.orchestration import project_git_context

    ctx = await project_git_context(db, task_id)
    return {k: ctx[k] for k in ("repo", "gh_repo", "forge") if k in ctx}


def _same_repo(found: str, wanted: str) -> bool:
    return bool(found) and found.lower() == wanted.lower()


Verdict = tuple[str, str, bool]  # trust, reason, final


def _unavailable(row: Any) -> Verdict:
    return (
        UNVERIFIED,
        FORGE_UNAVAILABLE,
        int(row["age_minutes"] or 0) >= GIVE_UP_MINUTES,
    )


def _run_problem(
    run: dict[str, Any],
    prov: dict[str, Any],
    row: Any,
    gh_repo: str,
    contract: CiContract,
) -> str:
    """The first way a run disagrees with the report, or ''."""
    if run["run_id"] != prov["run_id"] or run["run_attempt"] != prov["run_attempt"]:
        return RUN_NOT_FOUND
    if not _same_repo(run["repository"], gh_repo) or not _same_repo(
        run["head_repository"], gh_repo
    ):
        return WRONG_REPOSITORY
    if not canonical_path(run["path"], contract) or not canonical_path(
        str(prov.get("workflow_path") or ""), contract
    ):
        return WRONG_WORKFLOW
    if run["head_sha"] != row["head_sha"]:
        return WRONG_COMMIT
    if run["event"] not in ALLOWED_EVENTS or (
        prov.get("event") and prov["event"] != run["event"]
    ):
        return WRONG_EVENT
    return ""


async def check_attempt(
    row: Any, gh_args: Mapping[str, Any], contract: CiContract
) -> Verdict:
    """Ask GitHub whether one reported attempt is what its report claims.

    ``contract`` is the project's CI contract captured BEFORE the question: the
    workflow the run must be of (#1848).
    """
    prov = {k: str(v) for k, v in _json_map(row["provenance"]).items()}
    needed = ("run_id", "run_attempt", "artifact_id", "artifact_digest")
    if any(not prov.get(k) for k in needed):
        return UNVERIFIED, NO_PROVENANCE, True
    git = plugins.git_ops
    run = await git.ci_run_attempt(
        prov["run_id"],
        prov["run_attempt"],
        repo=gh_args.get("repo"),
        gh_repo=gh_args.get("gh_repo"),
        forge=gh_args.get("forge", ""),
    )
    if run is None:
        return _unavailable(row)
    if not run:
        return UNVERIFIED, RUN_NOT_FOUND, True
    gh_repo = str(gh_args.get("gh_repo") or config.REPO_NAME)
    problem = _run_problem(run, prov, row, gh_repo, contract)
    if problem:
        return UNVERIFIED, problem, True
    if run["status"] != "completed":
        return UNVERIFIED, RUN_IN_PROGRESS, False
    if run["conclusion"] != prov.get("run_conclusion", ""):
        return UNVERIFIED, CONCLUSION_MISMATCH, True
    return await _check_artifact(row, prov, gh_args, _unavailable(row))


async def _check_artifact(
    row: Any,
    prov: dict[str, str],
    gh_args: Mapping[str, Any],
    unavailable: Verdict,
) -> Verdict:
    git = plugins.git_ops
    listing = await git.ci_run_artifacts(
        prov["run_id"],
        repo=gh_args.get("repo"),
        gh_repo=gh_args.get("gh_repo"),
        forge=gh_args.get("forge", ""),
    )
    if listing is None:
        return unavailable
    wanted = f"{ARTIFACT_PREFIX}{prov['run_attempt']}"
    named = [a for a in listing if a["name"] == wanted]
    if len(named) > 1:
        return UNVERIFIED, ARTIFACT_MISMATCH, True
    if not named or named[0]["expired"]:
        return UNVERIFIED, ARTIFACT_MISSING, True
    artifact = named[0]
    if artifact["id"] != prov["artifact_id"]:
        return UNVERIFIED, ARTIFACT_MISMATCH, True
    if artifact["digest"] and artifact["digest"] != prov["artifact_digest"]:
        return UNVERIFIED, ARTIFACT_MISMATCH, True
    blob = await git.ci_artifact_zip(
        artifact["id"],
        max_bytes=ci_artifact.MAX_ARCHIVE_BYTES,
        repo=gh_args.get("repo"),
        gh_repo=gh_args.get("gh_repo"),
        forge=gh_args.get("forge", ""),
    )
    if blob is None:
        return unavailable
    if (
        not blob
        or "sha256:" + hashlib.sha256(blob).hexdigest() != prov["artifact_digest"]
    ):
        return UNVERIFIED, ARTIFACT_MISMATCH, True
    submitted = reported_body(row)
    try:
        expected = ci_artifact.expected_body(
            blob,
            head_sha=str(row["head_sha"]),
            task_id=int(row["task_id"]),
            submitted=submitted,
        )
    except ci_artifact.ArtifactRefused as exc:
        log.info("CI artifact of task %s refused: %s", row["task_id"], exc)
        return UNVERIFIED, ARTIFACT_INVALID, True
    if _json_map(row["submitted"]).get("partial"):
        # Stored by the migration with the AC map already cut to the AC the hub
        # knew then: compare those, not the ones it never kept.
        expected["ac_results"] = {
            k: v
            for k, v in expected["ac_results"].items()
            if k in submitted["ac_results"]
        }
    if expected != ci_artifact.submitted_comparable(submitted):
        return UNVERIFIED, BODY_MISMATCH, True
    return VERIFIED, "", True


def reported_body(row: Any) -> dict[str, Any]:
    """The body the reporter sent for one attempt, as the artifact is compared with.

    ``ac_results`` is the reporter's own map (the hub keeps only the AC it knows
    in the column); ``mutations`` / ``baseline`` are absent when none was sent —
    a sent block is never empty, the reporter stamps it with its run.
    """
    body: dict[str, Any] = {
        "ac_results": _json_map(row["submitted"]).get("ac_results") or {},
        "checks": _json_map(row["checks"]),
        "validation_status": row["validation_status"] or "",
        "validation_log": row["validation_log"] or "",
    }
    for key in ("mutations", "baseline"):
        block = _json_map(row[key])
        if block:
            body[key] = block
    return body


# ---- evidence of ANOTHER attempt of the same commit (#1936) -----------------

#: What a block must say to be borrowed: a step that really counted, not one that
#: failed or found nothing to run against. Per column of ``ci_report_attempts``.
BORROWABLE_STATES: dict[str, frozenset[str]] = {
    "mutations": frozenset({"ran", "no_changed_functions", "no_tests"}),
    "baseline": frozenset({"ran", "no_tests"}),
}

_PROJECTION_KEYS = (
    "attempt_key",
    "trust",
    "contract_fp",
    "mutations",
    "baseline",
    "provenance",
)

# ONE statement reads the attempts of a commit together with its projection, so
# both are the state of one moment (a statement is atomic; a read transaction on
# the shared connection would hold other coroutines' writes hostage).
_SNAPSHOT_SQL = (
    "SELECT a.*, r.attempt_key AS p_attempt_key, r.trust AS p_trust, "
    "r.contract_fp AS p_contract_fp, r.mutations AS p_mutations, "
    "r.baseline AS p_baseline, r.provenance AS p_provenance "
    "FROM ci_report_attempts a LEFT JOIN ci_run_reports r "
    "ON r.task_id=a.task_id AND r.head_sha=a.head_sha "
    "WHERE a.task_id=? AND a.head_sha=?"
)


@dataclass(frozen=True)
class BorrowedBlock:
    """A mutations/baseline block counted by another attempt of the commit."""

    raw: str
    run_id: str
    run_attempt: str


def _order(row: Mapping[str, Any]) -> tuple[int, int]:
    def number(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return number(row["run_id"]), number(row["run_attempt"])


def _usable_block(
    row: Mapping[str, Any], column: str, head_sha: str, fingerprint: str
) -> dict[str, Any] | None:
    """The block of a confirmed, successful attempt that was counted on ``head_sha``."""
    if trust_of(row, fingerprint)[0] != VERIFIED or not run_succeeded(
        {"provenance": row["provenance"]}
    ):
        return None
    block = _json_map(row[column])
    if block.get("head") != head_sha:  # no ``head`` = not counted on this commit
        return None
    state = block.get("state")
    # A state that is not a string (a list, a map) is garbage, not a membership test.
    ok = isinstance(state, str) and state in BORROWABLE_STATES[column]
    return block if ok else None


def _picks(
    rows: list[dict[str, Any]],
    head_sha: str,
    current: Mapping[str, Any],
    fingerprint: str,
) -> dict[str, BorrowedBlock]:
    """What one snapshot lets the brief borrow, per column. Pure: no read, no await."""
    if not rows:
        return {}
    projection = {k: rows[0][f"p_{k}"] for k in _PROJECTION_KEYS}
    if any(projection[k] != current.get(k) for k in _PROJECTION_KEYS):
        return {}  # not the row the brief is showing
    if trust_of(projection, fingerprint)[0] != VERIFIED or not run_succeeded(
        projection
    ):
        return {}
    picks: dict[str, BorrowedBlock] = {}
    for column in BORROWABLE_STATES:
        if _json_map(projection[column]):
            continue  # the current attempt carries its own block: it wins
        best: tuple[tuple[int, int], BorrowedBlock] | None = None
        for row in rows:
            if _usable_block(row, column, head_sha, fingerprint) is None:
                continue
            order = _order(row)
            if best is None or order > best[0]:
                found = BorrowedBlock(row[column], row["run_id"], row["run_attempt"])
                best = (order, found)
        if best is not None:
            picks[column] = best[1]
    return picks


async def borrowed_evidence_blocks(
    db: Any, task_id: int, head_sha: str, current: Mapping[str, Any]
) -> dict[str, BorrowedBlock]:
    """The mutations/baseline blocks another verified attempt of this commit counted.

    For the BRIEF only: the projection (``ci_run_reports``) stays the row of the
    current attempt and the gates read it, so nothing here lets a gate lean on
    another attempt (#1687). A ``pull_request:synchronize`` run counts no
    mutations or baseline and starts after the executor's dispatch run, so it is
    the current attempt and its empty blocks used to hide what was counted on
    the same commit.

    ``current`` is the confirmed projection row the brief shows. Both blocks are
    decided from ONE snapshot (attempts and projection in one statement) that
    must agree with the row the brief shows: if the projection moved since, no
    borrowing at all, i.e. the answer the brief gave before. The contract is
    read on both sides of the statement and must be the same; that interval is
    the consistency boundary, nothing is awaited after it. A candidate must be
    verified under the current contract, ``run_conclusion=success``, carry
    ``head == head_sha`` and an informative state; of several the newest by
    ``(run_id, run_attempt)`` wins. The projection must be verified and
    successful too. A block the current attempt carries always wins.
    """
    if not head_sha or not current:
        return {}
    # The contract lives in another table (the project is resolved through the
    # task's ancestry), so it cannot join the statement. It is read on both
    # sides of the snapshot: equal means it did not change while the snapshot
    # was taken. Known residual: a human PATCH that goes A -> B -> A inside this
    # interval is not seen (a fingerprint is a hash, not a revision); the cost
    # is one brief showing evidence the contract of that instant did not confirm.
    fingerprint = await ci_contract.fingerprint_for_task(db, task_id)
    rows = [dict(r) for r in await fetchall(db, _SNAPSHOT_SQL, (task_id, head_sha))]
    if await ci_contract.fingerprint_for_task(db, task_id) != fingerprint:
        return {}
    # Nothing is awaited from here: the interval above is the consistency
    # boundary. The answer is the state at that moment; a write after it is a
    # later moment, and a projection that moved since the brief read its own
    # copy (``current``) makes the keys differ and borrows nothing.
    return _picks(rows, head_sha, current, fingerprint)


# ---- which attempt is current ----------------------------------------------


def canonical_runs(
    runs: list[dict[str, Any]], head_sha: str, gh_repo: str, contract: CiContract
) -> list[dict[str, Any]]:
    """The runs that count: the contract's workflow of this repository, on this commit."""
    return [
        r
        for r in runs
        if canonical_path(r["path"], contract)
        and r["head_sha"] == head_sha
        and _same_repo(r["repository"], gh_repo)
        and _same_repo(r["head_repository"], gh_repo)
    ]


def current_attempt(
    runs: list[dict[str, Any]], head_sha: str, gh_repo: str, contract: CiContract
) -> dict[str, Any] | None:
    """The attempt that started last among the commit's runs of the contract's workflow."""
    mine = canonical_runs(runs, head_sha, gh_repo, contract)
    if not mine:
        return None
    return max(mine, key=lambda r: (r["run_started_at"], int(r["run_id"] or 0)))


def _standing_attempt(
    rows: Sequence[Any],
    runs: list[dict[str, Any]] | None,
    sha: str,
    gh_repo: str,
    projected_key: str,
    contract: CiContract,
) -> tuple[Mapping[str, Any] | None, str, str, str]:
    """``(attempt row, trust, reason, key)`` the projection must show."""
    by_key = {
        attempt_key(r["run_id"], r["run_attempt"]): r for r in rows if r["run_id"]
    }
    newest = rows[0]
    if runs is None:
        # GitHub could not say which attempt is current, so nothing is
        # confirmed NOW. A past confirmation stays on its attempt row as
        # history; it is not proof and the evidence it wrote is withdrawn.
        return None, UNVERIFIED, FORGE_UNAVAILABLE, projected_key
    cur = current_attempt(runs, sha, gh_repo, contract)
    if cur is None:
        if newest["trust"] != VERIFIED:
            return newest, newest["trust"], newest["trust_reason"], ""
        # The reported run is not among the commit's runs GitHub lists: the
        # current attempt cannot be established, whatever the report says.
        return None, UNVERIFIED, CURRENT_ATTEMPT_UNKNOWN, ""
    key = attempt_key(cur["run_id"], cur["run_attempt"])
    if cur["status"] != "completed":
        return None, UNVERIFIED, CURRENT_ATTEMPT_INCOMPLETE, key
    match = by_key.get(key)
    if match is None:
        # The attempt GitHub calls current has no report. A report that is
        # itself unconfirmed keeps its own reason (run_not_found,
        # wrong_repository, ...); a CONFIRMED one — of a run that is gone, or of
        # an older attempt — is history and is never shown as the standing.
        # VERIFIED comes back only for the current attempt, below.
        # #2027: a report with no run id (no_provenance) is tied to no run, so
        # it cannot be the report of the attempt GitHub calls current: that
        # attempt still has none, and the steward waits for it.
        if (
            newest["trust"] != VERIFIED
            and newest["run_id"]
            and newest["run_id"]
            not in {r["run_id"] for r in canonical_runs(runs, sha, gh_repo, contract)}
        ):
            return newest, newest["trust"], newest["trust_reason"], ""
        return None, UNVERIFIED, NEWER_ATTEMPT_WITHOUT_REPORT, key
    reason = match["trust_reason"]
    if reason == ARTIFACT_MISSING and len(rows) > 1:
        reason = NEWER_ATTEMPT_WITHOUT_REPORT
    return match, match["trust"], reason, key


# ---- the projection and the evidence it writes ------------------------------


async def write_evidence(
    db: Any,
    task_id: int,
    generation: int,
    row: Any | None,
    trust: str,
    key: str,
) -> list[dict]:
    """Make AC results and validation say what the current verified attempt says.

    What CI wrote for another attempt is withdrawn first. A run whose conclusion
    is not success still records what failed, but never a ``pass``.
    """
    if trust != VERIFIED or row is None:
        await repo.drop_ci_ac_test_results(db, task_id)
        await repo.drop_ci_validation(db, task_id)
        return []
    await repo.drop_ci_ac_test_results(db, task_id, keep_source=key)
    await repo.drop_ci_validation(db, task_id, keep_source=key)
    # A run that did not end in success never writes a ``pass``. A missing or
    # skipped check does not stop the per-AC facts from being recorded:
    # ``green_gap`` is the stricter question the gates ask of the same row.
    green = run_succeeded({"provenance": row["provenance"]})
    ac = {
        str(k): str(v)
        for k, v in _json_map(row["ac_results"]).items()
        if green or str(v) != "pass"
    }
    recorded = await record_ac_test_results(db, task_id, ac, generation, source=key)
    status = str(row["validation_status"] or "").strip()
    if status == "fail" or (status == "pass" and green):
        await record_validation_result(
            db,
            task_id,
            generation=generation,
            status=status,
            log_tail=row["validation_log"] or "",
            source=key,
        )
    return recorded


async def adopt(
    db: Any, task_id: int, head_sha: str, generation: int
) -> tuple[str, str, list[dict]] | None:
    """Write the stored, already-checked report onto a task that just pinned it.

    No network and no commit: this runs inside the submission lock, after
    ``refresh`` had its say. ``None`` — no report. Otherwise ``(trust, reason,
    recorded)``; an unverified report records nothing.
    """
    stored = await repo.get_ci_run_report(db, task_id, head_sha)
    if stored is None:
        return None
    row = dict(stored)
    trust, reason = trust_of(row, await ci_contract.fingerprint_for_task(db, task_id))
    recorded = await write_evidence(
        db, task_id, generation, row, trust, str(row.get("attempt_key") or "")
    )
    return trust, reason, recorded


def _under_contract(rows: Sequence[Any], contract: CiContract) -> list[dict[str, Any]]:
    """The attempts as the CURRENT contract sees them (#1848).

    A verdict made under another contract is not a confirmation now: it is shown
    as unverified(contract_changed) until the attempt is checked again. An
    unverified verdict stays what it was (it opens nothing either way).
    """
    out = []
    for row in rows:
        item = dict(row)
        if item["trust"] == VERIFIED and item["contract_fp"] != contract.fingerprint:
            item["trust"], item["trust_reason"] = UNVERIFIED, CONTRACT_CHANGED
        out.append(item)
    return out


@dataclass(frozen=True)
class ProjectionPlan:
    """Which attempt the projection of a commit shows and with what trust (#1975).

    Worked out from facts alone; ``_project`` saves it, ``projected_row`` shows
    the row it would leave. One plan, two readers: a dry run cannot disagree with
    the real refresh about what the projection says.
    """

    kind: str  # attempt: the picked attempt's body | trust: only the trust | bare
    pick: Mapping[str, Any] | None
    source: Mapping[str, Any]
    trust: str
    reason: str
    key: str
    stored: Mapping[str, Any] | None


def plan_projection(
    stored: Any,
    rows: Sequence[Any],
    runs: list[dict[str, Any]] | None,
    sha: str,
    gh_repo: str,
    contract: CiContract,
) -> ProjectionPlan:
    projected_key = str(dict(stored).get("attempt_key") or "") if stored else ""
    rows = _under_contract(rows, contract)
    pick, trust, reason, key = _standing_attempt(
        rows, runs, sha, gh_repo, projected_key, contract
    )
    source = pick if pick is not None else rows[0]
    kind = "attempt" if pick is not None else ("trust" if stored else "bare")
    return ProjectionPlan(
        kind, pick, source, trust, reason, key, dict(stored) if stored else None
    )


_BODY_COLUMNS = (
    "ac_results",
    "validation_status",
    "validation_log",
    "reason",
    "reported_by",
    "checks",
    "provenance",
)


def projected_row(plan: ProjectionPlan, task_id: int, sha: str) -> dict[str, Any]:
    """The ``ci_run_reports`` row ``_project`` leaves for this plan, without writing it."""
    src, row = plan.source, dict(plan.stored or {})
    if plan.kind == "trust":
        return {
            **row,
            "trust": plan.trust,
            "trust_reason": plan.reason,
            "attempt_key": plan.key,
        }
    row.update(task_id=task_id, head_sha=sha)
    for col in _BODY_COLUMNS:
        row[col] = src[col]
    for col in ("mutations", "baseline"):
        # A block the attempt did not carry keeps the stored one (#1606); the
        # bare row is written without them.
        carried = src[col] if plan.kind == "attempt" else None
        row[col] = carried if carried is not None else row.get(col, "{}")
    own = attempt_key(src["run_id"], src["run_attempt"])
    row.update(
        trust=plan.trust,
        trust_reason=plan.reason,
        attempt_key=(plan.key or own) if plan.kind == "attempt" else plan.key,
        contract_fp=src["contract_fp"],
    )
    return row


async def _project(
    db: Any,
    task: Mapping[str, Any],
    sha: str,
    rows: Sequence[Any],
    runs: list[dict[str, Any]] | None,
    gh_repo: str,
    contract: CiContract,
) -> Standing:
    task_id = int(task["id"])
    stored = await repo.get_ci_run_report(db, task_id, sha)
    plan = plan_projection(stored, rows, runs, sha, gh_repo, contract)
    pick, trust, reason = plan.pick, plan.trust, plan.reason
    key, source = plan.key, plan.source
    if plan.kind == "attempt":
        # The projection takes the attempt's whole body, so a reader of the old
        # one-row-per-commit table sees the current attempt and no other.
        await repo.upsert_ci_run_report(
            db,
            task_id=task_id,
            head_sha=sha,
            ac_results=source["ac_results"],
            validation_status=source["validation_status"],
            validation_log=source["validation_log"],
            reason=source["reason"],
            reported_by=source["reported_by"],
            checks=source["checks"],
            mutations=source["mutations"],
            baseline=source["baseline"],
            provenance=source["provenance"],
            trust=trust,
            trust_reason=reason,
            attempt_key=key or attempt_key(source["run_id"], source["run_attempt"]),
        )
        await ci_contract.stamp_projection(db, task_id, sha, source["contract_fp"])
    elif plan.kind == "trust":
        await repo.set_ci_run_report_trust(
            db, task_id, sha, trust=trust, trust_reason=reason, attempt_key=key
        )
    else:
        await repo.upsert_ci_run_report(
            db,
            task_id=task_id,
            head_sha=sha,
            ac_results=source["ac_results"],
            validation_status=source["validation_status"],
            validation_log=source["validation_log"],
            reason=source["reason"],
            reported_by=source["reported_by"],
            checks=source["checks"],
            provenance=source["provenance"],
            trust=trust,
            trust_reason=reason,
            attempt_key=key,
        )
        await ci_contract.stamp_projection(db, task_id, sha, source["contract_fp"])
    generation = int(task.get("submission_generation") or 0)
    applied, recorded = False, []
    if (task.get("submission_sha") or "").strip() == sha and generation > 0:
        recorded = await write_evidence(
            db,
            task_id,
            generation,
            pick,
            trust,
            key or attempt_key(source["run_id"], source["run_attempt"]),
        )
        applied = trust == VERIFIED
    return Standing(
        trust=VERIFIED if trust == VERIFIED else UNVERIFIED,
        reason="" if trust == VERIFIED else reason,
        attempt_key=key,
        has_report=True,
        applied=applied,
        ac_recorded=tuple(recorded),
    )


_locks: dict[tuple[int, str], asyncio.Lock] = {}
_in_recheck: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ci_trust_in_recheck", default=False
)


async def recheck_in_transaction(
    db: Any, *gates: Callable[[], Awaitable[None]]
) -> None:
    """The ONE place where a decision is re-made inside the transaction that applies it.

    A gate that depends on CI evidence asks GitHub BEFORE the write transaction
    (``prepare_ci_standing`` / ``refresh_for_gate``) and decides; between that
    decision and the write a new report may have been accepted and committed,
    which withdraws the evidence. So the transaction that applies the decision
    first runs the same gates again, here: each gate rereads the projection,
    the task, the AC results and the baseline from the database and refuses with
    its own error if its predicate no longer holds. Nothing in here may call
    GitHub — ``refresh`` raises while a recheck is running.
    """
    if not db.in_transaction:
        raise RuntimeError("recheck_in_transaction outside a write transaction")
    token = _in_recheck.set(True)
    try:
        for gate in gates:
            await gate()
    finally:
        _in_recheck.reset(token)


async def evidence_sha(db: Any, task_id: int, source: str) -> str:
    """The commit a piece of CI-written evidence (its ``source`` key) is about; '' if none.

    ``source`` is ``ci:<run>:<attempt>`` for evidence written from a confirmed
    attempt; ``manual`` / ``legacy`` name no commit.
    """
    parts = source.split(":")
    if len(parts) != 3 or parts[0] != "ci":
        return ""
    rows = await fetchall(
        db,
        "SELECT head_sha FROM ci_report_attempts "
        "WHERE task_id=? AND run_id=? AND run_attempt=? LIMIT 1",
        (task_id, parts[1], parts[2]),
    )
    return str(rows[0][0]) if rows else ""


async def pass_gap(db: Any, task_id: int, head_sha: str) -> str | None:
    """Why the commit has no confirmed GREEN pass in the database right now; None if it has.

    The predicate automation (the autopilot, the steward) approves on: a report
    GitHub confirmed, whose validation passed, and the green proof holds.
    """
    row = await trusted_report(db, task_id, head_sha)
    if row is None:
        return "нет отчёта CI, подтверждённого GitHub, о закреплённом коммите"
    report = dict(row)
    if str(report.get("validation_status") or "").strip() != "pass":
        return f"validation_status={report.get('validation_status') or 'пусто'}, нужен pass"
    return green_gap(report, await ci_contract.contract_for_task(db, task_id))


async def invalidate(db: Any, task: Mapping[str, Any], head_sha: str) -> None:
    """A new claim arrived: show the commit as unconfirmed and withdraw CI's evidence.

    Runs in the transaction of the POST. The attempts and their past verdicts
    stay; what is withdrawn is only the PROJECTION and the evidence derived from
    it, until ``refresh`` settles the claim.
    """
    task_id = int(task["id"])
    if await repo.get_ci_run_report(db, task_id, head_sha) is not None:
        await repo.set_ci_run_report_trust(
            db,
            task_id,
            head_sha,
            trust=UNVERIFIED,
            trust_reason=PENDING,
            attempt_key="",
        )
    generation = int(task.get("submission_generation") or 0)
    if (task.get("submission_sha") or "").strip() == head_sha and generation > 0:
        await write_evidence(db, task_id, generation, None, UNVERIFIED, "")


async def refresh(
    db: Any,
    task_id: int,
    head_sha: str,
    *,
    backoff: bool = False,
    offline: bool = False,
    deadline: float | None = None,
) -> Standing | None:
    """Check what is unchecked, find the current attempt, project it. None: no report.

    Correctness rests on the DATABASE, not on this process: the refresh captures
    the state version of the commit, asks GitHub, and writes the projection and
    the evidence only if the version is still the one it captured (see
    ``_refresh_locked``). The per-commit ``asyncio.Lock`` below only saves
    duplicate work inside one process. The whole refresh — the wait for that
    lock and every call to GitHub — has one deadline; past it the standing is
    recomputed offline. ``offline`` asks no question of GitHub. Never raises on
    a GitHub failure (that is ``forge_unavailable``, not an error).
    """
    head_sha = (head_sha or "").strip()
    if not head_sha:
        return None
    if _in_recheck.get():
        raise RuntimeError("no call to GitHub inside the in-transaction recheck")
    # #2054: the generation the check starts under travels to the wake, which
    # proceeds only if the task still stands on it when trust turned verified.
    task_row = await repo.get_task(db, task_id)
    generation = (
        int(dict(task_row).get("submission_generation") or 0) if task_row else 0
    )
    before = dict(await repo.get_ci_run_report(db, task_id, head_sha) or {})
    standing = await _refresh_guarded(db, task_id, head_sha, backoff, offline, deadline)
    if standing is not None and standing.verified and before.get("trust") != VERIFIED:
        # The edge: the stored report was not verified when this check began.
        from hub.services.ci_verified_wake import wake_on_verified

        # Outside the lock and the write transactions: the projection is committed.
        await wake_on_verified(db, task_id, head_sha, generation)
    return standing


async def _refresh_guarded(
    db: Any,
    task_id: int,
    head_sha: str,
    backoff: bool,
    offline: bool,
    deadline: float | None,
) -> Standing | None:
    lock = _locks.setdefault((task_id, head_sha), asyncio.Lock())
    limit = REFRESH_DEADLINE_SECONDS if deadline is None else deadline
    reserved: list[int] = []  # the versions the rounds of THIS refresh reserved
    try:
        try:
            async with asyncio.timeout(limit):
                async with lock:
                    return await _refresh_locked(
                        db, task_id, head_sha, backoff, offline, reserved
                    )
        except TimeoutError:
            log.warning("CI trust refresh of task %s timed out; offline", task_id)
            return await _offline_after_timeout(
                db, task_id, head_sha, reserved[-1] if reserved else None
            )
    finally:
        # Forgotten once nobody holds or awaits it, so the table does not grow.
        if not lock.locked() and not getattr(lock, "_waiters", None):
            _locks.pop((task_id, head_sha), None)


async def _offline_after_timeout(
    db: Any, task_id: int, head_sha: str, reserved: int | None
) -> Standing | None:
    """What a refresh that ran out of time may still say, without superseding anyone.

    It writes "not confirmed" (and marks the due attempts as tried) only while
    the version its own last round reserved is still the current one. Once
    another refresh or a POST moved the version, newer work stands and this one
    keeps its hands off: the standing is simply read back.
    """
    rows = await repo.list_ci_report_attempts(db, task_id, head_sha)
    task = dict(await repo.get_task(db, task_id) or {})
    if not rows or not task:
        return None
    if reserved is not None:
        gh_args = await _forge_args(db, task_id)
        contract = await ci_contract.contract_for_task(db, task_id)
        verdicts = {
            int(r["id"]): (_unavailable(r), int(r["body_version"]))
            for r in rows
            if not r["trust_final"] or r["contract_fp"] != contract.fingerprint
        }
        standing = await _write_round(
            db,
            task_id,
            head_sha,
            reserved,
            verdicts,
            None,
            (str(gh_args.get("gh_repo") or config.REPO_NAME), contract),
        )
        if standing is not None:
            return standing
    stored = await repo.get_ci_run_report(db, task_id, head_sha)
    trust, reason = (
        trust_of(
            dict(stored), (await ci_contract.contract_for_task(db, task_id)).fingerprint
        )
        if stored is not None
        else (UNVERIFIED, PENDING)
    )
    return Standing(
        trust=trust, reason=reason, has_report=stored is not None, forge_down=True
    )


async def _ask_github(
    rows: Sequence[Any],
    head_sha: str,
    gh_args: Mapping[str, Any],
    backoff: bool,
    contract: CiContract,
) -> tuple[dict[int, tuple[Verdict, int]], list[dict[str, Any]] | None]:
    """Everything one refresh needs from GitHub; goes offline at the first failure."""
    verdicts: dict[int, tuple[Verdict, int]] = {}
    down = False
    for row in rows:
        # The poller tries an unchecked attempt again every RETRY_MINUTES, not
        # every tick; a reader that needs the answer now asks without backoff.
        waiting = backoff and row["checked_minutes_ago"] < RETRY_MINUTES
        # A final verdict holds only under the contract it was made under
        # (#1848): under another one the attempt is checked again, now.
        stale = row["contract_fp"] != contract.fingerprint
        if (row["trust_final"] or waiting) and not stale:
            continue
        if down:  # GitHub already failed in this refresh: do not ask again
            verdict = _unavailable(row)
        else:
            verdict = await check_attempt(row, gh_args, contract)
            down = verdict[1] == FORGE_UNAVAILABLE
        verdicts[int(row["id"])] = (verdict, int(row["body_version"]))
    if down:
        return verdicts, None
    runs = await plugins.git_ops.ci_runs_for_sha(
        head_sha,
        repo=gh_args.get("repo"),
        gh_repo=gh_args.get("gh_repo"),
        forge=gh_args.get("forge", ""),
        workflow_path=contract.workflow_path,
    )
    return verdicts, runs


async def _refresh_locked(
    db: Any,
    task_id: int,
    head_sha: str,
    backoff: bool,
    offline: bool,
    reserved: list[int],
) -> Standing | None:
    gh_args: dict[str, Any] = {}
    for _round in range(MAX_CHECK_ROUNDS):
        # EVERY round reserves a version of its own, atomically, before it asks
        # anything: a round may write only if the database still holds exactly
        # the version it reserved. Adopting the version somebody else moved to
        # would let an answer computed before their write pass as current.
        async with write_transaction(db):
            version = await repo.bump_ci_state_version(db, task_id, head_sha)
        reserved.append(version)
        # The contract is captured HERE, before anything is asked of GitHub, and
        # compared again inside the transaction that writes (#1848).
        contract = await ci_contract.contract_for_task(db, task_id)
        if not contract.fingerprint:
            # Nothing may be asked or written under no contract: the caller
            # (refresh_quietly) turns this into unverified(check_failed).
            raise RuntimeError(f"CI contract of task {task_id} could not be read")
        rows = await repo.list_ci_report_attempts(db, task_id, head_sha)
        if not rows:
            return None
        task = dict(await repo.get_task(db, task_id) or {})
        if not task:
            return None
        if not gh_args:
            gh_args = await _forge_args(db, task_id)
        gh_repo = str(gh_args.get("gh_repo") or config.REPO_NAME)
        verdicts: dict[int, tuple[Verdict, int]] = {}
        runs: list[dict[str, Any]] | None = None
        if offline:
            # Not asked, so not confirmed: but the attempts that were due for a
            # check are marked as tried now (and final once six hours old), so
            # the poller neither hammers them nor waits for them forever.
            verdicts = {
                int(r["id"]): (_unavailable(r), int(r["body_version"]))
                for r in rows
                if not r["trust_final"] or r["contract_fp"] != contract.fingerprint
            }
        else:
            verdicts, runs = await _ask_github(
                rows, head_sha, gh_args, backoff, contract
            )
        standing = await _write_round(
            db, task_id, head_sha, version, verdicts, runs, (gh_repo, contract)
        )
        if standing is not None:
            return standing
    return await _superseded(db, task_id, head_sha)


async def _write_round(
    db: Any,
    task_id: int,
    head_sha: str,
    version: int,
    verdicts: dict[int, tuple[Verdict, int]],
    runs: list[dict[str, Any]] | None,
    asked: tuple[str, CiContract],
) -> Standing | None:
    """Write a round's verdicts and the projection, only if its version is current.

    One short transaction. None: the version moved (or a body was replaced
    meanwhile, or the project's CI contract changed since the round captured it,
    #1848) and nothing of this round was projected. ``asked`` is the repository
    and the contract the round asked GitHub under.
    """
    gh_repo, contract = asked
    async with write_transaction(db):
        if await repo.ci_state_version(db, task_id, head_sha) != version:
            return None
        if await ci_contract.fingerprint_for_task(db, task_id) != contract.fingerprint:
            return None
        stale = False
        for attempt_id, ((trust, reason, final), body) in verdicts.items():
            written = await repo.set_ci_attempt_trust(
                db,
                attempt_id,
                trust=trust,
                trust_reason=reason,
                final=final,
                body_version=body,
            )
            if written:
                await ci_contract.stamp_attempt(db, attempt_id, contract.fingerprint)
            stale = stale or not written
        if stale:
            return None
        fresh = await repo.list_ci_report_attempts(db, task_id, head_sha)
        task = dict(await repo.get_task(db, task_id) or {})
        standing = await _project(
            db, task, head_sha, list(fresh), runs, gh_repo, contract
        )
        return replace(standing, forge_down=runs is None, version=version)


async def _superseded(db: Any, task_id: int, head_sha: str) -> Standing:
    """The world kept changing under the check: unconfirmed, evidence withdrawn."""
    async with write_transaction(db):
        task = dict(await repo.get_task(db, task_id) or {"id": task_id})
        await invalidate(db, task, head_sha)
        await repo.set_ci_run_report_trust(
            db,
            task_id,
            head_sha,
            trust=UNVERIFIED,
            trust_reason=CHECK_SUPERSEDED,
            attempt_key="",
        )
    return Standing(trust=UNVERIFIED, reason=CHECK_SUPERSEDED, has_report=True)


async def _fail_closed(db: Any, task_id: int, head_sha: str) -> None:
    """The check itself broke: show unverified and withdraw what CI wrote."""
    async with write_transaction(db):
        task = dict(await repo.get_task(db, task_id) or {})
        stored = await repo.get_ci_run_report(db, task_id, head_sha)
        if stored is not None:
            await repo.set_ci_run_report_trust(
                db,
                task_id,
                head_sha,
                trust=UNVERIFIED,
                trust_reason=CHECK_FAILED,
                attempt_key="",
            )
        generation = int(task.get("submission_generation") or 0)
        if (task.get("submission_sha") or "").strip() == head_sha and generation > 0:
            await write_evidence(db, task_id, generation, None, UNVERIFIED, "")


async def refresh_quietly(
    db: Any,
    task_id: int,
    head_sha: str,
    *,
    backoff: bool = False,
    offline: bool = False,
) -> Standing | None:
    """``refresh`` for readers that must go on without it (a failure is logged).

    A reader that goes on must not go on with the old picture: when the check
    breaks, the standing becomes unverified(check_failed) and the evidence CI
    wrote is withdrawn.
    """
    try:
        return await refresh(db, task_id, head_sha, backoff=backoff, offline=offline)
    except Exception:  # noqa: BLE001 - a reader must not die of the check
        log.exception("CI trust refresh for task %s failed", task_id)
        try:
            await _fail_closed(db, task_id, (head_sha or "").strip())
        except Exception:  # noqa: BLE001
            log.exception("CI trust fail-closed for task %s failed", task_id)
        return None


async def _preview_round(
    db: Any, task_id: int, head_sha: str, stored: dict | None, *, offline: bool
) -> dict[str, Any] | None:
    """One refresh worked out in memory: the same questions, the same plan, no write."""
    contract = await ci_contract.contract_for_task(db, task_id)
    if not contract.fingerprint:
        raise RuntimeError(f"CI contract of task {task_id} could not be read")
    rows = await repo.list_ci_report_attempts(db, task_id, head_sha)
    if not rows or await repo.get_task(db, task_id) is None:
        return stored
    gh_args = await _forge_args(db, task_id)
    gh_repo = str(gh_args.get("gh_repo") or config.REPO_NAME)
    runs: list[dict[str, Any]] | None = None
    if offline:
        due = [
            r
            for r in rows
            if not r["trust_final"] or r["contract_fp"] != contract.fingerprint
        ]
        verdicts = {int(r["id"]): _unavailable(r) for r in due}
    else:
        asked, runs = await _ask_github(rows, head_sha, gh_args, False, contract)
        verdicts = {attempt_id: verdict for attempt_id, (verdict, _) in asked.items()}
    seen = []
    for row in rows:
        item = dict(row)
        if int(row["id"]) in verdicts:
            trust, reason, final = verdicts[int(row["id"])]
            item.update(
                trust=trust,
                trust_reason=reason,
                trust_final=final,
                contract_fp=contract.fingerprint,
            )
        seen.append(item)
    plan = plan_projection(stored, seen, runs, head_sha, gh_repo, contract)
    return projected_row(plan, task_id, head_sha)


PREVIEW_POLL_SECONDS = 0.01


async def _stored_row(db: Any, task_id: int, head_sha: str) -> dict[str, Any] | None:
    found = await repo.get_ci_run_report(db, task_id, head_sha)
    return dict(found) if found is not None else None


async def preview_report(db: Any, task_id: int, head_sha: str) -> Computed:
    """The projection ``refresh_quietly`` would leave, computed and never saved (#1975).

    A dry run reads GitHub like the refresh does, and the gates then read this
    row instead of the table. The refresh's own outcomes are mirrored: past the
    deadline the answer is the offline one, and a check that breaks leaves the
    stored row shown as unverified(check_failed). A refresh in flight is waited
    for (and, past the deadline, the stored projection stands), but no lock is
    taken, no version bumped, no transaction opened: nothing here may write.
    """
    head_sha = (head_sha or "").strip()
    if not head_sha:
        return Computed(None)
    stored = await _stored_row(db, task_id, head_sha)
    waiting_for_lock = False
    try:
        try:
            async with asyncio.timeout(REFRESH_DEADLINE_SECONDS):
                # A refresh of this commit in flight is waited for, as the real
                # one waits, under the same deadline. Polled, never acquired:
                # the preview takes no lock of its own.
                lock = _locks.get((task_id, head_sha))
                waiting_for_lock = True
                while lock is not None and lock.locked():
                    await asyncio.sleep(PREVIEW_POLL_SECONDS)
                waiting_for_lock = False
                stored = await _stored_row(
                    db, task_id, head_sha
                )  # as the refresh rereads
                row = await _preview_round(db, task_id, head_sha, stored, offline=False)
        except TimeoutError:
            # Out of time waiting for somebody else's refresh: the real refresh
            # reserved no version and saves nothing, the standing is whatever
            # the database holds NOW (``_offline_after_timeout`` with
            # ``reserved=None`` rereads it).
            if waiting_for_lock:
                stored = await _stored_row(db, task_id, head_sha)
                row = stored
            else:
                row = await _preview_round(db, task_id, head_sha, stored, offline=True)
    except Exception:  # noqa: BLE001 - mirrors refresh_quietly: fail closed
        log.exception("CI trust preview for task %s failed", task_id)
        row = (
            None
            if stored is None
            else {
                **stored,
                "trust": UNVERIFIED,
                "trust_reason": CHECK_FAILED,
                "attempt_key": "",
            }
        )
    return Computed(row)


async def refresh_for_gate(db: Any, task: Mapping[str, Any]) -> dict[str, Any]:
    """Bring the standing of the task's pinned commit up to date; the task, reread.

    For every gate that reads AC results or validation: they are written from
    the CURRENT attempt, and which attempt is current is only known by asking.
    """
    sha = (task.get("submission_sha") or "").strip()
    if not sha:
        return dict(task)
    await refresh_quietly(db, int(task["id"]), sha)
    return dict(await repo.get_task(db, int(task["id"])) or task)


_forge_down_until = 0.0
_sweep_cursor: tuple[int, str] | None = None


def forge_recently_down() -> bool:
    """GitHub failed a moment ago: ask nothing, show what is unconfirmed as such."""
    return time.monotonic() < _forge_down_until


async def sweep(db: Any) -> None:
    """The poller's part (#1687): retry what could not be checked, watch reruns.

    Two lists, both small. Attempts whose check is not final are tried again
    every ``RETRY_MINUTES`` (a GitHub that was down answers later; after six
    hours ``forge_unavailable`` is final). Tasks in review are refreshed every
    tick, because a rerun nobody reported changes which attempt is current
    without a single POST arriving.

    The tick is not held hostage by GitHub: no new verification starts after
    ``SWEEP_BUDGET_SECONDS`` (the rest waits for the next tick, its evidence
    stays as unverified as it was), and once GitHub fails it is not asked again
    for ``FORGE_BACKOFF_SECONDS`` — in that time the standing is only
    recomputed offline, so nothing stays confirmed on the strength of a
    GitHub that cannot be asked.
    """
    global _forge_down_until, _sweep_cursor
    found: set[tuple[int, str]] = set()
    for row in await repo.ci_attempts_awaiting_check(
        db, older_than_minutes=RETRY_MINUTES
    ):
        found.add((int(row["task_id"]), str(row["head_sha"])))
    for row in await repo.tasks_in_review_with_sha(db):
        found.add((int(row["id"]), str(row["submission_sha"])))
    # A contract changed under verdicts that were final (#1848): they are
    # checked again without waiting for a new report.
    for row in await ci_contract.attempts_to_recheck(
        db, await ci_contract.settled_attempts(db)
    ):
        found.add((int(row["task_id"]), str(row["head_sha"])))
    # The cursor: a tick that ran out of budget leaves the next one to start
    # AFTER the last commit it handled, so slow early tasks cannot starve the
    # tail of the list forever.
    ordered = sorted(found)
    after = [p for p in ordered if _sweep_cursor is None or p > _sweep_cursor]
    started = time.monotonic()
    for task_id, sha in after + [p for p in ordered if p not in set(after)]:
        if time.monotonic() - started > SWEEP_BUDGET_SECONDS:
            log.warning(
                "CI trust sweep out of budget; the rest waits for the next tick"
            )
            return
        offline = forge_recently_down()
        standing = await refresh_quietly(
            db, task_id, sha, backoff=True, offline=offline
        )
        _sweep_cursor = (task_id, sha)
        if standing is not None and standing.forge_down and not offline:
            _forge_down_until = time.monotonic() + FORGE_BACKOFF_SECONDS
    _sweep_cursor = None  # a full pass: the next tick starts from the top
