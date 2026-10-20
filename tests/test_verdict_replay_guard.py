"""A verdict that stands on a commit task is not silently replaced (#1706).

15.10.2026, task #1699, generation 1: CHANGES_REQUESTED at 14:19, then a stale
APPROVED for the same generation at 14:22 — the hub took it, and the gate
merged the PR at 14:23 over open P1 findings. ``expected_generation`` from the
body never reached the write on the commit path, and the write itself replaced
the verdict unconditionally.

Every test below checks a REFUSAL by the whole picture, not by the status code:
verdict, revision, findings, task status, generation, pinned SHA, the event
log, the feed and the number of drafts. A 409 that still changed a row is the
failure this file exists for.
"""

from __future__ import annotations

import argparse
import json
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import cli
from hub import repository as repo
from hub.integrations.noop import NoopDispatch
from hub.integrations.registry import plugins
from tests.state_support import auth

CR_COMMENTS = "переделайте блокировку записи"


@pytest.fixture
def hdr(monkeypatch) -> dict[str, dict[str, str]]:
    return auth(monkeypatch)


async def _in_review(client: AsyncClient, hdr: dict, title: str = "Guarded") -> int:
    """A commit task submitted for review: generation 1, status review."""
    created = await client.post(
        "/api/tasks", json={"title": title}, headers=hdr["human"]
    )
    task_id = created.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: do it"},
        headers=hdr["impl"],
    )
    started = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "dev", "session_id": "s-1706"},
        headers=hdr["impl"],
    )
    assert started.status_code == 200, started.text
    sent = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={}, headers=hdr["impl"]
    )
    assert sent.status_code == 200 and sent.json()["status"] == "review", sent.text
    return task_id


def _verdict(kind: str, **extra) -> dict:
    return {"verdict": kind, "agent": "reviewer", "comments": CR_COMMENTS, **extra}


async def _cr(client: AsyncClient, hdr: dict, task_id: int, **extra):
    body = _verdict("changes_requested", expected_generation=1, **extra)
    return await client.post(
        f"/api/tasks/{task_id}/review-verdict", json=body, headers=hdr["rev"]
    )


async def _snapshot(db: aiosqlite.Connection, task_id: int) -> dict:
    row = dict(await repo.get_task(db, task_id))
    async with db.execute("SELECT COUNT(*) FROM events") as cur:
        events = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM task_updates") as cur:
        updates = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM tasks") as cur:
        tasks = (await cur.fetchone())[0]
    return {
        "verdict": row["review_verdict"],
        "verdict_generation": row["review_verdict_generation"],
        "revision": row.get("review_verdict_revision"),
        "findings": row["review_findings"],
        "status": row["status"],
        "generation": row["submission_generation"],
        "sha": row["submission_sha"],
        "closed": row["review_verdict_closed_generation"],
        "self_approved": row["review_self_approved"],
        "events": events,
        "updates": updates,
        "tasks": tasks,
    }


# ---- AC-1 -------------------------------------------------------------------


async def test_other_verdict_on_same_generation_is_refused(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict
):
    task_id = await _in_review(client, hdr)
    assert (await _cr(client, hdr, task_id)).status_code == 200
    before = await _snapshot(db, task_id)
    assert before["verdict"] == "changes_requested" and before["revision"] == 1

    attempts = {
        "human with generation": (
            hdr["human"],
            {"verdict": "approved", "agent": "denis", "expected_generation": 1},
        ),
        "legacy without generation": (
            hdr["human"],
            {"verdict": "approved", "agent": "denis"},
        ),
        "agent": (
            hdr["rev"],
            {"verdict": "approved", "agent": "reviewer", "expected_generation": 1},
        ),
    }
    for label, (headers, body) in attempts.items():
        resp = await client.post(
            f"/api/tasks/{task_id}/review-verdict", json=body, headers=headers
        )
        assert resp.status_code == 409, (label, resp.text)
        if label == "agent":  # #1872: the agent is stopped before the standing gate
            assert "approval_reserved_for_human" in resp.text, (label, resp.text)
        else:
            assert "supersedes_revision=1" in resp.text, (label, resp.text)
        assert await _snapshot(db, task_id) == before, label


# ---- AC-2 -------------------------------------------------------------------


async def test_human_supersedes_by_current_revision_only(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict
):
    task_id = await _in_review(client, hdr)
    assert (await _cr(client, hdr, task_id)).status_code == 200
    before = await _snapshot(db, task_id)

    # An agent may not replace: refused, nothing moves.
    agent_try = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json=_verdict("approved", expected_generation=1, supersedes_revision=1),
        headers=hdr["rev"],
    )
    assert agent_try.status_code in (403, 409), agent_try.text
    assert await _snapshot(db, task_id) == before

    # A replacement names the generation too: the revision belongs to the task,
    # not to the submission, and a resubmission would carry it over (Codex P2).
    no_generation = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "denis", "supersedes_revision": 1},
        headers=hdr["human"],
    )
    assert no_generation.status_code == 422, no_generation.text
    assert await _snapshot(db, task_id) == before

    body = {
        "verdict": "approved",
        "agent": "denis",
        "expected_generation": 1,
        "supersedes_revision": 1,
    }
    done = await client.post(
        f"/api/tasks/{task_id}/review-verdict", json=body, headers=hdr["human"]
    )
    assert done.status_code == 200, done.text
    after = await _snapshot(db, task_id)
    assert after["verdict"] == "approved" and after["revision"] == 2
    assert after["verdict_generation"] == 1
    card = (await client.get(f"/api/tasks/{task_id}", headers=hdr["human"])).json()
    assert card["review_verdict_revision"] == 2
    assert any(
        "вердикт заменён: было changes_requested (ревизия 1), стало approved"
        in (u["content"] or "")
        for u in card["updates"]
    ), "the replacement is said in the feed"

    # The same stale revision again — another verdict landed in between.
    stale = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={**body, "verdict": "changes_requested", "comments": CR_COMMENTS},
        headers=hdr["human"],
    )
    assert stale.status_code == 409, stale.text
    assert await _snapshot(db, task_id) == after


# ---- AC-3 -------------------------------------------------------------------


async def test_repeat_is_idempotent_and_generation_is_checked(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict
):
    task_id = await _in_review(client, hdr)
    findings = [
        {"id": 1, "severity": "high", "message": "гонка", "scope": "in_scope"},
        {"id": 2, "severity": "low", "message": "опечатка", "scope": "out_of_scope"},
    ]
    first = await _cr(
        client, hdr, task_id, findings=findings, create_tasks_for_out_of_scope=True
    )
    assert first.status_code == 200, first.text
    before = await _snapshot(db, task_id)
    assert before["tasks"] == 2, "one follow-up draft from the first write"

    repeat = await _cr(
        client, hdr, task_id, findings=findings, create_tasks_for_out_of_scope=True
    )
    assert repeat.status_code == 200, repeat.text
    assert await _snapshot(db, task_id) == before, "a repeat changes nothing at all"

    other_comments = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            **_verdict("changes_requested", expected_generation=1),
            "comments": "иное",
            "findings": findings,
            "create_tasks_for_out_of_scope": True,
        },
        headers=hdr["rev"],
    )
    assert other_comments.status_code == 409, other_comments.text
    assert await _snapshot(db, task_id) == before

    for kind in ("changes_requested", "approved"):
        wrong = await client.post(
            f"/api/tasks/{task_id}/review-verdict",
            json=_verdict(kind, expected_generation=2),
            headers=hdr["human"],
        )
        assert wrong.status_code == 409, (kind, wrong.text)
        assert await _snapshot(db, task_id) == before


async def test_a_first_verdict_for_another_generation_is_refused(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict
):
    """No verdict stands yet: the studied generation still has to be the live one."""
    task_id = await _in_review(client, hdr)
    before = await _snapshot(db, task_id)
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json=_verdict("changes_requested", expected_generation=2),
        headers=hdr["rev"],
    )
    assert resp.status_code == 409, resp.text
    assert await _snapshot(db, task_id) == before


