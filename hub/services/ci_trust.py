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
produced it and is withdrawn when another attempt takes over.

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

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hub import config
from hub import repository as repo
from hub.db import write_transaction
from hub.integrations.registry import plugins
from hub.services import ci_artifact
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

CANONICAL_WORKFLOW = ".github/workflows/ci.yml"
ALLOWED_EVENTS = frozenset({"pull_request", "push", "workflow_dispatch"})
ARTIFACT_PREFIX = "hub-ci-results-"
# A green proof names these four checks; a missing key or a skip is not a pass.
REQUIRED_CHECKS = ("lint", "format", "types", "tests")
SUCCESS = "success"

RETRY_MINUTES = 5
GIVE_UP_MINUTES = 6 * 60


@dataclass(frozen=True)
class Standing:
    """What is true of a commit's CI report right now."""

    trust: str
    reason: str
    attempt_key: str = ""
    has_report: bool = False
    applied: bool = False
    ac_recorded: tuple[dict, ...] = ()

    @property
    def verified(self) -> bool:
        return self.trust == VERIFIED


# ---- reading the trust of a stored row --------------------------------------


def trust_of(report: Mapping[str, Any] | None) -> tuple[str, str]:
    """``(trust, reason)`` of a stored report; a row without the column is legacy."""
    if report is None:
        return UNVERIFIED, "no_report"
    trust = str(report.get("trust") or UNVERIFIED)
    reason = str(report.get("trust_reason") or LEGACY)
    if trust != VERIFIED:
        return UNVERIFIED, reason
    return VERIFIED, ""


def is_verified(report: Mapping[str, Any] | None) -> bool:
    return trust_of(report)[0] == VERIFIED


def unverified_note(reason: str) -> str:
    return f"CI-отчёт не подтверждён: {reason or LEGACY}"


async def trusted_report(db: Any, task_id: int, head_sha: str) -> Any:
    """The stored report of a commit when GitHub confirmed it, else None."""
    if not head_sha:
        return None
    row = await repo.get_ci_run_report(db, task_id, head_sha)
    return row if row is not None and is_verified(dict(row)) else None


def _json_map(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def green_gap(report: Mapping[str, Any]) -> str | None:
    """Why a verified report is not a GREEN proof; None when it is.

    Green needs the run to have ended in ``success`` and each of lint, format,
    types, tests to be ``pass`` — an absent key or a skip proves nothing.
    """
    conclusion = str(_json_map(report.get("provenance")).get("run_conclusion") or "")
    if conclusion != SUCCESS:
        return f"прогон закончился как {conclusion or 'неизвестно'}, нужен success"
    checks = _json_map(report.get("checks"))
    bad = [n for n in REQUIRED_CHECKS if str(checks.get(n) or "") != "pass"]
    if bad:
        return "проверки не pass: " + ", ".join(
            f"{n}={checks.get(n) or 'нет'}" for n in bad
        )
    return None


# ---- one attempt against GitHub --------------------------------------------


def canonical_path(path: str) -> bool:
    """ci.yml, with the documented ``@<ref>`` suffix of a run's path ignored."""
    return path.split("@", 1)[0] == CANONICAL_WORKFLOW


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
    run: dict[str, Any], prov: dict[str, Any], row: Any, gh_repo: str
) -> str:
    """The first way a run disagrees with the report, or ''."""
    if run["run_id"] != prov["run_id"] or run["run_attempt"] != prov["run_attempt"]:
        return RUN_NOT_FOUND
    if not _same_repo(run["repository"], gh_repo) or not _same_repo(
        run["head_repository"], gh_repo
    ):
        return WRONG_REPOSITORY
    if not canonical_path(run["path"]) or not canonical_path(
        str(prov.get("workflow_path") or "")
    ):
        return WRONG_WORKFLOW
    if run["head_sha"] != row["head_sha"]:
        return WRONG_COMMIT
    if run["event"] not in ALLOWED_EVENTS or (
        prov.get("event") and prov["event"] != run["event"]
    ):
        return WRONG_EVENT
    return ""


async def check_attempt(row: Any, gh_args: Mapping[str, Any]) -> Verdict:
    """Ask GitHub whether one reported attempt is what its report claims."""
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
    problem = _run_problem(run, prov, row, gh_repo)
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


# ---- which attempt is current ----------------------------------------------


def canonical_runs(
    runs: list[dict[str, Any]], head_sha: str, gh_repo: str
) -> list[dict[str, Any]]:
    """The runs that count: ci.yml of this repository, on this commit."""
    return [
        r
        for r in runs
        if canonical_path(r["path"])
        and r["head_sha"] == head_sha
        and _same_repo(r["repository"], gh_repo)
        and _same_repo(r["head_repository"], gh_repo)
    ]


def current_attempt(
    runs: list[dict[str, Any]], head_sha: str, gh_repo: str
) -> dict[str, Any] | None:
    """The attempt that started last among the commit's runs of ci.yml."""
    mine = canonical_runs(runs, head_sha, gh_repo)
    if not mine:
        return None
    return max(mine, key=lambda r: (r["run_started_at"], int(r["run_id"] or 0)))


