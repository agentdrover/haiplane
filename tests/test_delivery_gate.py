"""Work that never left the branch, named when it is called done (#498).

The hub learned today to tell "merged" from "running in production" (#497).
This is the earlier loss: a task finished with commits on its branch and no
pull request at all — delivery never started, and the report read exactly like
a delivered one.

The tests are as much about the silences as about the warning: a task without a
branch, without a workspace, or with a git that will not answer must produce
nothing. An accusation made out of ignorance is worse than saying nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.services.delivery_gate import undelivered_warning
from tests.test_pair_merge_gate import _approved_pair_task, _git, _report_done


async def _running_task(client: AsyncClient, title: str = "Undelivered?") -> int:
    task_id = (await client.post("/api/tasks", json={"title": title})).json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    return task_id


def _workspace_with_changes(monkeypatch, changed: list[str] | None) -> None:
    """Point the check at a workspace whose branch reports ``changed``.

    ``None`` stands for "git would not answer" — the case that must stay quiet.
    """
    from hub import app as hub_app
    from hub.integrations.registry import plugins

    monkeypatch.setattr(
        hub_app.services,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )
    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
        raising=False,
    )
    monkeypatch.setattr(
        plugins.git_ops, "branch_diff_paths", AsyncMock(return_value=changed)
    )


async def test_commits_without_pr_warn_on_done(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#498): changes on the branch, no PR, no merge — delivery never
    # started, and the report must say so instead of reading like any other.
    _workspace_with_changes(monkeypatch, ["hub/web.py", "tests/test_web.py"])
    task_id = await _running_task(client)

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "Готово"},
    )

    assert resp.status_code == 200, resp.text
    warnings = resp.json()["warnings"]
    assert warnings, "undelivered work must be named on the report itself"
    assert "не начала доставляться" in warnings[0]
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] in ("completed", "review", "ci_check"), (
        "the warning is advisory — completion must not be blocked"
    )


async def test_delivered_work_is_not_warned_about(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#498): a PR (or a merge the hub performed) means delivery started.
    # Warning here would train people to ignore the warning.
    _workspace_with_changes(monkeypatch, ["hub/web.py"])
    task_id = await _running_task(client)
    await repo.update_task(db, task_id, pr_number=4242)
    await db.commit()

    with_pr = await undelivered_warning(db, dict(await repo.get_task(db, task_id)))

    await repo.update_task(db, task_id, pr_number=None)
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (1, 4243, task_id, "merged-somewhere"),
    )
    await db.commit()
    with_merge = await undelivered_warning(db, dict(await repo.get_task(db, task_id)))

    assert with_pr == ""
    assert with_merge == ""


async def test_unknown_delivery_stays_silent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#498): three ways of not knowing, three silences. This is the same
    # line #839, #497 and #883 hold, turned around: there an absence could not
    # be printed as denial, here it cannot be printed as fault.
    task_id = await _running_task(client)
    task = dict(await repo.get_task(db, task_id))

    # git will not answer
    _workspace_with_changes(monkeypatch, None)
    assert await undelivered_warning(db, task) == ""

    # the branch changes nothing
    _workspace_with_changes(monkeypatch, [])
    assert await undelivered_warning(db, task) == ""

    # no branch at all: research and decisions were never meant to leave one
    await repo.update_task(db, task_id, branch=None)
    await db.commit()
    _workspace_with_changes(monkeypatch, ["hub/web.py"])
    assert await undelivered_warning(db, dict(await repo.get_task(db, task_id))) == ""


async def test_warning_reaches_the_task_feed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#498): the agent reads the response, the owner reads the feed.
    # A warning in only one of the two reaches nobody who can act on it — the
    # defect #826 found in review findings.
    _workspace_with_changes(monkeypatch, ["hub/web.py"])
    task_id = await _running_task(client)

    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "Готово"},
    )

    updates = (await client.get(f"/api/tasks/{task_id}/updates")).json()
    alerts = [u for u in updates if u["kind"] == "alert"]
    assert any("не начала доставляться" in u["content"] for u in alerts), (
        "the owner reads the feed, not the agent's response"
    )


async def test_missing_run_within_window_keeps_task_running(
    db: aiosqlite.Connection,
) -> None:
    # #1041 AC-1: workflows exist, this SHA has no run yet, the grace window
    # has not elapsed — delivery waits. needs_decision would turn a GitHub
    # registration lag into a human chore. The stand-in is not hex: detect-secrets
    # treats hex high-entropy strings as secrets and that scan is the same CI
    # job the delivery gate reads.
    g = _git(CIProbeOutcome.passed, merged=True)
    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(
            CIProbeOutcome.missing_run, "no_workflow_runs", details="head-sha-not-hex"
        )
    )
    plugins.git_ops = g
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running"
    assert task["ci_check_started_at"], (
        "the window is measured from this stamp; without it every later "
        "done report treats elapsed as 0 and the task waits forever"
    )
    g.merge_pr.assert_not_awaited()
    events = [dict(e) for e in await repo.list_events(db, since=0)]
    assert not any(
        e["kind"] == "needs_decision" and e["task_id"] == task_id for e in events
    )


async def test_missing_run_after_window_escalates_with_named_fact(
    db: aiosqlite.Connection,
) -> None:
    # #1041 AC-2: the same fact past the window is a decision, and the reason
    # names the fact and the commit — not the old ci_absent: no_workflow_runs
    # slug that hid "GitHub has not registered a run yet" as "there is no CI".
    g = _git(CIProbeOutcome.passed, merged=True)
    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(
            CIProbeOutcome.missing_run, "no_workflow_runs", details="head-sha-not-hex"
        )
    )
    plugins.git_ops = g
    task_id = await _approved_pair_task(db)
    await db.execute(
        "UPDATE tasks SET ci_check_started_at = datetime('now', '-30 minutes') "
        "WHERE id=?",
        (task_id,),
    )
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision"
    g.merge_pr.assert_not_awaited()
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "ci_absent: no_workflow_runs" not in body
    assert "workflow есть" in body
    assert "head-sha-not-hex" in body


# ---- #1186: mergeable is not deliverable when the base is still unmerged ----
#
# The incident, 06.09.2026 on spike-bo: task #1183's branch was cut from
# #1175's, #1183 got an auto verdict and a green CI, and the gate squash-merged
# its PR while #1175 was still in review. All five commits of the stack landed
# in main under #1183's number; #1175's PR went empty and DIRTY, its task stuck
# in review with a diff there was nowhere left to apply, and the attribution
# is gone for good. The signal existed the whole time — detect_branch_stacking
# — but only ever addressed a human. The gate asked about CI and mergeability
# and merged, and said so in the feed: "Условия доставки были выполнены
# целиком, ждать было нечего."


async def _base_task_in_review(db: aiosqlite.Connection, branch: str) -> int:
    """Another task whose branch is alive and unmerged — a stack's base."""
    from hub.models import TaskCreate
    from hub.services import create_task

    tv = await create_task(db, TaskCreate(title="Base of the stack"))
    await repo.update_task(db, tv.id, status="review", branch=branch)
    await db.commit()
    return tv.id


