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

from hub import repository as repo
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
