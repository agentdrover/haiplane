"""The deploy job tells the Hub what is running (#496).

The Hub knew when it MERGED a change and read that as delivery. On 21.08.2026
task #823 sat ``completed`` with its PR merged into develop while this very job
was marked ``skipped`` — deployment runs from main. Nobody could see that from
the Hub; it took reading GitHub's logs.

These tests read the workflow itself, because that is where the property
lives. They cannot prove a callback reaches the Hub — only a real release does
that — and they are written to fail for the reasons that would matter: the step
disappearing, being moved before the rollout, hard-coding success, or being
allowed to turn a completed deploy into a red job.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
CALLBACK_STEP = "Report deploy to Hub"


@pytest.fixture
def deploy_steps() -> list[dict]:
    if not WORKFLOW.exists():  # pragma: no cover - only in a partial checkout
        pytest.skip(f"workflow not present at {WORKFLOW}")
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return workflow["jobs"]["deploy"]["steps"]


def _callback(steps: list[dict]) -> dict:
    for step in steps:
        if step.get("name") == CALLBACK_STEP:
            return step
    raise AssertionError(f"no {CALLBACK_STEP!r} step in the deploy job")


def test_callback_step_runs_after_deploy_even_on_failure(deploy_steps: list[dict]):
    # AC-1 (#496): after the rollout, and not conditional on it succeeding —
    # a deploy that FELL OVER is a fact the Hub needs too. Reporting only the
    # good ones would show a pipeline that never breaks.
    names = [step.get("name") for step in deploy_steps]
    assert CALLBACK_STEP in names

    rollout_at = names.index("Deploy and health check")
    callback_at = names.index(CALLBACK_STEP)
    assert rollout_at < callback_at, "nothing to report before the rollout ran"
    assert _callback(deploy_steps).get("if") == "always()"


def test_status_follows_the_deploy_outcome(deploy_steps: list[dict]):
    # AC-2 (#496): the status is derived, never asserted. A hard-coded
    # "success" would make the Hub's record of production a formality.
    step = _callback(deploy_steps)
    env = step.get("env", {})

    assert env.get("ROLLOUT_OUTCOME") == "${{ steps.rollout.outcome }}", (
        "the rollout step must be the source of the reported status"
    )
    assert "STATUS=failed" in step["run"], "a failed rollout must be reportable"
    assert "STATUS=success" in step["run"]

    rollout = next(
        s for s in deploy_steps if s.get("name") == "Deploy and health check"
    )
    assert rollout.get("id") == "rollout", "the outcome is read through this id"


def test_callback_reports_the_deployed_sha(deploy_steps: list[dict]):
    # AC-3 (#496): the commit that shipped, to the endpoint that records it.
    step = _callback(deploy_steps)
    env = step.get("env", {})

    assert env.get("DEPLOYED_SHA") == "${{ github.sha }}"
    assert env.get("DEPLOYED_REF") == "${{ github.ref_name }}"
    assert "/api/deploys" in step["run"]
    assert "$DEPLOYED_SHA" in step["run"]


def test_reporting_failure_does_not_fail_the_deploy(deploy_steps: list[dict]):
    # AC-4 (#496): a red job here would claim the deploy did not happen when
    # it did. The failure is still printed — the Hub reads a missing record as
    # "unknown", never as "not deployed" (#839), so a quiet gap stays honest.
    step = _callback(deploy_steps)

    assert step.get("continue-on-error") is True
    assert "::warning::" in step["run"], "a swallowed failure must still be visible"
    assert "::notice::" in step["run"], "a fork without secrets skips, and says so"