def _standing_attempt(
    rows: Sequence[Any],
    runs: list[dict[str, Any]] | None,
    sha: str,
    gh_repo: str,
    projected_key: str,
) -> tuple[Mapping[str, Any] | None, str, str, str]:
    """``(attempt row, trust, reason, key)`` the projection must show."""
    by_key = {
        attempt_key(r["run_id"], r["run_attempt"]): r for r in rows if r["run_id"]
    }
    newest = rows[0]
    if runs is None:
        # GitHub could not say which attempt is current: keep a confirmed
        # projection, otherwise show the newest report with what is known of it.
        held = by_key.get(projected_key)
        pick = held if held is not None and held["trust"] == VERIFIED else newest
        return pick, pick["trust"], pick["trust_reason"], projected_key
    cur = current_attempt(runs, sha, gh_repo)
    if cur is None:
        return newest, newest["trust"], newest["trust_reason"], ""
    key = attempt_key(cur["run_id"], cur["run_attempt"])
    if cur["status"] != "completed":
        return None, UNVERIFIED, CURRENT_ATTEMPT_INCOMPLETE, key
    match = by_key.get(key)
    if match is None:
        # A report about a run GitHub does not list for this commit is judged
        # on its own (run_not_found, wrong_repository, ...). Only when the
        # reported runs are real and a later attempt said nothing is the
        # answer "newer attempt without report".
        known = {r["run_id"] for r in canonical_runs(runs, sha, gh_repo)}
        if not by_key or newest["run_id"] not in known:
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
    green = str(_json_map(row["provenance"]).get("run_conclusion") or "") == SUCCESS
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
    trust, reason = trust_of(row)
    recorded = await write_evidence(
        db, task_id, generation, row, trust, str(row.get("attempt_key") or "")
    )
    return trust, reason, recorded


async def _project(
    db: Any,
    task: Mapping[str, Any],
    sha: str,
    rows: Sequence[Any],
    runs: list[dict[str, Any]] | None,
    gh_repo: str,
) -> Standing:
    task_id = int(task["id"])
    stored = await repo.get_ci_run_report(db, task_id, sha)
    projected_key = str(dict(stored).get("attempt_key") or "") if stored else ""
    pick, trust, reason, key = _standing_attempt(
        rows, runs, sha, gh_repo, projected_key
    )
    source = pick if pick is not None else rows[0]
    if pick is not None:
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
    elif stored is not None:
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


async def refresh(
    db: Any, task_id: int, head_sha: str, *, backoff: bool = False
) -> Standing | None:
    """Check what is unchecked, find the current attempt, project it. None: no report.

    The network part runs first and holds no transaction; the write part is one
    short transaction. Safe to call from any reader and from the poller; never
    raises on a GitHub failure (that is ``forge_unavailable``, not an error).
    """
    head_sha = (head_sha or "").strip()
    if not head_sha:
        return None
    rows = await repo.list_ci_report_attempts(db, task_id, head_sha)
    if not rows:
        return None
    task = dict(await repo.get_task(db, task_id) or {})
    if not task:
        return None
    gh_args = await _forge_args(db, task_id)
    gh_repo = str(gh_args.get("gh_repo") or config.REPO_NAME)
    verdicts: dict[int, Verdict] = {}
    for row in rows:
        # The poller tries an unchecked attempt again every RETRY_MINUTES, not
        # every tick; a reader that needs the answer now asks without backoff.
        waiting = backoff and row["checked_minutes_ago"] < RETRY_MINUTES
        if not row["trust_final"] and not waiting:
            verdicts[int(row["id"])] = await check_attempt(row, gh_args)
    runs = await plugins.git_ops.ci_runs_for_sha(
        head_sha,
        repo=gh_args.get("repo"),
        gh_repo=gh_args.get("gh_repo"),
        forge=gh_args.get("forge", ""),
    )
    async with write_transaction(db):
        for attempt_id, (trust, reason, final) in verdicts.items():
            await repo.set_ci_attempt_trust(
                db, attempt_id, trust=trust, trust_reason=reason, final=final
            )
        fresh = await repo.list_ci_report_attempts(db, task_id, head_sha)
        task = dict(await repo.get_task(db, task_id) or task)
        return await _project(db, task, head_sha, list(fresh), runs, gh_repo)


async def refresh_quietly(
    db: Any, task_id: int, head_sha: str, *, backoff: bool = False
) -> Standing | None:
    """``refresh`` for readers that must go on without it (a failure is logged)."""
    try:
        return await refresh(db, task_id, head_sha, backoff=backoff)
    except Exception:  # noqa: BLE001 - a reader must not die of the check
        log.exception("CI trust refresh for task %s failed", task_id)
        return None


async def sweep(db: Any) -> None:
    """The poller's part (#1687): retry what could not be checked, watch reruns.

    Two lists, both small. Attempts whose check is not final are tried again
    every ``RETRY_MINUTES`` (a GitHub that was down answers later; after six
    hours ``forge_unavailable`` is final). Tasks in review are refreshed every
    tick, because a rerun nobody reported changes which attempt is current
    without a single POST arriving.
    """
    pairs: dict[tuple[int, str], None] = {}
    for row in await repo.ci_attempts_awaiting_check(
        db, older_than_minutes=RETRY_MINUTES
    ):
        pairs[(int(row["task_id"]), str(row["head_sha"]))] = None
    for row in await repo.tasks_in_review_with_sha(db):
        pairs[(int(row["id"]), str(row["submission_sha"]))] = None
    for task_id, sha in pairs:
        await refresh_quietly(db, task_id, sha, backoff=True)
