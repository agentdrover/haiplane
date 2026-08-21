"""Recording the answer to a task's outcome (#819).

#766 shipped the debt list read-only to find out whether these metrics can be
answered at all. #810 answered that on a live case: the numbers promised before
the release were checked against production after it, and the check matched.
But there was nowhere to put it, so the list could only ever grow — which made
it a measure of how much work had been done, not of whether anyone came back.

These tests pin the parts that make the loop closeable: an answer lands, a
second check does not erase the first, an answer without a measurement is
refused, and both sides of the debt are counted out loud.
"""

from __future__ import annotations

import argparse
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import services
from hub.models import TaskCreate


async def _completed_with_metric(
    db: aiosqlite.Connection, title: str, *, metric: str = "число X: с 0 до 5"
) -> int:
    """A completed task carrying a stated outcome — the only kind answerable."""
    tv = await services.create_task(db, TaskCreate(title=title))
    await db.execute(
        "UPDATE tasks SET status='completed', outcome_metric=?, "
        "completed_at=datetime('now', '-1 days') WHERE id=?",
        (metric, tv.id),
    )
    await db.commit()
    return tv.id


async def _answer(
    client: AsyncClient, task_id: int, **body: Any
) -> tuple[int, dict[str, Any]]:
    payload = {"verdict": "moved", "measured_value": "0 → 5"} | body
    resp = await client.post(f"/api/tasks/{task_id}/outcome-answers", json=payload)
    return resp.status_code, (resp.json() if resp.content else {})


async def _call_mcp(tool: Any) -> Any:
    """FastMCP wraps tools; older versions expose the coroutine directly."""
    return await (tool.fn() if hasattr(tool, "fn") else tool())


async def _debt(client: AsyncClient) -> dict[str, Any]:
    resp = await client.get("/api/metrics/outcome-debt")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_answer_is_recorded_on_a_completed_task(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-1. The answer lands, carries who wrote it, and leaves the task alone.

    No claim, no status change: the check happens after the work shipped and
    often by someone who did not do it.
    """
    task_id = await _completed_with_metric(db, "Metric task")

    status, result = await _answer(
        client, task_id, measured_value="17.12h → 0.81h на проде"
    )

    assert status == 200, result
    assert result["answers"] == 1
    latest = result["latest_answer"]
    assert latest["verdict"] == "moved"
    assert latest["measured_value"] == "17.12h → 0.81h на проде"
    assert latest["answered_by"], "the author comes from the authenticated identity"
    rows = await db.execute_fetchall(
        "SELECT status, claimed_by FROM tasks WHERE id=?", (task_id,)
    )
    assert rows[0]["status"] == "completed", "answering must not move the task"
    assert not rows[0]["claimed_by"], "no claim is required to answer"


async def test_second_check_does_not_overwrite_the_first(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-2. An outcome_deadline routinely names more than one moment — #810
    says "right after the release, again in two weeks". If the second check
    replaced the first, the only evidence that anyone came back twice would be
    destroyed by the act of coming back."""
    task_id = await _completed_with_metric(db, "Two checkpoints")

    await _answer(client, task_id, verdict="moved", measured_value="сразу: 0 → 5")
    status, result = await _answer(
        client, task_id, verdict="not_moved", measured_value="через 2 недели: 5 → 5"
    )

    assert status == 200, result
    assert result["answers"] == 2
    assert result["latest_answer"]["verdict"] == "not_moved"
    stored = await db.execute_fetchall(
        "SELECT verdict, measured_value FROM outcome_answers WHERE task_id=? "
        "ORDER BY id",
        (task_id,),
    )
    assert [r["verdict"] for r in stored] == ["moved", "not_moved"]
    assert "сразу" in stored[0]["measured_value"], "the first check survived"


@pytest.mark.parametrize("blank", ["", "   "])
async def test_answer_without_a_measurement_is_refused(
    db: aiosqlite.Connection, client: AsyncClient, blank: str
):
    """AC-3. An answer without a number or an observation is an opinion, and a
    log of opinions closes the loop only in appearance."""
    task_id = await _completed_with_metric(db, "No measurement")

    status, _ = await _answer(client, task_id, measured_value=blank)

    assert status == 422
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM outcome_answers WHERE task_id=?", (task_id,)
    )
    assert rows[0]["n"] == 0, "nothing may reach the log"


