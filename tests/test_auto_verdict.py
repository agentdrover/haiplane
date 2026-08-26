"""Auto-verdict under project policy (#745): clean grounds or the human.

A clean machine review + green CI on the pinned commit + a diff that stayed
inside its declaration earns APPROVED from the policy; any risk signal
escalates visibly; everything else silently stays with the human — today's
behavior in full.
"""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.models import TaskRefine, TaskSubmitReview
from hub.services.ci_report import VALIDATION_PASS

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
    ):
        return 41

    async def check_pr_ci(self, pr_number, repo=None, gh_repo=None):
        return CIProbeResult(CIProbeOutcome.passed, "checks_passed")

    async def merge_pr(
        self, pr_number, task_id, title, repo=None, gh_repo=None, delete_branch=True
    ):
        return True

    async def merge_commit_sha(self, pr_number, repo=None, gh_repo=None):
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


async def _events(db: aiosqlite.Connection, kind: str, task_id: int) -> list[dict]:
    rows = await repo.list_events(db, since=0, kinds=[kind], limit=200)
    return [dict(r) for r in rows if r["task_id"] == task_id]


async def test_clean_submission_gets_policy_approved(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-1 (#745): clean review + green CI on the pinned sha → APPROVED by
    # the policy, with actor=policy and the grounds in the feed.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _submitted_task(client, db, "spike-clean", {"verdict": "auto"})

    await _post_review(client, task_id)

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["review_verdict"] == "approved"
    assert body["review_approved_current"] is True
    assert body["status"] == "running", "approved returns the task to the done path"

    verdicts = await _events(db, "review_verdict_recorded", task_id)
    assert verdicts and verdicts[-1]["actor"] == "policy"
    feed = [u["content"] for u in body["updates"] or []]
    grounds = [c for c in feed if "Автовердикт APPROVED" in c]
    assert grounds, "the grounds snapshot must be in the feed"
    assert _TIP[:12] in grounds[0]


async def test_any_unclean_ground_leaves_review_to_human(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-2 (#745): a confirmed finding, a missing CI report, or a report
    # with no surfaced candidates — each leaves the verdict to the human,
    # silently (these are not escalation triggers).
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")

    with_finding = await _submitted_task(
        client, db, "spike-finding", {"verdict": "auto"}
    )
    await _post_review(
        client,
        with_finding,
        findings_confirmed=[
            {"title": "real bug", "severity": "high", "category": "correctness"}
        ],
    )
    assert (await client.get(f"/api/tasks/{with_finding}")).json()["status"] == "review"

    no_ci = await _submitted_task(
        client, db, "spike-noci", {"verdict": "auto"}, with_ci=False
    )
    await _post_review(client, no_ci)
    assert (await client.get(f"/api/tasks/{no_ci}")).json()["status"] == "review"

    no_data = await _submitted_task(client, db, "spike-nodata", {"verdict": "auto"})
    await _post_review(client, no_data, raw_count=0, findings_rejected=[])
    body = (await client.get(f"/api/tasks/{no_data}")).json()
    assert body["status"] == "review", (
        "zero surfaced candidates is no data, not no findings (harness v7)"
    )
    for tid in (with_finding, no_ci, no_data):
        assert not await _events(db, "verdict_escalated", tid), (
            "unclean grounds are silent refusals, not escalations"
        )


async def test_triggers_escalate_with_reason(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-3 (#745): a security mention in ANY finding status, or a budget
    # overrun — no auto-verdict, and the escalation is visible with its
    # reason; no new statuses appear.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")

    security = await _submitted_task(client, db, "spike-sec", {"verdict": "auto"})
    await _post_review(
        client,
        security,
        findings_rejected=[
            {"title": "possible auth bypass", "category": "security", "reason": "n/a"}
        ],
    )
    body = (await client.get(f"/api/tasks/{security}")).json()
    assert body["status"] == "review"
    events = await _events(db, "verdict_escalated", security)
    assert events and "security" in json.loads(events[0]["payload"])["reason"]
    alerts = [
        u["content"] for u in body["updates"] or [] if "эскалация" in u["content"]
    ]
    assert alerts, "an escalation must be visible in the feed"

    over_budget = await _submitted_task(client, db, "spike-budget", {"verdict": "auto"})
    await _post_review(client, over_budget, tokens_spent=999_999)
    assert (await client.get(f"/api/tasks/{over_budget}")).json()["status"] == "review"
    events = await _events(db, "verdict_escalated", over_budget)
    assert events and "бюджет" in json.loads(events[0]["payload"])["reason"]


async def test_scope_and_kill_switch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-4 (#745): the same clean submission without verdict=auto, or with
    # the global switch off, behaves exactly as today.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    unscoped = await _submitted_task(client, db, "spike-noauto", None)
    await _post_review(client, unscoped)
    assert (await client.get(f"/api/tasks/{unscoped}")).json()["status"] == "review"

    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    killed = await _submitted_task(client, db, "spike-koff", {"verdict": "auto"})
    await _post_review(client, killed)
    assert (await client.get(f"/api/tasks/{killed}")).json()["status"] == "review"


async def test_policy_verdict_is_auditable_and_deliverable(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-5 (#745): the record names the policy and its grounds, and the
    # normal done path works after it.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _submitted_task(client, db, "spike-audit", {"verdict": "auto"})
    await _post_review(client, task_id)

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["review_verdict"] == "approved"
    feed = [u["content"] for u in body["updates"] or []]
    grounds = [c for c in feed if "Автовердикт APPROVED" in c]
    assert grounds and "machine-review" in grounds[0]

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "implemented"},
    )
    assert resp.status_code == 200, resp.text
    assert (await client.get(f"/api/tasks/{task_id}")).json()["status"] == "completed"


async def _settled_dispatch(
    db: aiosqlite.Connection, task_id: int, *, usage_total, monkeypatch
) -> None:
    """A done hub dispatch for gen 1 plus a stubbed provider usage answer."""
    from hub.integrations import cursor_cloud

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


async def test_proven_empty_review_is_auto_approved(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-1 (#769): raw_count=0, but the hub's own dispatch settled and the
    # provider's billing clears the floor and agrees with the report →
    # APPROVED by policy with the proof in the grounds line.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _submitted_task(client, db, "spike-proven", {"verdict": "auto"})
    await _settled_dispatch(db, task_id, usage_total=250_000, monkeypatch=monkeypatch)

    # tokens_spent agrees with billing AND stays under the budget trigger
    await _post_review(
        client, task_id, raw_count=0, findings_rejected=[], tokens_spent=240_000
    )

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["review_verdict"] == "approved"
    feed = [u["content"] for u in body["updates"] or []]
    grounds = [c for c in feed if "Автовердикт APPROVED" in c]
    assert grounds and "Пустота доказана: usage=250000" in grounds[0]


async def test_usage_mismatch_keeps_the_human_verdict(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-2 (#769): the live grok case — claimed 36k, billed 1.47M. The
    # mismatch voids the proof; the verdict stays with the human, silently.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _submitted_task(client, db, "spike-mismatch", {"verdict": "auto"})
    await _settled_dispatch(db, task_id, usage_total=1_465_666, monkeypatch=monkeypatch)

    await _post_review(
        client, task_id, raw_count=0, findings_rejected=[], tokens_spent=36_000
    )

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "review"
    assert body["review_verdict"] is None
    assert not await _events(db, "verdict_escalated", task_id), "silent, not a trigger"


async def test_unproven_empty_review_stays_human(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-3 (#769): no settled dispatch (or no usage data) — absence of data
    # is not proof; the #750 rule stands exactly as today.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _submitted_task(client, db, "spike-unproven", {"verdict": "auto"})

    await _post_review(
        client, task_id, raw_count=0, findings_rejected=[], tokens_spent=240_000
    )

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "review"
    assert body["review_verdict"] is None


async def test_proven_empty_above_class_ceiling_stays_human(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-1 (#835): the emptiness is proven exactly as #769 demands, but the
    # task is R2 and the ceiling is R1. Billed usage proves the reviewer
    # worked, never that it could find — and above the ceiling that is not
    # enough. Refused quietly, with the reason in the feed for the digest.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", "r1")
    task_id = await _submitted_task(
        client,
        db,
        "spike-ceiling",
        {"verdict": "auto"},
        areas=["hub/services/auto_verdict.py"],
    )
    await _settled_dispatch(db, task_id, usage_total=250_000, monkeypatch=monkeypatch)

    await _post_review(
        client, task_id, raw_count=0, findings_rejected=[], tokens_spent=240_000
    )

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["risk_class"] == "R2", "the probe must actually sit above the ceiling"
    assert body["status"] == "review"
    assert body["review_verdict"] is None
    feed = [u["content"] for u in body["updates"] or []]
    held = [c for c in feed if "пустое ревью выше потолка класса" in c]
    assert held, "the digest must be able to see WHY the ceiling held it back"
    assert "R2" in held[0] and "R1" in held[0]
    assert not await _events(db, "verdict_escalated", task_id), "silent, not a trigger"


async def test_proven_empty_at_ceiling_still_auto_approved(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-2 (#835): at the ceiling nothing changes — #769 keeps working for
    # the classes it was meant for, or the ceiling would be a rollback.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", "r1")
    task_id = await _submitted_task(
        client, db, "spike-at-ceiling", {"verdict": "auto"}, areas=["tests/test_x.py"]
    )
    await _settled_dispatch(db, task_id, usage_total=250_000, monkeypatch=monkeypatch)

    await _post_review(
        client, task_id, raw_count=0, findings_rejected=[], tokens_spent=240_000
    )

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["risk_class"] == "R1", "the probe must sit exactly at the ceiling"
    assert body["review_verdict"] == "approved"
    feed = [u["content"] for u in body["updates"] or []]
    assert any("Пустота доказана: usage=250000" in c for c in feed)


async def test_ceiling_does_not_touch_non_empty_reviews(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-3 (#835): raw_count >= 1 on an R3 task still auto-approves. The
    # ceiling narrows the INFERRED proof of #769, not the ordinary ground:
    # candidates on the table are capability shown, not inferred.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", "r1")
    task_id = await _submitted_task(
        client, db, "spike-nonempty", {"verdict": "auto"}, areas=["hub/auth.py"]
    )

    await _post_review(client, task_id, raw_count=3)

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["risk_class"] == "R3", "the probe must sit far above the ceiling"
    assert body["review_verdict"] == "approved"
    feed = [u["content"] for u in body["updates"] or []]
    assert not any("выше потолка класса" in c for c in feed)


async def test_proven_empty_without_class_stays_human(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-4 (#835): no computed class is absence of data about the blast
    # radius, and an empty review is absence of data about the code. Two
    # unknowns are not a ground — NULL never reads as R0.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", "r1")
    task_id = await _submitted_task(client, db, "spike-noclass", {"verdict": "auto"})
    await db.execute("UPDATE tasks SET risk_class = NULL WHERE id = ?", (task_id,))
    await db.commit()
    await _settled_dispatch(db, task_id, usage_total=250_000, monkeypatch=monkeypatch)

    await _post_review(
        client, task_id, raw_count=0, findings_rejected=[], tokens_spent=240_000
    )

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "review"
    assert body["review_verdict"] is None
    feed = [u["content"] for u in body["updates"] or []]
    held = [c for c in feed if "пустое ревью выше потолка класса" in c]
    assert held and "не вычислен" in held[0]


async def test_unknown_ceiling_value_is_strict(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-5 (#835): a typo in the drop-in closes the path instead of opening
    # it. An unreadable safeguard setting must never read as "no limit" —
    # the probe here is R0, which every real ceiling would have approved.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", "all")
    task_id = await _submitted_task(client, db, "spike-typo", {"verdict": "auto"})
    await _settled_dispatch(db, task_id, usage_total=250_000, monkeypatch=monkeypatch)

    await _post_review(
        client, task_id, raw_count=0, findings_rejected=[], tokens_spent=240_000
    )

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["risk_class"] == "R0", "even the harmless class must be refused"
    assert body["review_verdict"] is None
    feed = [u["content"] for u in body["updates"] or []]
    assert any("путь закрыт" in c for c in feed)
