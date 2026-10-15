"""The hub consumes run evidence instead of producing it (#546).

Mechanism tests behind the four acceptance criteria: what the intake stores, what
it refuses, what happens in the ORDER that actually occurs in production (CI runs
when the PR opens, submission pins the commit afterwards), and that the identity
CI authenticates as cannot do anything except report.
"""

from __future__ import annotations

import itertools
import json
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
from tests.ci_trust_support import FakeGitHub, backed_report, install_fake_github

# #1687: a report counts only for what GitHub confirms, so these tests run
# against a fake GitHub. ``accept`` and ``_post_report`` register the run a
# trusted reporter would have produced (a fresh attempt per call) and send the
# matching report; the checks of the trust itself are in test_ci_trust.py.
GH: FakeGitHub = FakeGitHub()
_RUNS = itertools.count(7000)


@pytest.fixture(autouse=True)
def _github(monkeypatch):
    global GH
    GH = install_fake_github(monkeypatch)


def _registered(task_id: int, report: dict, *, run_id: str | None = None) -> dict:
    """The report ``report`` describes, with the run behind it on the fake GitHub."""
    return backed_report(
        GH, task_id, report, run_id=str(next(_RUNS)) if run_id is None else run_id
    )


async def accept(db, task_id: int, **report):
    """accept_ci_run_report for a run that really happened."""
    return await accept_ci_run_report(db, task_id, **_registered(task_id, report))


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
        await accept(db, task_id, head_sha="  ", ac_results={})


