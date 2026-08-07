"""The hub consumes run evidence instead of producing it (#546).

Mechanism tests behind the four acceptance criteria: what the intake stores, what
it refuses, what happens in the ORDER that actually occurs in production (CI runs
when the PR opens, submission pins the commit afterwards), and that the identity
CI authenticates as cannot do anything except report.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import AcceptanceCriterion
from hub.services import orchestration
from hub.services.ci_report import (
    accept_ci_run_report,
    adopt_ci_run_report,
)

# Commit stand-ins are deliberately NOT hex. detect-secrets flags hex
# high-entropy strings, and that scan runs in the same CI job whose outcome the
# delivery gate reads (#605/#606) — a red scan would block merges repo-wide. The
# hub only ever compares a pinned SHA for equality, so a readable stand-in is
# exactly as strong a test as a realistic one.


async def _task(db, *, generation: int = 0, sha: str = "") -> int:
    task_id = await repo.create_task(
        db,
        title="Consume evidence",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.replace_acceptance_criteria(
        db,
        task_id,
        [
            AcceptanceCriterion(
                id="AC-1",
                given="g",
                when="w",
                then="t",
                verifiable_by="test",
                test_ref="tests/test_x.py::test_a",
            ),
            AcceptanceCriterion(
                id="AC-2", given="g", when="w", then="t", verifiable_by="manual"
            ),
        ],
    )
    for _ in range(generation):
        await repo.bump_submission_generation(db, task_id)
    if sha:
        await repo.update_task(db, task_id, submission_sha=sha)
    await db.commit()
    return task_id


# ---- what the intake refuses ----


async def test_a_report_without_a_commit_is_refused(db):
    # The commit is the whole binding. A report that does not name one could be
    # applied to any code, which is exactly what the pin (#572) exists to stop.
    task_id = await _task(db, generation=1, sha="sha-pinned")
    with pytest.raises(ValueError, match="head_sha"):
        await accept_ci_run_report(db, task_id, head_sha="  ", ac_results={})


async def test_an_unknown_status_is_refused_rather_than_coerced(db):
    # A status the hub does not understand must not be quietly mapped onto
    # something it does understand — that is how a red run becomes a green one.
    task_id = await _task(db, generation=1, sha="sha-pinned")
    with pytest.raises(ValueError, match="unknown AC status"):
        await accept_ci_run_report(
            db, task_id, head_sha="sha-pinned", ac_results={"AC-1": "probably"}
        )
    with pytest.raises(ValueError, match="unknown validation status"):
        await accept_ci_run_report(
            db,
            task_id,
            head_sha="sha-pinned",
            ac_results={},
            validation_status="greenish",
        )


async def test_a_report_may_not_invent_acceptance_criteria(db):
    # A report may only speak for AC the hub itself treats as machine verifiable.
    # AC-2 is manual and AC-9 does not exist: both are named back to the caller
    # instead of being dropped in silence.
    task_id = await _task(db, generation=1, sha="sha-pinned")
    out = await accept_ci_run_report(
        db,
        task_id,
        head_sha="sha-pinned",
        ac_results={"AC-1": "pass", "AC-2": "pass", "AC-9": "pass"},
    )
    assert out["applied"] is True
    assert [r["ac_id"] for r in out["ac_recorded"]] == ["AC-1"]
    assert sorted(out["ac_ignored"]) == ["AC-2", "AC-9"]


async def test_a_report_for_another_commit_is_kept_but_not_applied(db):
    # Kept, because it is true evidence about that commit and the task may yet
    # pin it. Not applied, because the code under review is different.
    task_id = await _task(db, generation=1, sha="sha-pinned-one")
    out = await accept_ci_run_report(
        db, task_id, head_sha="sha-not-pinned", ac_results={"AC-1": "pass"}
    )
    assert out["applied"] is False
    assert "sha-pinned-one"[:12] in out["reason"]
    assert await repo.get_ci_run_report(db, task_id, "sha-not-pinned") is not None
    assert [dict(r) for r in await repo.list_ac_test_results(db, task_id)] == []


async def test_an_unknown_validation_result_is_not_written_as_a_failure(db):
    # "Could not run" is not "ran and failed". Writing unknown onto the task
    # would make the gate say "validation_commands не прошли: статус unknown" —
    # an accusation about the work for something that never ran.
    task_id = await _task(db, generation=1, sha="sha-pinned")
    out = await accept_ci_run_report(
        db,
        task_id,
        head_sha="sha-pinned",
        ac_results={},
        validation_status="unknown",
        reason="среди validation_commands есть не-команда",
    )
    assert out["applied"] is True
    task = dict(await repo.get_task(db, task_id))
    assert task["validation_status"] in (None, "")
    assert task["validation_generation"] in (None, 0)
    stored = dict(await repo.get_ci_run_report(db, task_id, "sha-pinned"))
    assert stored["validation_status"] == "unknown"
    assert "не-команда" in stored["reason"], "the cause must survive in the record"


async def test_re_reporting_the_same_commit_updates_instead_of_duplicating(db):
    task_id = await _task(db, generation=1, sha="sha-pinned")
    await accept_ci_run_report(
        db, task_id, head_sha="sha-pinned", ac_results={"AC-1": "fail"}
    )
    await accept_ci_run_report(
        db, task_id, head_sha="sha-pinned", ac_results={"AC-1": "pass"}
    )
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM ci_run_reports WHERE task_id=?", (task_id,)
    )
    assert rows[0][0] == 1, "a re-run of the same commit is an update, not a second row"
    results = [dict(r) for r in await repo.list_ac_test_results(db, task_id)]
    assert results[0]["status"] == "pass"


# ---- the order that actually happens in production ----


async def test_a_report_filed_before_submission_is_adopted_when_the_sha_is_pinned(db):
    # CI runs when the PR opens: at that moment the task has no submission at
    # all (generation 0) and nothing is pinned. Keying evidence by commit is what
    # makes that order work — the report waits for the commit to become the one
    # under review.
    task_id = await _task(db)  # never submitted
    out = await accept_ci_run_report(
        db, task_id, head_sha="sha-early-run", ac_results={"AC-1": "pass"}
    )
    assert out["applied"] is False, "nothing to apply to yet"
    assert "нет ни одной сдачи" in out["reason"] or "не закреплён" in out["reason"]

    generation = await repo.bump_submission_generation(db, task_id)
    await repo.update_task(db, task_id, submission_sha="sha-early-run")
    adopted = await adopt_ci_run_report(db, task_id, "sha-early-run", generation)
    await db.commit()

    assert adopted is not None
    assert [r["ac_id"] for r in adopted["ac_recorded"]] == ["AC-1"]
    rows = [dict(r) for r in await repo.list_ac_test_results(db, task_id)]
    assert rows[0]["submission_generation"] == generation
    assert rows[0]["status"] == "pass"


async def test_adoption_ignores_a_commit_nobody_reported(db):
    task_id = await _task(db, generation=1, sha="sha-never-reported")
    assert await adopt_ci_run_report(db, task_id, "sha-never-reported", 1) is None
    assert [dict(r) for r in await repo.list_ac_test_results(db, task_id)] == []


def _ci_token_headers(monkeypatch) -> dict:
    """An identity holding tasks.ci_report.

    Production grants it through a DB principal with the ci_runner role; from env
    tokens only ``admin`` carries every permission, so that is what stands in
    here. A human or agent token must NOT work — see the refusal tests below.
    """
    from hub import config

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        config.parse_tokens("denis:human-token:human,ci:ci-token:admin"),
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {"Authorization": "Bearer ci-token"}


async def test_submission_adopts_the_report_end_to_end(
    client: AsyncClient, db, monkeypatch
):
    """The live sequence: CI reports the PR head, then the agent submits."""

    class _Git(NoopGitOps):
        async def fetch_base(self, repo: str, base: str):
            return (True, "")

        async def head_sha(self, repo: str, base: str) -> str:
            return "sha-pr-head"

    monkeypatch.setattr(plugins, "git_ops", _Git())
    monkeypatch.setattr(
        orchestration,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )

    task_id = (await client.post("/api/tasks", json={"title": "End to end"})).json()[
        "id"
    ]
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "g",
                    "when": "w",
                    "then": "t",
                    "verifiable_by": "test",
                    "test_ref": "tests/test_x.py::test_a",
                }
            ]
        },
    )
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )

    # CI finishes first — before any submission exists.
    ci = _ci_token_headers(monkeypatch)
    resp = await client.post(
        f"/api/tasks/{task_id}/ci-run-report",
        json={
            "head_sha": "sha-pr-head",
            "ac_results": {"AC-1": "pass"},
            "validation_status": "pass",
        },
        headers=ci,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is False

    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={}, headers=ci)
    assert resp.status_code == 200, resp.text

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief", headers=ci)).json()
    assert brief["ci_run_report"]["state"] == "current"
    assert brief["ac_test_results"] == [
        {"ac_id": "AC-1", "status": "pass", "is_current": True}
    ]
    task = dict(await repo.get_task(db, task_id))
    assert task["validation_status"] == "pass", (
        "the adopted report must also carry the validation verdict, "
        "not only the AC results"
    )
    updates = (await client.get(f"/api/tasks/{task_id}/updates", headers=ci)).json()
    assert any("CI run report adopted" in u["content"] for u in updates), (
        "the adoption must be visible in the task feed, not only in the database"
    )


# ---- the identity CI uses can only report ----


def test_the_ci_runner_role_can_report_and_nothing_else():
    # This token lives in a GitHub secret. Its blast radius is the point.
    from hub.db import ALL_PERMISSIONS, SYSTEM_ROLES

    assert "tasks.ci_report" in ALL_PERMISSIONS
    roles = {name: set(perms) for name, _label, _desc, perms in SYSTEM_ROLES}
    assert roles["ci_runner"] == {"tasks.read", "tasks.ci_report"}
    for forbidden in (
        "tasks.update",
        "tasks.agent_report",
        "tasks.human_gate",
        "tasks.decision",
        "tasks.delete",
    ):
        assert forbidden not in roles["ci_runner"]
    # And no pre-existing role silently gained the new permission.
    holders = {name for name, perms in roles.items() if "tasks.ci_report" in perms}
    assert holders == {"ci_runner", "super_admin"}


def test_default_env_token_roles_cannot_report_a_run():
    # Env tokens (human/agent) fall back to a default permission set, and neither
    # includes this permission — on purpose, and not only for the agent side. A
    # human hand-writing a green report is a declaration by the party whose work
    # is under review, which is precisely what #534/#572 established must never
    # be accepted in place of an observation.
    from hub.config import _AGENT_DEFAULT_PERMS, _HUMAN_DEFAULT_PERMS

    assert "tasks.ci_report" not in _AGENT_DEFAULT_PERMS
    assert "tasks.ci_report" not in _HUMAN_DEFAULT_PERMS