def _probes(g, outcome, reason: str = "scripted"):
    """Script the stacking probe on a git double (#1186)."""
    from hub.integrations.protocols import StackProbeResult

    g.branch_stacking_probe = AsyncMock(
        return_value=StackProbeResult(outcome=outcome, reason=reason)
    )
    return g


async def test_delivery_holds_while_the_base_branch_is_unmerged(
    db: aiosqlite.Connection,
) -> None:
    # AC-1: approved, green CI, mergeable — and the branch stands on the
    # unmerged branch of a task still in review. The merge is the irreversible
    # step, so it does not happen, and the feed names WHICH task is being
    # waited for. A hold, not an escalation: the base merges on its own and
    # this delivery becomes possible the moment it does.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    task_id = await _approved_pair_task(db)
    base_id = await _base_task_in_review(db, "task-1175/image-capture")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "an unmerged base is a wait, not a decision: needs_decision is a door "
        "that only opens outward (#1030)"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert f"#{base_id}" in body, "the feed must name the task being waited for"
    assert "task-1175/image-capture" in body
    events = [dict(e) for e in await repo.list_events(db, since=0)]
    assert not any(
        e["kind"] == "needs_decision" and e["task_id"] == task_id for e in events
    )


async def test_delivery_proceeds_once_the_base_has_merged(
    db: aiosqlite.Connection,
) -> None:
    # AC-2: the same stack, after the base has been delivered. A completed
    # task owns no unmerged branch, so there is nothing left to compare
    # against and the hold lifts by itself — no second signal to maintain.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    task_id = await _approved_pair_task(db)
    base_id = await _base_task_in_review(db, "task-1175/image-capture")
    await repo.update_task(db, base_id, status="completed")
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed"
    assert g.merge_pr.await_count == 1


async def test_delivery_unaffected_without_a_stack(
    db: aiosqlite.Connection,
) -> None:
    # AC-3: another task's branch IS alive, and the probe looked and found
    # them independent. Delivery behaves exactly as it did before #1186 —
    # the new condition must not start holding ordinary deliveries.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.clear)
    task_id = await _approved_pair_task(db)
    await _base_task_in_review(db, "task-900/unrelated-work")

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed"
    assert g.merge_pr.await_count == 1
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "стопк" not in body.lower(), (
        "a delivery that was checked and found independent says nothing new"
    )


async def test_unknown_stacking_is_not_read_as_no_stacking(
    db: aiosqlite.Connection,
) -> None:
    # AC-4. The trap this whole task is about, and the one the statement
    # named one layer too shallow: the predicate returned a bool, so a git
    # that could not answer — refs missing from the clone, rev-list failing,
    # no workspace — came back with the very same False that means "checked,
    # and they are independent". Advisory, that cost a missing hint. As a
    # delivery condition it costs the base task its work.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(
        _git(CIProbeOutcome.passed, merged=True),
        StackProbeOutcome.unavailable,
        reason="ref_unresolved",
    )
    task_id = await _approved_pair_task(db)
    await _base_task_in_review(db, "task-1175/image-capture")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "a call that did not land is cured by asking again, not by a human"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "ref_unresolved" in body, "the reason it could not look is named"


async def test_bool_only_plugin_is_unknown_rather_than_clear(
    db: aiosqlite.Connection,
) -> None:
    # AC-4, the other half: a plugin that predates the probe answers only
    # True/False, and its False cannot distinguish the two. Delivery goes
    # ahead — refusing would stall every delivery on such a plugin forever,
    # which is the constraint's other side — but it is said out loud, so a
    # reader can tell "we could not look" from "we looked and there was
    # nothing". Today the two are the same silence.
    g = _git(CIProbeOutcome.passed, merged=True)
    # A pre-#1186 plugin, declared the way this repo already declares one
    # (tests/test_stack_advisory.py does the same to branch_ancestry): the
    # attribute is simply not there to be found.
    g.branch_stacking_probe = None
    task_id = await _approved_pair_task(db)
    await _base_task_in_review(db, "task-1175/image-capture")

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", "an unanswerable plugin must not stall"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "проверить было нечем" in body
    assert "legacy_bool_predicate" in body
