"""Identity and place of a machine-review finding (#1007).

Before this, a confirmed finding had neither. It was addressed by its position
in ``findings_confirmed`` — a property of the list, not of the finding — and its
location was an optional ``file`` plus an optional single ``line``, so "nowhere
to point" and "nobody filled this in" were the same empty value.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from hub.models import FindingLocator, MachineReviewView
from hub.services.finding_identity import finding_uid, finding_uids


async def _reviewable_task(client: AsyncClient, db) -> int:
    from hub import repository as repo_module
    from hub import services as services_module
    from hub.models import TaskCreate

    tv = await services_module.create_task(db, TaskCreate(title="finding identity"))
    await repo_module.add_task_update(db, tv.id, "dev", "status", "Plan: mr")
    await db.commit()
    await services_module.pair_start_task(db, tv.id, caller="dev")
    await services_module.submit_for_review(db, tv.id)
    return tv.id


def _report(findings: list[dict]) -> dict:
    return {
        "raw_count": max(len(findings), 1),
        "incomplete": False,
        "harness_skill": "lite-diff-review",
        "findings_confirmed": findings,
        "agent": "reviewer",
    }


# --- AC-1: the locator decision is required on the write path --------------


async def test_finding_requires_locator_decision(client: AsyncClient, db):
    task_id = await _reviewable_task(client, db)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=_report([{"title": "boundary lost", "severity": "medium"}]),
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # The refusal has to name the three answers, or the caller learns only that
    # it is wrong — the same gap #596 found behind "no valid locator".
    assert "'lines'" in detail and "'file'" in detail and "'none'" in detail
    assert "#0" in detail


@pytest.mark.parametrize(
    "finding",
    [
        {"title": "no place", "severity": "low", "locator": "none"},
        {"title": "module", "severity": "low", "locator": "file", "file": "hub/a.py"},
        {
            "title": "lines",
            "severity": "high",
            "locator": "lines",
            "file": "hub/a.py",
            "start_line": 10,
            "end_line": 14,
        },
    ],
)
async def test_every_stated_locator_is_accepted(client: AsyncClient, db, finding):
    # 'none' is an ANSWER: a harness that cannot place a finding still files a
    # usable report. Refusing it would push callers to invent a file.
    task_id = await _reviewable_task(client, db)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review", json=_report([finding])
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "finding, complaint",
    [
        ({"title": "t", "severity": "low", "locator": "lines"}, "needs a file"),
        (
            {"title": "t", "severity": "low", "locator": "lines", "file": "a.py"},
            "needs start_line",
        ),
        (
            {
                "title": "t",
                "severity": "low",
                "locator": "lines",
                "file": "a.py",
                "start_line": 9,
                "end_line": 4,
            },
            "before start_line",
        ),
        (
            {
                "title": "t",
                "severity": "low",
                "locator": "file",
                "file": "a.py",
                "line": 12,
            },
            "carries no lines",
        ),
        ({"title": "t", "severity": "low", "locator": "none", "file": "a.py"}, "none"),
        (
            {"title": "t", "severity": "low", "locator": "none", "end_line": 14},
            "none",
        ),
        (
            {
                "title": "t",
                "severity": "low",
                "locator": "lines",
                "file": "a.py",
                "start_line": 10,
                "line": 40,
            },
            "disagree",
        ),
    ],
)
async def test_a_stated_locator_must_agree_with_itself(
    client: AsyncClient, db, finding, complaint
):
    task_id = await _reviewable_task(client, db)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review", json=_report([finding])
    )
    assert resp.status_code == 422
    assert complaint in resp.text


def test_legacy_reports_still_load_without_a_locator():
    # The read model stays permissive on purpose: 116 reports are already in the
    # ground without this field, and a required field would make them unreadable
    # — the split #505 drew between the write path and the stored row. Built
    # from a stored JSON row, not from keyword arguments, because that is the
    # shape the card and the brief actually read.
    stored = json.dumps(
        [{"title": "old", "severity": "low", "file": "hub/a.py", "line": 3}]
    )
    view = MachineReviewView(
        id=1,
        task_id=1,
        submission_generation=1,
        raw_count=1,
        findings_confirmed=stored,
    )
    assert view.findings_confirmed[0].locator is None
    # And it still gets an id, so a person judging an old report addresses it
    # the same way as a new one.
    assert view.findings_confirmed[0].finding_uid


async def test_the_report_comes_back_with_an_id_per_finding(client: AsyncClient, db):
    # AC: the derived id is useless if no reader ever sees it. The submit
    # response is itself a MachineReviewView, so the id is there from the first
    # moment the report exists.
    task_id = await _reviewable_task(client, db)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=_report(
            [
                {
                    "title": "a",
                    "severity": "low",
                    "locator": "lines",
                    "file": "hub/a.py",
                    "start_line": 10,
                },
                {"title": "b", "severity": "low", "locator": "none"},
            ]
        ),
    )
    assert resp.status_code == 200, resp.text
    uids = [f["finding_uid"] for f in resp.json()["findings_confirmed"]]
    assert all(uids) and len(set(uids)) == 2


async def test_a_supplied_id_is_refused(client: AsyncClient, db):
    # A harness has no memory of the previous report, so an id it invents is
    # random — and it would quietly beat the derived one.
    task_id = await _reviewable_task(client, db)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=_report(
            [
                {
                    "title": "a",
                    "severity": "low",
                    "locator": "none",
                    "finding_uid": "deadbeefdeadbeef",
                }
            ]
        ),
    )
    assert resp.status_code == 422
    assert "derives" in resp.text


def test_two_findings_in_one_file_are_two_ids():
    # The place is part of the identity: without it these two collapse into one
    # id, and a judgement lands on whichever survives the next report.
    first = {
        "title": "unchecked error",
        "category": "correctness",
        "file": "hub/app.py",
        "locator": "lines",
        "start_line": 40,
    }
    second = dict(first, start_line=200)
    assert finding_uid(first) != finding_uid(second)


# --- AC-2: identity is derived and repeats across generations --------------


def test_finding_uid_stable_across_generations():
    first = {
        "title": "Race on retry",
        "severity": "high",
        "category": "correctness",
        "file": "hub/poller.py",
    }
    # A second run words severity differently and reports the same defect; the
    # id is derived from what identifies the finding, not from the whole row.
    second = {
        "title": "race on  retry",
        "severity": "medium",
        "category": "Correctness",
        "file": "hub/poller.py",
    }
    assert finding_uid(first) == finding_uid(second)


def test_a_reworded_finding_is_a_different_id():
    # Stated as a test because it is a LIMIT, not an accident: the hub cannot
    # know that two sentences describe one defect, and an id that pretended
    # otherwise would put a confident number on a guess.
    a = {"title": "race on retry", "category": "correctness", "file": "hub/p.py"}
    b = {
        "title": "retry races with the poller",
        "category": "correctness",
        "file": "hub/p.py",
    }
    assert finding_uid(a) != finding_uid(b)


def test_twins_in_one_report_get_different_ids():
    twin = {"title": "same", "category": "tests", "file": "hub/a.py"}
    uids = finding_uids([twin, dict(twin)])
    assert uids[0] != uids[1]
    # The first occurrence keeps the plain content id, so adding a twin later
    # does not renumber the finding that was already judged.
    assert uids[0] == finding_uid(twin)


def test_uid_survives_reordering():
    a = {"title": "a", "category": "tests", "file": "hub/a.py"}
    b = {"title": "b", "category": "tests", "file": "hub/b.py"}
    assert finding_uids([a, b])[0] == finding_uids([b, a])[1]


def test_locator_enum_names_all_three_answers():
    assert {m.value for m in FindingLocator} == {"lines", "file", "none"}
