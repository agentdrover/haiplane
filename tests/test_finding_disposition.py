"""Dispositions addressed by the finding, not by its slot (#1007, #876).

``finding_index`` names a position in ``findings_confirmed``. A resubmitted
report reorders that list, so a judgement filed against slot 2 quietly starts
describing whatever landed in slot 2 the second time. Rows already stored that
way must keep reading — they were filed under the only scheme that existed.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from hub.models import FindingDisposition, FindingDispositionItem
from hub.services.finding_disposition import record_finding_dispositions
from hub.services.finding_identity import finding_uids

_FINDINGS = [
    {
        "title": "boundary lost",
        "severity": "medium",
        "category": "correctness",
        "file": "hub/a.py",
        "locator": "file",
    },
    {
        "title": "race on retry",
        "severity": "high",
        "category": "correctness",
        "file": "hub/b.py",
        "locator": "file",
    },
]


async def _task_with_report(client: AsyncClient, db) -> tuple[int, int]:
    from hub import repository as repo_module
    from hub import services as services_module
    from hub.models import TaskCreate

    tv = await services_module.create_task(db, TaskCreate(title="dispositions"))
    await repo_module.add_task_update(db, tv.id, "dev", "status", "Plan: mr")
    await db.commit()
    await services_module.pair_start_task(db, tv.id, caller="dev")
    await services_module.submit_for_review(db, tv.id)
    resp = await client.post(
        f"/api/tasks/{tv.id}/machine-review",
        json={
            "raw_count": 2,
            "incomplete": False,
            "harness_skill": "lite-diff-review",
            "findings_confirmed": _FINDINGS,
            "agent": "reviewer",
        },
    )
    assert resp.status_code == 200, resp.text
    row = await repo_module.get_latest_machine_review(db, tv.id)
    return tv.id, int(dict(row)["id"])


async def _stored(db, review_id: int) -> list[dict]:
    from hub import repository as repo_module

    return [dict(r) for r in await repo_module.list_finding_dispositions(db, review_id)]


async def test_disposition_is_addressed_by_uid(client: AsyncClient, db):
    task_id, review_id = await _task_with_report(client, db)
    uid = finding_uids(_FINDINGS)[1]
    result = await record_finding_dispositions(
        db,
        task_id,
        [FindingDispositionItem(finding_uid=uid, disposition=FindingDisposition.fixed)],
        decided_by="denis",
    )
    assert result["judged"] == 1
    rows = await _stored(db, review_id)
    # Both keys are stored: the uid is what was judged, the index is what the
    # existing unique constraint and the older readers still work with.
    assert rows[0]["finding_uid"] == uid
    assert rows[0]["finding_index"] == 1
    assert rows[0]["finding_title"] == "race on retry"


async def test_uid_from_another_report_is_refused(client: AsyncClient, db):
    task_id, _ = await _task_with_report(client, db)
    with pytest.raises(ValueError) as err:
        await record_finding_dispositions(
            db,
            task_id,
            [
                FindingDispositionItem(
                    finding_uid="0" * 16, disposition=FindingDisposition.wont_fix
                )
            ],
            decided_by="denis",
        )
    # Naming the likely cause matters: the usual way to hold a stale uid is to
    # have read the report before the author resubmitted it.
    assert "resubmitted" in str(err.value)


async def test_legacy_index_dispositions_still_read(client: AsyncClient, db):
    # A row exactly as #876 wrote them: a slot, a title and no uid at all.
    task_id, review_id = await _task_with_report(client, db)
    from hub import repository as repo_module

    await repo_module.upsert_finding_disposition(
        db,
        review_id=review_id,
        task_id=task_id,
        submission_generation=1,
        finding_index=0,
        finding_title="boundary lost",
        disposition="false_positive",
        note="checked by hand",
        decided_by="denis",
    )
    await db.commit()

    rows = await _stored(db, review_id)
    assert rows[0]["finding_uid"] == ""
    assert rows[0]["disposition"] == "false_positive"

    # And the whole read chain the card and the brief use still builds a view
    # out of it — an empty uid is not a parse failure.
    from hub.services.review_evidence import attach_dispositions

    class _View:
        id = review_id
        dispositions: list = []

    view = _View()
    await attach_dispositions(db, view)
    assert view.dispositions[0].finding_index == 0
    assert view.dispositions[0].finding_uid == ""
    assert view.dispositions[0].disposition is FindingDisposition.false_positive


async def test_index_addressing_still_works_for_old_callers(client: AsyncClient, db):
    task_id, review_id = await _task_with_report(client, db)
    await record_finding_dispositions(
        db,
        task_id,
        [
            FindingDispositionItem(
                finding_index=0, disposition=FindingDisposition.wont_fix
            )
        ],
        decided_by="denis",
    )
    rows = await _stored(db, review_id)
    # Resolved forward: a caller addressing a slot still gets the uid recorded,
    # so the row does not stay unidentifiable just because of how it arrived.
    assert rows[0]["finding_uid"] == finding_uids(_FINDINGS)[0]


def test_an_address_is_required():
    with pytest.raises(ValueError) as err:
        FindingDispositionItem(disposition=FindingDisposition.fixed)
    assert "finding_uid is required" in str(err.value)


def test_two_addresses_are_refused():
    # They can disagree, and the hub would have to pick a winner nobody asked
    # it to pick.
    with pytest.raises(ValueError) as err:
        FindingDispositionItem(
            finding_uid="a" * 16,
            finding_index=0,
            disposition=FindingDisposition.fixed,
        )
    assert "not both" in str(err.value)


async def test_out_of_range_index_still_refused(client: AsyncClient, db):
    task_id, _ = await _task_with_report(client, db)
    with pytest.raises(ValueError) as err:
        await record_finding_dispositions(
            db,
            task_id,
            [
                FindingDispositionItem(
                    finding_index=7, disposition=FindingDisposition.fixed
                )
            ],
            decided_by="denis",
        )
    assert "outside the 2 confirmed" in str(err.value)


async def test_uid_survives_a_reordered_resubmission(client: AsyncClient, db):
    """The defect this whole change exists to prevent.

    The same two findings come back in the opposite order. A judgement filed by
    slot would now point at the other finding; filed by uid it still points at
    the one that was judged.
    """
    task_id, review_id = await _task_with_report(client, db)
    uid = finding_uids(_FINDINGS)[0]

    # The report comes back with the same two findings in the opposite order.
    await db.execute(
        "UPDATE machine_reviews SET findings_confirmed=? WHERE id=?",
        (json.dumps(list(reversed(_FINDINGS))), review_id),
    )
    await db.commit()

    await record_finding_dispositions(
        db,
        task_id,
        [FindingDispositionItem(finding_uid=uid, disposition=FindingDisposition.fixed)],
        decided_by="denis",
    )
    rows = await _stored(db, review_id)
    # Slot 1 now, slot 0 before — the same finding either way.
    assert rows[0]["finding_index"] == 1
    assert rows[0]["finding_title"] == "boundary lost"