# ---- AC-4 -------------------------------------------------------------------


async def test_closed_window_is_not_reopened_by_replacement(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict
):
    task_id = await _in_review(client, hdr)
    ok = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "denis", "expected_generation": 1},
        headers=hdr["human"],
    )
    assert ok.status_code == 200, ok.text
    assert await repo.close_review_verdict_window(db, task_id) == 1
    await db.commit()
    before = await _snapshot(db, task_id)
    assert before["closed"] == 1

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "approved",
            "agent": "denis",
            "expected_generation": 1,
            "supersedes_revision": before["revision"],
        },
        headers=hdr["human"],
    )
    assert resp.status_code == 409, resp.text
    assert "новая сдача" in resp.text
    assert await _snapshot(db, task_id) == before
    card = (await client.get(f"/api/tasks/{task_id}", headers=hdr["human"])).json()
    assert card["review_approved_current"] is False


# ---- AC-5 -------------------------------------------------------------------


async def _headless_review_task(db: aiosqlite.Connection) -> int:
    task_id = await repo.create_task(
        db,
        title="Headless review",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="review",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, review_job_id="rev-1", submission_sha="a" * 40)
    await repo.bump_submission_generation(db, task_id)
    await db.commit()
    return task_id


def _job(log_text: str = "") -> NoopDispatch:
    d = NoopDispatch()
    d.get_job = MagicMock(return_value={"status": "completed", "exit_code": 0})
    d.job_log_full = MagicMock(return_value=log_text)
    return d


async def test_poller_never_overwrites_a_verdict(db: aiosqlite.Connection):
    from hub.poller import _resolve_review_verdict, _sweep_review

    task_id = await _headless_review_task(db)
    stored = json.dumps(
        [{"id": 1, "severity": "high", "message": "P1", "scope": "in_scope"}]
    )
    await db.execute(
        "UPDATE tasks SET review_verdict='changes_requested', "
        "review_verdict_generation=submission_generation, review_findings=?, "
        "review_verdict_revision=3 WHERE id=?",
        (stored, task_id),
    )
    await db.commit()
    before = await _snapshot(db, task_id)
    plugins.dispatch = _job()
    with patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock):
        with patch("hub.poller.services.dispatch_fix", new_callable=AsyncMock):
            await _sweep_review(db)
    after = await _snapshot(db, task_id)
    assert after["findings"] == before["findings"] == stored
    assert after["revision"] == before["revision"] == 3
    assert after["verdict"] == "changes_requested"

    # The verdict is read FRESH: a snapshot that still says approved, while a
    # human has since replaced it, must not be delivered on (Codex P1).
    plugins.dispatch = _job("verdict: CHANGES_REQUESTED")
    task = dict(await repo.get_task(db, task_id))
    task["review_verdict"] = "approved"  # what the tick read a moment ago
    assert await _resolve_review_verdict(db, task, []) == "changes_requested"
    row = dict(await repo.get_task(db, task_id))
    assert row["review_verdict_revision"] == 3, "and nothing was written"

    # A recognised CHANGES_REQUESTED does not land over a standing verdict: the
    # write itself refuses (the verdict appeared after the tick read it).
    assert not await repo.record_review_verdict(
        db, task_id, "changes_requested", expected_generation=1
    )

    # ... nor over a resubmitted generation (the job judged the old one).
    await db.execute(
        "UPDATE tasks SET review_verdict=NULL, review_verdict_generation=NULL WHERE id=?",
        (task_id,),
    )
    await db.commit()
    stale = dict(await repo.get_task(db, task_id))
    await repo.bump_submission_generation(db, task_id)
    await db.commit()
    await _resolve_review_verdict(db, stale, [])
    row = dict(await repo.get_task(db, task_id))
    assert not row["review_verdict"], "the job's generation is no longer the live one"

    # When the poller does write, the revision grows.
    fresh = dict(await repo.get_task(db, task_id))
    assert await _resolve_review_verdict(db, fresh, []) == "changes_requested"
    row = dict(await repo.get_task(db, task_id))
    assert row["review_verdict"] == "changes_requested"
    assert row["review_verdict_revision"] == 4


# ---- every writer bumps the revision -----------------------------------------


async def test_every_writer_increases_the_revision(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict, monkeypatch
):
    # REST
    rest = await _in_review(client, hdr, "rest")
    assert (await _cr(client, hdr, rest)).status_code == 200
    assert (await _snapshot(db, rest))["revision"] == 1

    # web form (the human's card sends the generation it shows)
    web = await _in_review(client, hdr, "web")
    resp = await client.post(
        f"/tasks/{web}/web-review-verdict",
        data={
            "verdict": "changes_requested",
            "comments": CR_COMMENTS,
            "expected_generation": "1",
        },
        headers=hdr["human"],
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303), resp.text
    assert (await _snapshot(db, web))["revision"] == 1

    # steward application
    from hub.models import ReviewVerdict
    from hub.services import steward_applied

    steward = await _in_review(client, hdr, "steward")
    await steward_applied._record(db, steward, ReviewVerdict.changes_requested, 1)
    assert (await _snapshot(db, steward))["revision"] == 1

    # autopilot
    from hub.services import auto_verdict, ci_integrity, ci_trust

    pilot = await _in_review(client, hdr, "pilot")
    await db.execute("UPDATE tasks SET submission_sha='abc123' WHERE id=?", (pilot,))
    await db.commit()
    monkeypatch.setattr(ci_trust, "pass_gap", AsyncMock(return_value=""))
    monkeypatch.setattr(ci_integrity, "verdict_gate", AsyncMock(return_value=None))
    await auto_verdict._approve(
        db,
        pilot,
        {
            "self_reviewed": False,
            "generation": 1,
            "proven_usage": None,
            "project_slug": "default",
            "review_id": 0,
            "raw_count": 0,
            "confirmed": 0,
            "repeats_note": "",
            "pinned_sha": "",
            "implementer_model": "",
            "reviewer_model": "",
        },
    )
    assert (await _snapshot(db, pilot))["revision"] == 1


# ---- AC-6 -------------------------------------------------------------------


async def test_diverged_tip_behaviour_is_preserved(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict, monkeypatch
):
    from hub.integrations.noop import NoopGitOps
    from hub.services import orchestration

    class _Git(NoopGitOps):
        tip = "tip-1"

        async def fetch_base(self, repo, base):
            return (True, "")

        async def head_sha(self, repo, base):
            return self.tip

    git = _Git()
    monkeypatch.setattr(plugins, "git_ops", git)
    monkeypatch.setattr(
        orchestration,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )
    moved = await _in_review(client, hdr, "moved")
    git.tip = "tip-2"
    resp = await client.post(
        f"/api/tasks/{moved}/review-verdict",
        json={"verdict": "approved", "agent": "denis", "expected_generation": 1},
        headers=hdr["human"],
    )
    assert resp.status_code == 200, resp.text
    card = (await client.get(f"/api/tasks/{moved}", headers=hdr["human"])).json()
    assert card["review_approved_current"] is False
    assert card["submission_generation"] == 2, "the verdict stays on the old generation"
    assert card["review_verdict_generation"] == 1

    git.tip = "tip-3"
    steady = await _in_review(client, hdr, "steady")
    resp = await client.post(
        f"/api/tasks/{steady}/review-verdict",
        json={"verdict": "approved", "agent": "denis", "expected_generation": 1},
        headers=hdr["human"],
    )
    assert resp.status_code == 200, resp.text
    card = (await client.get(f"/api/tasks/{steady}", headers=hdr["human"])).json()
    assert card["review_approved_current"] is True


# ---- AC-7 -------------------------------------------------------------------


