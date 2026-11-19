import pytest

from hub.integrations.protocols import CIProbeOutcome
from tests.test_ci_run_selection import (
    NEW,
    Evidence,
    _prove_hosted,
    _retry_with_old_failure,
)


@pytest.mark.parametrize("route", ["canonical", "other-workflow", "other-ref"])
@pytest.mark.parametrize(
    ("status", "conclusion", "canonical_result", "optional_result"),
    [
        ("completed", "success", "pass", "pass"),
        ("completed", "skipped", "unavailable", "pass"),
        ("completed", "neutral", "unavailable", "pass"),
        ("queued", None, "pending", "pass"),
        ("pending", None, "pending", "pass"),
        ("requested", None, "pending", "pass"),
        ("waiting", None, "pending", "pass"),
        ("in_progress", None, "pending", "pass"),
        ("completed", "failure", "fail", "fail"),
        ("completed", "cancelled", "fail", "fail"),
        ("completed", "timed_out", "fail", "fail"),
        ("completed", "startup_failure", "fail", "fail"),
        ("completed", "action_required", "fail", "fail"),
        ("completed", "stale", "fail", "fail"),
        ("completed", "unrecognized", "unavailable", "unavailable"),
        ("in_progress", "success", "pending", "pending"),
    ],
)
@pytest.mark.parametrize(
    "history",
    [
        "first-attempt",
        "exact-old",
        "unbound-old",
        "wrong-suite",
        "wrong-app",
        "historical-pending",
        "history-unreadable",
    ],
)
async def test_dispatch_history_and_current_state_matrix(
    monkeypatch, route, status, conclusion, canonical_result, optional_result, history
):
    e = Evidence()
    _prove_hosted(e)
    if route == "other-workflow":
        e.native["path"] = ".github/workflows/other.yml"
    elif route == "other-ref":
        e.native["head_branch"] = "task-2225/another"
    e.native.update(status=status, conclusion=conclusion)
    previous = (
        _retry_with_old_failure(e, e.native) if history != "first-attempt" else []
    )
    if status != "completed" or conclusion != "success":
        e.jobs[NEW][0].update(status=status, conclusion=conclusion)
    e.checks = [e._as_check(run, job) for run in e.runs for job in e.jobs[run["id"]]]
    if previous:
        stale_check = e._as_check(e.native, previous[0])
        if history == "unbound-old":
            stale_check["id"] = 999
        elif history == "wrong-suite":
            stale_check["check_suite"] = {"id": 999}
        elif history == "wrong-app":
            stale_check["app"] = {"slug": "independent-check-app"}
        elif history == "historical-pending":
            prior_state = {"status": "in_progress", "conclusion": None}
            previous[0].update(prior_state)
            stale_check.update(prior_state)
        elif history == "history-unreadable":
            e.missing.add((f"actions/runs/{NEW}/attempts/1/jobs", "1"))
        e.checks.append(stale_check)
    expected = canonical_result if route == "canonical" else optional_result
    if expected == "pass":
        if history == "unbound-old":
            expected = "fail"
        elif history == "historical-pending":
            expected = "pending"
        elif history in {"wrong-suite", "wrong-app", "history-unreadable"}:
            expected = "unavailable"
    result = await e.probe(monkeypatch)
    assert result.outcome is CIProbeOutcome(expected)


@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_run"])
@pytest.mark.parametrize(
    "status,conclusion,expected",
    [
        ("in_progress", None, CIProbeOutcome.pending),
        ("completed", "failure", CIProbeOutcome.failed),
    ],
)
async def test_independent_workflow_events_still_gate_ordinary_and_native_pr(
    monkeypatch,
    event,
    status,
    conclusion,
    expected,
):
    e = Evidence()
    _prove_hosted(e)
    e.runs.append(
        {
            **e.native,
            "id": NEW + 10,
            "event": event,
            "path": ".github/workflows/independent.yml",
            "status": status,
            "conclusion": conclusion,
        }
    )
    for path in (".github/workflows/tests.yml", ".github/workflows/optional.yml"):
        e.native["path"] = path
        assert (await e.probe(monkeypatch)).outcome is expected
