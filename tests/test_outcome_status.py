"""Derived outcome-hypothesis status (#576).

The answer store already exists (#819). This module pins the states that
must stay machine-distinct: no hypothesis, not yet due, due with no answer,
answered, and revised after the metric changed. Due/not-due come from the
last successful release (#839), never from parsing free-text outcome_deadline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.services.outcomes import derive_outcome_status, outcome_debt


async def _project(db: aiosqlite.Connection, slug: str = "ship") -> int:
    return await repo.create_project(db, slug=slug, name=slug.title())


async def _completed_task(
    db: aiosqlite.Connection,
    *,
    title: str,
    metric: str,
    project_id: int | None = None,
    completed_at: str = "datetime('now', '-2 days')",
) -> int:
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="completed",
        auto_review=True,
        task_type="feature",
        parent_id=None,
        priority="medium",
    )
    await db.execute(
        "UPDATE tasks SET outcome_metric=?, project_id=?, "
        f"completed_at={completed_at} WHERE id=?",
        (metric, project_id, task_id),
    )
    await db.commit()
    return task_id


_PR = iter(range(9000, 99999))


async def _fix_release(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    days_ago: float,
    sha: str,
    release_project_id: int | None = None,
) -> None:
    """The task's merge, stamped with a release first deployed ``days_ago`` ago."""
    await repo.record_pipeline_merge(
        db, pr_number=next(_PR), merge_sha=f"m{sha}", task_id=task_id
    )
    await db.execute(
        "UPDATE pipeline_merges SET released_pr=1, released_sha=? WHERE id = "
        "(SELECT MAX(id) FROM pipeline_merges WHERE task_id=?)",
        (f"  {sha.upper()} ", task_id),
    )
    await repo.record_release(
        db, deployed_sha=sha, project_id=release_project_id, ref="main", source="ci"
    )
    await db.execute(
        "UPDATE releases SET deployed_at=datetime('now', ?) WHERE deployed_sha=?",
        (f"-{days_ago} days", sha),
    )
    await db.commit()


async def _release_after_completion(
    db: aiosqlite.Connection, task_id: int, sha: str = "a" * 40
) -> None:
    await _fix_release(db, task_id, days_ago=20, sha=sha)


