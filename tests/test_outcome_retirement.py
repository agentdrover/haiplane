"""Снятие исторических обещаний исхода решением владельца (#2091, шаг 1C).

Журнал «проверка снята, результат неизвестен» - не ответ (#819) и не архив:
``tasks.archived``, lifecycle и ``outcome_answers`` не меняются. Предпросмотр
фиксирует когорту, применение пересчитывает отпечатки внутри транзакции.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.db import _migrate
from hub.services.outcome_retirement import (
    apply_retirement,
    cancel_retirement,
    preview_retirement,
)
from hub.services.outcomes import outcome_debt
from tests.test_outcome_ownership import _task

CUTOFF = "2026-10-01T00:00:00Z"


async def _at(db: aiosqlite.Connection, task_id: int, completed_at: str | None):
    await db.execute(
        "UPDATE tasks SET completed_at=? WHERE id=?", (completed_at, task_id)
    )
    await db.commit()


async def _old(db: aiosqlite.Connection, title: str = "Old", **kw: Any) -> int:
    task_id = await _task(db, title, **kw)
    await _at(db, task_id, "2026-09-10 12:00:00")
    return task_id


async def _preview(db: aiosqlite.Connection, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "cutoff": CUTOFF,
        "projects": ["default"],
        "created_by": "denis",
    }
    return await preview_retirement(db, **{**args, **kw})


async def _apply(db: aiosqlite.Connection, token: str, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "token": token,
        "reason": "history, nobody will check",
        "retired_by": "denis",
        "principal_id": 1,
    }
    return await apply_retirement(db, **{**args, **kw})


async def _retired_rows(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    return list(
        await db.execute_fetchall(
            "SELECT * FROM outcome_retirements ORDER BY id",
        )
    )


async def _counts(db: aiosqlite.Connection) -> dict[str, Any]:
    return await outcome_debt(db, only_counts=True)


async def _guarded_state(db: aiosqlite.Connection) -> list[Any]:
    """Всё, что снятие трогать не вправе."""
    out = []
    for sql in (
        "SELECT id, status, archived, completed_at, outcome_metric, updated_at "
        "FROM tasks ORDER BY id",
        "SELECT * FROM outcome_answers ORDER BY id",
    ):
        out.append([tuple(r) for r in await db.execute_fetchall(sql)])
    return out


async def test_migration_is_additive_and_idempotent(db: aiosqlite.Connection):
    """AC-1. Схема создаётся идемпотентно, задачи и ответы целы, никто не снят."""
    old = await _old(db)
    await repo.record_outcome_answer(
        db,
        task_id=old,
        verdict="moved",
        measured_value="1",
        note="",
        answered_by="x",
        hypothesis_snapshot="a number",
    )
    await db.commit()
    before = await _guarded_state(db)

    await _migrate(db)
    await _migrate(db)

    tables = {
        r[0]
        for r in await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"outcome_retirements", "outcome_retirement_previews"} <= tables
    assert await _guarded_state(db) == before
    assert await _retired_rows(db) == []
    assert (await _counts(db))["retired_total"] == 0, "nothing retired by itself"
    cols = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(tasks)")}
    assert "retired" not in cols, "no new task state and no new TaskStatus"


async def test_owner_applies_only_reviewed_cohort(
    db: aiosqlite.Connection, client: AsyncClient, monkeypatch
):
    """AC-2. Граница, ответ, без даты, чужой проект, смена после предпросмотра."""
    from hub import config
    from hub.config import TokenIdentity

    other_epic = await _task(db, "Other project epic", task_type="epic", metric="")
    other_project = await repo.create_project(db, slug="other", name="Other")
    await db.execute(
        "UPDATE tasks SET project_id=? WHERE id=?", (other_project, other_epic)
    )
    await db.commit()

    before_edge = await _task(db, "Just before")
    await _at(db, before_edge, "2026-09-30 23:59:59")
    on_edge = await _task(db, "Exactly on cutoff")
    await _at(db, on_edge, "2026-10-01 00:00:00")
    after_edge = await _task(db, "After")
    await _at(db, after_edge, "2026-10-05 00:00:00")
    answered = await _old(db, "Answered")
    await repo.record_outcome_answer(
        db,
        task_id=answered,
        verdict="not_moved",
        measured_value="0",
        note="",
        answered_by="x",
        hypothesis_snapshot="a number",
    )
    undated = await _task(db, "No date")
    await _at(db, undated, None)
    await db.execute(
        "UPDATE tasks SET updated_at='2026-01-01 00:00:00' WHERE id=?", (undated,)
    )
    foreign = await _old(db, "Other project", parent_id=other_epic)
    drifting = await _old(db, "Changes after preview")
    plain = await _old(db, "Plain old")
    await db.commit()

    with pytest.raises(ValueError):
        await _preview(db, cutoff="2026-10-01T00:00:00")  # no time zone
    with pytest.raises(ValueError):
        await _preview(db, projects=[])  # scope must be explicit
    # The same instant in another zone is the same boundary.
    shifted = await _preview(db, cutoff="2026-10-01T03:00:00+03:00")
    assert {c["task_id"] for c in shifted["cohort"]} == {
        c["task_id"] for c in (await _preview(db))["cohort"]
    }

    prev = await _preview(db)
    assert {c["task_id"] for c in prev["cohort"]} == {before_edge, drifting, plain}
    assert [u["task_id"] for u in prev["undated"]] == [undated], (
        "no guess by updated_at"
    )
    assert prev["cohort_total"] == 3
    for absent in (on_edge, after_edge, answered, foreign, undated):
        assert absent not in {c["task_id"] for c in prev["cohort"]}

    await db.execute(
        "UPDATE tasks SET outcome_metric='a different promise' WHERE id=?", (drifting,)
    )
    await db.commit()
    before = await _guarded_state(db)
    result = await _apply(db, prev["token"])

    assert sorted(result["retired"]) == sorted([before_edge, plain])
    assert result["skipped"] == [
        {"task_id": drifting, "reason": "changed_since_preview"}
    ]
    rows = await _retired_rows(db)
    assert sorted(r["task_id"] for r in rows) == sorted([before_edge, plain])
    for row in rows:
        assert row["retired_by"] == "denis" and row["principal_id"] == 1
        assert row["reason"] == "history, nobody will check"
        assert row["batch_id"] == prev["token"]
        assert row["cutoff"] and row["context_snapshot"]
    state_after = await _guarded_state(db)
    assert state_after[1] == before[1], "outcome_answers untouched"
    assert state_after[0] == before[0], "tasks untouched"

    counts = await _counts(db)
    assert counts["retired_total"] == 2
    # Only a human may do it.
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-token": TokenIdentity("impl-bot", "agent"),
            "human-token": TokenIdentity("denis", "human", 1),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    body = {"cutoff": CUTOFF, "projects": ["default"]}
    denied = await client.post(
        "/api/outcome-retirements/preview",
        json=body,
        headers={"Authorization": "Bearer agent-token"},
    )
    assert denied.status_code == 403
    denied = await client.post(
        "/api/outcome-retirements/apply",
        json={"token": prev["token"], "reason": "x"},
        headers={"Authorization": "Bearer agent-token"},
    )
    assert denied.status_code == 403
    allowed = await client.post(
        "/api/outcome-retirements/preview",
        json=body,
        headers={"Authorization": "Bearer human-token"},
    )
    assert allowed.status_code == 200
    from hub import mcp_server

    names = set(mcp_server.mcp._tool_manager._tools)
    assert not {n for n in names if "retire" in n or "retirement" in n}, (
        "no agent-visible tool may write the retirement"
    )


async def test_retirement_is_atomic_and_retry_safe(
    db: aiosqlite.Connection, db_dsn: str
):
    """AC-3. Повтор, два соединения, сбой аудита: ни дублей, ни половины."""
    tasks = [await _old(db, f"Old {i}") for i in range(3)]
    prev = await _preview(db)
    assert prev["cohort_total"] == 3
    twin = await _preview(db)  # a second preview of the same cohort
    before = await _guarded_state(db)

    # Audit failure (the journal write): neither rows nor a half result survive.
    calls = 0
    real = repo.insert_outcome_retirement

    async def flaky(*a: Any, **k: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("journal down")
        return await real(*a, **k)

    quiet = "SELECT (SELECT COUNT(*) FROM task_updates), (SELECT COUNT(*) FROM events)"
    feed_before = [tuple(r) for r in await db.execute_fetchall(quiet)]
    with (
        patch.object(repo, "insert_outcome_retirement", flaky),
        pytest.raises(RuntimeError),
    ):
        await _apply(db, prev["token"])
    await db.rollback()
    assert await _retired_rows(db) == []

    # Two connections applying the same token: one set of rows.
    from hub.db import connect

    other = await connect(db_dsn)
    try:
        first, second = await asyncio.gather(
            _apply(db, prev["token"]), _apply(other, prev["token"])
        )
    finally:
        await other.close()
    fresh = [r for r in (first, second) if not r["repeated"]]
    assert len(fresh) == 1, "a token applies once, whoever gets the lock first"
    assert sorted(fresh[0]["retired"]) == sorted(tasks)
    rows = await _retired_rows(db)
    assert sorted(r["task_id"] for r in rows) == sorted(tasks), "no duplicates"

    # No feed line, no event, no alert: 630 of them would wake every listener.
    assert [tuple(r) for r in await db.execute_fetchall(quiet)] == feed_before

    # Another preview of the same cohort cannot retire it twice.
    dup = await _apply(db, twin["token"])
    assert dup["retired"] == []
    assert {s["reason"] for s in dup["skipped"]} == {"already_retired"}

    # Retry of the same token returns the stored outcome and writes nothing.
    again = await _apply(db, prev["token"])
    assert again["repeated"] is True and again["retired"] == fresh[0]["retired"]
    assert len(await _retired_rows(db)) == 3
    assert (await _guarded_state(db))[1] == before[1]
    assert (await _guarded_state(db))[0] == before[0]
    assert (await _counts(db))["retired_total"] == 3

    # A late retry cannot replay over a later cancel.
    await cancel_retirement(
        db, task_id=tasks[0], reason="back", cancelled_by="denis", principal_id=1
    )
    await _apply(db, prev["token"])
    assert (await _counts(db))["retired_total"] == 2
    assert len(await _retired_rows(db)) == 3

    # A stale or unknown token is refused as a whole.
    with pytest.raises(LookupError):
        await _apply(db, "no-such-token")


async def test_later_answer_and_new_context_are_handled_explicitly(
    db: aiosqlite.Connection,
):
    """AC-4. Поздний ответ сильнее; отмена возвращает в долг; новый контекст не освобождён."""
    late = await _old(db, "Late answer")
    cancelled = await _old(db, "Cancelled")
    metric_changed = await _old(db, "Metric changed")
    redelivered = await _old(db, "Completed again")
    prev = await _preview(db)
    await _apply(db, prev["token"])
    base = await _counts(db)
    assert base["retired_total"] == 4

    # 1. A late answer wins; the retirement stays in history.
    await repo.record_outcome_answer(
        db,
        task_id=late,
        verdict="moved",
        measured_value="measured late",
        note="",
        answered_by="x",
        hypothesis_snapshot="a number",
    )
    await db.commit()
    after = await _counts(db)
    assert after["retired_total"] == 3
    assert after["answered_total"] == base["answered_total"] + 1
    states = {r["task_id"]: r["state"] for r in (await _history(db))}
    assert states[late] == "superseded_by_answer"
    assert any(r["task_id"] == late for r in await _retired_rows(db)), "history kept"

    # 2. Cancel returns the promise to debt and keeps the old decision.
    debt_before = (await _counts(db))["total"]
    await cancel_retirement(
        db,
        task_id=cancelled,
        reason="still needed",
        cancelled_by="denis",
        principal_id=1,
    )
    assert (await _counts(db))["total"] == debt_before + 1
    assert (await _counts(db))["retired_total"] == 2
    row = next(r for r in await _retired_rows(db) if r["task_id"] == cancelled)
    assert row["cancelled_at"] and row["cancelled_by"] == "denis"
    assert row["cancel_reason"] == "still needed"
    with pytest.raises(ValueError):
        await cancel_retirement(
            db, task_id=cancelled, reason="again", cancelled_by="denis"
        )

    # 3. New context: the old decision does not free it.
    await db.execute(
        "UPDATE tasks SET outcome_metric='a new promise' WHERE id=?", (metric_changed,)
    )
    await db.execute(
        "UPDATE tasks SET completed_at='2026-10-20 00:00:00' WHERE id=?", (redelivered,)
    )
    await db.commit()
    counts = await _counts(db)
    assert counts["retired_total"] == 0
    states = {r["task_id"]: r["state"] for r in await _history(db)}
    assert states[metric_changed] == "context_changed"
    assert states[redelivered] == "context_changed"
    assert counts["total"] == debt_before + 3

    # 4. The old row blocks nothing: a fresh preview offers the cancelled one
    # again and a new batch retires it; the history now has two rows for it.
    second = await _preview(db)
    assert cancelled in {c["task_id"] for c in second["cohort"]}
    await _apply(db, second["token"])
    rows_for = [r for r in await _retired_rows(db) if r["task_id"] == cancelled]
    assert len(rows_for) == 2
    assert sum(1 for r in rows_for if r["cancelled_at"] is None) == 1


async def _history(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    from hub.services.outcome_retirement import list_retirements

    return (await list_retirements(db))["rows"]


async def test_apply_skips_tasks_that_left_the_previewed_project(
    db: aiosqlite.Connection,
):
    """Область предпросмотра перепроверяется при применении (Codex P2)."""
    epic = await _task(db, "Epic", task_type="epic", metric="")
    child = await _old(db, "Child", parent_id=epic)
    prev = await _preview(db)
    assert [c["task_id"] for c in prev["cohort"]] == [child]
    other = await repo.create_project(db, slug="other", name="Other")
    await db.execute("UPDATE tasks SET project_id=? WHERE id=?", (other, epic))
    await db.commit()

    result = await _apply(db, prev["token"])

    assert result["retired"] == []
    assert result["skipped"] == [{"task_id": child, "reason": "left_project_scope"}]


_SAME_MOMENT = "2026-09-01 00:00:00"


async def _delivered(db: aiosqlite.Connection, sha_a: str = "a" * 40) -> int:
    task_id = await _old(db, "Delivered", released_days_ago=40)
    await db.execute("UPDATE releases SET deployed_at=?", (_SAME_MOMENT,))
    await db.commit()
    assert sha_a  # the first delivery is whatever _task stamped
    return task_id


async def _second_delivery(db: aiosqlite.Connection, task_id: int, sha: str) -> None:
    """A new merge of the same task released under a NEW sha, same first-deploy moment."""
    from tests.test_outcome_status import _fix_release

    await _fix_release(db, task_id, days_ago=40, sha=sha)
    await db.execute("UPDATE releases SET deployed_at=?", (_SAME_MOMENT,))
    await db.commit()


async def test_second_delivery_after_apply_is_new_context(db: aiosqlite.Connection):
    """AC-4 (Codex P2): поставка под новым SHA не наследует снятие."""
    task_id = await _delivered(db)
    await _apply(db, (await _preview(db))["token"])
    assert (await _counts(db))["retired_total"] == 1

    await _second_delivery(db, task_id, "b" * 40)

    assert (await _counts(db))["retired_total"] == 0
    states = {r["task_id"]: r["state"] for r in await _history(db)}
    assert states[task_id] == "context_changed"


async def test_delivery_between_preview_and_apply_is_skipped(
    db: aiosqlite.Connection,
):
    """AC-2/AC-4: поставка между предпросмотром и применением не снимается молча."""
    task_id = await _delivered(db)
    prev = await _preview(db)
    await _second_delivery(db, task_id, "c" * 40)

    result = await _apply(db, prev["token"])

    assert result["retired"] == []
    assert result["skipped"] == [
        {"task_id": task_id, "reason": "changed_since_preview"}
    ]


async def test_redeploy_of_the_same_sha_keeps_the_context(db: aiosqlite.Connection):
    """Повторный выкат известного SHA контекст не меняет."""
    task_id = await _delivered(db)
    await _apply(db, (await _preview(db))["token"])
    sha = (
        await db.execute_fetchall(
            "SELECT deployed_sha FROM releases ORDER BY id LIMIT 1"
        )
    )[0][0]

    await db.execute(
        "INSERT INTO releases (project_id, deployed_sha, ref, status, source, "
        "deployed_at) VALUES (NULL, ?, 'main', 'success', 'ci', '2026-10-20 00:00:00')",
        (sha,),
    )
    await db.commit()

    assert (await _counts(db))["retired_total"] == 1
    states = {r["task_id"]: r["state"] for r in await _history(db)}
    assert states[task_id] == "retired"


async def test_merge_is_delivery_second_merge_is_new_context(
    db: aiosqlite.Connection,
):
    """Codex P2: поставка проекта merge = delivery (без released_sha) тоже идентичность."""
    from tests.test_outcome_status import _delivery_project, _merge

    pid = await _delivery_project(db, "local-app", {"merge_is_delivery": True})
    task_id = await _old(db, "Local fix")
    await _merge(db, task_id, pid, days_ago=60)
    prev = await _preview(db)
    assert [c["task_id"] for c in prev["cohort"]] == [task_id]
    await _apply(db, prev["token"])
    assert (await _counts(db))["retired_total"] == 1

    await _merge(db, task_id, pid, days_ago=70)  # earlier moment, new merge

    assert (await _counts(db))["retired_total"] == 0
    states = {r["task_id"]: r["state"] for r in await _history(db)}
    assert states[task_id] == "context_changed"
