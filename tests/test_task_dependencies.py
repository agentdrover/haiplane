"""Dependency edges: add, remove, list, and the DAG invariant (#483, epic #478).

The order of work used to live outside the hub — in a chat, in an agent's
memory, in a sentence inside somebody's constraints. On 21.08.2026 that gap
stopped work already under way (#830 was approved and pair-started before
anyone noticed its dependency sat in an unmerged PR). These are the methods
that let the order live in the system.
"""

from __future__ import annotations

import aiosqlite
import pytest

from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.services import lifecycle
from hub.repository import DependencyCycleError, SelfDependencyError


async def _task(db: aiosqlite.Connection, title: str) -> int:
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
        task_type="task",
        parent_id=None,
        priority="medium",
    )


async def test_cycle_through_a_chain_is_refused(db: aiosqlite.Connection):
    # AC-1 (#483): A waits for B, B waits for C. Letting C wait for A would
    # mean nothing can ever start — the graph must stay a DAG.
    a = await _task(db, "A")
    b = await _task(db, "B")
    c = await _task(db, "C")
    assert await repo.add_task_dependency(db, a, b) is True
    assert await repo.add_task_dependency(db, b, c) is True

    with pytest.raises(DependencyCycleError) as exc:
        await repo.add_task_dependency(db, c, a)

    # The message names the path, not just the fact: "cycle detected" tells
    # the caller something is wrong, the chain tells them which edge to drop.
    assert f"#{a}" in str(exc.value) and f"#{c}" in str(exc.value)
    edges = await repo.list_task_dependencies(db, c)
    assert edges["blocked_by"] == [], "a refused edge must not be written"


async def test_diamond_is_not_a_cycle(db: aiosqlite.Connection):
    # AC-2 (#483): two tasks may legitimately wait for the same third one.
    # A walk that tracked depth instead of visited nodes would meet D twice
    # and call a perfectly ordinary diamond a loop.
    a = await _task(db, "A")
    b = await _task(db, "B")
    c = await _task(db, "C")
    d = await _task(db, "D")

    assert await repo.add_task_dependency(db, a, b) is True
    assert await repo.add_task_dependency(db, a, c) is True
    assert await repo.add_task_dependency(db, b, d) is True
    assert await repo.add_task_dependency(db, c, d) is True

    assert len((await repo.list_task_dependencies(db, a))["blocked_by"]) == 2
    assert len((await repo.list_task_dependencies(db, d))["unblocks"]) == 2


async def test_self_edge_is_refused_with_a_readable_reason(db: aiosqlite.Connection):
    # AC-3 (#483): the schema refuses this too (#482), but SQLite would say
    # "CHECK constraint failed" and leave the reader to work out which one.
    a = await _task(db, "lonely")

    with pytest.raises(SelfDependencyError) as exc:
        await repo.add_task_dependency(db, a, a)

    assert f"#{a}" in str(exc.value)


async def test_add_and_remove_are_idempotent(db: aiosqlite.Connection):
    # AC-4 (#483): adding an edge that exists already satisfies the caller's
    # intent — that is a no-op, not a failure. Same for removing one that is
    # already gone.
    a = await _task(db, "A")
    b = await _task(db, "B")

    assert await repo.add_task_dependency(db, a, b) is True
    assert await repo.add_task_dependency(db, a, b) is False
    assert len((await repo.list_task_dependencies(db, a))["blocked_by"]) == 1

    assert await repo.remove_task_dependency(db, a, b) is True
    assert await repo.remove_task_dependency(db, a, b) is False
    assert (await repo.list_task_dependencies(db, a))["blocked_by"] == []


async def test_list_shows_both_sides_with_statuses(db: aiosqlite.Connection):
    # AC-5 (#483): "blocked by #818" means nothing without knowing where #818
    # stands, so the status travels with the edge. Both directions are kept:
    # one is read when work starts, the other when it finishes.
    blocked = await _task(db, "waits")
    blocker = await _task(db, "blocks")
    dependent = await _task(db, "waits for the waiter")
    await repo.add_task_dependency(db, blocked, blocker)
    await repo.add_task_dependency(db, dependent, blocked)
    await repo.update_task(db, blocker, status="completed")
    await db.commit()

    edges = await repo.list_task_dependencies(db, blocked)

    assert [e["task_id"] for e in edges["blocked_by"]] == [blocker]
    assert edges["blocked_by"][0]["status"] == "completed"
    assert edges["blocked_by"][0]["title"] == "blocks"
    assert [e["task_id"] for e in edges["unblocks"]] == [dependent]
    assert edges["unblocks"][0]["status"] == "open"


