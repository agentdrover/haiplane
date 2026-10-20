"""Mutations and baseline of another verified attempt of the same commit (#1936).

A pull_request:synchronize run does not count mutations, and it starts after the
executor's dispatch run, so it is the CURRENT attempt and its empty blocks used
to hide what the dispatch run counted on the very same commit. The brief now
shows the dispatch run's blocks, signed with their source. Only the brief: the
stored projection (``ci_run_reports``) and the gates stay on the current attempt.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hub import repository as repo
from hub.services import ci_trust, review_brief_projection, review_evidence
from hub.services.ci_report import store_ci_run_report
from tests.ci_trust_support import FakeGitHub, install_fake_github
from tests.test_red_test_gate import (
    _TIP,
    _baseline,
    _detail,
    _REF_1,
    _REF_2,
    _report_baseline,
    _running_bug,
    _submit,
)

SHA = "a" * 40
OTHER_SHA = "b" * 40
GH: FakeGitHub = FakeGitHub()


@pytest.fixture(autouse=True)
def _github(monkeypatch):
    global GH
    GH = install_fake_github(monkeypatch)


async def _task(db, *, sha: str = SHA) -> int:
    from hub.models import AcceptanceCriterion

    task_id = await repo.create_task(
        db,
        title="Other attempt",
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
            )
        ],
    )
    await repo.bump_submission_generation(db, task_id)
    await repo.update_task(db, task_id, submission_sha=sha)
    await db.commit()
    return task_id


def mutations(state: str = "ran", head: str | None = SHA, **extra: Any) -> dict:
    block: dict[str, Any] = {"state": state, "survivors": [], **extra}
    if head is not None:
        block["head"] = head
    return block


def baseline(state: str = "ran", head: str | None = SHA, **extra: Any) -> dict:
    block: dict[str, Any] = {"state": state, "tests": {}, **extra}
    if head is not None:
        block["head"] = head
    return block


async def _attempt(db, task_id: int, sha: str = SHA, **kwargs: Any) -> str:
    """Report one attempt and let the hub check it against the (fake) GitHub."""
    sent = GH.attempt(task_id, sha, **kwargs)
    await store_ci_run_report(db, task_id, **sent)
    await db.commit()
    await ci_trust.refresh(db, task_id, sha)
    return sent["provenance"]["run_id"]


async def _evidence(db, task_id: int):
    return await review_evidence.ci_evidence_state(
        db, dict(await repo.get_task(db, task_id))
    )


async def _dispatch_then_synchronize(
    db, task_id: int, ids: tuple[str, str] = ("1001", "1002"), **dispatch: Any
):
    """The sequence of #1920: the dispatch counts, then the PR run (empty) starts later."""
    dispatch.setdefault("mutations", mutations())
    dispatch.setdefault("baseline", baseline())
    first = await _attempt(db, task_id, run_id=ids[0], **dispatch)
    second = await _attempt(db, task_id, run_id=ids[1], event="pull_request")
    return first, second


# ---- AC-1 -------------------------------------------------------------------


async def test_mutations_come_from_the_verified_dispatch_attempt(db):
    task_id = await _task(db)
    await _dispatch_then_synchronize(db, task_id)

    projection = dict(await repo.get_ci_run_report(db, task_id, SHA))
    assert projection["attempt_key"] == "ci:1002:1", "the PR run is the current attempt"
    assert json.loads(projection["mutations"]) == {}, "the projection is not touched"
    assert json.loads(projection["baseline"]) == {}

    evidence = await _evidence(db, task_id)
    for block in (evidence.mutations, evidence.baseline):
        assert block.state == "received"
        assert block.result == "ran"
        assert "из попытки 1001/1 того же коммита" in block.run
        assert "из попытки 1001/1 того же коммита" in block.reason
    assert "red-test" in evidence.baseline.reason
    assert "red-test" not in evidence.mutations.reason

    lines = review_brief_projection._checks_lines(
        {
            "prepass": {},
            "validation": {},
            "ac_test_results": {"current": 0, "stale": 0, "negative": []},
            "locator_resolution": {"unresolved": []},
            "ci_evidence": evidence.model_dump(),
            "live_check": {},
            "statement_freshness": None,
        }
    )
    compact = "\n".join(lines)
    assert "CI mutations [received]" in compact
    assert "из попытки 1001/1 того же коммита" in compact

    # AC results and validation stay those of the current (PR) attempt.
    assert projection["validation_status"] == "pass"
    assert projection["attempt_key"] == "ci:1002:1"


