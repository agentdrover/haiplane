"""Outcome debt (#766): the hypotheses the Hub collects and never checks."""

from __future__ import annotations

import aiosqlite

from hub import repository as repo
from hub.services.outcomes import outcome_debt


async def _task(
    db: aiosqlite.Connection,
    *,
    title: str,
    status: str,
    outcome_metric: str,
    outcome_deadline: str = "",
) -> int:
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status=status,
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        task_id,
        outcome_metric=outcome_metric,
        outcome_indicator="an indicator",
        outcome_deadline=outcome_deadline,
        outcome_revisit_condition="if it turns out to be ritual",
    )
    await db.commit()
    return task_id


async def test_debt_list_returns_unanswered_completed_tasks(db: aiosqlite.Connection):
    first = await _task(
        db,
        title="Links lost at capture",
        status="completed",
        outcome_metric="links invisible in a card: possible -> none",
        outcome_deadline="On the next forwarded post carrying a hidden link",
    )
    second = await _task(
        db,
        title="Forward left unindexed",
        status="completed",
        outcome_metric="forwards left unindexed: 1 of 1 -> 0",
    )

    result = await outcome_debt(db)

    assert result["total"] == 2
    ids = [item["task_id"] for item in result["items"]]
    assert ids == [first, second], (
        "oldest first: the longest wait is the likeliest answerable"
    )
    head = result["items"][0]
    assert head["outcome_metric"] == "links invisible in a card: possible -> none"
    assert head["outcome_indicator"] == "an indicator"
    assert head["outcome_revisit_condition"] == "if it turns out to be ritual"
    assert head["days_unanswered"] == 0


async def test_debt_list_excludes_empty_metrics_and_unfinished_tasks(
    db: aiosqlite.Connection,
):
    kept = await _task(
        db,
        title="Completed with a stated metric",
        status="completed",
        outcome_metric="something measurable",
    )
    await _task(
        db,
        title="Completed but promised nothing",
        status="completed",
        outcome_metric="",
    )
    await _task(
        db,
        title="Still running",
        status="running",
        outcome_metric="something measurable",
    )
    await _task(
        db,
        title="Whitespace is not a promise",
        status="completed",
        outcome_metric="   ",
    )

    result = await outcome_debt(db)

    assert [item["task_id"] for item in result["items"]] == [kept]


async def test_free_text_deadline_is_shown_not_parsed(db: aiosqlite.Connection):
    """Real deadlines are event descriptions, so filtering on them hides tasks."""
    await _task(
        db,
        title="Deadline nobody can parse",
        status="completed",
        outcome_metric="a number that should move",
        outcome_deadline="Within the first 30 captures",
    )

    result = await outcome_debt(db)

    assert result["total"] == 1, "an unparseable deadline must not hide the task"
    assert result["items"][0]["outcome_deadline"] == "Within the first 30 captures"
