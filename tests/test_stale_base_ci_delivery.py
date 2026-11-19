"""Recovery for a completed delivery blocked by stale-base CI evidence (#2393)."""

import pytest

from hub import repository as repo
from hub.integrations import protocols
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.models import TaskDecide
from hub.services import lifecycle
from tests.accepted_delivery_retry_support import APPROVED, accepted_task

PR_BASE = "1" * 40
LIVE_BASE = "2" * 40


def moved_base_probe():
    details = "base branch changed or is unreadable"
    # Keep the reproducer runnable on the old gate, before typed evidence exists.
    change_type = getattr(protocols, "CIBaseChange", None)
    if change_type is None:
        return CIProbeResult(CIProbeOutcome.unavailable, "ci_equivalence_unproved", details)
    return CIProbeResult(
        CIProbeOutcome.unavailable,
        "ci_equivalence_unproved",
        details,
        base_change=change_type(
            pr_number=77,
            head_sha=APPROVED,
            base_ref="develop",
            pinned_base_sha=PR_BASE,
            current_base_sha=LIVE_BASE,
        ),
    )


async def _deliver(rig, entry):
    if entry == "decide":
        await repo.update_task(rig.db, rig.task_id, status="needs_decision")
        await rig.db.commit()
        await lifecycle.decide_task(
            rig.db, rig.task_id, TaskDecide(action="accept", pr_disposition="deliver")
        )
    else:
        await repo.record_delivery_discrepancy(
            rig.db,
            task_id=rig.task_id,
            state="pr_open",
            reason="not delivered",
            pr_number=77,
            delivery_path="",
            disposition="deliver",
        )
        await rig.db.commit()
        with pytest.raises(lifecycle.RegistryDeliveryRefused):
            await lifecycle.deliver_from_registry(rig.db, rig.task_id, by="owner")


@pytest.mark.parametrize("entry", ["decide", "registry"])
async def test_proven_behind_arms_existing_delivery_retry(db, monkeypatch, entry):
    rig = await accepted_task(db, monkeypatch)
    rig.ci(moved_base_probe())
    await _deliver(rig, entry)

    assert "base_updated" in await rig.feed()
    rig.git.update_pr_branch.assert_awaited_once()
    assert rig.git.update_pr_branch.await_args.args == (77, APPROVED)
    assert (await rig.retry_row())["state"] == "armed"
    assert (await rig.task())["status"] == "completed"
    rig.git.merge_pr.assert_not_awaited()
    assert rig.posts == 0
