"""Tests for the extended GET /api/tasks/{id}/context contract (#41).

The /context endpoint must provide a Developer agent with enough
information to start work without additional round-trips:
- the task itself with all structured fields and ACs embedded
- a compact readiness summary
- the nearest epic/feature as "parent goal"
- a human-readable markdown digest (context_text)
"""

from __future__ import annotations

from httpx import AsyncClient


async def _make_dor_ready(client: AsyncClient, task_id: int) -> None:
    resp = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "work_type": "feature",
            "user_story": "as a user, I want X so that Y",
            "problem_statement": "ps",
            "business_value": "bv",
            "scope_in": ["frontend"],
            "scope_out": ["backend"],
            "validation_commands": ["uv run pytest"],
            "size": "S",
            "wip_tag": "feature_work",
            "affected_areas": ["hub/services/dor.py"],
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "g",
                    "when": "w",
                    "then": "t",
                    "verifiable_by": "test",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Backward compatibility: legacy keys stay present
# ---------------------------------------------------------------------------


async def test_context_preserves_legacy_keys(client: AsyncClient):
    resp = await client.post("/api/tasks", json={"title": "ctx"})
    task_id = resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}/context")
    assert resp.status_code == 200
    data = resp.json()
    for key in (
        "task_id",
        "breadcrumb",
        "siblings",
        "children",
        "progress",
        "context_text",
    ):
        assert key in data
    assert data["task_id"] == task_id


# ---------------------------------------------------------------------------
# Empty draft: readiness reflects missing fields
# ---------------------------------------------------------------------------


async def test_context_of_empty_draft_exposes_missing_required(
    client: AsyncClient,
):
    resp = await client.post(
        "/api/tasks", json={"title": "empty", "source": "agent", "agent": "test"}
    )
    task_id = resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}/context")
    data = resp.json()
    readiness = data["readiness"]
    assert readiness["dor_passed"] is False
    assert readiness["score"] < 100
    assert "has_user_story" in readiness["missing_required"]
    assert "has_acceptance_criteria" in readiness["missing_required"]


# ---------------------------------------------------------------------------
# DoR-ready task: readiness==100, no blocking recommendations, ACs embedded
# ---------------------------------------------------------------------------


async def test_context_of_ready_task_has_perfect_readiness_and_embedded_acs(
    client: AsyncClient,
):
    resp = await client.post(
        "/api/tasks",
        json={"title": "ready", "source": "agent", "agent": "test"},
    )
    task_id = resp.json()["id"]
    await _make_dor_ready(client, task_id)

    resp = await client.get(f"/api/tasks/{task_id}/context")
    data = resp.json()

    readiness = data["readiness"]
    assert readiness["dor_passed"] is True
    assert readiness["score"] == 100
    assert readiness["missing_required"] == []
    assert readiness["blocking_recommendations"] == []

    task = data["task"]
    assert task["user_story"] == "as a user, I want X so that Y"
    assert task["scope_in"] == ["frontend"]
    assert task["scope_out"] == ["backend"]
    assert task["validation_commands"] == ["uv run pytest"]
    assert task["acceptance_criteria"] is not None
    assert len(task["acceptance_criteria"]) == 1
    assert task["acceptance_criteria"][0]["id"] == "AC-1"

    # context_text must have the canonical sections so prompt-extractors
    # can target them.
    text = data["context_text"]
    for section in (
        "User story:",
        "Problem:",
        "Validation:",
        "Readiness:",
        "Acceptance criteria",
    ):
        assert section in text, f"missing section {section!r} in context_text"


# ---------------------------------------------------------------------------
# Parent goal: task inside an epic sees its epic
# ---------------------------------------------------------------------------


