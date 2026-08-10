"""A statement remembers its date, and says what landed since (#615).

Four statements were invalidated by later work in a single day (10.08.2026) and
nothing noticed: #471 was done by the delivery gate #605 a month later under a
different name, #461 became impossible when GitHub started demanding a paid plan,
#493/#494 were satisfied by pipeline_merges instead of the "releases" table they
asked for, and #546 rested on a premise #602 had already falsified. Two of my own
statements carried wrong numbers within the hour, so shelf life is not measured in
months.

The only defence was one agent's habit of re-reading premises. #572 rejected that
same argument for verdicts after discipline failed three times; the difference
here is the price — "unreviewed code merged" there, "unnecessary work done" here.
"""

from __future__ import annotations

import aiosqlite

from hub import repository as repo
from hub.models import TaskRefine
from hub.services.statement_freshness import (
    STATE_DELIVERIES,
    STATE_NO_OVERLAP,
    STATE_NOT_CHECKED,
    statement_freshness,
)


async def _task(
    db: aiosqlite.Connection,
    *,
    title: str = "under work",
    areas: list[str] | None = None,
    prepared_at: str | None = "2026-08-01 10:00:00",
) -> int:
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    if areas is not None:
        await repo.update_task_structured(db, task_id, TaskRefine(affected_areas=areas))
    if prepared_at is not None:
        await repo.update_task(db, task_id, prepared_at=prepared_at)
    await db.commit()
    return task_id


async def _delivered(
    db: aiosqlite.Connection,
    *,
    title: str,
    areas: list[str],
    merged_at: str,
) -> int:
    """A task that was delivered by the gate at ``merged_at``."""
    task_id = await _task(db, title=title, areas=areas, prepared_at=None)
    await repo.record_pipeline_merge(
        db,
        pr_number=1000 + task_id,
        merge_sha="sha-x",
        project_id=None,
        task_id=task_id,
    )
    await db.execute(
        "UPDATE pipeline_merges SET merged_at=? WHERE task_id=?", (merged_at, task_id)
    )
    await db.commit()
    return task_id


async def test_deliveries_in_the_same_areas_are_named_with_the_date(db):
    # AC-1 (#615): the reader gets the statement's date and the deliveries that
    # could have invalidated it — by number, so they can go and read them.
    task_id = await _task(db, areas=["hub/services/orchestration.py", "docs/x.md"])
    later = await _delivered(
        db,
        title="delivery gate merges the PR",
        areas=["hub/services/orchestration.py"],
        merged_at="2026-08-05 12:00:00",
    )
    # Same area but delivered BEFORE the statement — must not appear.
    await _delivered(
        db,
        title="ancient work",
        areas=["hub/services/orchestration.py"],
        merged_at="2026-07-20 12:00:00",
    )
    # Delivered after, but nothing in common.
    await _delivered(
        db, title="unrelated", areas=["hub/web.py"], merged_at="2026-08-06 12:00:00"
    )

    row = dict(await repo.get_task(db, task_id))
    out = await statement_freshness(db, row)

    assert out["state"] == STATE_DELIVERIES
    assert out["written_at"] == "2026-08-01 10:00:00"
    assert [d["task_id"] for d in out["deliveries"]] == [later], (
        "only deliveries that came AFTER the statement and share an area"
    )
    assert out["deliveries"][0]["shared_areas"] == ["hub/services/orchestration.py"]
    assert "Перечитайте посылки" in out["reason"]


async def test_no_overlap_is_said_out_loud(db):
    # AC-2 (#615): "nothing landed here" is an answer, not an empty field. An
    # absent warning would otherwise read as confirmation — the mistake #506 and
    # #546 both made about unavailable environments.
    task_id = await _task(db, areas=["hub/services/readiness.py"])
    await _delivered(
        db, title="elsewhere", areas=["hub/web.py"], merged_at="2026-08-05 12:00:00"
    )

    out = await statement_freshness(db, dict(await repo.get_task(db, task_id)))

    assert out["state"] == STATE_NO_OVERLAP
    assert out["deliveries"] == []
    assert out["reason"], "the state must carry words, not just a label"
    assert "ЗАЯВЛЕННЫМ" in out["declared_areas_note"], (
        "the payload must admit it compares declared areas, not real diffs"
    )


