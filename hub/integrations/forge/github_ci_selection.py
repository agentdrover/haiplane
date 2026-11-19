"""Conservative Actions evidence for the home-infra hosted-to-k8 pilot."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from hub.integrations.protocols import CIProbeOutcome, CIProbeResult

PILOT_REPO = "agentdrover/home-infra"
# Public fingerprint of the Hub CI configuration, followed by public Git blob IDs.
PILOT_CONTRACT = "0fcb35ba10a1636329938d2d6c626cbd6c33f7190a0ea4c8aa43e8faeeae5970"  # pragma: allowlist secret
WORKFLOW = ".github/workflows/tests.yml"
# Audited versions of the same job graph. The second adds test_codex_events to unittest.
WORKFLOW_BLOBS = {
    "bec1443cb1890775c3e11d1d2858eafe3c5fcb55": False,  # pragma: allowlist secret
    "f602fcbfef42ed6b93cd703ca0d48517f0b33a2e": True,  # pragma: allowlist secret
}
WORKFLOW_ID = 397248744
HOSTED_COLLECTOR = "Collect Hub evidence"
K8_COLLECTOR = "Collect Hub evidence on k8"
BALANCED = "k8 full suite (slot ${{ matrix.slot }}/2)"
CHECKOUT = "Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
RUNNER_PROOF = "Record k8 checkout and runner"
LAUNCHER = {
    f"test-launch.sh (part {number}/4)": (f"launcher-{number}", "test-launch.sh")
    for number in range(4)
}
CORE_JOBS = {
    **LAUNCHER,
    "other shell tests": (
        "scripts-0",
        "tests/test-*.sh and hosts/**/test-*.sh (except test-launch.sh)",
    ),
    "python tests (k8 agents)": ("python-0", "unittest"),
}
OPTIONAL_JOBS = {BALANCED, HOSTED_COLLECTOR, K8_COLLECTOR}
BILLING_MESSAGE = (
    "The job was not started because recent account payments have failed or "
    "your spending limit needs to be increased. Please check the 'Billing & plans' "
    "section in your settings"
)
PENDING = {"queued", "pending", "requested", "in_progress", "waiting"}
FAILED = {
    "failure",
    "cancelled",
    "timed_out",
    "startup_failure",
    "action_required",
    "stale",
}
GREEN = {"success", "skipped", "neutral"}
SHA = re.compile(r"[0-9a-f]{40}")
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_ATTEMPTS = 10
MAX_JOB_ATTEMPTS = 40
MAX_CONCURRENT_READS = 8
PROBE_TIMEOUT = 45
Get = Callable[[str], Awaitable[tuple[Any, bool]]]
Facts = dict[str, Any]


class Unverified(Exception):
    """A required read or identity check did not complete."""


async def read(get: Get, path: str) -> Any:
    data, absent = await get(path)
    if absent or data is None:
        raise Unverified(f"unavailable: {path}")
    return data


async def pages(get: Get, path: str, key: str | None = None) -> list[Facts]:
    result: list[Facts] = []
    total = None
    sep = "&" if "?" in path else "?"
    for page in range(1, MAX_PAGES + 1):
        payload = await read(get, f"{path}{sep}per_page={PAGE_SIZE}&page={page}")
        rows = _page_rows(payload, key)
        if key is not None:
            count = payload.get("total_count")
            if (
                type(count) is not int
                or count < 0
                or (total is not None and count != total)
            ):
                raise Unverified(f"inconsistent count: {path}")
            total = count
        result.extend(rows)
        if len(rows) < PAGE_SIZE:
            if total is not None and len(result) != total:
                raise Unverified(f"incomplete pages: {path}")
            return result
    raise Unverified(f"pagination limit: {path}")


def _page_rows(payload: Any, key: str | None) -> list[Facts]:
    rows = payload.get(key) if key and isinstance(payload, dict) else payload
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
        raise Unverified("invalid page")
    if any(not isinstance(row, dict) for row in rows):
        raise Unverified("invalid item in page")
    return rows


def index(rows: list[Facts], field: str) -> dict[Any, Facts]:
    result: dict[Any, Facts] = {}
    for row in rows:
        key = row.get(field)
        if type(key) not in (str, int) or not key or key in result:
            raise Unverified(f"invalid or duplicate {field}")
        result[key] = row
    return result


def _repo_name(value: Any) -> str:
    return str(value.get("full_name", "")).lower() if isinstance(value, dict) else ""


def pr_refs(pr: Any) -> tuple[str, str, str, str]:
    if not isinstance(pr, dict) or pr.get("state") != "open":
        raise Unverified("PR is unavailable or no longer open")
    refs: list[str] = []
    for side in ("head", "base"):
        item = pr.get(side)
        if not isinstance(item, dict) or _repo_name(item.get("repo")) != PILOT_REPO:
            raise Unverified(f"foreign or unreadable PR {side}")
        ref, sha = item.get("ref"), item.get("sha")
        if (
            not isinstance(ref, str)
            or not ref
            or not isinstance(sha, str)
            or not SHA.fullmatch(sha)
        ):
            raise Unverified(f"unreadable PR {side} ref")
        refs.extend((ref, sha))
    if not refs[0].startswith("task-"):
        raise Unverified("PR head is outside the audited task workflow")
    return refs[0], refs[1], refs[2], refs[3]


def verdict(row: Facts, label: str) -> CIProbeResult:
    status, conclusion = row.get("status"), row.get("conclusion")
    if status in PENDING:
        return CIProbeResult(CIProbeOutcome.pending, "current_ci_running", label)
    if status == "completed" and conclusion in FAILED:
        return CIProbeResult(CIProbeOutcome.failed, "current_ci_failed", label)
    if status == "completed" and conclusion in GREEN:
        return CIProbeResult(CIProbeOutcome.passed, "current_ci_complete", label)
    return CIProbeResult(CIProbeOutcome.unavailable, "ci_state_unknown", label)


def label(run: Facts) -> str:
    return (
        f"{run.get('path')} run {run.get('id')}/{run.get('run_attempt')} "
        f"SHA {run.get('head_sha')} ({run.get('event')})"
    )


def canonical(run: Facts, ref: str, sha: str) -> bool:
    return (
        run.get("path") == WORKFLOW
        and run.get("workflow_id") == WORKFLOW_ID
        and run.get("head_branch") == ref
        and run.get("head_sha") == sha
        and _repo_name(run.get("repository")) == PILOT_REPO
        and _repo_name(run.get("head_repository")) == PILOT_REPO
        and type(run.get("id")) is int
        and type(run.get("run_attempt")) is int
        and run["id"] > 0
        and run["run_attempt"] > 0
    )


def _older_pr(run: Facts, selected: Facts, number: int) -> bool:
    return (
        run.get("event") == "pull_request"
        and run.get("status") == "completed"
        and run["id"] < selected["id"]
        and isinstance(run.get("created_at"), str)
        and isinstance(selected.get("created_at"), str)
        and run["created_at"] < selected["created_at"]
        and any(
            isinstance(pr, dict) and pr.get("number") == number
            for pr in (run.get("pull_requests") or [])
        )
    )


async def attempt_jobs(get: Get, api: str, run: Facts) -> dict[str, Facts]:
    rows = await pages(
        get,
        f"{api}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs",
        "jobs",
    )
    for job in rows:
        if (
            type(job.get("run_id")) is not int
            or job["run_id"] != run["id"]
            or type(job.get("run_attempt")) is not int
            or job["run_attempt"] != run["run_attempt"]
            or job.get("head_sha") != run["head_sha"]
            or type(job.get("id")) is not int
            or job["id"] <= 0
            or not isinstance(job.get("name"), str)
            or not job["name"]
        ):
            raise Unverified(f"job identity mismatch: {label(run)}")
    return index(rows, "name")


def _job_completed(job: Facts, conclusion: str) -> bool:
    return job.get("status") == "completed" and job.get("conclusion") == conclusion


def _successful_steps(job: Facts, required: set[str]) -> bool:
    steps = job.get("steps")
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        return False
    actual = index(steps, "name")
    return required <= actual.keys() and all(
        _job_completed(actual[name], "success") for name in required
    )


def _executed(job: Facts, label: str, steps: set[str]) -> bool:
    return (
        _job_completed(job, "success")
        and job.get("labels") == [label]
        and type(job.get("runner_id")) is int
        and job["runner_id"] > 0
        and _successful_steps(job, steps | {CHECKOUT})
    )


def _native(job: Facts, run: Facts, suffix: str, steps: set[str]) -> bool:
    label = f"k8-home-infra-tests-{run['id']}-{run['run_attempt']}-{suffix}"
    return _executed(job, label, steps | {RUNNER_PROOF})


def native_profile(
    jobs: dict[str, Facts], run: Facts, *, collector_required: bool
) -> str:
    if jobs.keys() != CORE_JOBS.keys() | OPTIONAL_JOBS:
        raise Unverified("native job coverage differs from the audited workflow")
    if not all(
        _native(jobs[name], run, suffix, {step})
        for name, (suffix, step) in CORE_JOBS.items()
    ):
        raise Unverified("native checkout, route or required test steps not proved")
    if not all(
        _job_completed(jobs[name], "skipped") for name in (BALANCED, HOSTED_COLLECTOR)
    ):
        raise Unverified("mixed native/hosted or balanced profile")
    collector = jobs[K8_COLLECTOR]
    if (
        not collector_required
        and _job_completed(collector, "skipped")
        and collector.get("steps") == []
    ):
        return "six native test jobs; collector disabled"
    if _native(
        collector,
        run,
        "evidence-0",
        {
            "Summarize selected suite",
            "Collect AC test and validation results for the Hub",
            "Upload results for the trusted k8 sender",
        },
    ):
        return "six native test jobs plus native collector"
    raise Unverified("native collector enabled but its steps are not proved")


async def _billing_annotation(get: Get, api: str, job: Facts) -> bool:
    annotations = await pages(get, f"{api}/check-runs/{job['id']}/annotations")
    failures = {
        item.get("message")
        for item in annotations
        if item.get("annotation_level") not in {"notice", "warning"}
    }
    return failures == {BILLING_MESSAGE} and all(
        item.get("annotation_level") in {"notice", "warning", "failure"}
        for item in annotations
    )


async def prove_billing(get: Get, api: str, jobs: dict[str, Facts]) -> None:
    if jobs.keys() != CORE_JOBS.keys() | OPTIONAL_JOBS:
        raise Unverified("hosted job coverage not established")
    hosted = [jobs[name] for name in (*CORE_JOBS, HOSTED_COLLECTOR)]
    if not all(
        _job_completed(job, "failure")
        and job.get("runner_id") == 0
        and job.get("steps") == []
        and job.get("labels") == ["ubuntu-24.04"]
        for job in hosted
    ):
        raise Unverified("older failure was not a never-started hosted suite")
    if not all(
        _job_completed(jobs[name], "skipped") for name in (BALANCED, K8_COLLECTOR)
    ):
        raise Unverified("older failure includes another execution mode")
    if not all(
        await asyncio.gather(*(_billing_annotation(get, api, job) for job in hosted))
    ):
        raise Unverified("older failure has no authoritative billing annotation")


async def branch_proof(get: Get, api: str, base: str, base_ref: str) -> None:
    branch = quote(base_ref, safe="")
    rules, metadata = await asyncio.gather(
        read(get, f"{api}/rules/branches/{branch}"),
        read(get, f"{api}/branches/{branch}"),
    )
    if (
        not isinstance(rules, list)
        or rules
        or not isinstance(metadata, dict)
        or metadata.get("protected") is not False
    ):
        raise Unverified("independent branch requirements cannot be proved")
    if (metadata.get("commit") or {}).get("sha") != base:
        raise Unverified("base branch changed or is unreadable")


async def policy_proof(
    get: Get, api: str, head: str, base: str, base_ref: str
) -> tuple[str, str]:
    workflow = f"{api}/contents/{WORKFLOW}"
    head_blob, base_blob, _ = await asyncio.gather(
        read(get, f"{workflow}?ref={head}"),
        read(get, f"{workflow}?ref={base}"),
        branch_proof(get, api, base, base_ref),
    )
    if not all(
        isinstance(blob, dict) and blob.get("sha") in WORKFLOW_BLOBS
        for blob in (head_blob, base_blob)
    ):
        raise Unverified("workflow version is outside the audited CI profile")
    return head_blob["sha"], base_blob["sha"]


def _validate_history_run(run: Facts, sha: str) -> None:
    if (
        run.get("head_sha") != sha
        or _repo_name(run.get("repository")) != PILOT_REPO
        or _repo_name(run.get("head_repository")) != PILOT_REPO
        or any(
            type(run.get(field)) is not int or run[field] <= 0
            for field in ("id", "run_attempt", "check_suite_id", "workflow_id")
        )
        or any(
            not isinstance(run.get(field), str) or not run[field]
            for field in ("event", "head_branch", "path")
        )
    ):
        raise Unverified(f"attempt history identity is unreadable: {label(run)}")
    if run["run_attempt"] > MAX_ATTEMPTS:
        raise Unverified("workflow attempt history exceeds proof limit")


def _first_attempt(run: Facts) -> bool:
    return type(run.get("run_attempt")) is int and run["run_attempt"] == 1


def _history_runs(
    runs: list[Facts], profile_runs: tuple[Facts, ...], sha: str, *, ordinary: bool
) -> list[Facts]:
    required = {run["id"] for run in profile_runs}
    suite_counts = Counter(run.get("check_suite_id") for run in runs)
    selected = [
        run
        for run in runs
        if run["id"] in required
        or not _first_attempt(run)
        or (ordinary and run.get("event") == "workflow_dispatch")
    ]
    for run in selected:
        _validate_history_run(run, sha)
        if suite_counts[run["check_suite_id"]] != 1:
            raise Unverified("check suite maps to multiple workflow runs")
    if sum(run["run_attempt"] for run in selected) > MAX_JOB_ATTEMPTS:
        raise Unverified("workflow evidence exceeds attempt read budget")
    return selected


async def _read_histories(
    get: Get, api: str, runs: list[Facts]
) -> dict[int, list[dict[str, Facts]]]:
    histories: dict[int, list[dict[str, Facts]]] = {}
    tasks = []
    for run in runs:
        for number in range(1, run["run_attempt"] + 1):
            tasks.append(
                (
                    run["id"],
                    asyncio.create_task(
                        attempt_jobs(get, api, {**run, "run_attempt": number})
                    ),
                )
            )
    try:
        for run_id, task in tasks:
            histories.setdefault(run_id, []).append(await task)
    finally:
        for _, task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
    return histories


def _match_action_check(check: Facts, run: Facts, job: Facts) -> None:
    suite = run.get("check_suite_id")
    if type(suite) is not int or suite <= 0:
        raise Unverified("check-suite identity missing")
    if (
        check.get("name") != job["name"]
        or check.get("head_sha") != run["head_sha"]
        or (check.get("check_suite") or {}).get("id") != suite
        or (check.get("app") or {}).get("slug") != "github-actions"
        or check.get("status") != job.get("status")
        or check.get("conclusion") != job.get("conclusion")
    ):
        raise Unverified("Actions job does not match its exact check run")


def _genuinely_pending(row: Facts) -> bool:
    return row.get("status") in PENDING and row.get("conclusion") is None


class CheckSnapshot:
    """Commit checks with explicit ownership for any job we can match."""

    def __init__(self, checks: list[Facts]) -> None:
        self.by_id = index(checks, "id")
        self.claimed: set[int] = set()

    def match(self, run: Facts, job: Facts, *, required: bool) -> Facts | None:
        job_id = job["id"]
        if job_id in self.claimed:
            raise Unverified("check run maps to multiple Actions jobs")
        self.claimed.add(job_id)
        check = self.by_id.get(job_id)
        if check is None:
            if required:
                raise Unverified(
                    f"official check missing for job {job_id}: {label(run)}"
                )
            return None
        _match_action_check(check, run, job)
        return check

    def normalize(
        self, run: Facts, attempts: list[dict[str, Facts]], *, optional: bool
    ) -> CIProbeResult | None:
        # IDs bind check ownership; the name identifies the job slot across
        # attempts of this run. A partial rerun may leave earlier jobs current.
        verified = set()
        for job in attempts[-1].values():
            check, blocked = self._current_check(run, job, optional=optional)
            if blocked:
                return blocked
            if check is not None:
                verified.add(job["name"])
        for attempt in reversed(attempts[:-1]):
            self._apply_older_attempt(run, attempt, verified)
        return None

    def _current_check(
        self, run: Facts, job: Facts, *, optional: bool
    ) -> tuple[Facts | None, CIProbeResult | None]:
        if not optional:
            return self.match(run, job, required=True), None
        result = verdict(job, job["name"])
        pending = result.outcome == CIProbeOutcome.pending
        if pending and not (_genuinely_pending(run) and _genuinely_pending(job)):
            raise Unverified(f"pending job conflicts with dispatch state: {label(run)}")
        benign = pending or result.outcome == CIProbeOutcome.passed
        check = self.match(run, job, required=not benign or run["run_attempt"] > 1)
        if pending and check is not None:
            del self.by_id[job["id"]]
        return check, None if benign else result

    def _apply_older_attempt(
        self, run: Facts, jobs: dict[str, Facts], verified: set[str]
    ) -> None:
        for name, job in jobs.items():
            result = verdict(job, name)
            obsolete = (
                name in verified
                and job.get("status") == "completed"
                and result.outcome != CIProbeOutcome.unavailable
            )
            check = self.match(
                run,
                job,
                required=not obsolete and result.outcome != CIProbeOutcome.passed,
            )
            if check is None:
                continue
            if obsolete:
                del self.by_id[job["id"]]
            verified.add(name)


def _independent(checks: list[Facts], statuses: list[Facts]) -> CIProbeResult | None:
    for check in checks:
        result = verdict(check, str(check.get("name") or "independent check"))
        if result.outcome != CIProbeOutcome.passed:
            return result
    seen = set()
    for status in statuses:
        context, state = status.get("context"), status.get("state")
        if not isinstance(context, str) or not context:
            raise Unverified("unknown legacy status context")
        if context in seen:
            continue
        seen.add(context)
        if state != "success":
            outcome = {
                "pending": CIProbeOutcome.pending,
                "failure": CIProbeOutcome.failed,
                "error": CIProbeOutcome.failed,
            }.get(str(state), CIProbeOutcome.unavailable)
            return CIProbeResult(outcome, "independent_commit_status", context)
    return None


async def _commit_ci(get: Get, api: str, sha: str) -> tuple[list[Facts], list[Facts]]:
    checks, statuses = await asyncio.gather(
        pages(get, f"{api}/commits/{sha}/check-runs?filter=all", "check_runs"),
        pages(get, f"{api}/commits/{sha}/statuses"),
    )
    return checks, statuses


def _ordinary_profile(jobs: dict[str, Facts], run: Facts, collector: bool) -> str:
    if (
        jobs.keys() == CORE_JOBS.keys() | OPTIONAL_JOBS
        and all(
            _executed(jobs[name], "ubuntu-24.04", {step})
            for name, (_, step) in CORE_JOBS.items()
        )
        and _executed(
            jobs[HOSTED_COLLECTOR], "ubuntu-24.04", {"Summarize the whole test suite"}
        )
        and all(
            _job_completed(jobs[name], "skipped") for name in (BALANCED, K8_COLLECTOR)
        )
    ):
        return "six hosted test jobs"
    try:
        return native_profile(jobs, run, collector_required=collector)
    except Unverified as exc:
        raise Unverified(
            f"neither hosted nor native PR coverage is proved: {exc}"
        ) from exc


async def _profile_result(
    get: Get,
    api: str,
    selection: tuple[Facts, Facts] | Facts,
    histories: dict[int, list[dict[str, Facts]]],
    checks: CheckSnapshot,
    *,
    collector: bool,
) -> CIProbeResult:
    if isinstance(selection, dict):
        jobs = histories[selection["id"]][-1]
        profile = _ordinary_profile(jobs, selection, collector)
        return CIProbeResult(
            CIProbeOutcome.passed,
            "ci_ordinary_pr_proved",
            f"{label(selection)}: {profile}",
        )
    old, new = selection
    profile = native_profile(
        histories[new["id"]][-1], new, collector_required=collector
    )
    if old.get("conclusion") != "failure":
        return CIProbeResult(
            CIProbeOutcome.passed,
            "ci_native_run_proved",
            f"{label(new)}: {profile}; with successful {label(old)}",
        )
    old_jobs = histories[old["id"]][-1]
    await prove_billing(get, api, old_jobs)
    for job in old_jobs.values():
        del checks.by_id[job["id"]]
    return CIProbeResult(
        CIProbeOutcome.passed,
        "ci_equivalent_native_run",
        f"{label(new)}: {profile}; supersedes billing-only {label(old)}",
    )


async def equivalent_probe(
    get: Get, pr: int, contract_fingerprint: str
) -> CIProbeResult | None:
    """Return None when ordinary PR checks should decide."""
    api = f"repos/{PILOT_REPO}"
    interrupted: set[str] = set()
    capacity = asyncio.Semaphore(MAX_CONCURRENT_READS)
    active: set[asyncio.Future[tuple[Any, bool]]] = set()
    finished = False

    async def track_evidence_read(endpoint: str) -> tuple[Any, bool]:
        async with capacity:
            if finished:
                raise asyncio.CancelledError
            request = asyncio.ensure_future(get(endpoint))
            active.add(request)
            try:
                return await request
            except asyncio.CancelledError:
                interrupted.add(endpoint)
                raise
            finally:
                active.discard(request)

    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            return await _equivalent(track_evidence_read, api, pr, contract_fingerprint)
    except TimeoutError:
        endpoints = ", ".join(sorted(interrupted))
        return CIProbeResult(
            CIProbeOutcome.unavailable,
            "ci_equivalence_timeout",
            f"timed out reading {endpoints}"
            if endpoints
            else "CI evidence deadline exceeded",
        )
    except (Unverified, TypeError, AttributeError) as exc:
        return CIProbeResult(
            CIProbeOutcome.unavailable, "ci_equivalence_unproved", str(exc)
        )
    finally:
        # gather() can raise while independent reads are still outstanding.
        finished = True
        for request in active:
            request.cancel()
        await asyncio.gather(*active, return_exceptions=True)


async def _equivalent(
    get: Get, api: str, pr: int, contract_fingerprint: str
) -> CIProbeResult | None:
    pull_path = f"{api}/pulls/{pr}"
    ref, sha, base_ref, base = refs = pr_refs(await read(get, pull_path))
    run_path = f"{api}/actions/runs?head_sha={sha}"
    runs = await pages(get, run_path, "workflow_runs")
    selection = _select(runs, ref, sha, pr)
    if selection is None or isinstance(selection, CIProbeResult):
        return selection
    if contract_fingerprint != PILOT_CONTRACT:
        raise Unverified("project CI contract is outside the audited dispatch profile")
    ordinary = isinstance(selection, dict)
    profile_runs = (selection,) if isinstance(selection, dict) else selection
    audited = _history_runs(runs, profile_runs, sha, ordinary=ordinary)
    policy, snapshot, histories = await asyncio.gather(
        policy_proof(get, api, sha, base, base_ref),
        _commit_ci(get, api, sha),
        _read_histories(get, api, audited),
    )
    checks, statuses = snapshot
    bound = CheckSnapshot(checks)
    for run in audited:
        optional = ordinary and run.get("event") == "workflow_dispatch"
        if blocked := bound.normalize(run, histories[run["id"]], optional=optional):
            return blocked
    result = await _profile_result(
        get, api, selection, histories, bound, collector=WORKFLOW_BLOBS[policy[0]]
    )
    if blocked := _independent(list(bound.by_id.values()), statuses):
        return blocked
    await _recheck(get, pull_path, run_path, refs, runs)
    fresh_policy, fresh_snapshot = await asyncio.gather(
        policy_proof(get, api, sha, base, base_ref), _commit_ci(get, api, sha)
    )
    if policy != fresh_policy:
        raise Unverified("workflow version changed during the probe")
    if snapshot != fresh_snapshot:
        raise Unverified("independent checks or statuses changed during the probe")
    return result


def _blocking_run(runs: list[Facts]) -> CIProbeResult | None:
    for run in runs:
        result = verdict(run, label(run))
        if result.outcome != CIProbeOutcome.passed:
            return result
    return None


def _ordinary_pr(
    runs: list[Facts], dispatches: list[Facts], ref: str, sha: str, pr: int
) -> Facts | CIProbeResult | None:
    relevant = [
        run
        for run in runs
        if run.get("event") != "workflow_dispatch" or not _genuinely_pending(run)
    ]
    if blocked := _blocking_run(relevant):
        return blocked
    if not dispatches and all(_first_attempt(run) for run in runs):
        return None
    eligible = [
        run
        for run in relevant
        if canonical(run, ref, sha)
        and run.get("event") == "pull_request"
        and run.get("conclusion") == "success"
        and any(
            isinstance(ref, dict) and ref.get("number") == pr
            for ref in (run.get("pull_requests") or [])
        )
    ]
    if not eligible:
        raise Unverified("no successful ordinary PR or canonical dispatch")
    return max(eligible, key=lambda run: run["id"])


def _select(
    runs: list[Facts], ref: str, sha: str, pr: int
) -> tuple[Facts, Facts] | Facts | CIProbeResult | None:
    index(runs, "id")
    dispatches = [run for run in runs if run.get("event") == "workflow_dispatch"]
    manual = [run for run in dispatches if canonical(run, ref, sha)]
    if not manual:
        return _ordinary_pr(runs, dispatches, ref, sha, pr)
    new = max(manual, key=lambda run: run["id"])
    result = verdict(new, label(new))
    if result.outcome != CIProbeOutcome.passed:
        return result
    if new.get("conclusion") != "success":
        raise Unverified("selected workflow did not succeed")
    prior = [
        run for run in runs if canonical(run, ref, sha) and _older_pr(run, new, pr)
    ]
    billing = [
        run
        for run in prior
        if run.get("conclusion") == "failure" and run.get("run_attempt") == 1
    ]
    if len(billing) > 1:
        raise Unverified(
            "multiple older PR failures cannot be matched to one replacement"
        )
    green = [run for run in prior if run.get("conclusion") == "success"]
    old = billing[0] if billing else max(green, key=lambda run: run["id"], default=None)
    remainder = [run for run in runs if run is not old and run is not new]
    if blocked := _blocking_run(remainder):
        return blocked
    if old is None:
        raise Unverified("no matching older PR suite")
    return old, new


async def _recheck(
    get: Get,
    pull_path: str,
    run_path: str,
    refs: tuple[str, str, str, str],
    runs: list[Facts],
) -> None:
    pr, fresh = await asyncio.gather(
        read(get, pull_path), pages(get, run_path, "workflow_runs")
    )
    if pr_refs(pr) != refs or index(fresh, "id") != index(runs, "id"):
        raise Unverified("PR refs or workflow runs changed during the probe")