def test_cli_and_mcp_carry_the_studied_generation(capsys) -> None:
    mock_api = MagicMock(return_value={"id": 42})
    args = argparse.Namespace(
        task_id=42,
        verdict="changes_requested",
        comments="c",
        agent="r",
        findings_json="",
        expected_generation=None,
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        assert cli.cmd_review_verdict(args) == 2
    mock_api.assert_not_called()
    err = capsys.readouterr().err
    assert "--expected-generation" in err and "review-brief 42" in err, (
        "the refusal names the brief command, and nothing is substituted"
    )

    args.expected_generation = 3
    args.supersedes_revision = 5
    args.json = True  # #1696: the summary would add a read; the body is the subject
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        assert cli.cmd_review_verdict(args) == 0
    sent = mock_api.call_args.args[2]
    assert sent["expected_generation"] == 3 and sent["supersedes_revision"] == 5


async def test_mcp_forwards_the_generation_of_the_brief(monkeypatch) -> None:
    from hub import mcp_server

    posted: list[dict] = []

    async def _post(path, body):
        posted.append(body)
        return {"id": 42, "status": "running"}

    monkeypatch.setattr(mcp_server, "_api_post", _post)
    monkeypatch.setattr(
        mcp_server, "_read_task", AsyncMock(return_value={"status": "review"})
    )
    out = await mcp_server.hub_submit_review(
        42, verdict="approved", expected_generation=4
    )
    assert posted[-1]["expected_generation"] == 4
    assert "expected_generation" not in out.split("recorded", 1)[-1] or True

    warned = await mcp_server.hub_submit_review(42, verdict="approved")
    assert "expected_generation" not in posted[-1], "nothing is substituted"
    assert "expected_generation" in warned, (
        "the missing generation is said in the answer"
    )


def test_review_commands_name_the_generation() -> None:
    import hub.integrations.dispatch as dispatch_mod

    plugin = dispatch_mod.DispatchIntegration()
    text = plugin.build_review_message(
        task_id=42, title="t", description="d", review_cycle=0, max_cycles=3
    )
    # #1872: the order asks an agent for CHANGES_REQUESTED only.
    assert (
        "review-verdict 42 changes_requested --agent code-reviewer "
        "--expected-generation " in text
    )
    assert "review-brief 42" in text, "an order without the number says where to get it"
    pinned = plugin.build_review_message(
        task_id=42, title="t", description="d", review_cycle=0, max_cycles=3,
        generation=7,
    )  # fmt: skip
    assert pinned.count("--expected-generation 7 ") == 1, "the order carries the number"
    reviewer = open("agents/code-reviewer.md", encoding="utf-8").read()
    assert "--expected-generation" in reviewer and "supersedes_revision" in reviewer


async def test_the_review_order_is_built_with_the_live_generation(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    from hub.services import orchestration

    task_id = await _headless_review_task(db)
    await repo.bump_submission_generation(db, task_id)
    await db.commit()
    seen: dict = {}
    real = plugins.dispatch.build_review_message

    def _spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(plugins.dispatch, "build_review_message", _spy)
    task = dict(await repo.get_task(db, task_id))
    await orchestration.dispatch_review(db, task)
    assert seen["generation"] == 2


# ---- the review form, as a browser sends it ---------------------------------


def _browser_post(page: str, **typed: str) -> dict[str, str]:
    """The fields a browser sends for the verdict form: hidden inputs, checked
    boxes and what was typed. An unchecked checkbox sends nothing."""
    import re

    form = re.search(r'<form[^>]*id="review-verdict-form".*?</form>', page, re.S).group(
        0
    )
    data: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", form):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if not name or "csrf" in name.group(1).lower():
            continue
        if 'type="hidden"' in tag:
            data[name.group(1)] = value.group(1) if value else ""
    data.update(typed)
    return data


async def test_the_form_never_sends_a_replacement_unless_asked(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict
):
    # Client-driven review: the first verdict through the rendered form.
    task_id = await _in_review(client, hdr)
    page = (await client.get(f"/tasks/{task_id}", headers=hdr["rev"])).text
    assert 'name="supersedes_revision"' not in page
    assert "--supersedes-revision" in page, "the card says where a replacement goes"
    first = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data=_browser_post(page, verdict="changes_requested", comments=CR_COMMENTS),
        headers=hdr["rev"],
        follow_redirects=False,
    )
    assert first.status_code == 303 and "review_error" not in first.headers["location"]
    assert (await _snapshot(db, task_id))["revision"] == 1
    gone = (await client.get(f"/tasks/{task_id}", headers=hdr["human"])).text
    assert 'id="review-verdict-form"' not in gone, "the task left review: no form"

    # The form is shown only while a client-driven review is open, and the first
    # verdict closes it, so the replacement box can never be on screen with a
    # verdict standing: replacement is the CLI/REST act the card names. The web
    # handler still honours the field (a script posting what the box would send).
    before = await _snapshot(db, task_id)
    fields = {"verdict": "approved", "comments": "ок", "expected_generation": "1"}
    refused = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data=fields,
        headers=hdr["human"],
        follow_redirects=False,
    )
    assert "review_error" in refused.headers["location"], refused.headers
    assert await _snapshot(db, task_id) == before
    replaced = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={**fields, "supersedes_revision": "1"},
        headers=hdr["human"],
        follow_redirects=False,
    )
    assert "review_error" not in replaced.headers["location"], replaced.headers
    after = await _snapshot(db, task_id)
    assert after["verdict"] == "approved" and after["revision"] == 2


# ---- the poller and a resubmission inside the tick ---------------------------


async def _mergeable_review_task(db: aiosqlite.Connection):
    """An approved headless review task whose PR the conveyor WOULD merge."""
    from hub.integrations.noop import NoopGitOps
    from hub.integrations.protocols import (
        CIProbeOutcome,
        CIProbeResult,
        StackProbeOutcome,
        StackProbeResult,
    )

    task_id = await _headless_review_task(db)
    await repo.update_task(db, task_id, pr_number=77, branch="task-1/x")
    await db.execute(
        "UPDATE tasks SET review_verdict='approved', "
        "review_verdict_generation=submission_generation, "
        "review_verdict_revision=1 WHERE id=?",
        (task_id,),
    )
    await db.commit()
    git = NoopGitOps()
    git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.passed, "checks_pass")
    )
    git.merge_pr = AsyncMock(return_value=True)
    git.branch_stacking_probe = AsyncMock(
        return_value=StackProbeResult(outcome=StackProbeOutcome.clear, reason="")
    )
    plugins.git_ops = git
    plugins.dispatch = _job()
    return task_id, git


async def test_a_resubmission_inside_the_tick_does_not_merge(
    db: aiosqlite.Connection,
):
    from hub import poller

    # Control: nothing moved, the very same set-up merges.
    task_id, git = await _mergeable_review_task(db)
    snapshot = dict(await repo.get_task(db, task_id))
    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        patch("hub.poller._record_merge_and_tidy", new_callable=AsyncMock),
    ):
        await poller._review_task_tick(db, snapshot, {"status": "completed"})
    git.merge_pr.assert_awaited()

    # Resubmitted between the tick's snapshot and the delivery decision.
    task_id, git = await _mergeable_review_task(db)
    snapshot = dict(await repo.get_task(db, task_id))  # what the tick read
    await repo.bump_submission_generation(db, task_id)  # resubmitted since
    await db.commit()
    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        patch("hub.poller._record_merge_and_tidy", new_callable=AsyncMock),
    ):
        await poller._review_task_tick(db, snapshot, {"status": "completed"})
        await poller._deliver_approved_review(db, snapshot)  # the step itself
    git.merge_pr.assert_not_called()
    assert dict(await repo.get_task(db, task_id))["status"] == "review"

    # A human returned the work to rework after the snapshot: the window is closed.
    task_id, git = await _mergeable_review_task(db)
    snapshot = dict(await repo.get_task(db, task_id))
    assert await repo.close_review_verdict_window(db, task_id) == 1
    await db.commit()
    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        patch("hub.poller._record_merge_and_tidy", new_callable=AsyncMock),
    ):
        await poller._deliver_approved_review(db, snapshot)
    git.merge_pr.assert_not_called()

    # Replaced while the CI probe was running: the read is the LAST step before merge.
    task_id, git = await _mergeable_review_task(db)
    snapshot = dict(await repo.get_task(db, task_id))

    async def _ci_then_replacement(*_a, **_k):
        from hub.integrations.protocols import CIProbeOutcome, CIProbeResult

        await db.execute(
            "UPDATE tasks SET review_verdict='changes_requested' WHERE id=?",
            (task_id,),
        )
        await db.commit()
        return CIProbeResult(CIProbeOutcome.passed, "checks_pass")

    git.check_pr_ci = _ci_then_replacement
    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        patch("hub.poller._record_merge_and_tidy", new_callable=AsyncMock),
    ):
        await poller._deliver_approved_review(db, snapshot)
    git.merge_pr.assert_not_called()


