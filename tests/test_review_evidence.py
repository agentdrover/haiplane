"""The prepass is the source of truth about validation, not the author (#1246).

Observed on spike #1168, submission #1, sha 1f047cb7a91f: the prepass the hub
ran itself had FAILED — one test, the rebranding guard, found the old product
name twice in a document — while the submission text carried green runs of
lint, security and the complexity budget. The full suite had never been run by
the author. The hub cannot see how a command was run, so an "rc=0" in a
submission stays a word; the rule is to stop resting on it.

Three states, never two: passed, failed, and not run. "Not run" is not
"failed" and not "passed", and every surface that says a submission is checked
reads it from the prepass alone. The author's claim stays visible, labelled as
the author's word.
"""

from __future__ import annotations

from httpx import AsyncClient

from hub import repository as repo
from hub.services import review_evidence

# #1168's commit prefix, split so the secret scanner does not read a key.
_PINNED_SHA = "1f047c" + "b7a91f" + "0" * 28

# The shape of #1168's submission: green runs of everything but the tests.
_GREEN_CLAIM = (
    "ruff check: All checks passed! (rc=0); bandit -r hub -q: rc=0; "
    "complexity_budget.py: зелёный."
)


async def _submitted(client: AsyncClient, db, title: str, summary: str) -> int:
    resp = await client.post("/api/tasks", json={"title": title})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={"summary": summary})
    # No git behind the test: the fixture pins the commit the workspace would.
    await repo.update_task(db, task_id, submission_sha=_PINNED_SHA)
    await db.commit()
    return task_id


async def _prepass(db, task_id: int, checks: dict) -> None:
    from hub.services.ci_report import accept_ci_run_report

    await accept_ci_run_report(
        db,
        task_id,
        head_sha=_PINNED_SHA,
        ac_results={},
        checks=checks,
        reported_by="github-actions",
    )


async def _surfaces(client: AsyncClient, db, task_id: int):
    """Every surface that says whether a submission is checked, by name."""
    from hub.services.review_brief import build_review_brief
    from hub.services.steward_applied import _commit_signal

    brief = await build_review_brief(db, task_id)
    card = (await client.get(f"/tasks/{task_id}")).text
    return {
        "brief": brief.validation,
        "card": card,
        "reviewer_prompt": review_evidence.prepass_block(
            brief.prepass, brief.validation
        ),
        "steward": _commit_signal(brief),
    }


async def test_a_failed_prepass_outweighs_the_authors_claim(client: AsyncClient, db):
    # AC-1: prepass failed, the text is green. Nowhere reads it as checked, and
    # the discrepancy with the claim is named.
    task_id = await _submitted(client, db, "Spike 1168 shape", _GREEN_CLAIM)
    await _prepass(db, task_id, {"lint": "pass", "security": "pass", "tests": "fail"})

    s = await _surfaces(client, db, task_id)

    standing = s["brief"]
    assert standing.state == "failed"
    assert standing.verified is False
    assert standing.discrepancy is True
    assert standing.author_claims, "the author's word stays visible"
    assert "СЛОВО АВТОРА" in standing.headline
    assert "расходится" in standing.headline
    assert "tests" in standing.headline

    prompt = s["reviewer_prompt"]
    assert "СЛОВО АВТОРА" in prompt and "расходится" in prompt
    assert "не проверена" in prompt.lower()

    assert "Сдача проверена:" in s["card"]
    assert "расходится" in s["card"] and "СЛОВО АВТОРА" in s["card"]

    steward = dict(s["steward"])
    assert "checks_ran_on_the_submitted_commit" in steward
    assert "расходится" in steward["checks_ran_on_the_submitted_commit"]

    # And it is counted, with the sample beside it.
    tally = review_evidence.discrepancy_tally([standing])
    assert tally["discrepant"] == 1 and tally["sample"] == 1
    assert "1 из 1" in tally["line"]

    # The practice metric counts it through the same functions.
    from hub.services.orchestration import practice_metrics

    claims = (await practice_metrics(db))["validation_claims"]
    assert (claims["discrepant"], claims["sample"]) == (1, 1)


async def test_a_passing_prepass_needs_no_words(client: AsyncClient, db):
    # AC-2: prepass passed, no claim at all. Checked — the hub counted it.
    task_id = await _submitted(client, db, "Quiet author", "Сделано.")
    await _prepass(db, task_id, {"lint": "pass", "tests": "pass", "types": "pass"})

    s = await _surfaces(client, db, task_id)

    standing = s["brief"]
    assert standing.state == "passed"
    assert standing.verified is True
    assert standing.author_claims == []
    assert standing.discrepancy is False
    assert "СЛОВО АВТОРА" not in s["reviewer_prompt"]
    assert "Сдача проверена предпасом хаба" in s["card"]
    # The steward may still hold on sha or CI grounds (no git here); what it
    # must not do is refuse on the prepass or ask for words.
    steward_reason = dict(s["steward"]).get("checks_ran_on_the_submitted_commit", "")
    assert "предпас" not in steward_reason and "НЕ проверена" not in steward_reason

    tally = review_evidence.discrepancy_tally([standing])
    assert tally == {**tally, "discrepant": 0, "sample": 1}


