"""Regression cases for exact Actions attempt history across CI proof routes."""

import asyncio

import pytest

from hub.integrations.forge import github_ci_selection as selection

from hub.integrations.protocols import CIProbeOutcome
from tests.test_ci_run_selection import Evidence, _prove_hosted, _retry_with_old_failure

PASSED = CIProbeOutcome.passed
FAILED = CIProbeOutcome.failed
PENDING = CIProbeOutcome.pending
UNKNOWN = CIProbeOutcome.unavailable


def _extra_run(e, event, *, retry=True):
    run = {
        **e.native,
        "id": e.native["id"] + 1,
        "check_suite_id": e.native["check_suite_id"] + 1,
        "workflow_id": e.native["workflow_id"] + 1,
        "path": ".github/workflows/independent.yml",
        "event": event,
    }
    job = {
        **e.jobs[e.native["id"]][0],
        "name": "Independent test",
        "id": run["id"] * 100,
        "run_id": run["id"],
    }
    e.runs.append(run)
    e.jobs[run["id"]] = [job]
    prior = _retry_with_old_failure(e, run) if retry else []
    e.checks = [e._as_check(r, j) for r in e.runs for j in e.jobs[r["id"]]]
    e.checks.extend(e._as_check(run, j) for j in prior)
    return run, job, prior[0] if prior else None


def _route(e, route):
    if route == "native":
        return
    _prove_hosted(e)
    if route == "ordinary-with-dispatch":
        e.native["path"] = ".github/workflows/optional.yml"
    else:
        e.runs = [e.old]


def _update_job_check(e, job, **changes):
    job.update(changes)
    next(check for check in e.checks if check["id"] == job["id"]).update(changes)


@pytest.mark.parametrize(
    ("route", "event"),
    [
        (route, event)
        for route in ("native", "ordinary-with-dispatch", "ordinary-only")
        for event in ("workflow_dispatch", "push", "workflow_run", "pull_request")
        if route != "ordinary-only" or event != "workflow_dispatch"
    ],
)
async def test_admitted_run_history_uses_exact_attempt_jobs(monkeypatch, route, event):
    e = Evidence()
    _route(e, route)
    run, _, _ = _extra_run(e, event)
    result = await e.probe(monkeypatch)
    assert result.outcome is PASSED
    for attempt in (1, 2):
        needle = f"/runs/{run['id']}/attempts/{attempt}/jobs?"
        assert sum(needle in request for request in e.requests) == 1


@pytest.mark.parametrize("route", ["native", "ordinary-with-dispatch", "ordinary-only"])
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("independent_check", FAILED),
        ("independent_status", FAILED),
        ("unlinked_old", FAILED),
        ("wrong_suite", UNKNOWN),
        ("wrong_app", UNKNOWN),
        ("wrong_head", UNKNOWN),
        ("unreadable_history", UNKNOWN),
        ("foreign_run", UNKNOWN),
        ("malformed_attempt", UNKNOWN),
        ("malformed_job_name", UNKNOWN),
        ("duplicate_job", UNKNOWN),
        ("missing_current_check", UNKNOWN),
        ("partial_rerun", FAILED),
        ("current_failure", FAILED),
        ("current_cancelled", FAILED),
        ("current_unknown", UNKNOWN),
        ("current_pending", PENDING),
    ],
)
async def test_normalization_preserves_current_and_unproved_blockers(
    monkeypatch, route, scenario, expected
):
    e = Evidence()
    _route(e, route)
    run, current, prior = _extra_run(e, "workflow_run")
    if scenario == "independent_check":
        e.checks.append({**e.checks[-1], "id": 100, "app": {"slug": "other"}})
    elif scenario == "independent_status":
        e.statuses = [{"context": "external ci", "state": "failure"}]
    elif scenario == "unlinked_old":
        e.checks[-1]["id"] = 999
    elif scenario == "wrong_suite":
        e.checks[-1]["check_suite"] = {"id": 999}
    elif scenario == "wrong_app":
        e.checks[-1]["app"] = {"slug": "other"}
    elif scenario == "wrong_head":
        e.checks[-1]["head_sha"] = "e" * 40
    elif scenario == "unreadable_history":
        e.missing.add((f"actions/runs/{run['id']}/attempts/1/jobs", "1"))
    elif scenario == "foreign_run":
        run["head_repository"] = {"full_name": "foreign/home-infra"}
    elif scenario == "malformed_attempt":
        run["run_attempt"] = True
    elif scenario == "malformed_job_name":
        _update_job_check(e, current, name=13)
        _update_job_check(e, prior, name=13)
    elif scenario == "duplicate_job":
        prior["id"] = current["id"]
    elif scenario == "missing_current_check":
        e.checks = [c for c in e.checks if c["id"] != current["id"]]
    elif scenario == "partial_rerun":
        _update_job_check(e, current, name="Different test")
    elif scenario == "current_failure":
        _update_job_check(e, current, conclusion="failure")
    elif scenario == "current_cancelled":
        _update_job_check(e, current, conclusion="cancelled")
    elif scenario == "current_unknown":
        _update_job_check(e, current, conclusion="unknown")
    elif scenario == "current_pending":
        _update_job_check(e, current, status="in_progress", conclusion=None)
        run.update(status="in_progress", conclusion=None)
    assert (await e.probe(monkeypatch)).outcome is expected