async def test_debt_counts_answered_and_unanswered_separately(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-4. Both numbers are named. A count that can only grow says more about
    the age of the backlog than about the habit of checking — and `not_moved`
    counts as an answer, or the list would quietly reward good news only."""
    moved = await _completed_with_metric(db, "Moved")
    stalled = await _completed_with_metric(db, "Stalled")
    await _completed_with_metric(db, "Never asked")

    await _answer(client, moved, verdict="moved", measured_value="0 → 5")
    await _answer(client, stalled, verdict="not_moved", measured_value="0 → 0")

    debt = await _debt(client)

    assert debt["total"] == 1
    assert debt["answered_total"] == 2
    assert [item["task_id"] for item in debt["items"]] not in ([moved], [stalled])
    answered = {item["task_id"]: item for item in debt["answered"]}
    assert set(answered) == {moved, stalled}
    assert answered[stalled]["latest_answer"]["verdict"] == "not_moved"


async def test_an_open_task_cannot_be_answered(
    db: aiosqlite.Connection, client: AsyncClient
):
    """The promise is about what shipping did. Answering before it ships would
    put a measurement of nothing into the log."""
    tv = await services.create_task(db, TaskCreate(title="Still open"))
    await db.execute(
        "UPDATE tasks SET outcome_metric='число X: с 0 до 5' WHERE id=?", (tv.id,)
    )
    await db.commit()

    status, result = await _answer(client, tv.id)

    assert status == 400
    assert "completed" in json.dumps(result, ensure_ascii=False)


async def test_rest_cli_and_mcp_report_the_same_answers(
    db: aiosqlite.Connection, client: AsyncClient, capsys
):
    """AC-5. Three surfaces, one payload: the contract cannot drift between
    them, because CLI and MCP render exactly what REST returned."""
    from hub import cli
    from hub import mcp_server

    task_id = await _completed_with_metric(db, "Three surfaces")
    await _answer(client, task_id, verdict="unmeasurable", measured_value="нечем")
    payload = await _debt(client)

    with patch.object(cli, "_api", MagicMock(return_value=payload)):
        cli.cmd_outcome_debt(argparse.Namespace())
    cli_output = capsys.readouterr().out

    with patch.object(mcp_server, "_api_get", AsyncMock(return_value=payload)):
        mcp_result = await _call_mcp(mcp_server.hub_outcome_debt)
    mcp_text = "\n".join(
        block.text for block in mcp_result.content if hasattr(block, "text")
    )

    assert payload["answered"][0]["latest_answer"]["verdict"] == "unmeasurable"
    assert "unmeasurable" in cli_output and "нечем" in cli_output
    assert "unmeasurable" in mcp_text and "нечем" in mcp_text
    assert mcp_result.structuredContent["outcome_debt"] == payload


async def test_migration_adds_the_table_without_touching_tasks(
    db: aiosqlite.Connection,
):
    """AC-6. The table is new and the migration is additive: production rows
    are not rewritten, and a base that predates it still reads the debt."""
    from hub.db import _migrate

    task_id = await _completed_with_metric(db, "Older than the table")
    await db.execute("DROP TABLE outcome_answers")
    await db.execute(
        "DELETE FROM _migrations WHERE name IN "
        "('create_outcome_answers_table', 'idx_outcome_answers_task')"
    )
    await db.commit()

    await _migrate(db)

    debt = await services.outcome_debt(db)
    assert debt["answered_total"] == 0
    assert [item["task_id"] for item in debt["items"]] == [task_id]
    rows = await db.execute_fetchall(
        "SELECT status, outcome_metric FROM tasks WHERE id=?", (task_id,)
    )
    assert rows[0]["status"] == "completed"
    assert rows[0]["outcome_metric"] == "число X: с 0 до 5"
