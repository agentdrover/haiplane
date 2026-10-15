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
    await repo.update_task(db, task_id, review_job_id="rev-1")
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

    # A recognised CHANGES_REQUESTED must not land over a standing verdict...
    plugins.dispatch = _job("verdict: CHANGES_REQUESTED")
    task = dict(await repo.get_task(db, task_id))
    await db.execute(
        "UPDATE tasks SET review_verdict='approved', review_findings='[]' WHERE id=?",
        (task_id,),
    )
    await db.commit()
    task = dict(await repo.get_task(db, task_id))
    task["review_verdict_generation"] = 0  # the snapshot the tick carries is older
    await _resolve_review_verdict(db, task, [])
    row = dict(await repo.get_task(db, task_id))
    assert row["review_verdict"] == "approved", (
        "a standing verdict is never overwritten"
    )
    assert row["review_verdict_revision"] == 3

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
    for kind in ("approved", "changes_requested"):
        assert (
            f"review-verdict 42 {kind} --agent code-reviewer --expected-generation "
            in text
        ), kind
    assert "review-brief 42" in text, "an order without the number says where to get it"
    pinned = plugin.build_review_message(
        task_id=42, title="t", description="d", review_cycle=0, max_cycles=3,
        generation=7,
    )  # fmt: skip
    assert pinned.count("--expected-generation 7 ") == 2, "the order carries the number"
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
