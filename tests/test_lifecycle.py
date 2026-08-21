"""Risk class is shadow-mode (#581): the field must not change any gate.

AC-4: a task carrying the scariest class (R5) walks the canonical pair
lifecycle — approve, claim, pair-start, submit, changes_requested, resubmit,
approved, done — step for step identically to a task with no class at all.
The assertion compares whole transition traces, not single statuses, so a
gate that starts looking at the class shows up as a diff, not a flake.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo


async def _walk_pair_lifecycle(client: AsyncClient, task_id: int) -> list[str]:
    """Run the canonical pair cycle and return the observed status trace."""
    trace: list[str] = []

    async def _post(url: str, payload: dict) -> None:
        resp = await client.post(url, json=payload)
        assert resp.status_code == 200, f"{url}: {resp.status_code} {resp.text}"
        body = resp.json()
        if "status" in body:
            trace.append(body["status"])

    # Legacy force bypasses the DoR gate the same way test_api.py does;
    # DoR-aware approval is covered in test_api_approve_gate.py.
    await _post(f"/api/tasks/{task_id}/approve", {"force": True})
    await _post(f"/api/tasks/{task_id}/claim", {"agent": "dev"})
    await _post(
        f"/api/tasks/{task_id}/updates",
        {"agent": "dev", "kind": "status", "content": "Plan: implement"},
    )
    await _post(f"/api/tasks/{task_id}/pair-start", {"assigned_agent": "dev"})
    await _post(f"/api/tasks/{task_id}/submit-review", {"agent": "dev"})
    await _post(
        f"/api/tasks/{task_id}/review-verdict",
        {
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [{"id": 1, "severity": "high", "message": "fix it"}],
        },
    )
    await _post(f"/api/tasks/{task_id}/submit-review", {"agent": "dev"})
    await _post(
        f"/api/tasks/{task_id}/review-verdict",
        {"verdict": "approved", "agent": "reviewer"},
    )
    await _post(
        f"/api/tasks/{task_id}/updates",
        {"agent": "dev", "kind": "done", "content": "implemented"},
    )
    resp = await client.get(f"/api/tasks/{task_id}")
    trace.append(resp.json()["status"])
    return trace


async def test_risk_class_does_not_change_gates(
    client: AsyncClient, db: aiosqlite.Connection
):
    resp = await client.post(
        "/api/tasks", json={"title": "classified R5", "source": "agent"}
    )
    classified_id = resp.json()["id"]
    resp = await client.post(
        "/api/tasks", json={"title": "no class", "source": "agent"}
    )
    plain_id = resp.json()["id"]

    await db.execute(
        "UPDATE tasks SET risk_class = 'R5' WHERE id = ?", (classified_id,)
    )
    await db.commit()

    classified_trace = await _walk_pair_lifecycle(client, classified_id)
    plain_trace = await _walk_pair_lifecycle(client, plain_id)

    assert classified_trace == plain_trace, (
        "shadow mode: an R5 class changed a transition that must not see it"
    )
    assert classified_trace[-1] == "completed"

    # The class itself survived the whole cycle untouched.
    resp = await client.get(f"/api/tasks/{classified_id}")
    assert resp.json()["risk_class"] == "R5"


# ---- #852 AC-4: the session requirement is scoped to the pair path ----


async def test_headless_path_does_not_require_session(db: aiosqlite.Connection):
    """A dispatched task has no session by construction, and must not need one.

    #852 makes an agent name the session that takes a task, because a name
    does not identify an executor. Headless work is the case where that
    reasoning does not apply: the executor is the dispatch job, recorded in
    ``job_id``, and no session exists to name. If the new guard leaked into
    this path, every headless task would stop starting.
    """
    from hub import services
    from hub.models import TaskCreate, TaskStart

    tv = await services.create_task(db, TaskCreate(title="Dispatched, not paired"))

    started = await services.start_task(db, tv.id, TaskStart(plan="Plan: run headless"))

    assert started.status.value == "running", "headless start still works"
    task = dict(await repo.get_task(db, tv.id))
    assert not task["claim_session_id"], "nothing invented a session for it"

    # And it is not counted as unaddressable either: a dispatched task has an
    # executor, it just is not a session (job_id is what names it).
    await repo.update_task(db, tv.id, job_id="job-headless")
    await db.commit()
    orphans = [t.id for t in await services.unaddressable_tasks(db)]
    assert tv.id not in orphans
