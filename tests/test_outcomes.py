"""Outcome debt (#766): the hypotheses the Hub collects and never checks."""

from __future__ import annotations

import aiosqlite
import pytest

from hub import repository as repo
from hub.services.outcomes import outcome_debt
from tests.test_outcome_status import _completed_task, _fix_release


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


async def test_outcome_debt_marks_assumed_due(db: aiosqlite.Connection):
    """#1572: a due date taken from the merge says so; a measured one does not."""
    import json
    from datetime import UTC, datetime, timedelta

    pid = await repo.create_project(db, slug="local-app", name="Local app")
    await repo.update_project(
        db, pid, gate_policy=json.dumps({"merge_is_delivery": True})
    )
    task_id = await _task(
        db, title="Local fix", status="completed", outcome_metric="a number moves"
    )
    await repo.record_pipeline_merge(
        db,
        pr_number=1,
        merge_sha="s" * 40,
        project_id=pid,
        task_id=task_id,
        merged_at=(datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    await db.commit()

    item = (await outcome_debt(db))["items"][0]

    assert item["due_on"] == (datetime.now(UTC) + timedelta(days=11)).date().isoformat()
    assert item["due_assumed"] is True


_FULL_KEYS = {"items", "answered", "overdue", "observing", "unknown"}
_COUNT_KEYS = {
    "total",
    "answered_total",
    "overdue_total",
    "observing_total",
    "unknown_total",
    "window_days",
}


async def seed_debt(
    db: aiosqlite.Connection,
    *,
    overdue: int = 0,
    observing: int = 0,
    unknown: int = 0,
    answered: int = 0,
) -> None:
    """Completed tasks in each debt state (#1605): a fix released 20 days ago is
    overdue, 3 days ago observing, never released unknown."""
    n = 0

    async def _one(kind: str, days_ago: float | None) -> int:
        nonlocal n
        n += 1
        task_id = await _completed_task(db, title=f"{kind} {n}", metric="a number")
        if days_ago is not None:
            await _fix_release(db, task_id, days_ago=days_ago, sha=f"{n:040x}")
        return task_id

    for _ in range(overdue):
        await _one("overdue", 20)
    for _ in range(observing):
        await _one("observing", 3)
    for _ in range(unknown):
        await _one("unknown", None)
    for _ in range(answered):
        task_id = await _one("answered", None)
        await repo.record_outcome_answer(
            db, task_id=task_id, verdict="moved", measured_value="0 -> 5"
        )
    await db.commit()


async def test_outcome_debt_without_params_keeps_full_contract(
    db: aiosqlite.Connection,
):
    """AC-1. No parameters: the full payload, every list and counter in place."""
    await seed_debt(db, overdue=3, observing=2, unknown=2, answered=1)

    result = await outcome_debt(db)

    assert _FULL_KEYS <= set(result) and _COUNT_KEYS <= set(result)
    assert (len(result["overdue"]), len(result["observing"])) == (3, 2)
    assert (len(result["unknown"]), len(result["answered"])) == (2, 1)
    assert len(result["items"]) == 7 == result["total"]
    assert result["answered_total"] == 1
    assert "rows" not in result


async def test_outcome_debt_only_counts_has_no_rows(db: aiosqlite.Connection):
    """AC-3. only_counts: counters and window_days, no rows and no lists."""
    await seed_debt(db, overdue=3, observing=1, unknown=1, answered=1)

    result = await outcome_debt(db, only_counts=True)

    assert set(result) == _COUNT_KEYS
    assert result["overdue_total"] == 3 and result["total"] == 5


async def test_outcome_debt_pages_one_status_in_full_order(
    db: aiosqlite.Connection,
):
    """AC-2 (service). A page is a slice of the full list; counters stay full."""
    await seed_debt(db, overdue=45, observing=2, unknown=1, answered=1)
    full = await outcome_debt(db)

    first = await outcome_debt(db, status="overdue", limit=20)
    second = await outcome_debt(db, status="overdue", limit=20, offset=20)
    last = await outcome_debt(db, status="overdue", limit=20, offset=40)

    assert first["rows"] == full["overdue"][:20] and first["next_offset"] == 20
    assert second["rows"] == full["overdue"][20:40]
    assert (second["total_in_status"], second["next_offset"]) == (45, 40)
    assert len(last["rows"]) == 5 and last["next_offset"] is None
    assert second["overdue_total"] == 45 and second["observing_total"] == 2
    assert not _FULL_KEYS & set(second)


async def test_outcome_debt_filters_each_status(db: aiosqlite.Connection):
    await seed_debt(db, overdue=2, observing=3, unknown=1, answered=4)

    sizes = {
        status: (await outcome_debt(db, status=status))["total_in_status"]
        for status in ("overdue", "observing", "unknown", "answered")
    }
    answered = await outcome_debt(db, status="answered")

    assert sizes == {"overdue": 2, "observing": 3, "unknown": 1, "answered": 4}
    assert all("latest_answer" in row for row in answered["rows"])


async def test_outcome_debt_limit_alone_pages_overdue(db: aiosqlite.Connection):
    await seed_debt(db, overdue=3, observing=3)

    result = await outcome_debt(db, limit=2)

    assert result["status"] == "overdue" and len(result["rows"]) == 2
    assert result["limit"] == 2 and result["offset"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"status": "nope"}],
)
async def test_outcome_debt_rejects_bad_paging(db: aiosqlite.Connection, kwargs: dict):
    with pytest.raises(ValueError):
        await outcome_debt(db, **kwargs)