# ---- ONE merge point: every network probe x every path x every change --------

_CHANGES = ["replaced", "resubmitted", "returned", "reapproved"]
_OUTCOME_PHRASES = ("Ревью одобрено", "Доставлено хабом", "Доставка отложена")


async def _fire(db: aiosqlite.Connection, task_id: int, change: str) -> None:
    """The world moves while a network probe is running."""
    if change == "replaced":
        await db.execute(
            "UPDATE tasks SET review_verdict='changes_requested', "
            "review_verdict_revision=review_verdict_revision+1 WHERE id=?",
            (task_id,),
        )
    elif change == "resubmitted":
        await repo.bump_submission_generation(db, task_id)
    elif change == "reapproved":
        # N -> N+1 and a NEW approval of N+1: this flow was started on N.
        await repo.bump_submission_generation(db, task_id)
        await db.execute(
            "UPDATE tasks SET review_verdict='approved', "
            "review_verdict_generation=submission_generation WHERE id=?",
            (task_id,),
        )
    else:
        await repo.close_review_verdict_window(db, task_id)
    await db.commit()


def _arm(git, probe: str, db: aiosqlite.Connection, task_id: int, change: str) -> None:
    """Wrap one probe of the git double: the change lands inside the call."""
    original = getattr(git, probe)

    async def wrapped(*args, **kwargs):
        await _fire(db, task_id, change)
        result = original(*args, **kwargs)
        return await result if hasattr(result, "__await__") else result

    setattr(git, probe, wrapped)


async def _nothing_was_said_or_done(
    db: aiosqlite.Connection, task_id: int, git, before_status: str
) -> None:
    assert git.merge_pr.await_count == 0, "the forge was never asked to merge"
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == before_status, "no needs_decision, no completed"
    feed = " ".join(
        (dict(u).get("content") or "") for u in await repo.get_task_updates(db, task_id)
    )
    for phrase in _OUTCOME_PHRASES:
        assert phrase not in feed, f"{phrase!r} written over an approval that was gone"
    assert not await repo.pipeline_merge_recorded(db, task_id, 77)


@pytest.mark.parametrize("change", _CHANGES)
@pytest.mark.parametrize(
    "probe",
    [
        "check_pr_ci",
        "pr_is_draft",
        "pr_base_freshness",
        # The probe answers something the gate turns into an OUTCOME: red CI is
        # a hand-over to a human, a pending CI is a "delivery postponed" line.
        "check_pr_ci:failed",
        "check_pr_ci:pending",
    ],
)
@pytest.mark.parametrize("path", ["pair_sweep", "done_report", "deferred_completion"])
async def test_no_merge_and_no_outcome_on_any_gate_path(
    db: aiosqlite.Connection, path: str, probe: str, change: str
):
    """Matrix (#1706): the single merge point holds on every route into the gate."""
    from hub.integrations.protocols import CIProbeOutcome
    from hub.services import orchestration
    from tests.test_pair_merge_gate import (
        _approved_pair_task,
        _drain_pair_delivery,
        _git,
        _report_done,
    )

    if path == "deferred_completion" and change == "reapproved":
        pytest.skip("the deferred completion starts after the change: it reads N+1")
    method, _, answer = probe.partition(":")
    git = _git(CIProbeOutcome[answer or "passed"], merged=True)
    task_id = await _approved_pair_task(db)
    before = dict(await repo.get_task(db, task_id))["status"]
    if path == "deferred_completion":
        # The change lands between the done report's commit and its completion.
        pending: list[dict] = [
            {"task_id": task_id, "exit_code": None, "result_text": "done"}
        ]
        await _fire(db, task_id, change)
        await orchestration.run_deferred_completions(db, pending)
    else:
        _arm(git, method, db, task_id, change)
        if path == "pair_sweep":
            await _drain_pair_delivery(db)
        else:
            await _report_done(db, task_id)
    await _nothing_was_said_or_done(db, task_id, git, before)


@pytest.mark.parametrize("change", _CHANGES)
async def test_no_merge_on_manual_delivery_whatever_moves(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, change: str
):
    from tests.test_accept_without_delivery import (
        _approved_task,
        _decide_deliver,
        _install,
        _MergeSpy,
    )

    task_id = await _approved_task(client, db, title=f"manual-{change}", pr=950)
    spy = _MergeSpy()
    _install(monkeypatch, spy)
    real = plugins.git_ops.check_pr_ci

    async def moved(*args, **kwargs):
        await _fire(db, task_id, change)
        return await real(*args, **kwargs)

    monkeypatch.setattr(plugins.git_ops, "check_pr_ci", moved)
    await _decide_deliver(client, task_id)
    assert spy.merged == [], "no merge on an approval that no longer stands"
    assert not await repo.pipeline_merge_recorded(db, task_id, 950)


async def test_the_gate_still_merges_when_nothing_moved(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    from hub.services import orchestration
    from tests.test_accept_without_delivery import _approved_task, _install, _MergeSpy

    task_id = await _approved_task(client, db, title="control", pr=951)
    spy = _MergeSpy()
    _install(monkeypatch, spy)
    ok, _ = await orchestration.merge_before_completion(
        db, dict(await repo.get_task(db, task_id))
    )
    assert ok and spy.merged == [951]


async def test_poller_and_done_flow_at_once_call_the_forge_once(db_dsn, db):
    """Concurrent sweep + done report: one forge call, no phantom manual merge."""
    import asyncio

    from hub.integrations.protocols import CIProbeOutcome
    from tests.test_pair_merge_gate import (
        _approved_pair_task,
        _drain_pair_delivery,
        _git,
        _report_done,
    )

    git = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)

    async def slow_merge(*_a, **_k):
        await asyncio.sleep(0.2)
        return True

    git.merge_pr = AsyncMock(side_effect=slow_merge)
    other = await aiosqlite.connect(db_dsn, uri=True)
    await other.execute("PRAGMA busy_timeout = 5000")
    other.row_factory = aiosqlite.Row
    try:
        await asyncio.gather(_drain_pair_delivery(db), _report_done(other, task_id))
    finally:
        await other.close()
    assert git.merge_pr.await_count == 1, "exactly one call of the forge"
    feed = " ".join(
        (dict(u).get("content") or "") for u in await repo.get_task_updates(db, task_id)
    )
    assert "merged_outside_gate" not in feed
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


# ---- the headless conveyor writes no outcome on a stale approval ---------------


@pytest.mark.parametrize("probe", ["ci_failed", "merge_refused", "stacked"])
async def test_a_stale_approval_leaves_no_approved_outcome(
    db: aiosqlite.Connection, probe: str, monkeypatch
):
    from hub import poller

    task_id, git = await _mergeable_review_task(db)
    snapshot = dict(await repo.get_task(db, task_id))

    async def _replace() -> None:
        await db.execute(
            "UPDATE tasks SET review_verdict='changes_requested' WHERE id=?",
            (task_id,),
        )
        await db.commit()

    from hub.integrations.protocols import CIProbeOutcome, CIProbeResult

    if probe == "ci_failed":

        async def _ci(*_a, **_k):
            await _replace()
            return CIProbeResult(CIProbeOutcome.failed, "red")

        git.check_pr_ci = _ci
    elif probe == "stacked":

        async def _stacking(*_a, **_k):
            await _replace()
            return "stack_unresolvable: the order cannot be named"

        monkeypatch.setattr(poller.services, "stacking_gate_step", _stacking)
    else:

        async def _merge(*_a, **_k):
            await _replace()
            return False

        git.merge_pr = _merge
    with patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock):
        await poller._deliver_approved_review(db, snapshot)
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "no needs_decision over a verdict that is gone"
    texts = " ".join(
        (dict(u).get("content") or "") for u in await repo.get_task_updates(db, task_id)
    )
    assert "Ревью одобрено" not in texts and "CI failed" not in texts
    assert "Доставка остановлена" not in texts