async def test_context_exposes_parent_epic_as_parent_goal(client: AsyncClient):
    epic = await client.post(
        "/api/tasks",
        json={
            "title": "Big goal",
            "task_type": "epic",
            "source": "human",
        },
    )
    epic_id = epic.json()["id"]
    # Enrich the epic so parent_goal carries substance.
    await client.post(
        f"/api/tasks/{epic_id}/refine",
        json={
            "problem_statement": "the epic problem",
            "business_value": "the epic value",
        },
    )

    child = await client.post(
        "/api/tasks",
        json={"title": "child task", "parent_id": epic_id},
    )
    child_id = child.json()["id"]

    resp = await client.get(f"/api/tasks/{child_id}/context")
    data = resp.json()
    parent_goal = data["parent_goal"]
    assert parent_goal is not None
    assert parent_goal["id"] == epic_id
    assert parent_goal["task_type"] == "epic"
    assert parent_goal["title"] == "Big goal"
    assert parent_goal["problem_statement"] == "the epic problem"
    assert parent_goal["business_value"] == "the epic value"

    # And the digest mentions the parent goal.
    assert "Parent goal: Epic" in data["context_text"]


# ---------------------------------------------------------------------------
# Root-level task: no parent goal
# ---------------------------------------------------------------------------


async def test_context_of_root_task_has_no_parent_goal(client: AsyncClient):
    resp = await client.post("/api/tasks", json={"title": "standalone"})
    task_id = resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}/context")
    assert resp.json()["parent_goal"] is None


# ---------------------------------------------------------------------------
# 404 for unknown task
# ---------------------------------------------------------------------------


async def test_context_missing_task_returns_404(client: AsyncClient):
    resp = await client.get("/api/tasks/99999/context")
    assert resp.status_code == 404


async def test_context_text_renders_risks_via_enum_value(client: AsyncClient):
    """Regression for review I4: enum members must be serialized via .value
    so the digest reads as 'security:high', not 'RiskKind.security:...'.
    """
    resp = await client.post(
        "/api/tasks", json={"title": "with risks", "source": "agent", "agent": "t"}
    )
    task_id = resp.json()["id"]
    refine = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "risks": [
                {
                    "kind": "security",
                    "severity": "high",
                    "description": "auth bypass",
                    "mitigation": "add audit + 2FA",
                }
            ]
        },
    )
    assert refine.status_code == 200, refine.text

    ctx = await client.get(f"/api/tasks/{task_id}/context")
    text = ctx.json()["context_text"]
    assert "security:high" in text
    assert "RiskKind" not in text
    assert "RiskSeverity" not in text


# ---- Universal Review Gate (#308): latest review projection ----


async def _pair_submit(client: AsyncClient, title: str) -> int:
    resp = await client.post("/api/tasks", json={"title": title})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: implement"},
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "First submission"},
    )
    assert resp.status_code == 200, resp.text
    return task_id


async def test_latest_review_projection_in_status_and_context(client: AsyncClient, db):
    from hub import services
    from hub.models import (
        ReviewFinding,
        ReviewSeverity,
        ReviewVerdict,
        TaskReviewVerdict,
    )

    task_id = await _pair_submit(client, "Reviewed task")

    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested,
            agent="reviewer",
            comments="see findings",
            findings=[
                ReviewFinding(
                    id=1,
                    severity=ReviewSeverity.high,
                    message="Race in bump",
                    file="hub/repository.py",
                    line=10,
                    recommendation="Do the increment in SQL",
                ),
                ReviewFinding(
                    id=2, severity=ReviewSeverity.low, message="Typo in docstring"
                ),
            ],
        ),
    )

    # Task status: structured projection with numbered findings (AC-2).
    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    lr = resp.json()["latest_review"]
    assert lr["verdict"] == "changes_requested"
    assert lr["is_current"] is True
    assert [f["id"] for f in lr["findings"]] == [1, 2]
    assert lr["findings"][0]["severity"] == "high"
    assert lr["findings"][0]["file"] == "hub/repository.py"

    # Context: same structured data on the embedded task + digest line.
    resp = await client.get(f"/api/tasks/{task_id}/context")
    assert resp.status_code == 200
    data = resp.json()
    embedded = data["task"]["latest_review"]
    assert embedded["verdict"] == "changes_requested"
    assert embedded["findings"][0]["message"] == "Race in bump"
    assert "Latest review: CHANGES_REQUESTED" in data["context_text"]
    assert "1. [high] Race in bump" in data["context_text"]


