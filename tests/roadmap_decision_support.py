"""Authenticated API fixture steps for roadmap decisions."""

from hub import repository as repo
from tests import role_orders_support as support


async def _put_plan_check(s, target):
    response = await s.world.client.put(
        f"/api/tasks/{s.task_id}/acceptance_criteria",
        headers=s.world.human_auth,
        json=[
            {
                "id": "AC-1",
                "given": "A real redo after a pairing code failed.",
                "when": "The operator checks successful fresh slot redeems.",
                "then": "Provide the required proof before pilot expansion.",
                "verifiable_by": "log_check",
                "test_ref": f"roadmap:successful_fresh_redeems>={target}",
            }
        ],
    )
    assert response.status_code == 200, response.text


async def _approve_plan_check(s):
    await repo.update_task(s.world.db, s.task_id, status="draft")
    await s.world.db.commit()
    await _put_plan_check(s, 2)
    response = await s.world.client.post(
        f"/api/tasks/{s.task_id}/approve",
        headers=s.world.human_auth,
        json={"run": False, "force": True},
    )
    assert response.status_code == 200, response.text
    return response.json()["statement_generation"]


async def _start_role(s):
    lease = await support.leased(s.world)
    response = await support.started(s.world, "k8", lease["id"], lease["lease_id"])
    assert response.status_code == 200, response.text
    return lease


def _conclusion(source_order_id, recommendation="hold_expansion"):
    return {
        "recommendation": recommendation,
        "basis": {"source_order_id": source_order_id, "check_id": "AC-1"}
        if source_order_id
        else None,
        "risk": "repeat_redeem_failure",
    }


async def _structured_report(s, lease, result):
    return await s.world.client.post(
        f"/api/executor-hosts/k8/role-orders/{lease['id']}/report",
        headers=s.world.k8_auth,
        json={
            "lease_id": lease["lease_id"],
            "outcome": "done",
            "note": "Краткая заметка",
            "roadmap_result": result,
        },
    )


async def _evaluate(s, order_id, *, auth=None, grant_id=None):
    return await s.world.client.post(
        f"/api/tasks/{s.task_id}/roadmap-orders/{order_id}/decision",
        headers=auth or s.steward,
        json={"grant_id": grant_id or s.grants[s.task_id]},
    )


async def _decision_audits(s):
    return await support.rows(
        s.world.db,
        "SELECT * FROM admin_audit_log WHERE action='roadmap_decision_recorded'",
    )