async def test_an_impossible_check_says_so_with_a_reason(db):
    # AC-3 (#615): a task with no declared areas cannot be compared, and that is
    # a third outcome — not a quiet "all fresh".
    task_id = await _task(db, areas=[])
    await _delivered(
        db, title="something", areas=["hub/app.py"], merged_at="2026-08-05 12:00:00"
    )

    out = await statement_freshness(db, dict(await repo.get_task(db, task_id)))

    assert out["state"] == STATE_NOT_CHECKED
    assert "не заявлены affected_areas" in out["reason"]
    assert "не значит, что посылки свежие" in out["reason"], (
        "the reason must block the wrong inference explicitly"
    )


async def test_a_task_does_not_see_its_own_delivery(db):
    # AC-5 (#615): a resubmitted task would otherwise warn about itself.
    task_id = await _task(db, areas=["hub/app.py"])
    await repo.record_pipeline_merge(
        db, pr_number=777, merge_sha="sha-self", project_id=None, task_id=task_id
    )
    await db.execute(
        "UPDATE pipeline_merges SET merged_at=? WHERE task_id=?",
        ("2026-08-09 12:00:00", task_id),
    )
    await db.commit()

    out = await statement_freshness(db, dict(await repo.get_task(db, task_id)))

    assert out["state"] == STATE_NO_OVERLAP
    assert all(d["task_id"] != task_id for d in out["deliveries"])


async def test_freshness_never_blocks_and_never_moves_the_score(db):
    # AC-4 (#615): the check is advisory. It must not stop a start, and it must
    # not move the readiness number — a number that moves without a new fact is
    # the broken signal fixed in #610.
    from hub.services.readiness import calculate_readiness

    task_id = await _task(db, areas=["hub/app.py"])
    await _delivered(
        db, title="same area", areas=["hub/app.py"], merged_at="2026-08-05 12:00:00"
    )

    before = await calculate_readiness(db, task_id)
    out = await statement_freshness(db, dict(await repo.get_task(db, task_id)))
    after = await calculate_readiness(db, task_id)

    assert out["state"] == STATE_DELIVERIES, (
        "precondition: there IS something to warn about"
    )
    assert before.score == after.score, "age is reported, never scored"
    assert before.dor_passed == after.dor_passed

    # And a broken query degrades to not_checked instead of raising.
    class _Boom:
        async def execute_fetchall(self, *a, **k):
            raise RuntimeError("db went away")

    degraded = await statement_freshness(
        _Boom(), dict(await repo.get_task(db, task_id))
    )
    assert degraded["state"] == STATE_NOT_CHECKED
    assert "db went away" in degraded["reason"]


async def test_pair_start_and_the_brief_both_carry_it(client, db):
    # The computation is server-side precisely so every client sees it: a version
    # living in the MCP tool would leave CLI and REST blind — the "mechanism
    # right, path not wired" class that turned up ten times in two weeks.
    await _delivered(
        db, title="gate work", areas=["hub/app.py"], merged_at="2026-08-05 12:00:00"
    )
    task_id = (
        await client.post("/api/tasks", json={"title": "Freshness end to end"})
    ).json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/refine", json={"affected_areas": ["hub/app.py"]}
    )
    await repo.update_task(db, task_id, prepared_at="2026-08-01 10:00:00")
    await db.commit()
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )

    started = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert started.status_code == 200, started.text
    fresh = started.json()["statement_freshness"]
    assert fresh["state"] == STATE_DELIVERIES
    assert fresh["deliveries"], "pair-start must carry the deliveries, not just a state"

    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief["statement_freshness"]["state"] == STATE_DELIVERIES, (
        "the reviewer judges the statement too and needs the same warning"
    )