# --- Readiness is delivery, not status (#484) --------------------------------
#
# The owner's decision of 21.08.2026, taken after five cases where the blocker
# was undelivered code rather than an unfinished task. A gate reading status
# alone would have closed four of them and missed the most expensive: #830
# stopped after pair-start because its dependency sat in an open PR while its
# task was in review.


async def _pipeline_merge(
    db: aiosqlite.Connection, task_id: int, pr_number: int
) -> None:
    """What the gate writes when it merges a PR itself (#534)."""
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (NULL, ?, ?, ?)",
        (pr_number, task_id, "a" * 40),
    )
    await db.commit()


async def _alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
        and "недоставленных блокерах" in dict(u)["content"]
    ]


async def test_completed_but_unmerged_blocker_warns_on_start(
    db: aiosqlite.Connection,
):
    # AC-1 (#484): closing a task is not delivering its code. Between the done
    # report and the gate's merge there is a window, and a PR can still go back
    # for rework — exactly the shape that stopped #830.
    blocked = await _task(db, "waits for undelivered work")
    blocker = await _task(db, "closed but unmerged")
    await repo.add_task_dependency(db, blocked, blocker)
    await repo.update_task(db, blocker, status="completed", pr_number=8)
    await db.commit()

    blockers = await lifecycle.warn_about_undelivered_blockers(db, blocked)

    assert [b["task_id"] for b in blockers] == [blocker]
    assert blockers[0]["reason"] == "PR #8 не смержен гейтом"
    alerts = await _alerts(db, blocked)
    assert len(alerts) == 1 and f"#{blocker}" in alerts[0]


async def test_delivered_blocker_makes_a_task_startable(db: aiosqlite.Connection):
    # AC-2 (#484): a merge the gate performed is the evidence. The SHA is not
    # text the pusher controls, unlike "(#N)" in a commit subject (#534).
    blocked = await _task(db, "waits")
    blocker = await _task(db, "delivered")
    await repo.add_task_dependency(db, blocked, blocker)
    await repo.update_task(db, blocker, status="completed", pr_number=42)
    await _pipeline_merge(db, blocker, 42)

    assert await lifecycle.warn_about_undelivered_blockers(db, blocked) == []
    assert await _alerts(db, blocked) == []


async def test_blocker_without_a_pr_says_so(db: aiosqlite.Connection):
    # AC-3 (#484): "no PR declared" and "PR not merged" are different
    # situations. Collapsed into one line, neither can be acted on.
    blocked = await _task(db, "waits")
    blocker = await _task(db, "still working")
    await repo.add_task_dependency(db, blocked, blocker)
    await repo.update_task(db, blocker, status="running")
    await db.commit()

    blockers = await lifecycle.warn_about_undelivered_blockers(db, blocked)

    assert blockers[0]["reason"] == "PR не заявлен"


async def test_task_without_blockers_starts_silently(db: aiosqlite.Connection):
    # AC-4 (#484): silence where everything is in order. A check that speaks
    # on every start would be tuned out before it ever mattered.
    lonely = await _task(db, "no blockers at all")

    assert await lifecycle.warn_about_undelivered_blockers(db, lonely) == []
    assert await _alerts(db, lonely) == []


