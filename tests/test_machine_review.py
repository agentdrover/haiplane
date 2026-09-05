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

from hub.models import FindingLocator, MachineFinding, MachineReviewView
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


async def test_a_supplied_id_is_refused_in_the_unresolved_section(
    client: AsyncClient, db
):
    """Тот же сторож — на втором разделе (#1085).

    До того как у неразрешённой записи появилось поле finding_uid, лишний ключ
    отсекала сама схема (extra="forbid"). Теперь поле объявлено, и отказ держит
    только явная проверка: без неё харнесс сдал бы свой случайный id с кодом
    200. Промаха исходов при этом не случилось бы — на чтении id всё равно
    перевычисляется, — исчез бы сам отказ, то есть единственное место, где
    автору говорят, что идентичность выводит хаб.
    """
    task_id = await _reviewable_task(client, db)
    body = _report([{"title": "a", "severity": "low", "locator": "none"}])
    body["unresolved"] = [
        {
            "title": "никто не рассудил",
            "why": "голоса разошлись",
            "finding_uid": "deadbeefdeadbeef",
        }
    ]
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=body)
    assert resp.status_code == 422
    assert "unresolved" in resp.text, "отказ называет РАЗДЕЛ, в котором искать"
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
    # Computed on the payload the WRITE path requires (#1028): locator is
    # mandatory in production, so a stability test without it proved nothing
    # about the identity the hub actually stores.
    first = {
        "title": "Race on retry",
        "severity": "high",
        "category": "correctness",
        "file": "hub/poller.py",
        "locator": "lines",
        "start_line": 42,
    }
    # A second run words severity differently and reports the same defect; the
    # id is derived from what identifies the finding, not from the whole row.
    second = {
        "title": "race on  retry",
        "severity": "medium",
        "category": "Correctness",
        "file": "hub/poller.py",
        "locator": "lines",
        "start_line": 42,
    }
    assert finding_uid(first) == finding_uid(second)


def test_legacy_and_new_locator_give_one_id():
    """AC-1 (#1028): the format boundary must not break identity.

    116 reports are stored with no locator at all. If the field went into the
    hash verbatim, none of them could ever match a finding reported after the
    contract changed — and the id exists precisely to carry a human's judgement
    across that boundary.
    """
    legacy = {
        "title": "unchecked error",
        "category": "correctness",
        "file": "hub/app.py",
        "line": 40,
    }
    modern = {
        "title": "unchecked error",
        "category": "correctness",
        "file": "hub/app.py",
        "locator": "lines",
        "start_line": 40,
    }
    assert finding_uid(legacy) == finding_uid(modern)

    # And the same holds one step up: a file-only finding, however it says so.
    legacy_file = {"title": "t", "category": "c", "file": "hub/app.py"}
    modern_file = dict(legacy_file, locator="file")
    assert finding_uid(legacy_file) == finding_uid(modern_file)


def test_a_placed_finding_differs_from_an_unplaced_one():
    """AC-2 (#1028): canonicalising the place must not re-merge what #1007 split.

    The line is what tells two defects in one file apart. Dropping the raw
    locator from the hash is only safe while the line still counts.
    """
    placed = {
        "title": "t",
        "category": "c",
        "file": "hub/app.py",
        "locator": "lines",
        "start_line": 40,
    }
    unplaced = {"title": "t", "category": "c", "file": "hub/app.py", "locator": "file"}
    nowhere = {"title": "t", "category": "c", "locator": "none"}
    assert len({finding_uid(placed), finding_uid(unplaced), finding_uid(nowhere)}) == 3
    assert finding_uid(placed) != finding_uid(dict(placed, start_line=200))


def test_a_range_is_the_same_place_as_its_first_line():
    """The extent of a finding is not part of its place (#1028).

    Stated as a test because it is a DECISION, not an oversight. A reviewer
    that says lines 40-52 and one that says line 40 are pointing at one defect
    with different precision — the same relationship ``file`` and ``lines``
    have, and the same reason the raw locator stayed out of the hash. Hashing
    the end would put the id back at the mercy of how widely a harness happened
    to draw the range on its second run, and the disposition filed against the
    first would stop matching.
    """
    start_only = {
        "title": "t",
        "category": "c",
        "file": "hub/app.py",
        "locator": "lines",
        "start_line": 40,
    }
    ranged = dict(start_only, end_line=52)
    wider = dict(start_only, end_line=90)
    assert finding_uid(start_only) == finding_uid(ranged) == finding_uid(wider)

    # A range that lost its start still names a line, so it must not fall back
    # to file level — that would silently move it to a different place.
    end_only = {
        "title": "t",
        "category": "c",
        "file": "hub/app.py",
        "end_line": 40,
    }
    file_only = {"title": "t", "category": "c", "file": "hub/app.py"}
    assert finding_uid(end_only) == finding_uid(start_only)
    assert finding_uid(end_only) != finding_uid(file_only)


def test_identity_reads_a_model_and_a_dict_alike():
    """Both shapes reach this module, so both must expose the same keys.

    Storage holds JSON and the API layer holds parsed models. A key the model
    view forgets to copy is invisible only on one of the two paths — which is
    the hardest kind of divergence to see, because every test that builds a
    dict stays green.
    """
    # A legacy-shaped finding (no locator — the shape all 116 stored reports
    # have) whose only line information is an end. It is the one payload where
    # the model view forgetting ``end_line`` changes the answer: the dict path
    # places it on line 40, the model path would demote it to file level.
    payload = {
        "title": "t",
        "severity": "high",
        "category": "c",
        "file": "hub/app.py",
        "end_line": 40,
    }
    assert finding_uid(MachineFinding(**payload)) == finding_uid(payload)
    assert finding_uid(MachineFinding(**payload)) != finding_uid(
        {"title": "t", "category": "c", "file": "hub/app.py"}
    )


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
