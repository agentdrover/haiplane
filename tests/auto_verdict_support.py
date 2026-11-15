"""Shared setup of the auto-verdict tests: a pair task in review, its report, a settled dispatch.

Lives apart from ``tests/test_auto_verdict.py`` so that the file of the tests can
stay under its size ledger; other modules keep importing these names from the
test module, which re-exports them.
"""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.integrations import cursor_cloud
from hub.integrations.noop import NoopGitOps
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.models import TaskRefine, TaskSubmitReview
from hub.services.ci_report import VALIDATION_PASS
from tests.ci_trust_support import upsert_hub_report, VERIFIED_ROW

_TIP = "a" * 40

_CLEAN_REVIEW = {
    "harness_skill": "multi-agent-review",
    "harness_version": 7,
    "raw_count": 2,
    "findings_confirmed": [],
    "findings_rejected": [
        {"title": "candidate refuted", "category": "correctness", "reason": "not real"}
    ],
    "incomplete": False,
    "unresolved": [],
    "lost_dimensions": [],
    "agent": "reviewer-bot",
    # #758: the diversity rule needs both models declared — the clean
    # baseline is a cross-family pair (implementer claude, reviewer grok).
    "model": "cursor-grok-4.6",
}


class _PinnedGitOps(NoopGitOps):
    """A branch whose tip and diff the test controls.

    Since #967 a diff that positively shows commits gets its PR opened by the
    hub at submission, and the done path delivers it through the merge gate —
    so this fake must be able to push, open and merge a PR, or every test
    here would end in warnings about a PR nobody could open (and the done
    test in needs_decision instead of completed).
    """

    def __init__(self, tip: str, paths: list[str]) -> None:
        self._tip = tip
        self._paths = paths

    async def fetch_base(self, repo: str, base: str):
        return True, ""

    async def head_sha(self, repo: str, base: str) -> str:
        return self._tip

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        return self._paths

    async def branch_diff(self, repo, base, branch, context=0):
        # #1688: the integrity scan reads the diff of the pinned commit; an
        # empty one shows no change to CI inputs.
        return ""

    async def integrity_read(self, repo, base, sha):
        # #1688: the scan reads the diff of the exact commit; an empty one
        # shows no change to CI inputs.
        return {"diff": "", "base_sha": "b" * 40, "head_sha": sha}

    async def push_branch(self, branch, repo=None, force=False):
        return True

    async def create_pr(
        self,
        task_id,
        title,
        description,
        branch,
        repo=None,
        gh_repo=None,
        base_branch=None,
        forge: str = "",
    ):
        return 41

    async def check_pr_ci(self, pr_number, repo=None, gh_repo=None, forge: str = ""):
        return CIProbeResult(CIProbeOutcome.passed, "checks_passed")

    async def merge_pr(
        self,
        pr_number,
        task_id,
        title,
        repo=None,
        gh_repo=None,
        delete_branch=True,
        forge: str = "",
        expected_head_sha: str = "",
    ):
        return True

    async def merge_commit_sha(
        self, pr_number, repo=None, gh_repo=None, forge: str = ""
    ):
        return "b" * 40

    async def pull_main(self, repo=None, base_branch=None):
        return True

    async def delete_branch(self, branch, repo=None, base_branch=None):
        return True


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
    policy: dict | None,
    *,
    areas: list[str] | None = None,
    with_ci: bool = True,
) -> int:
    """A pair task in review: pinned tip, declared areas, optional green CI."""
    areas = areas if areas is not None else ["docs/notes.md"]
    pid = await repo.create_project(
        db, slug=slug, name=slug.title(), workspace_path="/tmp/ws"
    )
    if policy is not None:
        await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    task_id = await _node(db, title="review probe", task_type="task", parent_id=feature)
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: do the work")
    await repo.update_task_structured(db, task_id, TaskRefine(affected_areas=areas))
    await db.commit()

    plugins.git_ops = _PinnedGitOps(_TIP, list(areas))
    started = await services.pair_start_task(db, task_id, caller="dev-agent")
    assert started.status.value == "running"
    # Stored class must exist for the diff comparison — recompute via refine.
    resp = await client.post(
        f"/api/tasks/{task_id}/refine", json={"affected_areas": areas}
    )
    assert resp.status_code == 200, resp.text
    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    assert view.status.value == "review"
    assert view.submission_sha == _TIP, "the tip must be pinned at submission"

    if with_ci:
        await upsert_hub_report(
            db,
            task_id=task_id,
            head_sha=_TIP,
            ac_results="{}",
            validation_status=VALIDATION_PASS,
            validation_log="",
            reason="",
            reported_by="ci",
            **VERIFIED_ROW,
        )
        await db.commit()
    return task_id


async def _post_review(client: AsyncClient, task_id: int, **overrides) -> None:
    body = dict(_CLEAN_REVIEW)
    body.update(overrides)
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=body)
    assert resp.status_code == 200, resp.text


async def _events(db: aiosqlite.Connection, kind: str, task_id: int) -> list[dict]:
    rows = await repo.list_events(db, since=0, kinds=[kind], limit=200)
    return [dict(r) for r in rows if r["task_id"] == task_id]


async def _settled_dispatch(
    db: aiosqlite.Connection, task_id: int, *, usage_total, monkeypatch
) -> None:
    """A done hub dispatch for gen 1 plus a stubbed provider usage answer."""
    dispatch_id = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="bc-proof",
        run_id="run-proof",
        model="grok-4.6",
    )
    await repo.set_review_dispatch_status(db, dispatch_id, "done")
    await db.commit()

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": usage_total}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
