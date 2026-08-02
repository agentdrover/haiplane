"""The verdict binds to the code, not just to a submission number (#572).

Reproduced three times before this existed — #547, then #601 and #532 on the
same day: submit at tip X, push Y while the task waits in review, resubmission
correctly refused ("can only submit running"), APPROVED lands on the
submission number and reads as covering code the reviewer never saw. Each
time the divergence was caught by an agent writing a note — discipline, not a
mechanism.

The hub resolves the tip itself, never trusts the client: a value supplied by
the agent whose work is under review is a declaration, and this mechanism
exists precisely because declarations are not observations (#534, #532, #596).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins


class _Git(NoopGitOps):
    """Git double whose observed branch tip is scripted per test."""

    def __init__(self, tip: str | None = "aaa111"):
        self.tip = tip
        self.fetched: list[str] = []

    async def fetch_base(self, repo: str, base: str):
        self.fetched.append(base)
        if self.tip is None:
            return (False, "remote unreachable")
        return (True, "")

    async def head_sha(self, repo: str, base: str) -> str:
        return self.tip or ""


@pytest.fixture
def git(monkeypatch):
    def install(tip: str | None):
        g = _Git(tip)
        monkeypatch.setattr(plugins, "git_ops", g)
        return g

    yield install


async def _submitted_task(client: AsyncClient, *, workspace: str = "/srv/ws") -> int:
    """A pair task submitted for review, with a project workspace to observe."""
    resp = await client.post("/api/tasks", json={"title": "Bind me"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: do it"},
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text
    return task_id


def _patch_workspace(monkeypatch, path: str = "/srv/ws"):
    from hub.services import orchestration

    monkeypatch.setattr(
        orchestration,
        "project_git_context",
        AsyncMock(return_value={"repo": path, "base_branch": "develop"}),
    )


# ---- the pin is taken at submission, by the hub ----


async def test_submission_pins_the_branch_tip(client: AsyncClient, git, monkeypatch):
    _patch_workspace(monkeypatch)
    git("abc123")

    task_id = await _submitted_task(client)

    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["submission_sha"] == "abc123", (
        "the tip the hub observed at submission must be recorded on the task"
    )


# ---- AC-1: the branch moved, APPROVED must not cover the new code ----


async def test_verdict_on_moved_branch_is_not_current_and_returns_task_to_running(
    client: AsyncClient, git, monkeypatch
):
    """#601 replayed: submit at X, push Y while in review, then APPROVED."""
    _patch_workspace(monkeypatch)
    g = git("oldsha111")
    task_id = await _submitted_task(client)
    g.tip = "newsha222"  # someone pushed while the task sat in review

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert resp.status_code == 200, resp.text
    view = resp.json()

    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["review_approved_current"] is False, (
        "an approval must not read as covering commits the reviewer never saw"
    )
    assert task["status"] == "running", (
        "the task needs a legal way forward, not a locked review state"
    )
    hint = view.get("lifecycle_hint") or ""
    assert "oldsha111"[:12] in hint and "newsha222"[:12] in hint, (
        f"the response must name both tips, got: {hint!r}"
    )


# ---- AC-2: the brief warns before the reviewer spends an hour ----


async def test_review_brief_reports_submission_sha_and_divergence(
    client: AsyncClient, git, monkeypatch
):
    _patch_workspace(monkeypatch)
    g = git("oldsha111")
    task_id = await _submitted_task(client)
    g.tip = "newsha222"

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    assert brief["submission_sha"] == "oldsha111"
    assert brief["current_branch_tip"] == "newsha222"
    assert brief["sha_check"] == "diverged"
    assert "oldsha111"[:12] in brief["sha_check_reason"]


# ---- AC-3: an unmoved branch keeps today's path exactly ----


async def test_unchanged_branch_keeps_todays_approve_and_done_path(
    client: AsyncClient, git, monkeypatch
):
    _patch_workspace(monkeypatch)
    git("samesha1")
    task_id = await _submitted_task(client)

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert resp.status_code == 200, resp.text

    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["review_approved_current"] is True
    assert task["status"] == "running"

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief["sha_check"] == "match"

    done = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "done"},
    )
    assert done.status_code == 200, done.text
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["status"] == "completed"


# ---- AC-4: submissions from before the field exist behave as today ----


async def test_legacy_submission_without_sha_is_not_rejected(
    client: AsyncClient, git, monkeypatch, db
):
    _patch_workspace(monkeypatch)
    g = git(None)  # remote down at submission time -> empty pin, like legacy rows
    task_id = await _submitted_task(client)

    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["submission_sha"] == "", "precondition: nothing pinned"

    g.tip = "whatever9"  # remote is back; there is still nothing to compare with
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert resp.status_code == 200, resp.text

    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["review_approved_current"] is True, (
        "no pin means the check cannot run — behave as before the field existed"
    )
    hint = resp.json().get("lifecycle_hint") or ""
    assert "НЕ проводилась" in hint, (
        "unchecked must be said out loud, not passed off as verified"
    )


# ---- AC-5: an unreachable remote degrades, never 500 ----


async def test_unresolvable_branch_degrades_instead_of_failing(
    client: AsyncClient, git, monkeypatch
):
    _patch_workspace(monkeypatch)
    g = git("pinned01")
    task_id = await _submitted_task(client)
    g.tip = None  # remote голову не отдаёт в момент вердикта

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )

    assert resp.status_code == 200, (
        f"a verdict must not be hostage to the network: {resp.text}"
    )
    hint = resp.json().get("lifecycle_hint") or ""
    assert "НЕ проводилась" in hint and "pinned01"[:12] in hint, (
        "the response must say the check did not run and what was pinned"
    )
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["review_approved_current"] is True, (
        "degradation means today's behaviour, visibly — not a refusal"
    )


# ---- AC-6: a diverged task is never locked in review ----


async def test_diverged_task_has_a_legal_way_out_of_review(
    client: AsyncClient, git, monkeypatch
):
    _patch_workspace(monkeypatch)
    g = git("firstsha")
    task_id = await _submitted_task(client)
    g.tip = "movedsha"

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert resp.status_code == 200, resp.text

    # The way out: the task is back in running, so resubmission works — the
    # exact call that was correctly refused while the task sat in review.
    resub = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resub.status_code == 200, resub.text

    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["status"] == "review"
    assert task["submission_sha"] == "movedsha", (
        "the fresh submission pins the tip the reviewer will now actually see"
    )


# ---- the changes_requested path is untouched ----


async def test_changes_requested_path_is_unchanged(
    client: AsyncClient, git, monkeypatch
):
    """CR returns the task to work regardless; it creates no false safety and
    gets no new machinery."""
    _patch_workspace(monkeypatch)
    g = git("crsha001")
    task_id = await _submitted_task(client)
    g.tip = "crsha999"  # diverged — and it must not matter for CR

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [
                {"id": 1, "severity": "low", "message": "fix it", "scope": "in_scope"}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["status"] == "running"
    assert task["review_verdict"] == "changes_requested"
