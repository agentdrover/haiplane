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


_FROZEN = "2026-01-01 00:00:00"
_FULL_NOTE = (
    "Every task in `items` promised a number would move and was never "
    "asked whether it did. `answered` holds the ones somebody came back "
    "to, with the last verdict and what was measured - including "
    "not_moved and unmeasurable, which are answers too. "
    "`overdue`: the fix first reached production 14 "
    "days ago or more (`due_on` passed). `observing`: the window is "
    "still open. `unknown`: no recorded release of the fix (no merge, "
    "unreleased merge, or a release the hub never saw) - a gap in "
    "the record, not a debt. The due date is a machine fact, not a "
    "parse of outcome_deadline: that is free text and is not used for "
    "filtering, so nothing is hidden behind a value that cannot be parsed. "
    "`due_assumed`: the date counts from the merge, because the project "
    "declared merge = delivery (gate_policy `merge_is_delivery`), not from "
    "a recorded deploy."
)


async def _frozen_debt_base(db: aiosqlite.Connection) -> dict:
    """One task per state with every clock pinned, and the payload the full
    response must equal (#1605 AC-1): before the paging change, verbatim."""
    from datetime import UTC, datetime, timedelta

    await seed_debt(db, overdue=1, observing=1, unknown=1, answered=1)
    await db.execute("UPDATE tasks SET completed_at=?", (_FROZEN,))
    await db.execute("UPDATE outcome_answers SET answered_at=?", (_FROZEN,))
    await db.execute(
        "UPDATE releases SET deployed_at=? WHERE deployed_sha=?",
        (_FROZEN, f"{1:040x}"),
    )
    await db.commit()
    observing_at = datetime.fromisoformat(
        (
            await (
                await db.execute(
                    "SELECT deployed_at FROM releases WHERE deployed_sha=?",
                    (f"{2:040x}",),
                )
            ).fetchone()
        )[0].replace(" ", "T")
    )
    waited = (datetime.now(UTC) - datetime(2026, 1, 1, tzinfo=UTC)).days

    def row(task_id: int, kind: str, status: str, due: str | None) -> dict:
        return {
            "task_id": task_id,
            "title": f"{kind} {task_id}",
            "task_type": "feature",
            "outcome_metric": "a number",
            "outcome_indicator": "",
            "outcome_deadline": "",
            "outcome_revisit_condition": "",
            "completed_at": _FROZEN,
            "days_unanswered": waited,
            "outcome_status": status,
            "due_on": due,
            "due_assumed": False,
        }

    overdue = row(1, "overdue", "unanswered", "2026-01-15")
    observing = row(
        2,
        "observing",
        "not_due",
        (observing_at + timedelta(days=14)).date().isoformat(),
    )
    unknown = row(3, "unknown", "unknown", None)
    answered = row(4, "answered", "confirmed", None) | {
        "answers": 1,
        "latest_answer": {
            "id": 1,
            "verdict": "moved",
            "measured_value": "0 -> 5",
            "note": "",
            "answered_by": "",
            "answered_at": _FROZEN,
            "hypothesis_snapshot": None,
        },
    }
    return {
        "total": 3,
        "answered_total": 1,
        "items": [overdue, observing, unknown],
        "answered": [answered],
        "overdue": [overdue],
        "overdue_total": 1,
        "observing": [observing],
        "observing_total": 1,
        "unknown": [unknown],
        "unknown_total": 1,
        "window_days": 14,
        "note": _FULL_NOTE,
    }


async def test_outcome_debt_without_params_keeps_full_contract(
    db: aiosqlite.Connection,
):
    """AC-1. No parameters: the whole payload equals the pre-paging one, key by
    key, row by row, note included."""
    expected = await _frozen_debt_base(db)

    assert await outcome_debt(db) == expected
    assert list((await outcome_debt(db))) == list(expected)


async def test_outcome_debt_rest_without_params_equals_full_payload(
    db: aiosqlite.Connection, client
):
    """AC-1 (REST). The serialized answer without parameters is that payload."""
    expected = await _frozen_debt_base(db)

    resp = await client.get("/api/metrics/outcome-debt")

    assert resp.status_code == 200
    assert resp.json() == expected


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
