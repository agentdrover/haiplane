"""Autopilot daily digest and sampling audit (#739).

One digest per project per UTC day of autopilot activity; empty days stay
silent; the sample is deterministic; the spot-check flows back into the
human_gates metric as the ``audit`` gate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.services.digest import deterministic_sample, generate_due_digests
from hub.services.orchestration import practice_metrics


async def _node(
    db: aiosqlite.Connection, *, title: str, task_type: str, parent_id: int | None
) -> int:
    return await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )


async def _autopilot_project(db: aiosqlite.Connection, slug: str) -> tuple[int, int]:
    """(project_id, feature_id) — a project with dor=auto and a hierarchy."""
    pid = await repo.create_project(db, slug=slug, name=slug.title())
    await repo.update_project(db, pid, gate_policy=json.dumps({"dor": "auto"}))
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    await db.commit()
    return pid, feature


async def _policy_approved_task(
    db: aiosqlite.Connection, feature_id: int, title: str
) -> int:
    task_id = await _node(db, title=title, task_type="task", parent_id=feature_id)
    await repo.insert_event(
        db,
        kind="task_approved",
        task_id=task_id,
        actor="policy",
        payload={"auto": True, "risk_class": "R0"},
    )
    await db.commit()
    return task_id


def _tomorrow() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


async def test_digest_content_and_empty_day(db: aiosqlite.Connection):
    # AC-1 (#739): a day with autopilot activity produces one digest with
    # the approvals, escalations and a sample; a quiet project and a quiet
    # day produce nothing.
    _pid, feature = await _autopilot_project(db, "spike-dg")
    t1 = await _policy_approved_task(db, feature, "auto one")
    t2 = await _policy_approved_task(db, feature, "auto two")
    await repo.insert_event(
        db,
        kind="verdict_escalated",
        task_id=t1,
        actor="policy",
        payload={"reason": "security-находка"},
    )
    await db.commit()
    await _autopilot_project(db, "spike-quiet")  # policy on, no activity

    created = await generate_due_digests(db, now=_tomorrow())
    assert created == 1, "only the active project gets a digest"

    rows = await repo.list_digests(db)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert {a["task_id"] for a in payload["auto_approvals"]} == {t1, t2}
    assert payload["escalations"][0]["payload"]["reason"] == "security-находка"
    assert payload["audit_sample"], "a non-empty day carries a sample"
    assert set(payload["audit_sample"]) <= {t1, t2}

    # Same day again → idempotent; the NEXT (empty) day → nothing new.
    assert await generate_due_digests(db, now=_tomorrow()) == 0
    assert await generate_due_digests(db, now=_tomorrow() + timedelta(days=1)) == 0, (
        "a day without autopilot transitions must not create a digest"
    )


async def test_digest_event_published(client: AsyncClient, db: aiosqlite.Connection):
    # AC-2 (#739): the feed gets digest_created (hub_wait_events reads the
    # same feed), and the /digests page renders the digest.
    _pid, feature = await _autopilot_project(db, "spike-ev")
    await _policy_approved_task(db, feature, "auto ev")
    await generate_due_digests(db, now=_tomorrow())

    events = await repo.list_events(db, since=0, kinds=["digest_created"], limit=10)
    assert events, "the digest must announce itself in the events feed"
    payload = json.loads(dict(events[-1])["payload"])
    assert payload["auto_approvals"] == 1
    assert payload["audit_sample"]

    page = await client.get("/digests")
    assert page.status_code == 200
    assert "spike-ev" in page.text
    assert "ждёт проверки" in page.text


async def test_audit_result_feeds_metrics(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#739): the human-only audit endpoint stores the outcome in the
    # task feed and surfaces it in human_gates as the audit gate; agents 403.
    _pid, feature = await _autopilot_project(db, "spike-audit")
    task_id = await _policy_approved_task(db, feature, "auto audited")
    await generate_due_digests(db, now=_tomorrow())
    digest = dict((await repo.list_digests(db))[0])
    sample = json.loads(digest["payload"])["audit_sample"]
    assert sample == [task_id]

    resp = await client.post(
        f"/api/digests/{digest['id']}/audit",
        json={"task_id": task_id, "result": "problem", "comment": "не то поведение"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["audit_results"] == {str(task_id): "problem"}

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    audit_notes = [u for u in updates if "Выборочный аудит" in u["content"]]
    assert audit_notes and "проблема" in audit_notes[0]["content"]

    gates = (await practice_metrics(db))["human_gates"]
    audit_rows = [g for g in gates if g["gate"] == "audit"]
    assert audit_rows and audit_rows[0]["overrides"] == 1

    # Outside the sample → refused: auditing an unsampled task would
    # fabricate coverage.
    outsider = await _policy_approved_task(db, feature, "not sampled")
    resp = await client.post(
        f"/api/digests/{digest['id']}/audit",
        json={"task_id": outsider, "result": "ok"},
    )
    assert resp.status_code == 404

    # Agent token → 403: the audit is the owner's counterpart.
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {"agent-token": TokenIdentity("bot", "agent")},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    resp = await client.post(
        f"/api/digests/{digest['id']}/audit",
        json={"task_id": task_id, "result": "ok"},
        headers={"Authorization": "Bearer agent-token"},
    )
    assert resp.status_code == 403


def test_sample_deterministic():
    # AC-4 (#739): stable composition, ~10%, minimum one on a non-empty day.
    ids = list(range(1, 101))
    first = deterministic_sample(ids, "2026-08-20")
    second = deterministic_sample(ids, "2026-08-20")
    assert first == second
    assert 1 <= len(first) <= 25
    assert deterministic_sample([7], "2026-08-20") == [7]
    assert deterministic_sample([], "2026-08-20") == []
    other_day = deterministic_sample(ids, "2026-08-21")
    assert other_day == deterministic_sample(ids, "2026-08-21")