async def test_not_run_is_its_own_state(client: AsyncClient, db):
    # AC-3: no prepass for this commit. Neither passed nor failed, whatever
    # the author says — and not counted as a discrepancy either.
    task_id = await _submitted(client, db, "Nobody ran it", _GREEN_CLAIM)

    s = await _surfaces(client, db, task_id)

    standing = s["brief"]
    assert standing.state == "not_run"
    assert standing.state not in {"passed", "failed"}
    assert standing.verified is False
    assert standing.discrepancy is False, "no run is not a contradiction"
    assert standing.author_claims, "the claim is still shown"
    assert "не запускался" in standing.headline
    assert "не проверена" in s["reviewer_prompt"].lower()
    assert "не запускался" in s["card"]
    assert "checks_ran_on_the_submitted_commit" in dict(s["steward"])

    # The sample counts only submissions the prepass could judge.
    tally = review_evidence.discrepancy_tally([standing])
    assert tally["discrepant"] == 0 and tally["sample"] == 0
    assert tally["not_run"] == 1

    from hub.services.orchestration import practice_metrics

    claims = (await practice_metrics(db))["validation_claims"]
    assert (claims["discrepant"], claims["sample"], claims["not_run"]) == (0, 0, 1)


# --- The real path: submission, then the dispatch note (#1246, round 2) -----
#
# Finding c5f04f6bc0d366a4: the submission text was read as "the newest done,
# else the newest status". Right after a submission the hub writes its own
# status — "Кросс-модельное ревью вызвано хабом…" — and every surface then
# read the hub's line instead of the author's: no claims, no discrepancy, on
# every production submission. The AC tests above built the prompt by hand
# and never dispatched (7c096fa939f686f0), which is how it passed green.


async def test_the_authors_claim_survives_the_dispatch_note(
    client: AsyncClient, db, monkeypatch
):
    from hub.models import TaskRefine, TaskSubmitReview
    from hub.services import lifecycle
    from hub.services.ci_report import accept_ci_run_report
    from hub.services.orchestration import practice_metrics
    from hub.services.review_brief import build_review_brief
    from hub.services.steward_applied import _commit_signal
    from hub.integrations.registry import plugins
    from tests.test_review_dispatch import (
        _TIP,
        _DispatchRecorder,
        _node,
        _PinnedGitOps,
        _wire,
    )
    import json

    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    pid = await repo.create_project(
        db,
        slug="claim-path",
        name="Claim path",
        repo_name="mrPDA/spike-repo",
        workspace_path="/tmp/ws",
    )
    await repo.update_project(db, pid, gate_policy=json.dumps({"verdict": "auto"}))
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    task_id = await _node(db, title="probe", task_type="task", parent_id=feature)
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: work")
    await repo.update_task_structured(
        db, task_id, TaskRefine(affected_areas=["docs/notes.md"])
    )
    await db.commit()
    plugins.git_ops = _PinnedGitOps(_TIP, ["docs/notes.md"])
    await lifecycle.pair_start_task(db, task_id, caller="dev-agent")
    # The prepass of #1168: lint and security green, the suite red.
    await accept_ci_run_report(
        db,
        task_id,
        head_sha=_TIP,
        ac_results={},
        checks={"lint": "pass", "security": "pass", "tests": "fail"},
        reported_by="github-actions",
    )

    await lifecycle.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5", summary=_GREEN_CLAIM)
    )

    # The dispatch really happened, and wrote its note AFTER the submission.
    assert len(recorder.calls) == 1
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert updates[-1]["content"].startswith("Кросс-модельное ревью вызвано хабом")

    prompt = recorder.calls[0]["prompt_text"]
    assert "СЛОВО АВТОРА" in prompt and "расходится" in prompt

    brief = await build_review_brief(db, task_id)
    assert brief.validation.state == "failed"
    assert brief.validation.author_claims, "the author's claim must be found"
    assert brief.validation.discrepancy is True
    assert "расходится" in dict(_commit_signal(brief)).get(
        "checks_ran_on_the_submitted_commit", ""
    )
    card = (await client.get(f"/tasks/{task_id}")).text
    assert "СЛОВО АВТОРА" in card and "расходится" in card
    claims = (await practice_metrics(db))["validation_claims"]
    assert (claims["discrepant"], claims["sample"]) == (1, 1)


# --- What counts as a green claim (finding 0caf1bf5c4c9dda8) ----------------

_CLAIM_TABLE = {
    "0 passed, 3 failed": False,
    "12 passed, 1 failed": False,
    "5 passed, 2 errors": False,
    "Not all checks passed": False,
    "не все проверки: All checks passed не вышло": False,
    "не зелёный": False,
    "not green": False,
    "CI is no longer green": False,
    "4210 passed": True,
    "4210 passed in 99s": True,
    "rc=0": True,
    "всё зелёное": True,
    "ruff: All checks passed!": True,
    "green on CI": True,
}


def test_a_negated_or_failed_run_is_not_a_green_claim():
    wrong = {
        text: expected
        for text, expected in _CLAIM_TABLE.items()
        if bool(review_evidence.author_green_claims(text)) is not expected
    }
    assert not wrong, f"misclassified (text → expected claim): {wrong}"