async def test_latest_review_marked_stale_after_resubmission(client: AsyncClient, db):
    from hub import services
    from hub.models import ReviewVerdict, TaskReviewVerdict

    task_id = await _pair_submit(client, "Stale review task")
    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )

    # Simulate rework + resubmission: back to running, then an explicit new
    # submit-for-review. (A done report on approved work now completes the
    # task instead of resubmitting — Universal Review Gate, #306.)
    from hub import repository as repo_module

    await repo_module.update_task(db, task_id, status="running")
    await db.commit()
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"agent": "dev", "summary": "Second submission"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"/api/tasks/{task_id}")
    body = resp.json()
    assert body["submission_generation"] == 2
    assert body["latest_review"]["is_current"] is False
    assert body["review_approved_current"] is False
    resp = await client.get(f"/api/tasks/{task_id}/context")
    assert "stale — work resubmitted" in resp.json()["context_text"]


def _enable_live_worktree(monkeypatch, *, path: str, registered: bool = True):
    from unittest.mock import AsyncMock, MagicMock

    from hub.integrations.registry import plugins

    monkeypatch.setenv("HAIPLANE_WORKTREE_PER_TASK", "1")
    plugins.git_ops.pair_prepare_worktree = AsyncMock(return_value="task-wt")
    plugins.git_ops.worktree_path = MagicMock(return_value=path)
    plugins.git_ops.worktree_is_registered = AsyncMock(return_value=registered)


async def test_context_echoes_live_worktree_on_text_and_task(
    client: AsyncClient, monkeypatch
):
    """AC-2/AC-9 (#989): /context names the live tree in text and on task."""
    create_resp = await client.post("/api/tasks", json={"title": "Context WT"})
    task_id = create_resp.json()["id"]
    path = f"/srv/.ws-worktrees/task-{task_id}"
    _enable_live_worktree(monkeypatch, path=path)
    started = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"plan": "Plan: pair", "assigned_agent": "dev"},
    )
    assert started.json()["worktree_path"] == path

    ctx = await client.get(f"/api/tasks/{task_id}/context")
    assert ctx.status_code == 200
    body = ctx.json()
    assert body["task"]["worktree_path"] == path
    assert f"Worktree: {path}" in body["context_text"]


async def test_context_advisory_when_session_workspace_mismatches(
    client: AsyncClient, db, monkeypatch
):
    """AC-5 (#989): mismatch is an advisory line, never 409."""
    from hub import repository as repo

    create_resp = await client.post("/api/tasks", json={"title": "WT mismatch"})
    task_id = create_resp.json()["id"]
    path = f"/srv/.ws-worktrees/task-{task_id}"
    _enable_live_worktree(monkeypatch, path=path)
    await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"plan": "Plan: pair", "assigned_agent": "dev"},
    )
    await repo.upsert_agent_session(
        db,
        session_id="s-mismatch",
        principal_id=None,
        agent="dev",
        model="",
        host="",
        workspace="/tmp/main-clone",
    )
    await repo.update_task(db, task_id, claim_session_id="s-mismatch")
    await db.commit()

    ctx = await client.get(f"/api/tasks/{task_id}/context")
    assert ctx.status_code == 200
    text = ctx.json()["context_text"]
    assert "Advisory:" in text
    assert "/tmp/main-clone" in text
    assert path in text
    got = await client.get(f"/api/tasks/{task_id}")
    assert got.status_code == 200
    assert got.json()["worktree_path"] == path
