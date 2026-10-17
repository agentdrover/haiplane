"""The time of a task is split into the queue and the path after the start (#1790).

``completed_at − ready_at`` joined two things: the wait for somebody to start
(approval, the executor queue) and what happens after the start. Each interval
now has its own sample and its own reasons for leaving a row out, BESIDE the
old total, which keeps its cohort and its numbers: the series of comparisons
must stay comparable (#1490).
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import cli
from hub import repository as repo
from hub.models import TaskCreate, TaskType


async def _leaf(
    db: aiosqlite.Connection,
    title: str,
    *,
    ready: str | None,
    started: str | None,
    completed: str | None,
    unknown: bool = False,
    parent_id: int | None = None,
    task_type: TaskType = TaskType.task,
    touched: str = "-1 hours",
) -> int:
    """A completed task with explicit offsets from now; ``None`` is no stamp."""
    task_id = await repo.create_task_full(
        db,
        TaskCreate(
            title=title, work_type="feature", task_type=task_type, parent_id=parent_id
        ),
        status="open",
    )

    await db.execute(
        "UPDATE tasks SET status='completed', "
        "ready_at=CASE WHEN ?1 IS NULL THEN NULL ELSE datetime('now', ?1) END, "
        "started_at=CASE WHEN ?2 IS NULL THEN NULL ELSE datetime('now', ?2) END, "
        "completed_at=CASE WHEN ?3 IS NULL THEN NULL ELSE datetime('now', ?3) END, "
        "updated_at=datetime('now', ?4), start_history_unknown=?5 WHERE id=?6",
        (ready, started, completed, touched, int(unknown), task_id),
    )
    return task_id


async def _dataset(db: aiosqlite.Connection) -> None:
    """Feature leaves of every kind, plus a container and a parent of a leaf."""
    await _leaf(db, "A", ready="-50 hours", started="-40 hours", completed="-10 hours")
    await _leaf(db, "B", ready="-20 hours", started="-18 hours", completed="-2 hours")
    await _leaf(db, "C no ready", ready=None, started="-10 hours", completed="-1 hours")
    await _leaf(db, "D no start", ready="-10 hours", started=None, completed="-1 hours")
    await _leaf(
        db, "E ready after start", ready="-10 hours", started="-12 hours",
        completed="-1 hours",
    )  # fmt: skip
    await _leaf(
        db, "F old work", ready="-30 hours", started=None, completed="-1 hours",
        unknown=True,
    )  # fmt: skip
    await _leaf(
        db, "G completed before start", ready="-10 hours", started="-3 hours",
        completed="-5 hours",
    )  # fmt: skip
    await _leaf(
        db, "Z ready == start", ready="-9 hours", started="-9 hours",
        completed="-4 hours",
    )  # fmt: skip
    await _leaf(
        db, "N no completion", ready="-5 hours", started="-4 hours", completed=None
    )
    # A task with a child, and the child; and a feature container.
    parent = await _leaf(
        db, "P has a child", ready="-100 hours", started=None, completed="-1 hours"
    )
    await _leaf(
        db, "H child", ready="-8 hours", started="-7 hours", completed="-1 hours",
        parent_id=parent,
    )  # fmt: skip
    await _leaf(
        db, "K container", ready="-300 hours", started=None, completed="-1 hours",
        task_type=TaskType.feature,
    )  # fmt: skip
    await db.commit()


def _near(value: float | None, expected: float) -> None:
    assert value is not None and abs(value - expected) < 0.02, (value, expected)


async def _feature(client: AsyncClient) -> dict:
    resp = await client.get("/api/metrics/practices")
    assert resp.status_code == 200, resp.text
    return {c["work_type"]: c for c in resp.json()["cycle_times"]}["feature"]


async def test_each_interval_has_its_own_sample_and_reasons(
    db: aiosqlite.Connection, client: AsyncClient
):
    await _dataset(db)
    feature = await _feature(client)

    queue = feature["queue"]
    # A 10, B 2, G 7, H 1, Z 0 -> median 2: equal stamps are a measured zero
    assert queue["tasks"] == 5
    _near(queue["median_hours"], 2.0)
    assert queue["no_ready_at"] == 1  # C
    assert queue["no_started_at"] == 2  # D, F: no start to measure from
    assert queue["ready_after_start"] == 1  # E
    assert "zero" not in queue
    assert queue["no_completion"] == 1  # N

    since = feature["since_start"]
    # A 30, B 16, C 9, E 11, H 6, Z 5 -> median 10
    assert since["tasks"] == 6
    _near(since["median_hours"], 10.0)
    assert since["no_started_at"] == 1  # D
    assert since["start_history_unknown"] == 1  # F
    assert since["negative"] == 1  # G
    assert since["zero"] == 0
    assert since["no_completion"] == 1  # N

    # The container and the parent of a leaf are in neither interval.
    assert feature["rollup"] == {"tasks": 2}

    # The total is the old calculation, its cohort has no task_type filter:
    # A40 B18 D9 E9 F29 G5 Z5 H7 P99 K299 -> median 13.5. Without the two
    # containers it would be 9, so this fails if a filter leaks into it.
    assert feature["tasks"] == 10
    _near(feature["median_hours"], 13.5)
    assert feature["no_completion_tasks"] == 1
    assert feature["unmeasurable_tasks"] == 0


async def test_a_work_type_with_only_unmeasured_rows_still_says_so(
    db: aiosqlite.Connection, client: AsyncClient
):
    await _leaf(db, "C", ready=None, started=None, completed="-2 hours")
    await db.commit()

    feature = await _feature(client)

    assert feature["queue"]["tasks"] == 0 and feature["queue"]["median_hours"] is None
    assert feature["queue"]["no_ready_at"] == 1
    assert feature["since_start"]["median_hours"] is None
    assert feature["since_start"]["no_started_at"] == 1


async def test_the_split_is_the_same_on_every_surface(
    db: aiosqlite.Connection, client: AsyncClient
):
    from hub import mcp_server

    await _dataset(db)
    resp = await client.get(
        "/api/metrics/practices?compare=true&series=true&series_days=30"
    )
    assert resp.status_code == 200, resp.text
    rest = resp.json()
    feature = {c["work_type"]: c for c in rest["cycle_times"]}["feature"]

    # CLI: prints the REST answer as it is.
    with patch.object(cli, "_api", return_value=rest) as api:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_practice_metrics(
                argparse.Namespace(
                    since_days=90,
                    project="",
                    model="",
                    date_from="",
                    date_to="",
                    compare=True,
                    series=True,
                    series_days=30,
                )
            )
    assert rc == 0
    assert api.call_args.args[1].startswith("/api/metrics/practices?")
    printed = json.loads(buf.getvalue())
    assert printed["cycle_times"] == rest["cycle_times"]

    # MCP: the machine payload `metrics` carries the same section and indicators.
    with patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get:
        get.return_value = rest
        result = await mcp_server.hub_practice_metrics(compare=True, series=True)
    metrics = result.structuredContent["metrics"]
    assert metrics["cycle_times"] == rest["cycle_times"]
    assert metrics["comparison"] == rest["comparison"]
    text = "\n".join(block.text for block in result.content)
    assert "От начала до завершения, feature, медиана" in text

    # Comparison: the new indicator exists with its own sample, the old one
    # reads the total exactly as before.
    rows = {r["key"]: r for r in rest["comparison"]["indicators"]}
    old, new = (
        rows["cycle_time_feature_median_hours"],
        rows["cycle_since_start_feature_median_hours"],
    )
    assert (old["current"], old["current_n"]) == (13.5, feature["tasks"])
    assert (new["current"], new["current_n"]) == (
        feature["since_start"]["median_hours"],
        feature["since_start"]["tasks"],
    )
    assert new["source_field"].endswith("since_start.median_hours")

    # Series: the same pair of indicators per bucket, empty bucket is None.
    series = {s["key"]: s for s in rest["series"]["indicators"]}
    assert "cycle_since_start_feature_median_hours" in series
    assert "cycle_time_feature_median_hours" in series
    assert any(
        p["n"] == feature["since_start"]["tasks"]
        for p in series["cycle_since_start_feature_median_hours"]["points"]
    )

    # Page: both intervals are shown, with the reasons.
    page = await client.get("/metrics")
    assert page.status_code == 200
    html = page.text
    assert "Очередь и путь после начала" in html
    assert "start_history_unknown 1" in html
    assert "ready_after_start 1" in html
    assert 'data-metric="cycle_times.feature.since_start.tasks">6<' in html


@pytest.mark.parametrize("stamp", ["not a date", ""])
async def test_an_unreadable_stamp_is_a_reason_not_a_number(
    db: aiosqlite.Connection, client: AsyncClient, stamp: str
):
    task_id = await _leaf(
        db, "bad", ready="-5 hours", started="-4 hours", completed="-1 hours"
    )
    await db.execute("UPDATE tasks SET started_at=? WHERE id=?", (stamp, task_id))
    await db.commit()

    feature = await _feature(client)

    assert feature["queue"]["tasks"] == 0 and feature["since_start"]["tasks"] == 0
    assert feature["queue"]["unreadable"] == 1
    assert feature["since_start"]["unreadable"] == 1
