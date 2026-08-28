"""Risk class is shadow-mode (#581): the field must not change any gate.

AC-4: a task carrying the scariest class (R5) walks the canonical pair
lifecycle — approve, claim, pair-start, submit, changes_requested, resubmit,
approved, done — step for step identically to a task with no class at all.
The assertion compares whole transition traces, not single statuses, so a
gate that starts looking at the class shows up as a diff, not a flake.
"""

from __future__ import annotations

import json

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


# --- #1012: an approval that overrides findings says so -------------------
#
# On 2026-08-28 task #987 was approved while its current report carried three
# confirmed findings and not one disposition. The delivery gate merged it
# immediately, and nothing on the way to the verdict had said the report
# existed. What shipped was a test proven by that same report not to catch
# its own defect.


async def _task_in_review(client: AsyncClient, title: str) -> int:
    """A pair task submitted for review, ready for a verdict."""
    resp = await client.post("/api/tasks", json={"title": title, "source": "agent"})
    task_id = resp.json()["id"]
    for url, payload in (
        (f"/api/tasks/{task_id}/approve", {"force": True}),
        (f"/api/tasks/{task_id}/claim", {"agent": "dev"}),
        (
            f"/api/tasks/{task_id}/updates",
            {"agent": "dev", "kind": "status", "content": "Plan: implement"},
        ),
        (f"/api/tasks/{task_id}/pair-start", {"assigned_agent": "dev"}),
        (f"/api/tasks/{task_id}/submit-review", {"agent": "dev"}),
    ):
        resp = await client.post(url, json=payload)
        assert resp.status_code == 200, f"{url}: {resp.text}"
    return task_id


async def _report(
    db: aiosqlite.Connection, task_id: int, generation: int, titles: list[str]
) -> int:
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        raw_count=len(titles) + 2,
        incomplete=False,
        findings_confirmed=json.dumps(
            [{"title": t, "severity": "medium"} for t in titles], ensure_ascii=False
        ),
    )
    await db.commit()
    row = await repo.get_latest_machine_review(db, task_id)
    return int(dict(row)["id"])


async def _verdict_updates(client: AsyncClient, task_id: int) -> str:
    resp = await client.get(f"/api/tasks/{task_id}/updates")
    return "\n".join(u["content"] for u in resp.json() if u["kind"] == "review")


async def test_approved_records_note_about_undisposed_findings(
    client: AsyncClient, db: aiosqlite.Connection
):
    """AC-2: the caveat lands in the RECORD, not only on the screen."""
    task_id = await _task_in_review(client, "approved over findings")
    await _report(db, task_id, 1, ["cursor never advances", "page failure is silent"])

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert resp.status_code == 200, resp.text
    assert "без диспозиции" in (resp.json().get("lifecycle_hint") or "")

    written = await _verdict_updates(client, task_id)
    assert "ОДОБРЕНО ПРИ НЕРАЗМЕЧЕННЫХ НАХОДКАХ" in written
    assert "2 без диспозиции" in written


async def test_disposed_findings_produce_no_note(
    client: AsyncClient, db: aiosqlite.Connection
):
    """AC-3: answered findings raise nothing — a warning that always fires is wallpaper."""
    task_id = await _task_in_review(client, "approved after judging")
    review_id = await _report(db, task_id, 1, ["one", "two"])
    for index in (0, 1):
        await repo.upsert_finding_disposition(
            db,
            review_id=review_id,
            task_id=task_id,
            submission_generation=1,
            finding_index=index,
            finding_title="",
            disposition="fixed",
            note="",
            decided_by="denis",
        )
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert resp.status_code == 200, resp.text
    assert "НЕРАЗМЕЧЕННЫХ" not in await _verdict_updates(client, task_id)


async def test_stale_report_produces_no_note(
    client: AsyncClient, db: aiosqlite.Connection
):
    """AC-4: a report about an earlier submission describes other code."""
    task_id = await _task_in_review(client, "stale report")
    await _report(db, task_id, 99, ["about a submission that is gone"])

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert resp.status_code == 200, resp.text
    assert "НЕРАЗМЕЧЕННЫХ" not in await _verdict_updates(client, task_id)


async def test_note_does_not_block_approval(
    client: AsyncClient, db: aiosqlite.Connection
):
    """AC-5: the gate warns and stays the human's. It does not refuse."""
    task_id = await _task_in_review(client, "warned but approved")
    await _report(db, task_id, 1, ["unanswered"])

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert resp.status_code == 200, resp.text
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["review_verdict"] == "approved"
    assert task["review_approved_current"] is True
    assert task["status"] == "running", "an approval must still return the task to work"


async def test_changes_requested_carries_no_approval_note(
    client: AsyncClient, db: aiosqlite.Connection
):
    """The caveat belongs to approval: sending work back overrides nothing."""
    task_id = await _task_in_review(client, "sent back")
    await _report(db, task_id, 1, ["unanswered"])

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [{"id": 1, "severity": "high", "message": "fix"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert "НЕРАЗМЕЧЕННЫХ" not in await _verdict_updates(client, task_id)


async def test_second_report_names_the_dispatched_reviewer(
    client: AsyncClient, db: aiosqlite.Connection
):
    """AC-6: a hand-run report meeting a dispatch must not look like dishonesty.

    The spend reconciliation (#757) compares a report's declared tokens with
    the provider's usage for the DISPATCHED run. On 2026-08-28 a hand-run
    report of 71296 tokens was measured against a dispatch that had spent
    2574930, and the audit alert named a discrepancy nobody had caused.
    """
    task_id = await _task_in_review(client, "two reviewers, one submission")
    await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="bc-cloud-1",
        run_id="run-1",
        model="grok-4.6",
    )
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "raw_count": 3,
            "incomplete": False,
            "agent": "pda_claude",
            "model": "claude-fable-5",
            "tokens_spent": 71296,
            "findings_confirmed": [{"title": "found by hand", "severity": "low"}],
        },
    )
    assert resp.status_code == 200, resp.text

    updates = (await client.get(f"/api/tasks/{task_id}/updates")).json()
    alerts = "\n".join(u["content"] for u in updates if u["kind"] == "alert")
    assert "grok-4.6" in alerts
    assert "bc-cloud-1" in alerts
    assert "сверка расходов" in alerts.lower() or "расход" in alerts