async def test_not_due_and_unanswered_are_distinct(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-1. Due vs not-due come from a machine carrier, not outcome_deadline."""
    project_id = await _project(db)
    waiting = await _completed_task(
        db,
        title="Hypothesis waiting for a deploy",
        metric="lead time 3d -> 1d",
        project_id=project_id,
    )
    due = await _completed_task(
        db,
        title="Hypothesis after a deploy",
        metric="lead time 3d -> 1d",
        project_id=project_id,
    )
    await db.execute(
        "UPDATE tasks SET outcome_deadline=? WHERE id IN (?, ?)",
        ("Within the first 30 captures", waiting, due),
    )
    await db.commit()
    # Same free-text deadline, different machine states: the fix of `waiting`
    # went out 3 days ago (window open), the fix of `due` 20 days ago.
    await _fix_release(db, waiting, days_ago=3, sha="a" * 40)
    await _fix_release(db, due, days_ago=20, sha="f" * 40)
    waiting_body = (await client.get(f"/api/tasks/{waiting}")).json()
    due_body = (await client.get(f"/api/tasks/{due}")).json()

    assert waiting_body["outcome_status"] == "not_due"
    assert due_body["outcome_status"] == "unanswered"
    assert waiting_body["outcome_status"] != due_body["outcome_status"]
    assert waiting_body["outcome_deadline"] == due_body["outcome_deadline"]


async def test_no_hypothesis_is_never_overdue(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-2. Empty metric is no_hypothesis and stays out of overdue samples."""
    project_id = await _project(db, slug="bare")
    task_id = await _completed_task(
        db, title="Typical technical backlog", metric="", project_id=project_id
    )
    await _release_after_completion(db, task_id, sha="b" * 40)

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    debt = await outcome_debt(db)

    assert body["outcome_status"] == "no_hypothesis"
    assert task_id not in [item["task_id"] for item in debt["items"]]
    assert task_id not in [item["task_id"] for item in debt["overdue"]]


async def test_status_is_derived_from_answers_not_a_stored_column(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-3. The verdict lives in outcome_answers; tasks have no status column."""
    project_id = await _project(db, slug="answered")
    task_id = await _completed_task(
        db, title="Answered hypothesis", metric="X: 0 -> 5", project_id=project_id
    )
    await _release_after_completion(db, task_id, sha="c" * 40)
    resp = await client.post(
        f"/api/tasks/{task_id}/outcome-answers",
        json={"verdict": "moved", "measured_value": "0 → 5 on prod"},
    )
    assert resp.status_code == 200, resp.text

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    cols = {
        row["name"] for row in await db.execute_fetchall("PRAGMA table_info(tasks)")
    }

    assert "outcome_status" not in cols
    assert body["outcome_status"] == "confirmed"


async def test_rewritten_metric_reads_as_revised(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-4. An answer to the old metric is not an answer to the new one."""
    project_id = await _project(db, slug="rewritten")
    task_id = await _completed_task(
        db,
        title="Metric will change",
        metric="old number: 0 -> 5",
        project_id=project_id,
    )
    await client.post(
        f"/api/tasks/{task_id}/outcome-answers",
        json={"verdict": "moved", "measured_value": "5 on prod"},
    )
    await repo.update_task(db, task_id, outcome_metric="new number: 10 -> 20")
    await db.commit()

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["outcome_status"] == "revised"
    assert body["outcome_status"] != "confirmed"


async def test_legacy_answer_without_snapshot_is_not_revised(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-5. Pre-snapshot answers read as answered, not revised."""
    project_id = await _project(db, slug="legacy")
    task_id = await _completed_task(
        db,
        title="Answer from before the snapshot",
        metric="X: 0 -> 5",
        project_id=project_id,
    )
    await db.execute(
        "INSERT INTO outcome_answers (task_id, verdict, measured_value, note, "
        "answered_by) VALUES (?, 'moved', '0 → 5', '', 'owner')",
        (task_id,),
    )
    await db.commit()

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["outcome_status"] == "confirmed"
    assert body["outcome_status"] != "revised"


async def test_review_brief_and_card_expose_status(
    db: aiosqlite.Connection, client: AsyncClient
):
    project_id = await _project(db, slug="surfaces")
    task_id = await _completed_task(
        db, title="Visible status", metric="a number", project_id=project_id
    )
    await _release_after_completion(db, task_id, sha="d" * 40)

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    card = await client.get(f"/tasks/{task_id}")

    assert brief["outcome_status"] == "unanswered"
    assert card.status_code == 200
    assert 'data-outcome-status="unanswered"' in card.text
    assert "Срок наступил, ответа нет" in card.text


async def test_overdue_sample_uses_machine_deadline(db: aiosqlite.Connection):
    """AC-6. A release with no merge of the task is not its fix: unknown."""
    project_id = await _project(db, slug="overdue")
    without_merge = await _completed_task(
        db, title="Release but no merge", metric="a number", project_id=project_id
    )
    due = await _completed_task(
        db, title="Shipped, no answer", metric="a number", project_id=project_id
    )
    await repo.record_release(
        db, deployed_sha="e" * 40, project_id=project_id, ref="main", source="ci"
    )
    await db.execute("UPDATE releases SET deployed_at=datetime('now', '-30 days')")
    await db.commit()
    await _fix_release(db, due, days_ago=20, sha="9" * 40)

    debt = await outcome_debt(db)
    overdue_ids = [item["task_id"] for item in debt["overdue"]]
    by_id = {item["task_id"]: item["outcome_status"] for item in debt["items"]}

    assert by_id[without_merge] == "unknown"
    assert by_id[due] == "unanswered"
    assert due in overdue_ids
    assert without_merge not in overdue_ids


async def test_window_not_reached_is_observing(db: aiosqlite.Connection):
    """AC-1. Fix deployed 3 days ago: observing, due_on = deploy + 14 days."""
    task_id = await _completed_task(db, title="Fresh fix", metric="a number")
    await _fix_release(db, task_id, days_ago=3, sha="1" * 40)

    debt = await outcome_debt(db)
    item = next(i for i in debt["items"] if i["task_id"] == task_id)
    expected = (datetime.now(UTC) + timedelta(days=11)).date().isoformat()

    assert item["outcome_status"] == "not_due"
    assert item["due_on"] == expected
    assert task_id in [i["task_id"] for i in debt["observing"]]
    assert task_id not in [i["task_id"] for i in debt["overdue"]]
    assert debt["observing_total"] == 1 and debt["overdue_total"] == 0


async def test_window_reached_is_overdue(db: aiosqlite.Connection):
    """AC-2. Fix first deployed 20 days ago: overdue. A later re-deploy of the
    same sha does not move the anchor; sha match is trim+lower, no project_id."""
    task_id = await _completed_task(db, title="Old fix", metric="a number")
    await _fix_release(db, task_id, days_ago=20, sha="2" * 40, release_project_id=None)
    await repo.record_release(
        db, deployed_sha="2" * 40, project_id=7, ref="main", source="ci"
    )

    debt = await outcome_debt(db)

    assert [i["task_id"] for i in debt["overdue"]] == [task_id]
    assert debt["overdue_total"] == 1
    assert debt["observing_total"] == 0 and debt["unknown_total"] == 0


async def test_no_fix_release_is_unknown(db: aiosqlite.Connection):
    """AC-3. No release of the fix: unknown, counted, never overdue."""
    merged_unreleased = await _completed_task(db, title="Merged", metric="a number")
    await repo.record_pipeline_merge(
        db, pr_number=next(_PR), merge_sha="abc", task_id=merged_unreleased
    )
    sha_without_row = await _completed_task(db, title="No row", metric="a number")
    await repo.record_pipeline_merge(
        db, pr_number=next(_PR), merge_sha="def", task_id=sha_without_row
    )
    await db.execute(
        "UPDATE pipeline_merges SET released_sha='dead' WHERE task_id=?",
        (sha_without_row,),
    )
    await db.commit()
    no_merge = await _completed_task(db, title="Nothing", metric="a number")

    debt = await outcome_debt(db)
    by_id = {i["task_id"]: i for i in debt["items"]}

    for tid in (merged_unreleased, sha_without_row, no_merge):
        assert by_id[tid]["outcome_status"] == "unknown"
        assert by_id[tid]["due_on"] is None
    assert debt["unknown_total"] == 3
    assert [i["task_id"] for i in debt["unknown"]] == [
        merged_unreleased,
        sha_without_row,
        no_merge,
    ]
    assert debt["overdue_total"] == 0


async def test_epic_due_from_children_fix_release(db: aiosqlite.Connection):
    """AC-4. Epic and feature without a merge of their own: the latest fix
    release among descendants sets the deadline."""
    epic = await _completed_task(db, title="Epic", metric="a number")
    feature = await _completed_task(db, title="Feature", metric="a number")
    old_child = await _completed_task(db, title="Old child", metric="")
    new_child = await _completed_task(db, title="New child", metric="")
    grandchild = await _completed_task(db, title="Grandchild", metric="")
    await db.execute("UPDATE tasks SET task_type='epic' WHERE id=?", (epic,))
    await db.execute("UPDATE tasks SET parent_id=? WHERE id=?", (epic, feature))
    await db.execute("UPDATE tasks SET parent_id=? WHERE id=?", (epic, old_child))
    await db.execute("UPDATE tasks SET parent_id=? WHERE id=?", (feature, new_child))
    await db.execute("UPDATE tasks SET parent_id=? WHERE id=?", (new_child, grandchild))
    await db.commit()
    await _fix_release(db, old_child, days_ago=40, sha="3" * 40)
    await _fix_release(db, grandchild, days_ago=20, sha="4" * 40)

    debt = await outcome_debt(db)
    overdue = [i["task_id"] for i in debt["overdue"]]
    assert epic in overdue and feature in overdue

    # The latest child release still inside the window keeps the epic observing.
    fresh = await _completed_task(db, title="Fresh child", metric="")
    await db.execute("UPDATE tasks SET parent_id=? WHERE id=?", (epic, fresh))
    await db.commit()
    await _fix_release(db, fresh, days_ago=2, sha="5" * 40)
    debt = await outcome_debt(db)
    by_id = {i["task_id"]: i["outcome_status"] for i in debt["items"]}
    assert by_id[epic] == "not_due"
    assert by_id[feature] == "unanswered"


async def test_card_and_debt_agree(db: aiosqlite.Connection, client: AsyncClient):
    """AC-5. Card, brief and debt read one function."""
    fresh = await _completed_task(db, title="Fresh", metric="a number")
    old = await _completed_task(db, title="Old", metric="a number")
    none = await _completed_task(db, title="None", metric="a number")
    await _fix_release(db, fresh, days_ago=1, sha="6" * 40)
    await _fix_release(db, old, days_ago=30, sha="7" * 40)

    debt = await outcome_debt(db)
    by_id = {i["task_id"]: i["outcome_status"] for i in debt["items"]}
    assert set(by_id.values()) == {"not_due", "unanswered", "unknown"}
    for tid in (fresh, old, none):
        card = (await client.get(f"/api/tasks/{tid}")).json()
        brief = (await client.get(f"/api/tasks/{tid}/review-brief")).json()
        assert card["outcome_status"] == by_id[tid] == brief["outcome_status"]
    page = await client.get(f"/tasks/{none}")
    assert 'data-outcome-status="unknown"' in page.text
    assert "Релиз фикса неизвестен" in page.text


async def test_derive_maps_verdicts_and_ignores_deadline_text():
    """Pure mapping: one stored fact, one status. Free-text deadline unused."""
    answers = [{"verdict": "not_moved", "hypothesis_snapshot": "metric"}]
    assert (
        derive_outcome_status(
            outcome_metric="metric",
            answers=answers,
            fix_released_at="2026-02-01T00:00:00+00:00",
        )
        == "refuted"
    )
    assert (
        derive_outcome_status(
            outcome_metric="",
            answers=[],
            fix_released_at="2026-02-01T00:00:00+00:00",
        )
        == "no_hypothesis"
    )
    assert (
        derive_outcome_status(
            outcome_metric="metric",
            answers=[],
            fix_released_at=None,
        )
        == "unknown"
    )


async def test_mcp_and_cli_show_the_three_buckets(db: aiosqlite.Connection):
    """MCP text names overdue/observing/unknown counts and the due date; the CLI
    prints the REST payload as is, so the new fields reach it unchanged."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from hub import cli, mcp_server

    fresh = await _completed_task(db, title="Fresh", metric="a number")
    await _fix_release(db, fresh, days_ago=1, sha="8" * 40)
    await _completed_task(db, title="No fix", metric="a number")
    payload = await outcome_debt(db)

    with patch.object(mcp_server, "_api_get", AsyncMock(return_value=payload)):
        tool = mcp_server.hub_outcome_debt
        result = await (tool.fn() if hasattr(tool, "fn") else tool())
    text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
    with patch.object(cli, "_api", MagicMock(return_value=payload)):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_outcome_debt(MagicMock())

    assert "0 overdue, 1 observing, 1 unknown" in text
    assert f"due {payload['observing'][0]['due_on']}" in text
    assert '"unknown_total": 1' in buf.getvalue()


async def test_anchor_is_the_last_merge_of_the_task(db: aiosqlite.Connection):
    """AC-1/AC-2. A re-opened task is anchored on its LAST merge's release."""
    task_id = await _completed_task(db, title="Merged twice", metric="a number")
    await _fix_release(db, task_id, days_ago=40, sha="a1" * 20)
    await _fix_release(db, task_id, days_ago=2, sha="b2" * 20)

    debt = await outcome_debt(db)

    assert [i["task_id"] for i in debt["observing"]] == [task_id]
    assert debt["overdue_total"] == 0
