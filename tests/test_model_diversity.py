"""Model diversity for the auto-verdict (#758).

Code and review from one model family share blind spots — the auto-verdict
now requires a cross-family review, treats missing declarations as NOT
diverse, and names both models in its grounds.
"""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import TaskRefine, TaskSubmitReview
from hub.services.ci_report import VALIDATION_PASS
from hub.services.model_family import family, same_family

_TIP = "b" * 40

_CLEAN_REVIEW = {
    "harness_skill": "multi-agent-review",
    "harness_version": 8,
    "raw_count": 2,
    "findings_confirmed": [],
    "findings_rejected": [
        {"title": "candidate refuted", "category": "correctness", "reason": "not real"}
    ],
    "incomplete": False,
    "unresolved": [],
    "lost_dimensions": [],
    "agent": "reviewer-bot",
}


class _PinnedGitOps(NoopGitOps):
    def __init__(self, tip: str, paths: list[str]) -> None:
        self._tip = tip
        self._paths = paths

    async def fetch_base(self, repo: str, base: str):
        return True, ""

    async def head_sha(self, repo: str, base: str) -> str:
        return self._tip

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        return self._paths


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


async def _submitted_task(
    client: AsyncClient,
    db: aiosqlite.Connection,
    slug: str,
    *,
    implementer_model: str,
) -> int:
    areas = ["docs/notes.md"]
    pid = await repo.create_project(
        db, slug=slug, name=slug.title(), workspace_path="/tmp/ws"
    )
    await repo.update_project(db, pid, gate_policy=json.dumps({"verdict": "auto"}))
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    task_id = await _node(db, title="probe", task_type="task", parent_id=feature)
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: work")
    await repo.update_task_structured(db, task_id, TaskRefine(affected_areas=areas))
    await db.commit()

    plugins.git_ops = _PinnedGitOps(_TIP, areas)
    started = await services.pair_start_task(db, task_id, caller="dev-agent")
    assert started.status.value == "running"
    resp = await client.post(
        f"/api/tasks/{task_id}/refine", json={"affected_areas": areas}
    )
    assert resp.status_code == 200, resp.text
    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(model=implementer_model)
    )
    assert view.status.value == "review"

    await repo.upsert_ci_run_report(
        db,
        task_id=task_id,
        head_sha=_TIP,
        ac_results="{}",
        validation_status=VALIDATION_PASS,
        validation_log="",
        reason="",
        reported_by="ci",
    )
    await db.commit()
    return task_id


async def _post_review(client: AsyncClient, task_id: int, **overrides) -> None:
    body = dict(_CLEAN_REVIEW)
    body.update(overrides)
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=body)
    assert resp.status_code == 200, resp.text


def test_family_map_prioritises_the_model_over_the_wrapper():
    assert family("claude-fable-5") == "anthropic"
    assert family("cursor-grok-4.6") == "xai", "the wrapper must not hide grok"
    assert family("composer-2") == "cursor"
    assert family("gpt-5.3-codex-high") == "openai"
    assert family("us.anthropic.claude-4") == "anthropic"
    assert family("mystery-9000") == "unknown:mystery-9000"
    assert same_family("mystery-9000", "enigma-1") is False
    assert same_family("", "grok-4") is None
    assert same_family("claude-4", None) is None


async def test_submission_model_is_declared_and_visible(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-1 (#758): the declaration is stored, shown in the feed, and an
    # empty declaration reads as empty — not as some default.
    task_id = await _submitted_task(
        client, db, "spike-decl", implementer_model="claude-fable-5"
    )
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["submission_model"] == "claude-fable-5"
    feed = [u["content"] for u in body["updates"] or []]
    assert any("Модель исполнителя (декларация): claude-fable-5" in c for c in feed)

    bare = await _submitted_task(client, db, "spike-bare", implementer_model="")
    assert (await client.get(f"/api/tasks/{bare}")).json()["submission_model"] == ""


async def test_cross_family_review_passes(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#758): claude code + grok review → verdict, both models named.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _submitted_task(
        client, db, "spike-cross", implementer_model="claude-fable-5"
    )
    await _post_review(client, task_id, model="cursor-grok-4.6")

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["review_verdict"] == "approved"
    assert body["status"] == "running"
    grounds = [
        u["content"]
        for u in body["updates"] or []
        if "Автовердикт APPROVED" in u["content"]
    ]
    assert grounds
    assert "claude-fable-5" in grounds[0] and "cursor-grok-4.6" in grounds[0]


async def test_same_family_escalates_monoculture(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#758): clean submission, but one family on both sides — the
    # verdict stays with the human, loudly.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _submitted_task(
        client, db, "spike-mono", implementer_model="claude-fable-5"
    )
    await _post_review(client, task_id, model="claude-4-sonnet")

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["review_verdict"] is None
    assert body["status"] == "review"
    events = [
        dict(r)
        for r in await repo.list_events(
            db, since=0, kinds=["verdict_escalated"], limit=50
        )
        if r["task_id"] == task_id
    ]
    assert events
    assert "монокультура" in json.loads(events[0]["payload"])["reason"]


async def test_missing_model_data_stays_with_human(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#758): no declaration on either side → no verdict and no
    # escalation — absence of data is not diversity.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")

    undeclared = await _submitted_task(client, db, "spike-nodecl", implementer_model="")
    await _post_review(client, undeclared, model="cursor-grok-4.6")
    assert (await client.get(f"/api/tasks/{undeclared}")).json()["status"] == "review"

    no_review_model = await _submitted_task(
        client, db, "spike-norev", implementer_model="claude-fable-5"
    )
    await _post_review(client, no_review_model, model="")
    assert (await client.get(f"/api/tasks/{no_review_model}")).json()[
        "status"
    ] == "review"

    for tid in (undeclared, no_review_model):
        escalations = [
            r
            for r in await repo.list_events(
                db, since=0, kinds=["verdict_escalated"], limit=50
            )
            if dict(r)["task_id"] == tid
        ]
        assert not escalations, "missing data is a silent refusal, not noise"
