"""Pair/claimed done with auto_review=false still drops unanswered-finding warnings.

#1155 named unanswered findings on pending_report. Subtasks are forced
auto_review=False, may pair-start and submit-for-review, then done completes
via _complete_without_review and never calls HEADLESS gates — the same class
of silence that pending_report just stopped.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo


def _finding(title: str, **over: Any) -> dict[str, Any]:
    base = {
        "title": title,
        "severity": "high",
        "category": "correctness",
        "locator": "file",
        "file": "hub/db.py",
    }
    base.update(over)
    return base


@pytest.fixture
def quiet_git_ops(monkeypatch):
    """Same git-tail stub as tests/test_finding_outcomes_on_done.py: this test
    is about the unanswered-finding warn, not about clone/PR delivery."""
    from hub.integrations.registry import plugins

    for name, value in (
        ("pair_prepare_branch", "task-x/pair"),
        ("pair_prepare_worktree", "task-x/pair"),
        ("checkout", True),
        ("dirty_paths", []),
        ("auto_commit", True),
        ("squash_branch", True),
        ("push_branch", True),
        ("create_pr", None),
        ("branch_tip", ""),
        ("changed_paths", []),
        ("branch_diff_paths", None),
    ):
        if hasattr(plugins.git_ops, name):
            monkeypatch.setattr(
                plugins.git_ops, name, AsyncMock(return_value=value), raising=False
            )
    monkeypatch.setattr(
        plugins.dispatch, "submit_task", AsyncMock(return_value={}), raising=False
    )
    yield plugins.git_ops


async def _subtask_sent_back_with_a_finding(
    client: AsyncClient, db: aiosqlite.Connection, title: str
) -> int:
    """Subtask (forced auto_review=False) returned with a confirmed finding."""
    parent = await client.post("/api/tasks", json={"title": f"Parent of {title}"})
    assert parent.status_code == 200, parent.text
    resp = await client.post(
        "/api/tasks",
        json={
            "title": title,
            "task_type": "subtask",
            "parent_id": parent.json()["id"],
        },
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]
    row = dict(await repo.get_task(db, task_id))
    assert not row["auto_review"], "subtasks are forced auto_review=False"
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: работать"},
    )
    started = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert started.status_code == 200, started.text
    submitted = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert submitted.status_code == 200, submitted.text
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="lite-diff-review",
        raw_count=1,
        findings_confirmed=json.dumps(
            [_finding("утечка курсора")], ensure_ascii=False
        ),
        unresolved=json.dumps([], ensure_ascii=False),
        incomplete=False,
    )
    await db.commit()
    verdict = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )
    assert verdict.status_code == 200, verdict.text
    return task_id


async def test_pair_done_with_auto_review_off_names_unanswered_findings(
    db: aiosqlite.Connection, client: AsyncClient, monkeypatch, quiet_git_ops
):
    """The pending_report warn must also fire on the auto_review=False pair path."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _subtask_sent_back_with_a_finding(
        client, db, "Сабтаск без auto_review"
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "готово"},
    )
    assert resp.status_code == 200, resp.text
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "completed", (
        "this is the _complete_without_review door, not the default pair review path"
    )
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "утечка курсора" in feed, (
        "done closed the task without naming the unanswered finding — "
        "unanswered_findings_note lives only on pending_report, and "
        "_complete_without_review never calls HEADLESS"
    )