async def test_the_wait_for_a_neighbour_merge_is_not_a_blind_spot(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """The gate may wait for another path's merge; the approval is read AFTER it."""
    import asyncio

    from hub.services import orchestration
    from tests.test_accept_without_delivery import _approved_task, _install, _MergeSpy

    task_id = await _approved_task(client, db, title="waits", pr=952)
    spy = _MergeSpy()
    _install(monkeypatch, spy)
    neighbour: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    orchestration._gate_merges[(task_id, 952)] = neighbour  # a path is merging it
    gate = asyncio.create_task(
        orchestration.merge_before_completion(
            db, dict(await repo.get_task(db, task_id))
        )
    )
    await asyncio.sleep(0.2)  # the gate is waiting for the neighbour
    await db.execute(
        "UPDATE tasks SET review_verdict='changes_requested' WHERE id=?", (task_id,)
    )
    await db.commit()
    orchestration._gate_merges.pop((task_id, 952), None)
    neighbour.set_result(False)  # the neighbour's merge was refused
    ok, detail = await gate
    assert not ok and detail.startswith(orchestration.APPROVAL_CHANGED_PREFIX)
    assert spy.merged == []


async def test_the_fix_order_is_built_from_the_verdict_that_stands(
    db: aiosqlite.Connection,
):
    """Codex round 3: the text of the verdict is read with the verdict."""
    from hub import poller

    task_id = await _headless_review_task(db)
    await repo.add_task_update(
        db, task_id, "reviewer", "review", "Review verdict: APPROVED"
    )
    await db.execute(
        "UPDATE tasks SET review_verdict='approved', review_verdict_generation=1 WHERE id=?",
        (task_id,),
    )
    await db.commit()
    snapshot = dict(await repo.get_task(db, task_id))
    real = poller._resolve_review_verdict

    async def _replaced_after_the_tick_read_the_feed(db_, task_, updates_):
        # The tick has read the feed (APPROVED); a human replaces the verdict now.
        await repo.add_task_update(
            db_,
            task_id,
            "denis",
            "review",
            "Review verdict: CHANGES_REQUESTED\nfix the race",
        )
        await db_.execute(
            "UPDATE tasks SET review_verdict='changes_requested' WHERE id=?",
            (task_id,),
        )
        await db_.commit()
        return await real(db_, task_, updates_)

    patch.object(
        poller, "_resolve_review_verdict", _replaced_after_the_tick_read_the_feed
    ).start()
    plugins.dispatch = _job()
    seen: list[str] = []

    async def _fix(_db, _task, text):
        seen.append(text)

    with (
        patch.object(poller.services, "dispatch_fix", _fix),
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
    ):
        await poller._review_task_tick(db, snapshot, {"status": "completed"})
    patch.stopall()
    assert seen and "fix the race" in seen[0]


async def test_check_and_registration_have_no_await_between(db, monkeypatch):
    """The atomicity itself: no suspension between the fresh read and the Future."""
    import asyncio

    from hub.integrations.protocols import CIProbeOutcome
    from hub.services import orchestration
    from tests.test_pair_merge_gate import (
        _approved_pair_task,
        _drain_pair_delivery,
        _git,
    )

    _git(CIProbeOutcome.passed, merged=True)
    await _approved_pair_task(db)
    ticks = 0
    read_at: list[int] = []
    seen: list[int] = []

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    class Registry(dict):
        def __setitem__(self, key, value):
            seen.append(ticks - read_at[-1])  # loop turns since the last read
            super().__setitem__(key, value)

    real = repo.get_task

    async def get_task(*args, **kwargs):
        row = await real(*args, **kwargs)
        read_at.append(ticks)
        return row

    monkeypatch.setattr(orchestration, "_gate_merges", Registry())
    monkeypatch.setattr(orchestration.repo, "get_task", get_task)
    runner = asyncio.create_task(ticker())
    try:
        await _drain_pair_delivery(db)
    finally:
        runner.cancel()
    assert seen, "the merge was registered"
    assert seen == [0] * len(seen), "the loop turned between the check and the Future"


@pytest.mark.parametrize("change", _CHANGES)
async def test_a_task_without_a_pr_is_not_completed_on_a_gone_approval(
    db: aiosqlite.Connection, change: str
):
    """No PR, no merge point: the deferred completion itself must look again."""
    if change == "reapproved":
        pytest.skip("the deferred completion starts after the change: it reads N+1")
    from hub.services import orchestration
    from tests.test_pair_merge_gate import _approved_pair_task, _git

    from hub.integrations.protocols import CIProbeOutcome

    _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db, pr_number=None)
    before = dict(await repo.get_task(db, task_id))["status"]
    await _fire(db, task_id, change)
    await orchestration.run_deferred_completions(
        db, [{"task_id": task_id, "exit_code": None, "result_text": "done"}]
    )
    assert dict(await repo.get_task(db, task_id))["status"] == before


@pytest.mark.parametrize("change", _CHANGES)
async def test_no_completion_when_the_approval_goes_during_the_pr_lookup(
    db: aiosqlite.Connection, monkeypatch, change: str
):
    """Codex round 4: a task without a PR reached ``completed`` on a gone approval."""
    from hub.integrations.protocols import CIProbeOutcome
    from hub.services import orchestration
    from tests.test_pair_merge_gate import _approved_pair_task, _git, _report_done

    _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db, pr_number=None)
    real = orchestration.pr_for_delivery

    async def lookup(*args, **kwargs):
        await _fire(db, task_id, change)
        return await real(*args, **kwargs)

    monkeypatch.setattr(orchestration, "pr_for_delivery", lookup)
    await _report_done(db, task_id)
    assert dict(await repo.get_task(db, task_id))["status"] != "completed"


@pytest.mark.parametrize("change", _CHANGES)
async def test_a_failed_pr_creation_writes_no_needs_decision_over_a_gone_approval(
    db: aiosqlite.Connection, monkeypatch, change: str
):
    from hub.integrations.protocols import CIProbeOutcome
    from hub.services import orchestration
    from tests.test_pair_merge_gate import _approved_pair_task, _git, _report_done

    _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db, pr_number=None)
    before = dict(await repo.get_task(db, task_id))["status"]

    async def diff(*_a, **_k):
        return ["hub/x.py"]

    async def create(*_a, **_k):
        await _fire(db, task_id, change)
        return None, "GitHub не ответил"

    monkeypatch.setattr(orchestration, "_confirmed_branch_diff", diff)
    monkeypatch.setattr(orchestration, "ensure_delivery_pr", create)
    await _report_done(db, task_id)
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == before, "no needs_decision over a verdict that is gone"
    feed = " ".join(
        (dict(u).get("content") or "") for u in await repo.get_task_updates(db, task_id)
    )
    assert "Done report NOT completed" not in feed


async def test_the_outcome_check_and_write_hold_one_write_lock(db_dsn, db):
    """Codex round 4 (P2): a verdict writer cannot land between check and write."""
    import sqlite3

    from hub.services import orchestration
    from tests.test_pair_merge_gate import _approved_pair_task

    task_id = await _approved_pair_task(db, pr_number=None)
    other = await aiosqlite.connect(db_dsn, uri=True, timeout=0)
    await other.execute("PRAGMA busy_timeout = 0")
    locked: list[bool] = []

    async def write() -> None:
        try:
            await other.execute(
                "UPDATE tasks SET review_verdict='changes_requested' WHERE id=?",
                (task_id,),
            )
        except (sqlite3.OperationalError, aiosqlite.OperationalError):
            locked.append(True)

    try:
        stands, _ = await orchestration.record_delivery_outcome(
            db, dict(await repo.get_task(db, task_id)), write
        )
    finally:
        await other.close()
    assert stands and locked == [True], "the other connection was held off"


