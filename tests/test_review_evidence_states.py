"""The exact state of a missing check result, manual AC and profile downgrade (#2026).

The brief used to say "не получено ... это не результат" and "словом автора
основание не служит" for every absence alike; an advisor read that as a hole in
the evidence even where the step was never meant to run. Each absence now names
one of four states with the event kind; gates keep reading the current attempt.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hub import repository as repo
from hub.models import AcceptanceCriterion, ReviewBrief
from hub.services import (
    ci_trust,
    evidence_states,
    review_brief_projection,
    review_evidence,
    steward_evidence,
)
from hub.services.ci_report import store_ci_run_report
from tests.ci_trust_support import FakeGitHub, install_fake_github
from tests.test_ci_evidence_other_attempt import _task, baseline, mutations

SHA = "a" * 40
GH: FakeGitHub = FakeGitHub()


@pytest.fixture(autouse=True)
def _github(monkeypatch):
    global GH
    GH = install_fake_github(monkeypatch)


async def _attempt(db, task_id: int, **kwargs: Any) -> None:
    sent = GH.attempt(task_id, SHA, **kwargs)
    await store_ci_run_report(db, task_id, **sent)
    await db.commit()
    await ci_trust.refresh(db, task_id, SHA)


async def _evidence(db, task_id: int):
    return await review_evidence.ci_evidence_state(
        db, dict(await repo.get_task(db, task_id))
    )


async def test_missing_result_states_are_exact(db):
    # 1. workflow_dispatch that counted: received, event and run named.
    counted = await _task(db)
    await _attempt(
        db, counted, run_id="2001", mutations=mutations(), baseline=baseline()
    )
    block = (await _evidence(db, counted)).mutations
    assert (block.state, block.expectation) == ("received", "received")
    assert "workflow_dispatch" in block.run and "2001" in block.run

    # 2. workflow_dispatch with no result: the step was expected -> missing.
    lost = await _task(db)
    await _attempt(db, lost, run_id="2002")
    block = (await _evidence(db, lost)).mutations
    assert (block.state, block.expectation) == ("not_received", "missing_unexpectedly")
    assert "вопреки ожиданию" in block.reason
    assert "workflow_dispatch" in block.reason and "2002" in block.reason
    assert block.reason.startswith("не получено")

    # 3. a pull_request run after a counting dispatch: another verified attempt.
    borrowed = await _task(db)
    await _attempt(
        db, borrowed, run_id="2003", mutations=mutations(), baseline=baseline()
    )
    await _attempt(db, borrowed, run_id="2004", event="pull_request")
    block = (await _evidence(db, borrowed)).baseline
    assert (block.state, block.expectation) == ("received", "other_attempt")
    assert "из попытки 2003/1 того же коммита" in block.reason

    # 4. a pull_request run alone: GitHub's event has no action, so whether the
    # step was expected (opened) or not (synchronize) is not confirmed.
    alone = await _task(db)
    await _attempt(db, alone, run_id="2005", event="pull_request")
    block = (await _evidence(db, alone)).mutations
    assert (block.state, block.expectation) == ("not_received", "origin_unconfirmed")
    assert "pull_request" in block.reason and "не подтверждено" in block.reason

    # 5. no report at all for the commit, and a report GitHub did not confirm.
    silent = await _task(db)
    block = (await _evidence(db, silent)).mutations
    assert block.expectation == "origin_unconfirmed"
    assert "отчёта CI" in block.reason

    # The gates are untouched: the projection is still the current attempt.
    projection = dict(await repo.get_ci_run_report(db, borrowed, SHA))
    assert projection["attempt_key"] == "ci:2004:1"
    assert json.loads(projection["mutations"]) == {}


def test_event_kinds_decide_the_expectation():
    for event, want in {
        "workflow_dispatch": "missing_unexpectedly",
        "pull_request.opened": "missing_unexpectedly",
        "pull_request.reopened": "missing_unexpectedly",
        "pull_request.synchronize": "not_expected",
        "push": "not_expected",
        "pull_request": "origin_unconfirmed",
        "": "origin_unconfirmed",
        "schedule": "origin_unconfirmed",
    }.items():
        block = evidence_states.missing_block("мутации", SHA, event, "77")
        assert block.expectation == want, event
        assert block.state == "not_received"
        assert block.reason.startswith("не получено")
        assert "выполнено" not in block.reason.replace("не отметка «выполнено»", "")


def _acs():
    return [
        AcceptanceCriterion(
            id="AC-1", given="g", when="w", then="t", verifiable_by="manual"
        ),
        AcceptanceCriterion(
            id="AC-2",
            given="g",
            when="w",
            then="t",
            verifiable_by="test",
            test_ref="tests/test_x.py::test_a",
        ),
        AcceptanceCriterion(
            id="AC-3", given="g", when="w", then="t", verifiable_by="log_check"
        ),
    ]


def test_manual_ac_and_downgrade_are_explained():
    notes = evidence_states.manual_ac_notes(_acs())
    assert [n["ac_id"] for n in notes] == ["AC-1", "AC-3"]
    for note in notes:
        assert "тестовый локатор неприменим" in note["note"]
        assert "не отметка о выполнении" in note["note"]

    brief = ReviewBrief.model_construct(
        locator_resolution=[], acceptance_criteria=_acs()
    )
    fact = steward_evidence._locator_fact(brief)
    assert "тестовый локатор неприменим" in fact.detail
    assert "AC-1" in fact.detail and "AC-2" not in fact.detail.split("неприменим")[1]

    part = review_brief_projection._locators_part(
        {
            "locator_resolution": [],
            "acceptance_criteria": [a.model_dump(mode="json") for a in _acs()],
        }
    )
    assert [n["ac_id"] for n in part["not_applicable"]] == ["AC-1", "AC-3"]
    assert part["unresolved"] == []
    assert "not_applicable" not in review_brief_projection._locators_part(
        {"locator_resolution": [], "acceptance_criteria": []}
    )

    lines = review_brief_projection._checks_lines(
        {
            "prepass": {},
            "validation": {},
            "ac_test_results": {"current": 0, "stale": 0, "negative": []},
            "locator_resolution": part,
            "ci_evidence": {},
            "live_check": {},
            "statement_freshness": None,
        }
    )
    shown = "\n".join(lines)
    assert (
        "AC locators not applicable: AC-1=manual (тестовый локатор неприменим" in shown
    )
    assert "AC-3=log_check" in shown

    cap = ["deep по правилу (риск), но суточный потолок 2 исчерпан — lite"]
    circle = ["круг: 3 захода, deep приостановлен до решения человека"]
    assert evidence_states.downgrade_rules(cap) == ["#1414"]
    assert evidence_states.downgrade_rules(circle) == ["#1432"]
    assert evidence_states.downgrade_rules(cap + circle) == ["#1414", "#1432"]
    assert evidence_states.downgrade_rules(["правило r1"]) == []


async def test_downgrade_headline_cites_the_rule(db):
    from hub.services.review_brief import build_review_brief
    from tests.test_review_brief import _brief_task_with_orders

    cap = ["deep по правилу (риск), но суточный потолок 2 исчерпан — lite"]
    task_id = await _brief_task_with_orders(db, [("cloud", "lite", cap)])
    block = (await build_review_brief(db, task_id)).profile_downgrade
    assert block is not None
    assert block.rules == ["#1414"]
    assert "правило #1414" in block.headline
    assert "выполнен" not in block.headline