async def test_warning_never_blocks_the_start(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-5 (#484): advisory means advisory. The emergency flow and deliberate
    # work on a branch stack stay possible; the task really does reach running.
    resp = await client.post("/api/tasks", json={"title": "blocked but starting"})
    blocked = resp.json()["id"]
    blocker = await _task(db, "undelivered")
    await repo.add_task_dependency(db, blocked, blocker)
    await repo.update_task(db, blocker, status="completed", pr_number=7)
    await db.commit()
    await client.post(
        f"/api/tasks/{blocked}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )

    started = await client.post(
        f"/api/tasks/{blocked}/pair-start", json={"assigned_agent": "dev"}
    )

    assert started.status_code == 200, started.text
    assert started.json()["status"] == "running", "a warning must not gate the start"
    contents = [u["content"] for u in started.json()["updates"] or []]
    assert any("недоставленных блокерах" in c for c in contents), (
        "the warning travels in the same response the agent already reads"
    )


# --- The edges become readable (#485) ----------------------------------------
#
# They were already stored (#482, #483) and already warned at start (#484), but
# nothing outside SQL could see them: a task looked as if it had neither
# blockers nor dependents.


async def test_task_view_carries_both_sides(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-1 (#485): both directions on the single-task read.
    resp = await client.post("/api/tasks", json={"title": "middle"})
    middle = resp.json()["id"]
    blocker = await _task(db, "upstream")
    dependent = await _task(db, "downstream")
    await repo.add_task_dependency(db, middle, blocker)
    await repo.add_task_dependency(db, dependent, middle)
    await db.commit()

    view = (await client.get(f"/api/tasks/{middle}")).json()

    deps = view["dependencies"]
    assert [d["task_id"] for d in deps["blocked_by"]] == [blocker]
    assert [d["task_id"] for d in deps["unblocks"]] == [dependent]
    assert deps["blocked_by"][0]["status"] == "open"


async def test_blocked_by_shows_delivery_not_just_status(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-2 (#485): the status said "completed" for #818 while its PR sat open,
    # and that is precisely what let #830 start. Delivery travels beside it.
    resp = await client.post("/api/tasks", json={"title": "waits"})
    blocked = resp.json()["id"]
    blocker = await _task(db, "closed but unmerged")
    await repo.add_task_dependency(db, blocked, blocker)
    await repo.update_task(db, blocker, status="completed", pr_number=8)
    await db.commit()

    view = (await client.get(f"/api/tasks/{blocked}")).json()

    entry = view["dependencies"]["blocked_by"][0]
    assert entry["status"] == "completed"
    assert entry["delivered"] is False
    assert entry["reason"] == "PR #8 не смержен гейтом"


async def test_task_without_edges_says_nothing(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-3 (#485): silence, not empty lists. Most tasks have no edges, and a
    # section printed for all of them is noise that trains readers to skip it.
    from hub.mcp_server import _dependency_lines

    resp = await client.post("/api/tasks", json={"title": "no edges"})
    lonely = resp.json()["id"]

    view = (await client.get(f"/api/tasks/{lonely}")).json()

    assert view["dependencies"] is None
    assert _dependency_lines(view) == []


async def test_rest_and_mcp_agree_on_dependencies(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-4 (#485): one source, two presentations. Assembled from a second
    # query, the text would drift from the payload and nobody could tell which
    # one had aged.
    from hub.mcp_server import _dependency_lines

    resp = await client.post("/api/tasks", json={"title": "linked"})
    task_id = resp.json()["id"]
    blocker = await _task(db, "upstream work")
    await repo.add_task_dependency(db, task_id, blocker)
    await repo.update_task(db, blocker, status="running")
    await db.commit()

    view = (await client.get(f"/api/tasks/{task_id}")).json()
    lines = "\n".join(_dependency_lines(view))

    assert f"#{blocker}" in lines
    assert "upstream work" in lines
    assert "НЕ доставлен" in lines and "PR не заявлен" in lines
    assert view["dependencies"]["blocked_by"][0]["delivered"] is False


# --- REST for the graph (#486) -----------------------------------------------
#
# The graph existed since #482 but only hub code could write to it, so it
# stayed empty while four statements went on saying the order is "checked by
# eye". These endpoints hand the pen to whoever knows about the order.


async def test_rest_add_is_idempotent(client: AsyncClient, db: aiosqlite.Connection):
    # AC-1 (#486): the contract must not raise where the layer beneath it
    # shrugs (#483), or callers end up writing retry logic around a no-op.
    waits = (await client.post("/api/tasks", json={"title": "waits"})).json()["id"]
    blocker = (await client.post("/api/tasks", json={"title": "blocks"})).json()["id"]

    first = await client.post(
        f"/api/tasks/{waits}/dependencies", json={"depends_on_task_id": blocker}
    )
    second = await client.post(
        f"/api/tasks/{waits}/dependencies", json={"depends_on_task_id": blocker}
    )

    assert first.status_code == 200 and first.json()["created"] is True
    assert second.status_code == 200 and second.json()["created"] is False
    edges = (await client.get(f"/api/tasks/{waits}/dependencies")).json()
    assert len(edges["blocked_by"]) == 1


async def test_rest_cycle_is_a_structured_conflict(client: AsyncClient):
    # AC-2 (#486): the hint carries the chain. "A cycle was detected" cannot
    # be acted on; the chain names the edge to reconsider.
    a = (await client.post("/api/tasks", json={"title": "A"})).json()["id"]
    b = (await client.post("/api/tasks", json={"title": "B"})).json()["id"]
    c = (await client.post("/api/tasks", json={"title": "C"})).json()["id"]
    await client.post(f"/api/tasks/{a}/dependencies", json={"depends_on_task_id": b})
    await client.post(f"/api/tasks/{b}/dependencies", json={"depends_on_task_id": c})

    resp = await client.post(
        f"/api/tasks/{c}/dependencies", json={"depends_on_task_id": a}
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "dependency_cycle"
    assert f"#{a}" in detail["hint"] and f"#{c}" in detail["hint"]
    assert (await client.get(f"/api/tasks/{c}/dependencies")).json()["blocked_by"] == []


async def test_rest_self_edge_is_unprocessable(client: AsyncClient):
    # AC-3 (#486): a request that cannot mean anything, not a state conflict.
    task_id = (await client.post("/api/tasks", json={"title": "lonely"})).json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/dependencies", json={"depends_on_task_id": task_id}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "self_dependency"


async def test_rest_delete_is_idempotent(client: AsyncClient):
    # AC-4 (#486): the caller wanted the edge gone, and it is gone.
    waits = (await client.post("/api/tasks", json={"title": "waits"})).json()["id"]
    blocker = (await client.post("/api/tasks", json={"title": "blocks"})).json()["id"]
    await client.post(
        f"/api/tasks/{waits}/dependencies", json={"depends_on_task_id": blocker}
    )

    first = await client.delete(f"/api/tasks/{waits}/dependencies/{blocker}")
    second = await client.delete(f"/api/tasks/{waits}/dependencies/{blocker}")

    assert first.status_code == 200 and first.json()["removed"] is True
    assert second.status_code == 200 and second.json()["removed"] is False


async def test_rest_get_matches_the_task_context(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-5 (#486): one reader behind both answers. Assembled separately, the
    # endpoint and the task context would drift and nobody could say which
    # one had aged.
    waits = (await client.post("/api/tasks", json={"title": "waits"})).json()["id"]
    blocker = (await client.post("/api/tasks", json={"title": "unmerged"})).json()["id"]
    await client.post(
        f"/api/tasks/{waits}/dependencies", json={"depends_on_task_id": blocker}
    )
    await repo.update_task(db, blocker, status="completed", pr_number=8)
    await db.commit()

    endpoint = (await client.get(f"/api/tasks/{waits}/dependencies")).json()
    context = (await client.get(f"/api/tasks/{waits}")).json()["dependencies"]

    assert endpoint == context
    assert endpoint["blocked_by"][0]["delivered"] is False
    assert endpoint["blocked_by"][0]["reason"] == "PR #8 не смержен гейтом"


async def test_rest_refuses_an_edge_to_a_missing_task(client: AsyncClient):
    # An edge pointing at a task nobody can finish would read as a blocker
    # that never clears — refused before anything is written.
    task_id = (await client.post("/api/tasks", json={"title": "real"})).json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/dependencies", json={"depends_on_task_id": 999_999}
    )

    assert resp.status_code == 404, resp.text
    assert (await client.get(f"/api/tasks/{task_id}")).json()["dependencies"] is None


# --- The tools that finally let the graph be filled (#487) -------------------
#
# The graph has existed since #482 and REST since #486, but an agent works
# through MCP: until these tools existed, edges could only be written with
# curl, which is to say they were not written at all.


async def _mcp_text(result) -> str:
    # Refusals come back as a plain JSON string (_format_hub_api_error);
    # successes as a CallToolResult. Both are text to the reader.
    if isinstance(result, str):
        return result
    return "\n".join(
        block.text for block in result.content if getattr(block, "text", None)
    )


async def test_mcp_and_rest_return_the_same_edges(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#487): the tools call REST rather than the database, so there is
    # no second implementation to drift from the first.
    from hub import mcp_server

    waits = (await client.post("/api/tasks", json={"title": "waits"})).json()["id"]
    blocker = (await client.post("/api/tasks", json={"title": "blocks"})).json()["id"]

    async def _get(path, **kwargs):
        return (await client.get(path)).json()

    async def _post(path, body=None, **kwargs):
        return (await client.post(path, json=body or {})).json()

    monkeypatch.setattr(mcp_server, "_api_get", _get)
    monkeypatch.setattr(mcp_server, "_api_post", _post)

    added = await mcp_server.hub_add_dependency(waits, blocker)
    listed = await mcp_server.hub_list_dependencies(waits)

    assert "создано" in await _mcp_text(added)
    rest = (await client.get(f"/api/tasks/{waits}/dependencies")).json()
    assert listed.structuredContent["dependencies"] == rest
    assert [d["task_id"] for d in rest["blocked_by"]] == [blocker]


async def test_mcp_add_reports_idempotency(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#487): both calls succeed, and the wording distinguishes them.
    # "Already there" and "just created" are different facts about the world.
    from hub import mcp_server

    waits = (await client.post("/api/tasks", json={"title": "waits"})).json()["id"]
    blocker = (await client.post("/api/tasks", json={"title": "blocks"})).json()["id"]

    async def _post(path, body=None, **kwargs):
        return (await client.post(path, json=body or {})).json()

    monkeypatch.setattr(mcp_server, "_api_post", _post)

    first = await _mcp_text(await mcp_server.hub_add_dependency(waits, blocker))
    second = await _mcp_text(await mcp_server.hub_add_dependency(waits, blocker))

    assert "создано" in first
    assert "уже было" in second


async def test_mcp_cycle_error_keeps_the_chain(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#487): the chain survives the translation from a structured
    # refusal into text. Without it the reader knows something is wrong and
    # nothing about which edge to drop.
    from hub import mcp_server

    a = (await client.post("/api/tasks", json={"title": "A"})).json()["id"]
    b = (await client.post("/api/tasks", json={"title": "B"})).json()["id"]

    async def _post(path, body=None, **kwargs):
        resp = await client.post(path, json=body or {})
        if resp.status_code >= 400:
            raise mcp_server.HubApiError(resp.json()["detail"])
        return resp.json()

    monkeypatch.setattr(mcp_server, "_api_post", _post)
    await mcp_server.hub_add_dependency(a, b)

    refused = await _mcp_text(await mcp_server.hub_add_dependency(b, a))

    assert "dependency_cycle" in refused
    assert f"#{a}" in refused and f"#{b}" in refused


async def test_mcp_list_shows_delivery(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#487): a completed blocker with an open PR still blocks, and the
    # tool says so — the exact shape that stopped #830.
    from hub import mcp_server

    waits = (await client.post("/api/tasks", json={"title": "waits"})).json()["id"]
    blocker = (await client.post("/api/tasks", json={"title": "digest"})).json()["id"]
    await client.post(
        f"/api/tasks/{waits}/dependencies", json={"depends_on_task_id": blocker}
    )
    await repo.update_task(db, blocker, status="completed", pr_number=8)
    await db.commit()

    async def _get(path, **kwargs):
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _get)

    text = await _mcp_text(await mcp_server.hub_list_dependencies(waits))

    assert "НЕ доставлен" in text
    assert "PR #8 не смержен гейтом" in text


# --- Delivery is about the base branch, not about who pressed merge (#885) ---
#
# Readiness was read from pipeline_merges alone — merges the hub performed
# itself. A merge made outside the gate left no row, so the blocker read as
# undelivered while its code sat in the base branch: on 21.08.2026 the edge
# #830 → #818 said exactly that, an hour after #818 was merged. A warning
# that is wrong in the obvious case teaches the reader to skip the line.


class _AncestorGitOps(NoopGitOps):
    """Answers whether a commit is in the base branch, like the real one does."""

    def __init__(self, reachable: bool | None) -> None:
        self._reachable = reachable
        self.calls: list[tuple[str, str]] = []

    async def is_ancestor(self, repo, ancestor, descendant):
        self.calls.append((ancestor, descendant))
        return self._reachable


async def _blocked_pair(
    client: AsyncClient, db: aiosqlite.Connection, **blocker_fields
):
    # The base-branch question needs a workspace to ask it in; without one the
    # answer is "could not look", which is a different test than these.
    project = await repo.get_project_by_slug(db, "default")
    if project is None:
        await repo.create_project(
            db, slug="default", name="default", workspace_path="/tmp/ws"
        )
    else:
        await repo.update_project(db, dict(project)["id"], workspace_path="/tmp/ws")
    blocked = (await client.post("/api/tasks", json={"title": "waits"})).json()["id"]
    blocker = (await client.post("/api/tasks", json={"title": "upstream"})).json()["id"]
    await repo.add_task_dependency(db, blocked, blocker)
    if blocker_fields:
        await repo.update_task(db, blocker, **blocker_fields)
    await db.commit()
    return blocked, blocker


async def test_merge_outside_the_gate_still_counts_as_delivered(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-1 (#885): the code is in the base branch. Who merged it is a
    # separate question, and the answer says which — a manual merge is
    # against the rules here and stays visible instead of being smoothed over.
    blocked, blocker = await _blocked_pair(
        client, db, status="completed", pr_number=8, submission_sha="a" * 40
    )
    plugins.git_ops = _AncestorGitOps(True)

    edges = (await client.get(f"/api/tasks/{blocked}/dependencies")).json()

    entry = edges["blocked_by"][0]
    assert entry["delivered"] is True
    assert entry["delivery_path"] == "outside_gate"
    assert "мимо гейта" in entry["reason"]


async def test_completed_without_a_merge_still_blocks(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-2 (#885): the rule of #484 is untouched. Between a done report and a
    # merge there is a window, and a PR can still go back for rework — so
    # "closed" never means "delivered", however tempting the shortcut is.
    blocked, blocker = await _blocked_pair(
        client, db, status="completed", pr_number=8, submission_sha="b" * 40
    )
    plugins.git_ops = _AncestorGitOps(False)

    edges = (await client.get(f"/api/tasks/{blocked}/dependencies")).json()

    entry = edges["blocked_by"][0]
    assert entry["status"] == "completed"
    assert entry["delivered"] is False
    assert entry["delivery_path"] == "none"


async def test_unreadable_base_branch_is_not_a_denial(
    client: AsyncClient, db: aiosqlite.Connection
):
    # "Could not look" and "looked and it is not there" are different answers
    # (#725). The reason keeps its original text and says the second source
    # stayed silent — it does not invent a verdict.
    blocked, blocker = await _blocked_pair(
        client, db, status="completed", pr_number=8, submission_sha="c" * 40
    )
    plugins.git_ops = _AncestorGitOps(None)

    edges = (await client.get(f"/api/tasks/{blocked}/dependencies")).json()

    entry = edges["blocked_by"][0]
    assert entry["delivered"] is False
    assert entry["delivery_path"] == "unknown"
    assert "PR #8 не смержен гейтом" in entry["reason"]
    assert "проверить базовую ветку не удалось" in entry["reason"]


async def test_gate_merge_reads_as_before(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-3 (#885): a gate merge answers from pipeline_merges and costs no git
    # call at all — the cheap path stays cheap.
    blocked, blocker = await _blocked_pair(client, db, submission_sha="d" * 40)
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (1, 7, ?, 'deadbeef')",
        (blocker,),
    )
    await db.commit()
    ops = _AncestorGitOps(False)
    plugins.git_ops = ops

    edges = (await client.get(f"/api/tasks/{blocked}/dependencies")).json()

    entry = edges["blocked_by"][0]
    assert entry["delivered"] is True
    assert entry["delivery_path"] == "gate"
    assert ops.calls == [], "a gate merge must not cost a git call"


async def test_all_readers_agree_on_delivery(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-4 (#885): the start gate, the task context and REST run the same
    # enrichment. Three answers about one blocker would leave the reader
    # unable to tell which one aged.
    blocked, blocker = await _blocked_pair(
        client, db, status="completed", pr_number=8, submission_sha="e" * 40
    )
    plugins.git_ops = _AncestorGitOps(True)

    from hub.services import lifecycle

    rest = (await client.get(f"/api/tasks/{blocked}/dependencies")).json()
    context = (await client.get(f"/api/tasks/{blocked}")).json()["dependencies"]
    still_blocking = await lifecycle.warn_about_undelivered_blockers(db, blocked)

    assert rest == context
    assert rest["blocked_by"][0]["delivered"] is True
    assert still_blocking == [], "a delivered blocker must not warn at start"


# ---- #1442: блокер-контейнер (фича, эпик) доставлен своими детьми ----


async def _container(
    db: aiosqlite.Connection, title: str, task_type: str, parent_id: int | None = None
) -> int:
    return await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="completed",
        auto_review=False,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )


async def _delivered_child(db: aiosqlite.Connection, parent: int, pr: int) -> int:
    child = await _task(db, f"child {pr}")
    await repo.update_task(
        db, child, parent_id=parent, status="completed", pr_number=pr
    )
    await _pipeline_merge(db, child, pr)
    return child


async def test_a_feature_whose_children_are_delivered_is_a_delivered_blocker(
    db: aiosqlite.Connection,
):
    """AC-1: фича completed, все дети влиты гейтом — блокер доставлен: ни
    тревоги при старте, ни пропуска в очереди F1. И вложенно: эпик → фича."""
    from hub.services import orchestrator_queue

    epic = await _container(db, "эпик", "epic")
    feature = await _container(db, "фича", "feature", parent_id=epic)
    await _delivered_child(db, feature, 101)
    await _delivered_child(db, feature, 102)
    on_feature = await _task(db, "ждёт фичу")
    on_epic = await _task(db, "ждёт эпик")
    await repo.add_task_dependency(db, on_feature, feature)
    await repo.add_task_dependency(db, on_epic, epic)
    await db.commit()

    assert await lifecycle.warn_about_undelivered_blockers(db, on_feature) == []
    assert await _alerts(db, on_feature) == []
    assert await orchestrator_queue._undelivered_blockers(db, on_feature) == []
    assert await orchestrator_queue._undelivered_blockers(db, on_epic) == []


async def test_a_feature_with_an_undelivered_child_names_that_child(
    db: aiosqlite.Connection,
):
    """AC-2: ребёнок закрыт без доставки — фича не доставлена, и причина
    называет этого ребёнка, а не «PR не заявлен»."""
    feature = await _container(db, "фича", "feature")
    await _delivered_child(db, feature, 201)
    stranded = await _task(db, "закрыт без доставки")
    await repo.update_task(
        db, stranded, parent_id=feature, status="completed", pr_number=202
    )
    waits = await _task(db, "ждёт")
    await repo.add_task_dependency(db, waits, feature)
    await db.commit()

    blockers = await lifecycle.warn_about_undelivered_blockers(db, waits)

    assert [b["task_id"] for b in blockers] == [feature]
    assert f"#{stranded}" in blockers[0]["reason"], blockers[0]["reason"]
    assert "PR не заявлен" not in blockers[0]["reason"]


async def test_an_open_feature_is_not_delivered_even_with_delivered_children(
    db: aiosqlite.Connection,
):
    """Фича не завершена — не доставлена, даже если уже влитые дети есть."""
    feature = await _container(db, "фича", "feature")
    await repo.update_task(db, feature, status="running")
    await _delivered_child(db, feature, 301)
    waits = await _task(db, "ждёт")
    await repo.add_task_dependency(db, waits, feature)
    await db.commit()

    blockers = await lifecycle.warn_about_undelivered_blockers(db, waits)

    assert [b["task_id"] for b in blockers] == [feature]


async def test_a_rejected_child_does_not_hold_a_delivered_feature(
    db: aiosqlite.Connection,
):
    """Находка ревью #1442: отклонённый черновик-ребёнок («работа не нужна»,
    правило свёртки #742/#579) фичу не держит; failed-ребёнок — держит."""
    feature = await _container(db, "фича", "feature")
    await _delivered_child(db, feature, 401)
    discarded = await _task(db, "smoke draft")
    await repo.update_task(db, discarded, parent_id=feature, status="rejected")
    waits = await _task(db, "ждёт")
    await repo.add_task_dependency(db, waits, feature)
    await db.commit()

    assert await lifecycle.warn_about_undelivered_blockers(db, waits) == []

    broken = await _task(db, "не сделана")
    await repo.update_task(db, broken, parent_id=feature, status="failed")
    await db.commit()

    blockers = await lifecycle.warn_about_undelivered_blockers(db, waits)
    assert [b["task_id"] for b in blockers] == [feature]
    assert f"#{broken}" in blockers[0]["reason"]
    assert f"#{discarded}" not in blockers[0]["reason"]


# ---- #1648: принятая задача-состояние разблокирует зависимые ----


async def test_accepted_state_task_unblocks_dependents_until_reworked(
    client: AsyncClient, db: aiosqlite.Connection
):
    """AC-4 (#1648): B depends_on state-задачи A. Пока A в review — B ждёт и
    причина называет принятие, а не «PR не заявлен». Принята (completed,
    state_approved) — B свободна во всех читателях. Вернули A в работу — снова
    ждёт. pipeline_merges и releases для A не создаются. Родитель со state- и
    commit-детьми: state по принятию, commit по доставке. Commit-блокер — как
    раньше."""
    from hub.services import orchestrator_queue
    from tests.state_support import (
        drive_to_review,
        human_verdict,
        make_state_task,
    )

    a = await make_state_task(db, title="Настроить сервер")
    b = await _task(db, "Переключить DNS")
    await repo.add_task_dependency(db, b, a)
    await db.commit()
    plugins.git_ops = _AncestorGitOps(False)

    async def readers() -> tuple[bool | None, list[int], list[int], str]:
        rest = (await client.get(f"/api/tasks/{b}/dependencies")).json()["blocked_by"][
            0
        ]
        warned = await lifecycle.warn_about_undelivered_blockers(db, b)
        queued = await orchestrator_queue._undelivered_blockers(db, b)
        return (
            rest["delivered"],
            [w["task_id"] for w in warned],
            [q["task_id"] for q in queued],
            rest["reason"],
        )

    delivered, warned, queued, reason = await readers()
    assert (delivered, warned, queued) == (False, [a], [a])
    assert "PR не заявлен" not in reason and "принят" in reason, reason

    await drive_to_review(client, db, a)
    assert (await readers())[0] is False, "в review ещё не принята"

    accepted = await human_verdict(client, a, "approved", generation=1)
    assert accepted.status_code == 200 and accepted.json()["status"] == "completed"
    delivered, warned, queued, reason = await readers()
    assert (delivered, warned, queued) == (True, [], [])
    entry = (await client.get(f"/api/tasks/{b}/dependencies")).json()["blocked_by"][0]
    assert entry["delivery_path"] == "state_accepted"
    context = (await client.get(f"/api/tasks/{b}")).json()["dependencies"]
    assert context["blocked_by"][0]["delivered"] is True

    merges = await db.execute_fetchall(
        "SELECT COUNT(*) FROM pipeline_merges WHERE task_id=?", (a,)
    )
    assert merges[0][0] == 0
    assert (await db.execute_fetchall("SELECT COUNT(*) FROM releases"))[0][0] == 0

    # возврат в работу снимает готовность
    await repo.update_task(db, a, status="running")
    await db.commit()
    delivered, warned, queued, reason = await readers()
    assert (delivered, warned, queued) == (False, [a], [a])

    # commit-блокер без мержа — как раньше
    c = await _task(db, "commit-блокер")
    await repo.update_task(db, c, status="completed", pr_number=9)
    d = await _task(db, "ждёт commit")
    await repo.add_task_dependency(db, d, c)
    await db.commit()
    commit_entry = (await client.get(f"/api/tasks/{d}/dependencies")).json()[
        "blocked_by"
    ][0]
    assert commit_entry["delivered"] is False
    assert "PR #9 не смержен гейтом" in commit_entry["reason"]

    # родитель со state- и commit-детьми
    from tests.state_support import make_state_task as _mk

    s = await _mk(db, title="state-ребёнок", under_feature=True)
    parent = dict(await repo.get_task(db, s))["parent_id"]
    commit_child = await _task(db, "commit-ребёнок")
    await repo.update_task(
        db, commit_child, parent_id=parent, status="completed", pr_number=31
    )
    await drive_to_review(client, db, s)
    assert (await human_verdict(client, s, "approved", generation=1)).status_code == 200
    await repo.update_task(db, parent, status="completed")
    waits = await _task(db, "ждёт фичу")
    await repo.add_task_dependency(db, waits, parent)
    await db.commit()

    held = await lifecycle.warn_about_undelivered_blockers(db, waits)
    assert [x["task_id"] for x in held] == [parent]
    assert f"#{commit_child}" in held[0]["reason"], "commit-ребёнок держит по доставке"
    assert f"#{s}" not in held[0]["reason"], "принятый state-ребёнок не держит"

    await _pipeline_merge(db, commit_child, 31)
    assert await lifecycle.warn_about_undelivered_blockers(db, waits) == []

    await repo.update_task(db, s, status="running")
    await db.commit()
    held = await lifecycle.warn_about_undelivered_blockers(db, waits)
    assert [x["task_id"] for x in held] == [parent]
    assert f"#{s}" in held[0]["reason"], "возвращённый state-ребёнок держит родителя"


def test_state_accepted_needs_completed_state_and_a_verdict_of_the_current_generation():
    """#1648: единый признак принятия — каждое условие названо отдельно."""
    from hub.services.result_kind import state_accepted

    base = {
        "result_kind": "state",
        "status": "completed",
        "review_verdict": "approved",
        "review_verdict_generation": 2,
        "submission_generation": 2,
    }
    assert state_accepted(base) is True
    for change in (
        {"result_kind": "commit"},
        {"status": "review"},
        {"status": "running"},
        {"review_verdict": "changes_requested"},
        {"review_verdict": None},
        {"review_verdict_generation": 1},
        {"submission_generation": 0, "review_verdict_generation": 0},
    ):
        assert state_accepted({**base, **change}) is False, change
    assert state_accepted(None) is False


async def test_force_completed_state_task_does_not_unblock_dependents(
    client: AsyncClient, db: aiosqlite.Connection
):
    """#1648: статус completed без вердикта человека на поколение — не принятие
    (force-complete закрывает и недоделанное). Зависимая остаётся ждать."""
    from tests.state_support import drive_to_review, make_state_task

    a = await make_state_task(db, title="state")
    await drive_to_review(client, db, a)
    await repo.update_task(db, a, status="completed")
    b = await _task(db, "ждёт")
    await repo.add_task_dependency(db, b, a)
    await db.commit()
    plugins.git_ops = _AncestorGitOps(False)

    entry = (await client.get(f"/api/tasks/{b}/dependencies")).json()["blocked_by"][0]
    assert entry["delivered"] is False
    assert entry["delivery_path"] == "state_pending"