async def test_an_unknown_status_is_refused_rather_than_coerced(db):
    # A status the hub does not understand must not be quietly mapped onto
    # something it does understand — that is how a red run becomes a green one.
    task_id = await _task(db, generation=1, sha="sha-pinned")
    with pytest.raises(ValueError, match="unknown AC status"):
        await accept(
            db, task_id, head_sha="sha-pinned", ac_results={"AC-1": "probably"}
        )
    with pytest.raises(ValueError, match="unknown validation status"):
        await accept(
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
    out = await accept(
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
    out = await accept(
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
    out = await accept(
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
    await accept(db, task_id, head_sha="sha-pinned", ac_results={"AC-1": "fail"})
    await accept(db, task_id, head_sha="sha-pinned", ac_results={"AC-1": "pass"})
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM ci_run_reports WHERE task_id=?", (task_id,)
    )
    assert rows[0][0] == 1, "a re-run of the same commit is an update, not a second row"
    results = [dict(r) for r in await repo.list_ac_test_results(db, task_id)]
    assert results[0]["status"] == "pass"


async def test_a_mutation_report_is_stored_and_grants_nothing(db):
    # #1270: warning only. The mutation result is kept as evidence about the
    # commit, but it neither applies nor blocks anything by itself.
    import json

    task_id = await _task(db, generation=1, sha="sha-pinned")
    out = await accept(
        db,
        task_id,
        head_sha="sha-pinned",
        ac_results={},
        mutations={"state": "baseline_red", "survivors": None},
    )
    assert out["applied"] is True
    assert out["mutations_state"] == "baseline_red"
    stored = dict(await repo.get_ci_run_report(db, task_id, "sha-pinned"))
    assert json.loads(stored["mutations"]) == {
        "state": "baseline_red",
        "survivors": None,
    }
    assert json.loads(stored["checks"]) == {}


async def test_a_report_without_mutations_says_none_were_reported(db):
    task_id = await _task(db, generation=1, sha="sha-pinned")
    out = await accept(db, task_id, head_sha="sha-pinned", ac_results={})
    assert out["mutations_state"] == "not_reported"
    stored = dict(await repo.get_ci_run_report(db, task_id, "sha-pinned"))
    assert stored["mutations"] == "{}"


async def test_an_oversized_mutation_report_is_refused(db):
    task_id = await _task(db, generation=1, sha="sha-pinned")
    with pytest.raises(ValueError, match="mutations"):
        await accept(
            db,
            task_id,
            head_sha="sha-pinned",
            ac_results={},
            mutations={"state": "ran", "blob": "x" * 40_000},
        )


# ---- #1606: a report without the evidence keys does not erase the stored ones ----


_PROV_1 = {
    "run_id": "111",
    "run_url": "https://github.example/runs/111",
    "event": "workflow_dispatch",
    "at": "2026-10-06T10:00:00Z",
}
_FULL_MUTATIONS_BARE = {"state": "ran", "survivors": []}
_FULL_MUTATIONS = {**_FULL_MUTATIONS_BARE, "provenance": _PROV_1}
_FULL_BASELINE_BARE = {
    "state": "ran",
    "merge_base": "base-sha",
    "tests": {"tests/test_x.py::test_a": "failed"},
}
_FULL_BASELINE = {**_FULL_BASELINE_BARE, "provenance": _PROV_1}


async def _post_report(client: AsyncClient, ci, task_id: int, **keys):
    """POST the report of a run registered on the fake GitHub; the JSON answer."""
    report = {
        "head_sha": "sha-pinned",
        "ac_results": {"AC-1": "pass"},
        "validation_status": "pass",
        **keys,
    }
    run_id = keys.pop("run_id", None)
    sent = _registered(task_id, report, run_id=run_id)
    body = {k: v for k, v in sent.items() if v is not None and k != "provenance"}
    body.update(sent["provenance"])
    body.update({k: v for k, v in keys.items() if k == "reported_by"})
    resp = await client.post(
        f"/api/tasks/{task_id}/ci-run-report", json=body, headers=ci
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _stored_evidence(db, task_id: int) -> tuple[dict, dict, dict]:
    import json

    row = dict(await repo.get_ci_run_report(db, task_id, "sha-pinned"))
    return (
        json.loads(row["mutations"]),
        json.loads(row["baseline"]),
        json.loads(row["checks"]),
    )


async def _resend(client: AsyncClient, ci, task_id: int, sent: dict, **changes):
    """POST the SAME attempt again, with ``changes`` to the body (None drops a key)."""
    body = {k: v for k, v in sent.items() if k != "provenance"}
    body.update(sent["provenance"])
    body.update(changes)
    body = {k: v for k, v in body.items() if v is not None}
    resp = await client.post(
        f"/api/tasks/{task_id}/ci-run-report", json=body, headers=ci
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_report_without_evidence_keys_keeps_the_stored_evidence(
    client: AsyncClient, db, monkeypatch
):
    """AC-2: absent key = the step did not run; present key (even {}) replaces.

    Since #1687 the rule holds WITHIN one attempt: the same attempt reported
    again is an update of its row, another attempt is another row.
    """
    ci = _ci_token_headers(monkeypatch)
    task_id = await _task(db, generation=1, sha="sha-pinned")
    sent = GH.attempt(
        task_id,
        "sha-pinned",
        checks={"lint": "pass"},
        mutations=_FULL_MUTATIONS_BARE,
        baseline=_FULL_BASELINE_BARE,
        stamp=False,
    )
    sent["mutations"] = _FULL_MUTATIONS
    sent["baseline"] = _FULL_BASELINE
    await _resend(client, ci, task_id, sent)

    # A synchronize-style report of the same attempt: no mutations, no baseline.
    out = await _resend(client, ci, task_id, sent, mutations=None, baseline=None)
    mutations, baseline, checks = await _stored_evidence(db, task_id)
    assert mutations == _FULL_MUTATIONS, "an absent key must not erase the evidence"
    assert baseline == _FULL_BASELINE
    assert mutations["provenance"] == _PROV_1, "provenance travels with its block"
    assert checks == {"lint": "pass"}
    assert out["mutations_state"] == "ran" and out["baseline_state"] == "ran"

    # A present error replaces — the failure must be visible, not hidden.
    error = {"state": "error", "reason": "timeout", "provenance": _PROV_1}
    await _resend(client, ci, task_id, sent, mutations=error, baseline=None)
    mutations, baseline, _ = await _stored_evidence(db, task_id)
    assert mutations == error
    assert baseline == _FULL_BASELINE, "the other key is still untouched"

    # A present empty object replaces too.
    await _resend(client, ci, task_id, sent, mutations={}, baseline={})
    mutations, baseline, _ = await _stored_evidence(db, task_id)
    assert mutations == {} and baseline == {}


async def test_both_report_orders_leave_the_same_evidence(
    client: AsyncClient, db, monkeypatch
):
    """AC-2: the two halves of one attempt's report arrive in either order."""
    ci = _ci_token_headers(monkeypatch)
    first = await _task(db, generation=1, sha="sha-pinned")
    second = await _task(db, generation=1, sha="sha-pinned")
    for task_id, order in ((first, "checks-first"), (second, "evidence-first")):
        sent = GH.attempt(
            task_id,
            "sha-pinned",
            run_id=f"80{task_id}",
            checks={"lint": "pass"},
            mutations=_FULL_MUTATIONS_BARE,
            baseline=_FULL_BASELINE_BARE,
            stamp=False,
        )
        sent["mutations"], sent["baseline"] = _FULL_MUTATIONS, _FULL_BASELINE
        bare = dict(mutations=None, baseline=None)
        if order == "checks-first":
            await _resend(client, ci, task_id, sent, **bare)
            await _resend(client, ci, task_id, sent)
        else:
            await _resend(client, ci, task_id, sent)
            await _resend(client, ci, task_id, sent, **bare)

    one = await _stored_evidence(db, first)
    two = await _stored_evidence(db, second)
    assert one[0] == two[0] == _FULL_MUTATIONS
    assert one[1] == two[1] == _FULL_BASELINE


# ---- the order that actually happens in production ----


async def test_a_report_filed_before_submission_is_adopted_when_the_sha_is_pinned(db):
    # CI runs when the PR opens: at that moment the task has no submission at
    # all (generation 0) and nothing is pinned. Keying evidence by commit is what
    # makes that order work — the report waits for the commit to become the one
    # under review.
    task_id = await _task(db)  # never submitted
    out = await accept(
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
    await _adoption_flow(client, db, monkeypatch)


async def test_the_brief_shows_the_run_and_what_github_made_of_it(
    client: AsyncClient, db, monkeypatch
):
    """#1686, #1687: the run the reporter named reaches the review brief, with its trust."""
    task_id, brief = await _adoption_flow(
        client, db, monkeypatch, run_id="9001", attempt="2", event="pull_request"
    )
    report = brief["ci_run_report"]
    assert report["provenance"]["run_id"] == "9001"
    assert report["provenance"]["run_attempt"] == "2"
    assert report["provenance"]["event"] == "pull_request"
    assert report["trust"] == "verified" and report["trust_reason"] == ""
    assert "claimed by the reporter" in report["provenance_note"]


async def _adoption_flow(client: AsyncClient, db, monkeypatch, **provenance):

    class _Git(NoopGitOps):
        async def fetch_base(self, repo: str, base: str):
            return (True, "")

        async def head_sha(self, repo: str, base: str) -> str:
            return "sha-pr-head"

    monkeypatch.setattr(plugins, "git_ops", _Git())
    global GH
    GH = install_fake_github(monkeypatch)
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
    sent = GH.attempt(
        task_id,
        "sha-pr-head",
        ac_results={"AC-1": "pass"},
        validation_status="pass",
        **provenance,
    )
    body = {k: v for k, v in sent.items() if v is not None and k != "provenance"}
    body.update(sent["provenance"])
    resp = await client.post(
        f"/api/tasks/{task_id}/ci-run-report", json=body, headers=ci
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is False
    assert resp.json()["trust"] == "verified"

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
    return task_id, brief


# ---- the identity CI uses can only report ----


def test_the_ci_runner_role_can_report_and_nothing_else():
    # This token lives in a GitHub secret. Its blast radius is the point.
    from hub.db import ALL_PERMISSIONS, SYSTEM_ROLES

    assert "tasks.ci_report" in ALL_PERMISSIONS
    roles = {name: set(perms) for name, _label, _desc, perms in SYSTEM_ROLES}
    # #495 widened this set by exactly one verb, deliberately: the deploy job
    # runs under the same secret and reports a second FACT — what production
    # is running — which is a different claim from "the tests ran on this
    # commit" and therefore a different permission. The exact-set assertion is
    # what forced that to be a decision instead of a drift, which is why it is
    # written this way and should stay written this way.
    assert roles["ci_runner"] == {"tasks.read", "tasks.ci_report", "deploys.record"}
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
    # The same lock over the newer permission: nothing else may quietly gain
    # the ability to declare what is deployed.
    deployers = {name for name, perms in roles.items() if "deploys.record" in perms}
    assert deployers == {"ci_runner", "super_admin"}


def test_default_env_token_roles_cannot_report_a_run():
    # Env tokens (human/agent) fall back to a default permission set, and neither
    # includes this permission — on purpose, and not only for the agent side. A
    # human hand-writing a green report is a declaration by the party whose work
    # is under review, which is precisely what #534/#572 established must never
    # be accepted in place of an observation.
    from hub.config import _AGENT_DEFAULT_PERMS, _HUMAN_DEFAULT_PERMS

    assert "tasks.ci_report" not in _AGENT_DEFAULT_PERMS
    assert "tasks.ci_report" not in _HUMAN_DEFAULT_PERMS


async def test_upsert_stores_empty_blocks_for_a_new_row_and_flags_per_key(db):
    """#1606: direct INSERT/ON CONFLICT semantics of the repository."""
    task_id = await _task(db, generation=1, sha="sha-pinned")
    common = dict(
        task_id=task_id,
        head_sha="sha-pinned",
        ac_results="{}",
        validation_status="pass",
        validation_log="",
        reason="",
        reported_by="ci",
    )
    await repo.upsert_ci_run_report(db, **common)  # new row, no keys
    row = dict(await repo.get_ci_run_report(db, task_id, "sha-pinned"))
    assert row["mutations"] == "{}" and row["baseline"] == "{}"

    await repo.upsert_ci_run_report(db, **common, mutations='{"a": 1}')
    await repo.upsert_ci_run_report(db, **common, baseline='{"b": 2}')
    row = dict(await repo.get_ci_run_report(db, task_id, "sha-pinned"))
    assert row["mutations"] == '{"a": 1}', "only the key sent changes"
    assert row["baseline"] == '{"b": 2}'
    await repo.upsert_ci_run_report(db, **common, mutations="{}", baseline='{"c": 3}')
    row = dict(await repo.get_ci_run_report(db, task_id, "sha-pinned"))
    assert row["mutations"] == "{}" and row["baseline"] == '{"c": 3}'

    # A brand-new row given explicit text stores exactly that text.
    other = await _task(db, generation=1, sha="sha-pinned")
    await repo.upsert_ci_run_report(
        db, **{**common, "task_id": other}, mutations='{"m": 1}', baseline='{"n": 2}'
    )
    row = dict(await repo.get_ci_run_report(db, other, "sha-pinned"))
    assert row["mutations"] == '{"m": 1}' and row["baseline"] == '{"n": 2}'


async def test_ci_runner_db_key_reports_and_is_not_human(ci_runner_hub):
    """AC-3 (#1639): the CI key still reports, and judges like any other agent."""
    from tests.test_auto_verdict import _submitted_task

    hub = ci_runner_hub
    db = hub.db

    # Regression: the one job of this key still works.
    task_id = await _task(db, generation=1, sha="sha-pinned")
    report = await _post_report(hub.client, hub.ci, task_id)
    assert report["applied"] is True

    # A foreign task on default whose verdict belongs to the steward: an agent's
    # approved is refused, changes_requested stays open (the agent's own rule).
    # The setup helper talks to the API without a token: lend it the human's,
    # then take it back so every assertion below runs as the CI key.
    hub.client.headers.update(hub.human)
    try:
        foreign = await _submitted_task(
            hub.client, db, "default", {"verdict": "steward"}
        )
        own = await _submitted_task(hub.client, db, "ci-own", {"verdict": "human"})
    finally:
        hub.client.headers.pop("Authorization", None)
    url = f"/api/tasks/{foreign}/review-verdict"
    refused = await hub.client.post(
        url, json={"verdict": "approved", "agent": "ci"}, headers=hub.ci
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == "default_verdict_reserved_for_steward"
    task = (await hub.client.get(f"/api/tasks/{foreign}", headers=hub.human)).json()
    assert task["review_verdict"] is None and task["status"] == "review"
    back = await hub.client.post(
        url,
        json={"verdict": "changes_requested", "comments": "fix it", "agent": "ci"},
        headers=hub.ci,
    )
    assert back.status_code == 200, back.text

    # The task this principal implemented cannot be reviewed by it.
    await db.execute(
        "UPDATE tasks SET assigned_agent = ?, implementer_principal_id = ? "
        "WHERE id = ?",
        (hub.ci_principal["username"], hub.ci_principal["id"], own),
    )
    await db.commit()
    self_review = await hub.client.post(
        f"/api/tasks/{own}/review-verdict",
        json={"verdict": "approved", "agent": "someone-else"},
        headers=hub.ci,
    )
    assert self_review.status_code == 403, self_review.text
    assert self_review.json()["detail"]["reason"] == "self_review_forbidden"
    task = (await hub.client.get(f"/api/tasks/{own}", headers=hub.human)).json()
    assert task["review_verdict"] is None and task["status"] == "review"


# ---- #1644: the CI key is bound to a project ----


async def _project_task(db, slug: str) -> int:
    """A task with AC-1 inside a fresh project ``slug``, pinned to sha-pinned."""
    pid = await repo.create_project(db, slug=slug, name=slug, workspace_path="/tmp/ws")
    epic = await repo.create_task(
        db,
        title="epic",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="running",
        auto_review=True,
        task_type="epic",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, epic, project_id=pid)
    task_id = await _task(db, generation=1, sha="sha-pinned")
    await repo.update_task(db, task_id, parent_id=epic)
    await db.commit()
    return task_id


async def _snapshot(db, task_id: int) -> tuple:
    rows = [
        tuple(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM ci_run_reports WHERE task_id = ? ORDER BY id", (task_id,)
        )
    ]
    ac = [
        tuple(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM acceptance_criteria WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
    ]
    task = tuple(
        (await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (task_id,)))[0]
    )
    return rows, ac, task


async def _key_events(db) -> list[dict]:
    import json as _json

    rows = await db.execute_fetchall(
        "SELECT payload FROM events WHERE kind = 'ci_key_unscoped'"
    )
    return [_json.loads(r[0]) for r in rows]


async def test_scoped_ci_key_cannot_report_for_another_project(
    ci_runner_hub, scoped_ci_key
):
    """AC-1 (#1644): a key bound to audit-in cannot speak for a default task."""
    hub = ci_runner_hub
    db = hub.db
    mine = await _project_task(db, "audit-in")
    foreign = await _task(db, generation=1, sha="sha-pinned")
    first = await _post_report(hub.client, hub.ci, foreign)
    assert first["applied"] is True
    before = await _snapshot(db, foreign)

    scoped = await scoped_ci_key('["project:audit-in"]')
    ok = await _post_report(hub.client, scoped, mine)
    assert ok["applied"] is True

    refused = await hub.client.post(
        f"/api/tasks/{foreign}/ci-run-report",
        json={
            "head_sha": "sha-pinned",
            "ac_results": {"AC-1": "fail"},
            "validation_status": "fail",
            "validation_log": "forged",
        },
        headers=scoped,
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["reason"] == "ci_report_out_of_scope"
    assert await _snapshot(db, foreign) == before, "a refused report writes nothing"


async def test_unscoped_key_is_flagged_once_and_damaged_scope_fails_closed(
    ci_runner_hub, scoped_ci_key
):
    """AC-3 (#1644): legacy works and is flagged once a day; damage fails closed."""
    hub = ci_runner_hub
    db = hub.db
    task_id = await _task(db, generation=1, sha="sha-pinned")
    await _post_report(hub.client, hub.ci, task_id)
    await _post_report(hub.client, hub.ci, task_id)
    deploy = await hub.client.post(
        "/api/deploys",
        json={"sha": "legacy-deploy", "ref": "main", "status": "success"},
        headers=hub.ci,
    )
    assert deploy.status_code == 200, deploy.text
    events = await _key_events(db)
    assert len(events) == 1, events
    assert events[0]["api_key_id"] is not None

    for raw in ("{not json", '["nonsense"]', '"project:default"', "null"):
        damaged = await scoped_ci_key(raw, name=f"damaged-{len(raw)}")
        report = await hub.client.post(
            f"/api/tasks/{task_id}/ci-run-report",
            json={"head_sha": "sha-pinned", "ac_results": {"AC-1": "pass"}},
            headers=damaged,
        )
        assert report.status_code == 403, (raw, report.text)
        dep = await hub.client.post(
            "/api/deploys",
            json={
                "sha": "damaged-deploy",
                "ref": "main",
                "status": "success",
                "project": "default",
            },
            headers=damaged,
        )
        assert dep.status_code == 403, (raw, dep.text)
    rows = await db.execute_fetchall(
        "SELECT 1 FROM releases WHERE deployed_sha = 'damaged-deploy'"
    )
    assert list(rows) == []


async def test_reported_by_comes_from_the_key_not_the_body(ci_runner_hub):
    """AC-4 (#1644): the reporter name is the principal, whatever the body says."""
    hub = ci_runner_hub
    task_id = await _task(hub.db, generation=1, sha="sha-pinned")
    await _post_report(hub.client, hub.ci, task_id, reported_by="mallory")
    row = dict(await repo.get_ci_run_report(hub.db, task_id, "sha-pinned"))
    assert row["reported_by"] == hub.ci_principal["username"]


async def test_the_real_reporter_payload_still_passes_for_an_unscoped_key(
    ci_runner_hub,
):
    """#1644: scripts/ci_report_to_hub.py payload shape keeps working (require)."""
    hub = ci_runner_hub
    task_id = await _task(hub.db, generation=1, sha="sha-pinned")
    sent = GH.attempt(
        task_id,
        "sha-pinned",
        validation_log="ok",
        checks={"ruff": "pass", "mypy": "skipped"},
    )
    payload = {k: v for k, v in sent.items() if v is not None and k != "provenance"}
    payload.update(sent["provenance"])
    payload["reason"] = ""
    resp = await hub.client.post(
        f"/api/tasks/{task_id}/ci-run-report", json=payload, headers=hub.ci
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True


# ---- #1644, round 2: bypasses found by the second review ----


async def test_scoped_ci_key_is_confined_to_the_ci_routes(ci_runner_hub, scoped_ci_key):
    """P1: a bound key must not refine, run commands, or reach MCP/other writes."""
    hub = ci_runner_hub
    db = hub.db
    mine = await _project_task(db, "audit-in")
    foreign = await _task(db, generation=1, sha="sha-pinned")
    scoped = await scoped_ci_key('["project:audit-in"]')
    before = await _snapshot(db, foreign)

    refused = [
        ("POST", f"/api/tasks/{foreign}/refine", {"affected_areas": ["x"]}),
        ("POST", "/api/tasks/refine-bulk", {"items": []}),
        ("POST", f"/api/tasks/{foreign}/run-validation", {}),
        ("POST", f"/api/tasks/{foreign}/run-ac-tests", {}),
        ("POST", "/api/tasks", {"title": "drafted by a CI key"}),
        ("POST", "/mcp", {}),
    ]
    for method, path, body in refused:
        resp = await hub.client.request(method, path, json=body, headers=scoped)
        assert resp.status_code == 403, (path, resp.status_code, resp.text[:200])
    assert await _snapshot(db, foreign) == before
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM tasks")
    assert rows[0][0] == 3, "no task was created by the bound key"

    # What the real reporter does keeps working.
    got = await hub.client.get(f"/api/tasks/{mine}", headers=scoped)
    assert got.status_code == 200, got.text
    assert (await _post_report(hub.client, scoped, mine))["applied"] is True


async def test_empty_or_non_json_scopes_are_damaged_not_unrestricted(
    ci_runner_hub, scoped_ci_key
):
    """P2: only a well-formed ``[]`` means "no restriction"."""
    from hub.services.admin import parse_key_scopes

    assert parse_key_scopes("[]") == ((), False)
    for raw in ("", None, "  ", "[ ]x"):
        assert parse_key_scopes(raw)[1] is True, raw
    hub = ci_runner_hub
    task_id = await _task(hub.db, generation=1, sha="sha-pinned")
    key = await scoped_ci_key("")
    resp = await hub.client.post(
        f"/api/tasks/{task_id}/ci-run-report",
        json={"head_sha": "sha-pinned", "ac_results": {"AC-1": "pass"}},
        headers=key,
    )
    assert resp.status_code == 403, resp.text


async def test_a_scope_project_that_is_not_active_stops_the_key(
    ci_runner_hub, scoped_ci_key
):
    """P2: the project was moved to pending after the key was issued."""
    hub = ci_runner_hub
    db = hub.db
    mine = await _project_task(db, "audit-in")
    scoped = await scoped_ci_key('["project:audit-in"]')
    await db.execute("UPDATE projects SET status = 'pending' WHERE slug = 'audit-in'")
    await db.commit()
    resp = await hub.client.post(
        f"/api/tasks/{mine}/ci-run-report",
        json={"head_sha": "sha-pinned", "ac_results": {"AC-1": "pass"}},
        headers=scoped,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["reason"] == "ci_key_scope_project_inactive"
    dep = await hub.client.post(
        "/api/deploys",
        json={
            "sha": "deploysha1",
            "ref": "main",
            "status": "success",
            "project": "audit-in",
        },
        headers=scoped,
    )
    assert dep.status_code == 403, dep.text
    assert list(await db.execute_fetchall("SELECT 1 FROM releases")) == []


async def test_guard_and_write_of_a_report_share_one_write_transaction(
    ci_runner_hub, scoped_ci_key, monkeypatch
):
    """P2: resolver, scope check and UPSERT run under one BEGIN IMMEDIATE."""
    from hub import app as hub_app
    from hub.services import ci_scope

    hub = ci_runner_hub
    mine = await _project_task(hub.db, "audit-in")
    scoped = await scoped_ci_key('["project:audit-in"]')
    seen: dict[str, bool] = {}

    real_resolve = repo.resolve_bound_project
    real_enforce = ci_scope.enforce_ci_project_scope
    real_accept = hub_app.store_ci_run_report

    async def resolve(db, task_id):
        seen["resolver"] = db.in_transaction
        return await real_resolve(db, task_id)

    async def enforce(db, *a, **kw):
        seen["guard"] = db.in_transaction
        return await real_enforce(db, *a, **kw)

    async def accept(db, *a, **kw):
        seen["accept"] = db.in_transaction
        return await real_accept(db, *a, **kw)

    monkeypatch.setattr(repo, "resolve_bound_project", resolve)
    monkeypatch.setattr(ci_scope, "enforce_ci_project_scope", enforce)
    monkeypatch.setattr(hub_app, "store_ci_run_report", accept)
    await _post_report(hub.client, scoped, mine)
    assert seen == {"resolver": True, "guard": True, "accept": True}, seen


async def test_service_principal_has_no_password_and_no_browser_session(
    ci_runner_hub,
):
    """P3: reset-password -> /login -> cookie must not give a CI principal a door."""
    from hub.services import admin as admin_svc

    hub = ci_runner_hub
    with pytest.raises(ValueError):
        await admin_svc.set_password(hub.db, hub.ci_principal["id"], "Str0ng!pass-1")
    # A credential that got in some other way still opens nothing.
    await hub.db.execute(
        "INSERT INTO password_credentials (principal_id, password_hash) VALUES (?, ?)",
        (hub.ci_principal["id"], admin_svc.hash_password("Str0ng!pass-1")),
    )
    await hub.db.commit()
    assert (
        await admin_svc.authenticate_password(
            hub.db, hub.ci_principal["username"], "Str0ng!pass-1"
        )
        is None
    )
    task_id = await _task(hub.db, generation=1, sha="sha-pinned")
    for headers in (hub.ci_cookie,):
        who = await hub.client.get("/api/whoami", headers=headers)
        assert who.status_code == 401, who.text
        rep = await hub.client.post(
            f"/api/tasks/{task_id}/ci-run-report",
            json={"head_sha": "sha-pinned", "ac_results": {}},
            headers=headers,
        )
        assert rep.status_code == 401, rep.text


# ---- #1644, round 3 ----


async def test_default_scoped_key_cannot_report_for_an_inactive_foreign_project(
    ci_runner_hub, scoped_ci_key
):
    """P1: routing falls back to default for a pending project; auth must not."""
    hub = ci_runner_hub
    db = hub.db
    if await repo.get_project_by_slug(db, "default") is None:
        await repo.create_project(db, slug="default", name="default")
    foreign = await _project_task(db, "other-co")
    await db.execute("UPDATE projects SET status = 'pending' WHERE slug = 'other-co'")
    await db.commit()
    before = await _snapshot(db, foreign)
    key = await scoped_ci_key('["project:default"]')
    resp = await hub.client.post(
        f"/api/tasks/{foreign}/ci-run-report",
        json={
            "head_sha": "sha-pinned",
            "ac_results": {"AC-1": "pass"},
            "validation_status": "pass",
        },
        headers=key,
    )
    assert resp.status_code == 403, resp.text
    assert await _snapshot(db, foreign) == before


async def test_a_failure_after_the_write_rolls_the_report_back(
    ci_runner_hub, monkeypatch
):
    """P3: the handler owns the transaction until the first half is done."""
    from hub import app as hub_app

    hub = ci_runner_hub
    task_id = await _task(hub.db, generation=1, sha="sha-pinned")
    real_store = hub_app.store_ci_run_report

    async def boom(*a, **kw):
        await real_store(*a, **kw)
        raise RuntimeError("failed after the write")

    monkeypatch.setattr(hub_app, "store_ci_run_report", boom)
    try:
        await hub.client.post(
            f"/api/tasks/{task_id}/ci-run-report",
            json={"head_sha": "sha-pinned", "ac_results": {"AC-1": "pass"}},
            headers=hub.ci,
        )
    except RuntimeError:
        pass
    rows = await hub.db.execute_fetchall(
        "SELECT 1 FROM ci_report_attempts WHERE task_id = ?", (task_id,)
    )
    assert list(rows) == [], "the report must not survive a failed request"


# ---- #1686: the key next to branch code reads; the audit key files drafts ----


async def _role_key(hub, role: str, scopes_raw: str | None, name: str) -> dict:
    """A REAL DB key of a fresh principal holding ``role``, bound by ``scopes_raw``."""
    from hub.services import admin as admin_svc

    principal = await admin_svc.create_principal(
        hub.db, kind="service", username=f"{role}-{name}", role_slug=role
    )
    key = await admin_svc.create_api_key(hub.db, principal["id"], name=name)
    if scopes_raw is not None:
        await hub.db.execute(
            "UPDATE api_keys SET scopes = ? WHERE id = ?", (scopes_raw, key["id"])
        )
    await hub.db.commit()
    return {"Authorization": f"Bearer {key['plaintext_key']}"}


_AUDIT_DRAFT = {
    "title": "Уязвимость PYSEC-1 в pkg: обновить зависимость",
    "description": "d",
    "source": "agent",
    "client_request_id": "pip-audit:PYSEC-1",
}


async def test_the_read_and_audit_keys_do_only_their_job(ci_runner_hub):
    hub = ci_runner_hub
    db, client = hub.db, hub.client
    if await repo.get_project_by_slug(db, "default") is None:
        await repo.create_project(db, slug="default", name="default")
    mine = await _project_task(db, "audit-in")
    foreign = await _task(db, generation=1, sha="sha-pinned")

    # ci_reader bound to audit-in: reads ITS task, nothing else.
    reader = await _role_key(hub, "ci_reader", '["project:audit-in"]', "r1")
    assert (await client.get(f"/api/tasks/{mine}", headers=reader)).status_code == 200
    other = await client.get(f"/api/tasks/{foreign}", headers=reader)
    assert other.status_code == 403, other.text
    assert other.json()["detail"]["reason"] == "ci_read_out_of_scope"
    report = {"head_sha": "sha-pinned", "ac_results": {"AC-1": "pass"}}
    for method, path, body in (
        ("POST", f"/api/tasks/{mine}/ci-run-report", report),
        ("POST", "/api/tasks", {"title": "x"}),
        ("POST", "/api/tasks", {**_AUDIT_DRAFT, "client_request_id": "pip-audit:Z"}),
        ("POST", "/api/deploys", {"sha": "a" * 40, "ref": "main"}),
        ("POST", f"/api/tasks/{mine}/refine", {"affected_areas": ["x"]}),
    ):
        resp = await client.request(method, path, json=body, headers=reader)
        assert resp.status_code == 403, (path, resp.status_code, resp.text[:200])
    # No scope, no reading: the new roles have no legacy mode.
    unbound = await _role_key(hub, "ci_reader", None, "r2")
    assert (await client.get(f"/api/tasks/{mine}", headers=unbound)).status_code == 403

    # ci_auditor bound to default: pip-audit:<id> drafts, once.
    auditor = await _role_key(hub, "ci_auditor", '["project:default"]', "a1")
    first = await client.post("/api/tasks", json=_AUDIT_DRAFT, headers=auditor)
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "draft"
    filed = await repo.resolve_project_for_task(db, first.json()["id"])
    assert dict(filed)["slug"] == "default", "the draft lands in the key's project"
    again = await client.post("/api/tasks", json=_AUDIT_DRAFT, headers=auditor)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM tasks WHERE title = ?", (_AUDIT_DRAFT["title"],)
    )
    assert rows[0][0] == 1, "the second post did not duplicate the draft"
    for bad in (
        {**_AUDIT_DRAFT, "client_request_id": "other:PYSEC-1"},
        {k: v for k, v in _AUDIT_DRAFT.items() if k != "client_request_id"},
        {**_AUDIT_DRAFT, "source": "human"},
    ):
        resp = await client.post("/api/tasks", json=bad, headers=auditor)
        assert resp.status_code == 403, (bad, resp.text[:200])
    assert (await client.get(f"/api/tasks/{mine}", headers=auditor)).status_code == 403
    posted = await client.post(
        f"/api/tasks/{foreign}/ci-run-report", json=report, headers=auditor
    )
    assert posted.status_code == 403
    # Bound to another project, the auditor cannot file into default.
    elsewhere = await _role_key(hub, "ci_auditor", '["project:audit-in"]', "a2")
    refused = await client.post(
        "/api/tasks",
        json={**_AUDIT_DRAFT, "client_request_id": "pip-audit:PYSEC-2"},
        headers=elsewhere,
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["reason"] == "ci_audit_out_of_scope"

    # The run provenance survives the round trip; without it the report is
    # stored and is not evidence (#1687).
    with_prov = await _post_report(client, hub.ci, foreign, run_id="9001")
    assert with_prov["provenance"]["run_id"] == "9001"
    assert with_prov["trust"] == "verified"
    stored = dict(await repo.get_ci_run_report(db, foreign, "sha-pinned"))
    assert json.loads(stored["provenance"])["run_id"] == "9001"
    bare = await client.post(
        f"/api/tasks/{foreign}/ci-run-report", json=report, headers=hub.ci
    )
    assert bare.status_code == 200, bare.text
    # A report that names no run is stored as its own (unverified) claim and
    # changes nothing: the current attempt, confirmed above, still stands.
    without = bare.json()
    assert without["provenance"]["run_id"] == "9001"
    assert without["trust"] == "verified"
    kept = await repo.list_ci_report_attempts(db, foreign, "sha-pinned")
    assert [(r["run_id"], r["trust_reason"]) for r in kept] == [
        ("", "no_provenance"),
        ("9001", ""),
    ]


async def test_the_new_roles_hold_nothing_that_reports_a_run():
    from hub.db import SYSTEM_ROLES

    roles = {name: set(perms) for name, _label, _desc, perms in SYSTEM_ROLES}
    assert roles["ci_reader"] == {"tasks.read"}
    assert roles["ci_auditor"] == {"tasks.create"}


async def test_provenance_migration_on_an_old_schema(tmp_path):
    """A database that predates #1686 gets the column and the roles at start."""
    import sqlite3

    from hub import db as db_module

    path = tmp_path / "old.db"
    fresh = await db_module.connect(str(path))
    await db_module.bootstrap(fresh)
    await fresh.execute("INSERT INTO tasks (title) VALUES ('t')")
    await fresh.execute(
        "INSERT INTO ci_run_reports (task_id, head_sha, reported_by) "
        "VALUES (1, 'old-sha', 'ci')"
    )
    await fresh.commit()
    await fresh.close()
    # Take the schema back to before this task.
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE ci_run_reports DROP COLUMN provenance")
    raw.execute("DELETE FROM _migrations WHERE name = 'add_ci_run_reports_provenance'")
    raw.execute("DELETE FROM roles WHERE slug IN ('ci_reader', 'ci_auditor')")
    raw.commit()
    raw.close()

    upgraded = await db_module.connect(str(path))
    await db_module.bootstrap(upgraded)
    await db_module.bootstrap(upgraded)  # twice: idempotent
    row = dict(await repo.get_ci_run_report(upgraded, 1, "old-sha"))
    assert row["provenance"] == "{}", "an old report reads as 'provenance not sent'"
    roles = {
        r[0]
        for r in await upgraded.execute_fetchall(
            "SELECT slug FROM roles WHERE slug IN ('ci_reader', 'ci_auditor')"
        )
    }
    assert roles == {"ci_reader", "ci_auditor"}
    await upgraded.close()


async def test_the_audit_key_files_only_plain_parentless_drafts(ci_runner_hub):
    """#1686 round 2: no field moves the draft out of the project the key covers."""
    hub = ci_runner_hub
    db, client = hub.db, hub.client
    if await repo.get_project_by_slug(db, "default") is None:
        await repo.create_project(db, slug="default", name="default")
    in_foreign = await _project_task(db, "foreign")
    epic = (
        await db.execute_fetchall(
            "SELECT parent_id FROM tasks WHERE id = ?", (in_foreign,)
        )
    )[0][0]
    auditor = await _role_key(hub, "ci_auditor", '["project:default"]', "a-round2")

    async def count() -> int:
        return (await db.execute_fetchall("SELECT COUNT(*) FROM tasks"))[0][0]

    before = await count()
    attempts = {
        "an epic bound to a foreign project": {
            **_AUDIT_DRAFT,
            "client_request_id": "pip-audit:E1",
            "task_type": "epic",
            "project": "foreign",
        },
        "a task naming a foreign project": {
            **_AUDIT_DRAFT,
            "client_request_id": "pip-audit:T1",
            "project": "foreign",
        },
        "a child of a foreign epic": {
            **_AUDIT_DRAFT,
            "client_request_id": "pip-audit:C1",
            "parent_id": epic,
        },
        "an epic without a project": {
            **_AUDIT_DRAFT,
            "client_request_id": "pip-audit:E2",
            "task_type": "epic",
        },
        "a draft that starts a run": {
            **_AUDIT_DRAFT,
            "client_request_id": "pip-audit:R1",
            "run_immediately": True,
        },
    }
    for label, body in attempts.items():
        resp = await client.post("/api/tasks", json=body, headers=auditor)
        assert resp.status_code == 403, (label, resp.status_code, resp.text[:200])
        assert resp.json()["detail"]["reason"] == "ci_audit_draft_only", label
    assert await count() == before, "a refused draft creates nothing"

    plain = await client.post("/api/tasks", json=_AUDIT_DRAFT, headers=auditor)
    assert plain.status_code == 201, plain.text
    landed = await repo.resolve_project_for_task(db, plain.json()["id"])
    assert dict(landed)["slug"] == "default"
    repeat = await client.post("/api/tasks", json=_AUDIT_DRAFT, headers=auditor)
    assert repeat.status_code == 200 and repeat.json()["id"] == plain.json()["id"]
    assert await count() == before + 1

    # The project of the key must be active at the moment of the write.
    await db.execute("UPDATE projects SET status = 'pending' WHERE slug = 'default'")
    await db.commit()
    stopped = await client.post(
        "/api/tasks",
        json={**_AUDIT_DRAFT, "client_request_id": "pip-audit:P1"},
        headers=auditor,
    )
    assert stopped.status_code == 403
    assert stopped.json()["detail"]["reason"] == "ci_key_scope_project_inactive"
    assert await count() == before + 1


# ---- #1700: the timing of a finished attempt --------------------------------


def _timing_body(
    run_id: str = "5001", attempt: str = "1", *, minutes: int = 10, **over
):
    """The body the trusted workflow sends; GitHub's own times, no clock."""
    body = {
        "run_id": run_id,
        "run_attempt": attempt,
        "head_sha": "sha-pinned",
        "event": "pull_request",
        "conclusion": "success",
        "completed_at": "2026-10-15T12:30:00Z",
        "state": "ok",
        "jobs": [
            {
                "name": "Ruff and pytest",
                "conclusion": "success",
                "started_at": "2026-10-15T12:00:00Z",
                "completed_at": f"2026-10-15T12:{minutes:02d}:00Z",
                "steps": [{"key": "test", "conclusion": "success", "seconds": 60.0}],
            }
        ],
    }
    body.update(over)
    return body


async def _post_timing(client, headers, task_id: int, body: dict):
    return await client.post(
        f"/api/tasks/{task_id}/ci-run-timing", json=body, headers=headers
    )


async def test_a_retried_attempt_is_counted_once(ci_runner_hub):
    """AC-4: a resend is one row; a rerun is a second; the sum has each once."""
    from hub.services.metrics_scope import Scope
    from hub.services.ci_timing import ci_minutes_metrics

    hub = ci_runner_hub
    task_id = await _task(hub.db, generation=1, sha="sha-pinned")
    first = _timing_body("5001", "1", minutes=10)
    for _ in range(2):  # the same attempt posted again
        resp = await _post_timing(hub.client, hub.ci, task_id, first)
        assert resp.status_code == 200, resp.text
    rerun = await _post_timing(
        hub.client, hub.ci, task_id, _timing_body("5001", "2", minutes=5)
    )
    assert rerun.status_code == 200, rerun.text

    rows = await hub.db.execute_fetchall(
        "SELECT run_attempt FROM ci_attempt_timings WHERE task_id = ? ORDER BY 1",
        (task_id,),
    )
    assert [r[0] for r in rows] == ["1", "2"], "one row per (task, run, attempt)"
    metrics = await ci_minutes_metrics(hub.db, Scope.relative(30))
    (task,) = metrics["by_task"]
    assert task["runner_minutes"] == 15.0, "10 + 5 minutes, each attempt once"
    assert task["attempts"] == 2

    # A degraded resend does not erase timing that was read.
    degraded = _timing_body("5001", "1", state="timing_unavailable", jobs=[])
    assert (
        await _post_timing(hub.client, hub.ci, task_id, degraded)
    ).status_code == 200
    metrics = await ci_minutes_metrics(hub.db, Scope.relative(30))
    assert metrics["by_task"][0]["runner_minutes"] == 15.0

    # Nor does a resend that says state=ok but carries no jobs (#1700 review).
    blank = _timing_body("5001", "1", jobs=[])
    assert (await _post_timing(hub.client, hub.ci, task_id, blank)).status_code == 200
    metrics = await ci_minutes_metrics(hub.db, Scope.relative(30))
    assert metrics["by_task"][0]["runner_minutes"] == 15.0


async def test_ci_run_timing_is_scoped_like_the_ci_report(ci_runner_hub, scoped_ci_key):
    """AC-5: a key without the task's project is refused, ci_reader/auditor too."""
    hub = ci_runner_hub
    db = hub.db
    if await repo.get_project_by_slug(db, "default") is None:
        await repo.create_project(db, slug="default", name="default")
    mine = await _project_task(db, "audit-in")
    foreign = await _task(db, generation=1, sha="sha-pinned")
    scoped = await scoped_ci_key('["project:audit-in"]')

    ok = await _post_timing(hub.client, scoped, mine, _timing_body())
    assert ok.status_code == 200, ok.text
    refused = await _post_timing(hub.client, scoped, foreign, _timing_body())
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["reason"] == "ci_report_out_of_scope"
    for role in ("ci_reader", "ci_auditor"):
        key = await _role_key(hub, role, '["project:audit-in"]', f"timing-{role}")
        resp = await _post_timing(hub.client, key, mine, _timing_body("5002"))
        assert resp.status_code == 403, (role, resp.text[:200])
    # A human key is not the CI reporter either.
    human = await _post_timing(hub.client, hub.human, mine, _timing_body("5003"))
    assert human.status_code == 403, human.text
    rows = await db.execute_fetchall(
        "SELECT task_id, run_id FROM ci_attempt_timings ORDER BY id"
    )
    assert [tuple(r) for r in rows] == [(mine, "5001")], "only the allowed post wrote"

    # The timing route is in the bound key's allowlist, and a bad time is a 400.
    from hub.auth import ci_scoped_route_allowed

    assert ci_scoped_route_allowed("POST", f"/api/tasks/{mine}/ci-run-timing")
    bad = await _post_timing(
        hub.client, hub.ci, foreign, _timing_body(completed_at="yesterday")
    )
    assert bad.status_code == 400, bad.text
    gone = await _post_timing(hub.client, hub.ci, 999999, _timing_body())
    assert gone.status_code == 404, gone.text


async def test_a_partial_resend_never_replaces_a_complete_measure(ci_runner_hub):
    """#1700 review: a job without completed_at is a lower bound, not the sum."""
    from hub.services.ci_timing import ci_minutes_metrics
    from hub.services.metrics_scope import Scope

    hub = ci_runner_hub
    task_id = await _task(hub.db, generation=1, sha="sha-pinned")

    def two_jobs(*, second_done: bool) -> dict:
        body = _timing_body("6001", "1", minutes=10)
        second = dict(body["jobs"][0], name="second")
        if not second_done:
            second["completed_at"] = None
        body["jobs"] = [body["jobs"][0], second]  # 600 s + 600 s (or 600 s alone)
        return body

    assert (
        await _post_timing(hub.client, hub.ci, task_id, two_jobs(second_done=True))
    ).status_code == 200
    metrics = await ci_minutes_metrics(hub.db, Scope.relative(30))
    assert metrics["by_task"][0]["runner_minutes"] == 20.0
    assert metrics["by_task"][0]["partial"] is False

    assert (
        await _post_timing(hub.client, hub.ci, task_id, two_jobs(second_done=False))
    ).status_code == 200
    metrics = await ci_minutes_metrics(hub.db, Scope.relative(30))
    assert metrics["by_task"][0]["runner_minutes"] == 20.0, "the complete measure stays"

    # A partial attempt that stands alone is shown as partial, not as exact.
    other = await _task(hub.db, generation=1, sha="sha-pinned")
    await _post_timing(hub.client, hub.ci, other, two_jobs(second_done=False))
    metrics = await ci_minutes_metrics(hub.db, Scope.relative(30))
    row = next(r for r in metrics["by_task"] if r["task_id"] == other)
    assert (row["runner_minutes"], row["partial"], row["measured_attempts"]) == (
        10.0,
        True,
        0,
    )
    assert metrics["attempts"]["partial_duration"] == 1
    assert metrics["by_class"] == [] or all(
        c["tasks"] == 1 for c in metrics["by_class"]
    ), "the partial attempt is not in a class"
