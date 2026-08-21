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
