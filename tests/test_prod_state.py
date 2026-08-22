"""What is running in production, asked of the whole board at once (#499).

The facts existed before this: the deploy CI reported (#839, #496), the merges
the hub performed (#534) and the comparison between them (#497). A card could
answer for one task; "what has not reached production" meant opening cards one
by one.

Two of these tests are about restraint rather than output: unknown must stay
its own list, and the window the snapshot covers must be stated. A bounded list
presented as the whole board is the failure #824 refused to ship.
"""

from __future__ import annotations


import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.services.prod_state import format_prod_state, prod_state
from tests.test_delivery_state import _task_merged_at, _use_real_git


async def _completed(client: AsyncClient, db, merge_sha: str) -> int:
    task_id = await _task_merged_at(client, db, merge_sha)
    await repo.update_task(db, task_id, status="completed")
    await db.commit()
    return task_id


async def test_snapshot_splits_delivered_from_waiting(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-1 (#499): one call answers for the whole board — what shipped and
    # what is merged and still waiting.
    _use_real_git(monkeypatch, history["repo"])
    monkeypatch.setattr(
        plugins.git_ops,
        "commit_exists",
        GitOpsIntegration().commit_exists,
        raising=False,
    )
    shipped = await _completed(client, db, history["shipped"])
    waiting = await _completed(client, db, history["pending"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    snapshot = await prod_state(db)

    assert snapshot["deployed"]["sha"] == history["released"]
    assert shipped in [e["task_id"] for e in snapshot["in_prod"]]
    assert waiting in [e["task_id"] for e in snapshot["not_in_prod"]]


async def test_unknown_is_its_own_list(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-2 (#499): a task the hub never merged cannot be compared with
    # anything. Putting it among not_in_prod would say it failed to ship.
    _use_real_git(monkeypatch, history["repo"])
    task_id = (await client.post("/api/tasks", json={"title": "Never merged"})).json()[
        "id"
    ]
    await repo.update_task(db, task_id, status="completed")
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )
    await db.commit()

    snapshot = await prod_state(db)

    assert task_id in [e["task_id"] for e in snapshot["unknown"]]
    assert task_id not in [e["task_id"] for e in snapshot["not_in_prod"]]
    entry = next(e for e in snapshot["unknown"] if e["task_id"] == task_id)
    assert entry["reason"], "an unknown without a cause is just a blank"


async def test_no_releases_explains_itself(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-3 (#499): an installation with no delivery facts knows nothing about
    # production. The snapshot must say that instead of showing an empty
    # in_prod list, which reads as "nothing ever shipped".
    _use_real_git(monkeypatch, history["repo"])
    await _completed(client, db, history["shipped"])

    snapshot = await prod_state(db)

    assert snapshot["deployed"]["sha"] == ""
    assert "не знает" in snapshot["note"]
    assert "неизвестно" in format_prod_state(snapshot)


async def test_interfaces_share_one_builder(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-4 (#499): REST and the CLI/MCP rendering come from the same snapshot.
    # Two renderings of the same facts drift, and then two readers disagree
    # about production — the reason #808 and #823 unified their builders.
    _use_real_git(monkeypatch, history["repo"])
    monkeypatch.setattr(
        plugins.git_ops,
        "commit_exists",
        GitOpsIntegration().commit_exists,
        raising=False,
    )
    await _completed(client, db, history["shipped"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    over_rest = (await client.get("/api/prod-state")).json()
    direct = await prod_state(db)

    assert over_rest["deployed"] == direct["deployed"]
    assert [e["task_id"] for e in over_rest["in_prod"]] == [
        e["task_id"] for e in direct["in_prod"]
    ]
    assert over_rest["note"] == direct["note"], "the stated window must match too"
    assert history["released"][:12] in format_prod_state(over_rest)


async def test_window_is_stated_not_implied(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # #824's lesson, applied here: the snapshot is bounded, and a bound nobody
    # is told about reads as the whole board.
    _use_real_git(monkeypatch, history["repo"])
    for _ in range(3):
        await _completed(client, db, history["shipped"])

    snapshot = await prod_state(db, limit=2)

    assert snapshot["window"] == 2
    assert snapshot["examined"] == 2
    assert "окно 2" in snapshot["note"]
    assert "старше окна" in snapshot["note"]