# ---- AC-2 -------------------------------------------------------------------


async def _break_contract(db, task_id, run_id):
    await db.execute(
        "UPDATE ci_report_attempts SET contract_fp='other' WHERE run_id=?", (run_id,)
    )
    await db.commit()


async def _repost(db, task_id, run_id):
    """A second POST of the same attempt replaces it: a new claim, pending."""
    sent = GH.attempt(task_id, SHA, run_id=run_id, mutations=mutations())
    await store_ci_run_report(db, task_id, **sent)
    await db.commit()


# name -> (dispatch kwargs, expected borrowed labels)
_BLOCKS = {"mutations": mutations(), "baseline": baseline()}
CASES = {
    "artifact_not_confirmed": ({**_BLOCKS, "artifact": False}, set()),
    "run_failed": ({**_BLOCKS, "conclusion": "failure"}, set()),
    "head_is_another_commit": (
        {"mutations": mutations(head="c" * 40), "baseline": baseline(head="c" * 40)},
        set(),
    ),
    "block_without_head": (
        {"mutations": mutations(head=None), "baseline": baseline(head=None)},
        set(),
    ),
    "state_error": (
        {"mutations": mutations("error"), "baseline": baseline("error")},
        set(),
    ),
    "state_baseline_red": (
        {"mutations": mutations("baseline_red"), "baseline": baseline("error")},
        set(),
    ),
    "state_is_a_list": (
        {"mutations": mutations([]), "baseline": baseline({})},
        set(),
    ),
    "state_unknown": (
        {"mutations": mutations("weird"), "baseline": baseline("weird")},
        set(),
    ),
    "mutations_ran_baseline_error": (
        {"mutations": mutations(), "baseline": baseline("error")},
        {"mutations"},
    ),
    "no_changed_functions_is_informative": (
        {
            "mutations": mutations("no_changed_functions"),
            "baseline": baseline("no_tests"),
        },
        {"mutations", "baseline"},
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
async def test_an_attempt_failing_any_condition_is_never_a_source(db, case):
    dispatch, expected = CASES[case]
    task_id = await _task(db)
    await _dispatch_then_synchronize(db, task_id, **dispatch)
    await _check(db, task_id, expected)


async def _check(db, task_id: int, expected: set[str]) -> None:
    evidence = await _evidence(db, task_id)
    got = {
        label
        for label, block in (
            ("mutations", evidence.mutations),
            ("baseline", evidence.baseline),
        )
        if block.state == "received"
    }
    assert got == expected
    for label, block in (
        ("mutations", evidence.mutations),
        ("baseline", evidence.baseline),
    ):
        if label not in expected:
            assert block.state == "not_received"
            assert "из попытки" not in block.reason


async def test_a_source_that_lost_its_confirmation_is_not_a_source(db):
    task_id = await _task(db)
    first, _ = await _dispatch_then_synchronize(db, task_id)
    assert (await _evidence(db, task_id)).mutations.state == "received"

    await _break_contract(db, task_id, first)  # another contract than the current one
    await _check(db, task_id, set())

    task_id = await _task(db)
    first, _ = await _dispatch_then_synchronize(db, task_id, ("3001", "3002"))
    await _repost(db, task_id, first)  # pending again after a repeated POST
    await _check(db, task_id, set())


async def test_only_the_same_commit_and_the_same_task_are_a_source(db):
    # Another commit of the same task.
    task_id = await _task(db)
    await _attempt(
        db,
        task_id,
        OTHER_SHA,
        run_id="1001",
        mutations=mutations(head=OTHER_SHA),
        baseline=baseline(head=OTHER_SHA),
    )
    await _attempt(db, task_id, run_id="1002", event="pull_request")
    await _check(db, task_id, set())

    # The same commit reported for another task.
    mine = await _task(db)
    theirs = await _task(db)
    await _attempt(
        db, theirs, run_id="2001", mutations=mutations(), baseline=baseline()
    )
    await _attempt(db, mine, run_id="2002", event="pull_request")
    await _check(db, mine, set())


async def test_without_a_confirmed_successful_current_attempt_nothing_is_borrowed(db):
    # The current attempt is not confirmed: the answer is the one given before.
    task_id = await _task(db)
    await _attempt(
        db, task_id, run_id="1001", mutations=mutations(), baseline=baseline()
    )
    await _attempt(db, task_id, run_id="1002", event="pull_request", artifact=False)
    evidence = await _evidence(db, task_id)
    for block in (evidence.mutations, evidence.baseline):
        assert block.state == "not_received"
        assert "из попытки" not in block.reason
        assert "не подтверждён" in block.reason

    # The current attempt ended in failure.
    task_id = await _task(db)
    await _attempt(
        db, task_id, run_id="2001", mutations=mutations(), baseline=baseline()
    )
    await _attempt(
        db, task_id, run_id="2002", event="pull_request", conclusion="failure"
    )
    await _check(db, task_id, set())


async def test_the_newest_of_several_sources_wins_per_block(db):
    task_id = await _task(db)
    await _attempt(
        db, task_id, run_id="900", mutations=mutations(), baseline=baseline()
    )
    await _attempt(
        db, task_id, run_id="1001", mutations=mutations(), baseline=baseline("error")
    )
    await _attempt(db, task_id, run_id="1002", event="pull_request")
    evidence = await _evidence(db, task_id)
    assert "из попытки 1001/1" in evidence.mutations.reason
    assert "из попытки 900/1" in evidence.baseline.reason, "1001 has no usable baseline"


# ---- AC-3 -------------------------------------------------------------------


async def test_the_current_attempt_wins_when_it_carries_evidence(db):
    task_id = await _task(db)
    await _attempt(
        db,
        task_id,
        run_id="1001",
        mutations=mutations(reason="dispatch"),
        baseline=baseline(reason="dispatch"),
    )
    await _attempt(
        db,
        task_id,
        run_id="1002",
        attempt="2",
        event="workflow_dispatch",
        mutations=mutations(reason="rerun"),
        baseline=baseline(reason="rerun"),
    )
    evidence = await _evidence(db, task_id)
    for block in (evidence.mutations, evidence.baseline):
        assert block.state == "received"
        assert block.reason == "rerun"
        assert "из попытки" not in block.run
    assert "1002" in evidence.mutations.run

    # The AC results are those of the current attempt only.
    task_id = await _task(db)
    await _attempt(
        db,
        task_id,
        run_id="3001",
        ac_results={"AC-1": "fail"},
        mutations=mutations(),
        baseline=baseline(),
    )
    await _attempt(db, task_id, run_id="3002", event="pull_request")
    results = {
        r["ac_id"]: r["status"] for r in await repo.list_ac_test_results(db, task_id)
    }
    assert results == {"AC-1": "pass"}


# ---- AC-4 -------------------------------------------------------------------


async def test_the_bug_red_test_gate_does_not_borrow(client, db):
    task_id = await _running_bug(db, "red-borrow", "require")
    await _report_baseline(
        client, task_id, _baseline({_REF_1: "failed", _REF_2: "failed"}, head=_TIP)
    )
    await _report_baseline(client, task_id, None)  # the synchronize run: no baseline

    resp, dispatch = await _submit(client, task_id)
    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["reason"] == "bug_red_test_unproven"
    assert all("нет baseline" in v for v in detail["violations"])
    dispatch.assert_not_awaited()

    # The brief may show the dispatch run's baseline, and says the gate ignores it.
    await repo.update_task(db, task_id, submission_sha=_TIP)
    await db.commit()
    evidence = await review_evidence.ci_evidence_state(
        db, dict(await repo.get_task(db, task_id))
    )
    assert evidence.baseline.state == "received"
    assert "гейт red-test багов его не учитывает" in evidence.baseline.reason


async def test_a_source_made_pending_while_the_current_report_stays_trusted(db):
    task_id = await _task(db)
    first, _ = await _dispatch_then_synchronize(db, task_id)
    await db.execute(
        "UPDATE ci_report_attempts SET trust='unverified', trust_reason='pending' "
        "WHERE run_id=?",
        (first,),
    )
    await db.commit()
    assert await ci_trust.trusted_report(db, task_id, SHA) is not None
    await _check(db, task_id, set())


async def test_a_change_between_the_reads_borrows_nothing(db, monkeypatch):
    """The projection moves between the snapshot and its recheck: nothing is borrowed."""
    task_id = await _task(db)
    await _dispatch_then_synchronize(db, task_id)

    async def action():
        await db.execute("UPDATE ci_run_reports SET attempt_key='ci:1003:1'")
        await db.commit()

    _after_read(monkeypatch, "ci_run_reports", 1, action)
    await _check(db, task_id, set())


# ---- one snapshot (round 2 of #1936) -----------------------------------------


def _after_read(monkeypatch, table: str, nth: int, action):
    """Run ``action`` once, right after the ``nth`` read of ``table`` returns."""
    from hub.services import ci_trust as trust_module

    seen = {"reads": 0, "fired": False}

    def wrap(real):
        async def read(db, sql, parameters=()):
            rows = await real(db, sql, parameters)
            if table in sql and not seen["fired"]:
                seen["reads"] += 1
                if seen["reads"] == nth:
                    seen["fired"] = True
                    await action()
            return rows

        return read

    monkeypatch.setattr(repo, "fetchall", wrap(repo.fetchall))
    monkeypatch.setattr(trust_module, "fetchall", wrap(trust_module.fetchall))
    return seen


async def _replace_the_source(db, task_id: int) -> None:
    """A repeated POST of the dispatch run: a new claim whose artifact differs."""
    started = GH.runs[("1001", "1")]["run_started_at"]  # same run, same start
    sent = GH.attempt(
        task_id, SHA, run_id="1001", started=started, mutations=mutations()
    )
    await store_ci_run_report(db, task_id, **sent)
    await db.commit()
    await ci_trust.refresh(db, task_id, SHA)


async def test_a_source_replaced_after_it_was_read_is_not_borrowed(db, monkeypatch):
    task_id = await _task(db)
    await _dispatch_then_synchronize(db, task_id)

    async def action():
        await _replace_the_source(db, task_id)

    seen = _after_read(monkeypatch, "ci_run_reports", 1, action)
    await _check(db, task_id, set())
    assert seen["fired"]
    source = [
        dict(r)
        for r in await repo.list_ci_report_attempts(db, task_id, SHA)
        if r["run_id"] == "1001"
    ][0]
    assert source["trust"] != "verified", "the interleaving really unconfirmed it"
    assert (await ci_trust.trusted_report(db, task_id, SHA)) is not None


async def test_the_projection_invalidated_right_after_its_read_borrows_nothing(
    db, monkeypatch
):
    task_id = await _task(db)
    await _dispatch_then_synchronize(db, task_id)

    async def action():
        sent = GH.attempt(task_id, SHA, run_id="1002", event="pull_request")
        await store_ci_run_report(db, task_id, **sent)  # a POST: pending again
        await db.commit()

    seen = _after_read(monkeypatch, "ci_run_reports", 1, action)
    evidence = await _evidence(db, task_id)
    assert seen["fired"]
    assert await ci_trust.trusted_report(db, task_id, SHA) is None
    for block in (evidence.mutations, evidence.baseline):
        assert "из попытки" not in block.reason


async def test_both_blocks_are_decided_from_one_snapshot(db, monkeypatch):
    task_id = await _task(db)
    await _dispatch_then_synchronize(db, task_id)

    async def action():
        await _replace_the_source(db, task_id)

    _after_read(monkeypatch, "ci_run_reports", 1, action)
    evidence = await _evidence(db, task_id)
    # Whatever moved, the two blocks agree: never one borrowed and one not.
    assert evidence.mutations.state == evidence.baseline.state == "not_received"


async def test_one_snapshot_serves_both_blocks(db, monkeypatch):
    from hub.services import ci_trust as trust_module

    task_id = await _task(db)
    await _dispatch_then_synchronize(db, task_id)
    reads = {"n": 0}

    def counted(real):
        async def read(db_, sql, parameters=()):
            if "ci_report_attempts" in sql and "ci_run_reports" in sql:
                reads["n"] += 1  # one statement reads attempts AND projection
            return await real(db_, sql, parameters)

        return read

    monkeypatch.setattr(repo, "fetchall", counted(repo.fetchall))
    monkeypatch.setattr(trust_module, "fetchall", counted(trust_module.fetchall))
    evidence = await _evidence(db, task_id)
    assert "из попытки 1001/1" in evidence.mutations.reason
    assert "из попытки 1001/1" in evidence.baseline.reason
    assert reads["n"] == 1, "ONE snapshot serves BOTH blocks, not one per block"


async def test_the_snapshot_statement_is_the_consistency_boundary(db, monkeypatch):
    """A write after the snapshot is a later moment: the answer is whole, not a mix."""

    async def action():
        await _replace_the_source(db, task_id)

    task_id = await _task(db)
    await _dispatch_then_synchronize(db, task_id)
    _after_read(monkeypatch, "ci_report_attempts", 1, action)
    evidence = await _evidence(db, task_id)
    assert evidence.mutations.state == evidence.baseline.state == "received"
    assert "из попытки 1001/1" in evidence.mutations.reason
    assert "из попытки 1001/1" in evidence.baseline.reason