@pytest.mark.parametrize("route", ["native", "ordinary-with-dispatch", "ordinary-only"])
async def test_recheck_rejects_a_run_with_changed_suite_identity(monkeypatch, route):
    e = Evidence()
    _route(e, route)
    run, _, _ = _extra_run(e, "push")
    api = e.api

    async def changed(endpoint, **kwargs):
        if "actions/runs?" in endpoint and e.pr_reads > 1:
            run["check_suite_id"] += 10
        return await api(endpoint, **kwargs)

    monkeypatch.setattr(e, "api", changed)
    assert (await e.probe(monkeypatch)).outcome is UNKNOWN


async def test_ordinary_first_attempt_uses_no_history_reads(monkeypatch):
    e = Evidence()
    _route(e, "ordinary-only")
    _extra_run(e, "push", retry=False)
    result = await e.probe(
        monkeypatch, primary=(0, '[{"name":"tests","state":"SUCCESS"}]', "")
    )
    assert result.outcome is PASSED
    assert not any("/jobs?" in url or "/check-runs?" in url for url in e.requests)


async def test_total_history_budget_is_checked_before_reading_jobs(monkeypatch):
    e = Evidence()
    _extra_run(e, "push")
    monkeypatch.setattr(selection, "MAX_JOB_ATTEMPTS", 3)
    result = await e.probe(monkeypatch)
    assert result.outcome is UNKNOWN
    assert result.details and "attempt read budget" in result.details
    assert not any("/jobs?" in url for url in e.requests)


async def test_concurrent_evidence_reads_are_bounded(monkeypatch):
    e = Evidence()
    _extra_run(e, "workflow_run")
    api = e.api
    active = peak = 0

    async def yield_while_reading(endpoint, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            return await api(endpoint, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(e, "api", yield_while_reading)
    monkeypatch.setattr(selection, "MAX_CONCURRENT_READS", 2)
    assert (await e.probe(monkeypatch)).outcome is PASSED
    assert peak == 2
    assert active == 0


@pytest.mark.parametrize("route", ["native", "ordinary-with-dispatch", "ordinary-only"])
async def test_recheck_rejects_changed_independent_checks(monkeypatch, route):
    e = Evidence()
    _route(e, route)
    _extra_run(e, "push")
    api = e.api

    async def changed(endpoint, **kwargs):
        if "/check-runs?" in endpoint and e.pr_reads > 1:
            e.checks.append({**e.checks[-1], "id": 101, "app": {"slug": "other"}})
        return await api(endpoint, **kwargs)

    monkeypatch.setattr(e, "api", changed)
    result = await e.probe(monkeypatch)
    assert result.outcome is UNKNOWN
    assert result.details and "checks or statuses changed" in result.details


async def test_failed_snapshot_read_cancels_outstanding_job_reads(monkeypatch):
    e = Evidence()
    api = e.api
    started = asyncio.Event()
    active_jobs = 0

    async def interrupted(endpoint, **kwargs):
        nonlocal active_jobs
        if "/jobs?" in endpoint:
            active_jobs += 1
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active_jobs -= 1
        if "/statuses?" in endpoint:
            await started.wait()
            return None, False
        return await api(endpoint, **kwargs)

    monkeypatch.setattr(e, "api", interrupted)
    result = await e.probe(monkeypatch)
    assert result.outcome is UNKNOWN
    assert result.details and "/statuses?" in result.details
    assert started.is_set()
    assert active_jobs == 0