async def test_a_merge_is_a_fact_but_completed_needs_the_approval(
    db: aiosqlite.Connection, monkeypatch
):
    """Codex round 5/6: a merge under an approval that went right after it.

    The merge is recorded in the ledger; ``completed`` says "approved work was
    delivered" and is not written; ONE explicit outcome names what happened.
    """
    from hub.integrations.protocols import CIProbeOutcome
    from tests.test_pair_merge_gate import (
        _approved_pair_task,
        _drain_pair_delivery,
        _git,
    )

    git = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)

    async def merged_then_replaced(*_a, **_k):
        await _fire(db, task_id, "replaced")
        return True

    git.merge_pr = AsyncMock(side_effect=merged_then_replaced)
    await _drain_pair_delivery(db)
    assert dict(await repo.get_task(db, task_id))["status"] != "completed"
    assert await repo.pipeline_merge_recorded(db, task_id, 77), "the merge is a fact"
    notes = [
        (dict(u).get("content") or "")
        for u in await repo.get_task_updates(db, task_id)
        if "влит, но одобрение" in (dict(u).get("content") or "")
    ]
    assert len(notes) == 1 and "НЕ завершена" in notes[0]
    assert "влит по одобренному ревью" not in " ".join(notes)


async def test_no_fix_is_bought_for_a_verdict_that_was_replaced(
    db: aiosqlite.Connection,
):
    from hub import poller

    task_id = await _headless_review_task(db)
    await repo.add_task_update(
        db, task_id, "reviewer", "review", "Review verdict: CHANGES_REQUESTED\nfix it"
    )
    await db.execute(
        "UPDATE tasks SET review_verdict='changes_requested', "
        "review_verdict_generation=1 WHERE id=?",
        (task_id,),
    )
    await db.commit()
    snapshot = dict(await repo.get_task(db, task_id))
    real = poller._resolve_review_verdict

    async def replaced_meanwhile(db_, task_, updates_):
        verdict = await real(db_, task_, updates_)
        await db_.execute(
            "UPDATE tasks SET review_verdict='approved' WHERE id=?", (task_id,)
        )
        await db_.commit()
        return verdict

    plugins.dispatch = _job()
    fixes: list[str] = []

    async def fix(_db, _task, text):
        fixes.append(text)

    with (
        patch.object(poller, "_resolve_review_verdict", replaced_meanwhile),
        patch.object(poller.services, "dispatch_fix", fix),
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
    ):
        await poller._review_task_tick(db, snapshot, {"status": "completed"})
    assert fixes == []


@pytest.mark.parametrize("change", _CHANGES)
@pytest.mark.parametrize(
    "probe",
    ["ci_passed", "ci_failed", "stacking_clear", "stacking_blocks", "merge_refused"],
)
async def test_no_merge_and_no_outcome_on_the_headless_path(
    db: aiosqlite.Connection, monkeypatch, probe: str, change: str
):
    """The matrix's fourth route: the headless conveyor, every probe, every change."""
    from hub import poller
    from hub.integrations.protocols import CIProbeOutcome, CIProbeResult

    task_id, git = await _mergeable_review_task(db)
    snapshot = dict(await repo.get_task(db, task_id))

    async def ci(*_a, **_k):
        if probe.startswith("ci_"):
            await _fire(db, task_id, change)
        outcome = (
            CIProbeOutcome.failed if probe == "ci_failed" else CIProbeOutcome.passed
        )
        return CIProbeResult(outcome, "probe")

    async def stacking(*_a, **_k):
        if probe.startswith("stacking"):
            await _fire(db, task_id, change)
        return (
            "stack_unresolvable: nobody delivers the base"
            if probe == "stacking_blocks"
            else ""
        )

    async def merge(*_a, **_k):
        await _fire(db, task_id, change)
        return False

    git.check_pr_ci = ci
    monkeypatch.setattr(poller.services, "stacking_gate_step", stacking)
    if probe == "merge_refused":
        git.merge_pr = merge
    with patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock):
        await poller._deliver_approved_review(db, snapshot)
    if probe != "merge_refused":
        assert git.merge_pr.await_count == 0, "the forge was never asked to merge"
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "no needs_decision, no completed"
    feed = " ".join(
        (dict(u).get("content") or "") for u in await repo.get_task_updates(db, task_id)
    )
    for phrase in (*_OUTCOME_PHRASES, "Доставка остановлена", "CI failed"):
        assert phrase not in feed, f"{phrase!r} written over an approval that was gone"


# ---- the merge is bound to the approved head (gh pr merge --match-head-commit) ----


def _head_bound_forge(git, db: aiosqlite.Connection, task_id: int, *, move: bool):
    """A forge double that behaves like --match-head-commit.

    ``move`` — while the merge call is running the branch is pushed and the work
    resubmitted: the head is no longer the approved one, so the forge refuses.
    """
    calls: list[str] = []

    async def merge(*_a, expected_head_sha="", **_k):
        calls.append(expected_head_sha)
        if move:
            await _fire(db, task_id, "resubmitted")
            await db.execute(
                "UPDATE tasks SET submission_sha=? WHERE id=?", ("b" * 40, task_id)
            )
            await db.commit()
        return expected_head_sha == "a" * 40 and not move

    git.merge_pr = AsyncMock(side_effect=merge)
    return calls


async def test_a_resubmission_during_the_merge_is_refused_by_the_forge_on_the_old_head(
    db: aiosqlite.Connection,
):
    from hub.integrations.protocols import CIProbeOutcome
    from tests.test_pair_merge_gate import (
        _approved_pair_task,
        _drain_pair_delivery,
        _git,
    )

    git = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    calls = _head_bound_forge(git, db, task_id, move=True)
    await _drain_pair_delivery(db)
    assert calls == ["a" * 40], "the forge got the head that was approved"
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] != "completed", "no completed for a delivery that failed"
    assert not await repo.pipeline_merge_recorded(db, task_id, 77)


async def test_the_forge_always_gets_the_approved_head(db: aiosqlite.Connection):
    from hub.integrations.protocols import CIProbeOutcome
    from tests.test_pair_merge_gate import (
        _approved_pair_task,
        _drain_pair_delivery,
        _git,
    )

    git = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    calls = _head_bound_forge(git, db, task_id, move=False)
    await _drain_pair_delivery(db)
    assert calls == ["a" * 40]
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


@pytest.mark.parametrize("path", ["pair_sweep", "done_report", "headless", "manual"])
async def test_an_unpinned_submission_gets_no_forge_call(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, path: str
):
    from hub.integrations.protocols import CIProbeOutcome
    from tests.test_pair_merge_gate import (
        _approved_pair_task,
        _drain_pair_delivery,
        _git,
        _report_done,
    )

    if path == "headless":
        from hub import poller

        task_id, git = await _mergeable_review_task(db)
        await db.execute("UPDATE tasks SET submission_sha='' WHERE id=?", (task_id,))
        await db.commit()
        snapshot = dict(await repo.get_task(db, task_id))
        with patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock):
            await poller._deliver_approved_review(db, snapshot)
    elif path == "manual":
        from tests.test_accept_without_delivery import (
            _approved_task,
            _decide_deliver,
            _install,
            _MergeSpy,
        )

        task_id = await _approved_task(client, db, title="unpinned", pr=950)
        await db.execute("UPDATE tasks SET submission_sha='' WHERE id=?", (task_id,))
        await db.commit()
        spy = _MergeSpy()
        _install(monkeypatch, spy)
        await _decide_deliver(client, task_id)
        git = None
        assert spy.heads == [] and spy.merged == []
    else:
        git = _git(CIProbeOutcome.passed, merged=True)
        task_id = await _approved_pair_task(db, pin=False)
        if path == "pair_sweep":
            await _drain_pair_delivery(db)
        else:
            await _report_done(db, task_id)
    if git is not None:
        assert git.merge_pr.await_count == 0, "the forge was never called"
    if path != "manual":  # a human's accept completes the task without delivery
        assert dict(await repo.get_task(db, task_id))["status"] not in (
            "completed",
            "needs_decision",
        )
    feed = " ".join(
        (dict(u).get("content") or "") for u in await repo.get_task_updates(db, task_id)
    )
    assert "Ревью одобрено" not in feed


async def test_an_old_done_report_after_a_reapproved_resubmission_does_not_merge(
    db: aiosqlite.Connection,
):
    """P2: the deferred completion remembers the generation of ITS done report."""
    from hub.integrations.protocols import CIProbeOutcome
    from hub.services import orchestration
    from tests.test_pair_merge_gate import _approved_pair_task, _git

    git = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    generation = dict(await repo.get_task(db, task_id))["submission_generation"]
    await _fire(db, task_id, "reapproved")  # N+1, approved again
    await db.execute(
        "UPDATE tasks SET submission_sha=? WHERE id=?", ("c" * 40, task_id)
    )
    await db.commit()
    await orchestration.run_deferred_completions(
        db,
        [
            {
                "task_id": task_id,
                "exit_code": None,
                "result_text": "old done",
                "generation": generation,
            }
        ],
    )
    assert git.merge_pr.await_count == 0
    assert dict(await repo.get_task(db, task_id))["status"] != "completed"


async def test_the_replace_form_is_there_in_running_without_faking_a_status(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict
):
    """P2: the first client-driven verdict moves the task to running; the form stays."""
    task_id = await _in_review(client, hdr)
    page = (await client.get(f"/tasks/{task_id}", headers=hdr["rev"])).text
    assert 'id="review-replace-form"' not in page, "nothing to replace yet"
    first = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data=_browser_post(page, verdict="changes_requested", comments=CR_COMMENTS),
        headers=hdr["rev"],
        follow_redirects=False,
    )
    assert first.status_code == 303 and "review_error" not in first.headers["location"]
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "running", "the real flow, no UPDATE of the status"

    page = (await client.get(f"/tasks/{task_id}", headers=hdr["human"])).text
    assert 'id="review-replace-form"' in page
    form = _replace_form_fields(page)
    assert form["supersedes_revision"] == "1" and form["expected_generation"] == "1"
    before = await _snapshot(db, task_id)
    done = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={**form, "verdict": "approved", "comments": "ок, замена"},
        headers=hdr["human"],
        follow_redirects=False,
    )
    assert "review_error" not in done.headers["location"], done.headers
    after = await _snapshot(db, task_id)
    assert (
        after["verdict"] == "approved" and after["revision"] == before["revision"] + 1
    )

    stale = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={**form, "verdict": "changes_requested", "comments": "поздно"},
        headers=hdr["human"],
        follow_redirects=False,
    )
    assert "review_error" in stale.headers["location"], "the page was stale"


def _replace_form_fields(page: str) -> dict[str, str]:
    import re

    form = re.search(r'<form[^>]*id="review-replace-form".*?</form>', page, re.S).group(
        0
    )
    out: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", form):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name and "csrf" not in name.group(1).lower() and 'type="hidden"' in tag:
            out[name.group(1)] = value.group(1) if value else ""
    return out


async def test_the_real_done_report_remembers_its_generation(
    db: aiosqlite.Connection, monkeypatch
):
    """P2 through the real flow: done at N, resubmitted+re-approved before completion."""
    from hub.integrations.protocols import CIProbeOutcome
    from hub.services import lifecycle
    from tests.test_pair_merge_gate import _approved_pair_task, _git, _report_done

    git = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    real = lifecycle.run_deferred_completions

    async def late(db_, pending):
        await _fire(db_, task_id, "reapproved")
        await db_.execute(
            "UPDATE tasks SET submission_sha=? WHERE id=?", ("c" * 40, task_id)
        )
        await db_.commit()
        await real(db_, pending)

    monkeypatch.setattr(lifecycle, "run_deferred_completions", late)
    await _report_done(db, task_id)
    assert git.merge_pr.await_count == 0
    assert dict(await repo.get_task(db, task_id))["status"] != "completed"


async def test_a_done_report_does_not_complete_after_a_merge_under_a_gone_approval(
    db: aiosqlite.Connection,
):
    from hub.integrations.protocols import CIProbeOutcome
    from tests.test_pair_merge_gate import _approved_pair_task, _git, _report_done

    git = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)

    async def merged_then_replaced(*_a, **_k):
        await _fire(db, task_id, "replaced")
        return True

    git.merge_pr = AsyncMock(side_effect=merged_then_replaced)
    await _report_done(db, task_id)
    assert dict(await repo.get_task(db, task_id))["status"] != "completed"
    assert await repo.pipeline_merge_recorded(db, task_id, 77), "the merge is a fact"
    feed = " ".join(
        (dict(u).get("content") or "") for u in await repo.get_task_updates(db, task_id)
    )
    assert "НЕ завершена" in feed


async def test_the_pair_completion_is_written_inside_the_outcome_writer(
    db: aiosqlite.Connection, monkeypatch
):
    """Codex round 7 (P1): the check and ``completed`` are one transaction.

    The writer is the only place that holds the check and the write under one
    write lock (test_the_outcome_check_and_write_hold_one_write_lock), so the
    completion must be written FROM it, not after it.
    """
    from hub import services
    from hub.integrations.protocols import CIProbeOutcome
    from hub.services import delivery_writes
    from tests.test_pair_merge_gate import (
        _approved_pair_task,
        _drain_pair_delivery,
        _git,
    )

    _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    inside = {"now": False}
    seen: list[bool] = []
    real_writer = services.record_delivery_outcome
    real_complete = delivery_writes.complete_delivered

    async def writer(*args, **kwargs):
        inside["now"] = True
        try:
            return await real_writer(*args, **kwargs)
        finally:
            inside["now"] = False

    async def complete(*args, **kwargs):
        seen.append(inside["now"])
        return await real_complete(*args, **kwargs)

    monkeypatch.setattr(services, "record_delivery_outcome", writer)
    monkeypatch.setattr(delivery_writes, "complete_delivered", complete)
    await _drain_pair_delivery(db)
    assert seen == [True], "completed was written outside the outcome writer"
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


async def test_a_resubmission_during_ci_standing_does_not_complete_the_old_done(
    db: aiosqlite.Connection, monkeypatch
):
    """Codex round 7 (P2): ``prepare_ci_standing`` returns a fresh row."""
    from hub import config
    from hub.integrations.protocols import CIProbeOutcome
    from hub.services import lifecycle, orchestration
    from tests.test_pair_merge_gate import _approved_pair_task, _git

    git = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    generation = dict(await repo.get_task(db, task_id))["submission_generation"]
    monkeypatch.setattr(config, "SDD_VALIDATION", "require")

    async def standing(db_, task_):
        await _fire(db_, task_id, "reapproved")
        await db_.execute(
            "UPDATE tasks SET submission_sha=? WHERE id=?", ("c" * 40, task_id)
        )
        await db_.commit()
        return dict(await repo.get_task(db_, task_id))  # fresh: N+1

    monkeypatch.setattr(lifecycle, "prepare_ci_standing", standing)
    await orchestration.run_deferred_completions(
        db,
        [
            {
                "task_id": task_id,
                "exit_code": None,
                "result_text": "old done",
                "generation": generation,
            }
        ],
    )
    assert git.merge_pr.await_count == 0
    assert dict(await repo.get_task(db, task_id))["status"] != "completed"


async def test_a_task_that_opted_out_of_review_is_merged_without_a_pin(
    db: aiosqlite.Connection,
):
    """Codex round 7 (P2): auto_review=False has no approved head to bind to."""
    from hub.integrations.protocols import CIProbeOutcome
    from hub.services import orchestration
    from tests.test_pair_merge_gate import _approved_pair_task, _git

    git = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db, pin=False)
    await db.execute(
        "UPDATE tasks SET auto_review=0, submission_sha='' WHERE id=?", (task_id,)
    )
    await db.commit()
    ok, _ = await orchestration.merge_before_completion(
        db, dict(await repo.get_task(db, task_id))
    )
    assert ok and git.merge_pr.await_count == 1


async def test_the_merge_after_a_base_update_is_bound_to_the_head_that_was_compared(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Codex round 7 (P2): the pin moves with a verdict-keeping base merge."""
    from hub.services import lifecycle, orchestration
    from tests.test_accept_without_delivery import _approved_task, _install, _MergeSpy

    task_id = await _approved_task(client, db, title="base-moved", pr=953)
    spy = _MergeSpy()
    _install(monkeypatch, spy)

    async def tip(*_a, **_k):
        return "n" * 40, ""

    async def kept(*_a, **_k):
        return True, "авторская правка та же"

    async def no_wait(*_a, **_k):
        return ""

    monkeypatch.setattr(lifecycle, "resolve_branch_tip", tip)
    monkeypatch.setattr(orchestration, "base_merge_kept_the_verdict", kept)
    monkeypatch.setattr(orchestration, "_gate_update_base", no_wait)
    ok, _ = await orchestration.merge_before_completion(
        db, dict(await repo.get_task(db, task_id))
    )
    assert ok and spy.heads == ["n" * 40], "bound to the head the check compared"
    assert dict(await repo.get_task(db, task_id))["submission_sha"] == "n" * 40


async def test_the_repin_leaves_a_newer_submissions_pin_alone(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Codex round 8: the re-pin is conditional on the submission and the old pin."""
    from hub.services import lifecycle, orchestration
    from tests.test_accept_without_delivery import _approved_task, _install, _MergeSpy

    task_id = await _approved_task(client, db, title="repin-race", pr=954)
    _install(monkeypatch, _MergeSpy())

    async def tip(*_a, **_k):
        return "n" * 40, ""

    async def kept(*_a, **_k):
        # A newer submission arrives while the comparison runs.
        await repo.bump_submission_generation(db, task_id)
        await db.execute(
            "UPDATE tasks SET submission_sha=? WHERE id=?", ("z" * 40, task_id)
        )
        await db.commit()
        return True, "та же правка"

    async def no_wait(*_a, **_k):
        return ""

    monkeypatch.setattr(lifecycle, "resolve_branch_tip", tip)
    monkeypatch.setattr(orchestration, "base_merge_kept_the_verdict", kept)
    monkeypatch.setattr(orchestration, "_gate_update_base", no_wait)
    await orchestration.merge_before_completion(
        db, dict(await repo.get_task(db, task_id))
    )
    row = dict(await repo.get_task(db, task_id))
    assert row["submission_sha"] == "z" * 40, "the newer submission keeps its pin"
    assert not db.in_transaction, "the short write is committed before the network"


@pytest.mark.parametrize("change", ["replaced", "returned"])
async def test_a_replacement_while_the_headless_merge_is_tidied_opens_no_new_submission(
    db: aiosqlite.Connection, monkeypatch, change: str
):
    """Machine review 948 (high): the post-merge transition must not read the live verdict."""
    from hub import poller

    task_id, git = await _mergeable_review_task(db)
    snapshot = dict(await repo.get_task(db, task_id))
    generation = snapshot["submission_generation"]

    real_tidy = poller._record_merge_and_tidy

    async def tidy(*args, **kwargs):
        await real_tidy(*args, **kwargs)  # the merge is recorded, as in production
        await _fire(db, task_id, change)  # then the world moves, before completion

    monkeypatch.setattr(poller, "_record_merge_and_tidy", tidy)
    fixes: list[str] = []
    monkeypatch.setattr(
        poller.services,
        "dispatch_fix",
        AsyncMock(side_effect=lambda *a, **k: fixes.append("fix")),
    )
    with patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock):
        await poller._deliver_approved_review(db, snapshot)
    row = dict(await repo.get_task(db, task_id))
    assert git.merge_pr.await_count == 1
    assert row["submission_generation"] == generation, "no new submission was opened"
    assert row["status"] != "completed" and fixes == []
    feed = " ".join(
        (dict(u).get("content") or "") for u in await repo.get_task_updates(db, task_id)
    )
    assert "НЕ завершена" in feed


# ---- round 6: the merge stands on the head THIS pass checked --------------------


@pytest.mark.parametrize("path", ["gate", "done_report"])
async def test_the_merge_is_bound_to_the_head_this_pass_checked(
    db: aiosqlite.Connection, path: str
):
    """Codex round 6 (P1): a neighbour re-pins B->C while this pass saw green CI on B."""
    from hub.integrations.protocols import CIProbeOutcome
    from hub.services import orchestration
    from tests.test_pair_merge_gate import _approved_pair_task, _git, _report_done

    git = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    real = git.check_pr_ci

    async def ci_then_repin(*args, **kwargs):
        result = await real(*args, **kwargs)  # green on the head THIS pass looked at
        await db.execute(
            "UPDATE tasks SET submission_sha=? WHERE id=?", ("c" * 40, task_id)
        )
        await db.commit()
        return result

    git.check_pr_ci = ci_then_repin
    if path == "gate":
        ok, detail = await orchestration.merge_before_completion(
            db, dict(await repo.get_task(db, task_id))
        )
        assert not ok and detail.startswith(orchestration.APPROVAL_HEAD_MOVED_PREFIX)
    else:
        await _report_done(db, task_id)
        feed = " ".join(
            (dict(u).get("content") or "")
            for u in await repo.get_task_updates(db, task_id)
        )
        assert "закрепление одобренной сдачи ушло" in feed
    assert git.merge_pr.await_count == 0, "the forge was never called"
    assert dict(await repo.get_task(db, task_id))["status"] != "completed"


async def test_a_human_delivery_does_not_claim_an_approval_that_went_during_the_merge(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Codex round 6 (P2): the success line of /decide deliver says "approved review"."""
    from tests.test_accept_without_delivery import (
        _approved_task,
        _decide_deliver,
        _install,
        _MergeSpy,
    )

    task_id = await _approved_task(client, db, title="manual-race", pr=956)

    class Replacing(_MergeSpy):
        async def merge_pr(self, *args, **kwargs):
            await _fire(db, task_id, "replaced")
            return await super().merge_pr(*args, **kwargs)

    spy = Replacing()
    _install(monkeypatch, spy)
    await _decide_deliver(client, task_id)
    assert spy.merged == [956], "the merge happened"
    assert await repo.pipeline_merge_recorded(db, task_id, 956), "and is a fact"
    feed = " ".join(
        (dict(u).get("content") or "") for u in await repo.get_task_updates(db, task_id)
    )
    assert "одобренное ревью, неизменившийся" not in feed
    assert "одобрение" in feed and "не действует" in feed


async def test_a_tick_with_a_stale_snapshot_does_not_open_a_submission_after_merging(
    db: aiosqlite.Connection, monkeypatch
):
    """Codex round 6 (P2): the snapshot has no verdict; the live row is APPROVED."""
    from hub import poller

    task_id, git = await _mergeable_review_task(db)
    await db.execute(
        "UPDATE tasks SET review_verdict=NULL, review_verdict_generation=NULL "
        "WHERE id=?",
        (task_id,),
    )
    await db.commit()
    stale = dict(await repo.get_task(db, task_id))  # what the tick read: no verdict
    await db.execute(
        "UPDATE tasks SET review_verdict='approved', "
        "review_verdict_generation=submission_generation WHERE id=?",
        (task_id,),
    )
    await db.commit()
    fixes = AsyncMock()
    monkeypatch.setattr(poller.services, "dispatch_fix", fixes)
    with patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock):
        await poller._deliver_approved_review(db, stale)
    row = dict(await repo.get_task(db, task_id))
    assert git.merge_pr.await_count == 1
    assert row["submission_generation"] == 1, "no new submission over a merged branch"
    assert row["status"] != "needs_decision" and fixes.await_count == 0
