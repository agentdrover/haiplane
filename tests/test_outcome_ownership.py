"""Явный владелец обещанного исхода и срез долга actionable (#2090, шаг 1A).

Обещание ребёнка снимает с долга только ЯВНАЯ ссылка «моё обещание держит #N»,
проверяемая по цепочке предков при каждом чтении. Непустая метрика предка —
подсказка (`suggested_owner_task_id`), не покрытие. Покрытие — отдельное
измерение: ответ (#819) оно не заменяет и не стирает.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from tests.cli_support import _run_main
from tests.test_outcome_status import _fix_release

_SHA = itertools.count(1)


async def _task(
    db: aiosqlite.Connection,
    title: str,
    *,
    task_type: str = "task",
    parent_id: int | None = None,
    status: str = "completed",
    metric: str = "a number",
    archived: bool = False,
    released_days_ago: float | None = None,
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
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )
    await db.execute(
        "UPDATE tasks SET outcome_metric=?, archived=?, "
        "completed_at=datetime('now', '-2 days') WHERE id=?",
        (metric, int(archived), task_id),
    )
    await db.commit()
    if released_days_ago is not None:
        await _fix_release(
            db, task_id, days_ago=released_days_ago, sha=f"{next(_SHA):040x}"
        )
    return task_id


async def _delegate(db: aiosqlite.Connection, task_id: int, owner: int | None) -> None:
    from hub.services.outcome_ownership import set_outcome_owner

    await set_outcome_owner(db, task_id=task_id, owner_task_id=owner, set_by="denis")


async def _rows(db: aiosqlite.Connection, status: str) -> list[dict[str, Any]]:
    from hub.services.outcomes import outcome_debt

    rows: list[dict[str, Any]] = []
    offset = 0
    while offset is not None:
        page = await outcome_debt(
            db, view="actionable", status=status, limit=200, offset=offset
        )
        rows += page["rows"]
        offset = page["next_offset"]
    return rows


def _ids(rows: Iterable[dict[str, Any]]) -> set[int]:
    return {row["task_id"] for row in rows}


async def test_only_explicitly_covered_promises_leave_debt(db: aiosqlite.Connection):
    """AC-1. Метрика предка не освобождает; освобождает только явная ссылка."""
    epic = await _task(db, "Epic without metric", task_type="epic", metric="")
    feature = await _task(
        db, "Feature with metric", task_type="feature", parent_id=epic, metric="f"
    )
    independent = await _task(
        db, "Own promise", parent_id=feature, released_days_ago=20
    )
    delegated = await _task(db, "Held by the feature", parent_id=feature)
    to_bare_epic = await _task(
        db, "Points at an epic without a metric", parent_id=feature
    )
    await _delegate(db, delegated, feature)
    await _delegate(db, to_bare_epic, epic)

    debt = {
        status: await _rows(db, status)
        for status in ("overdue", "observing", "unknown")
    }
    in_debt = set().union(*(_ids(rows) for rows in debt.values()))
    covered = await _rows(db, "covered")

    assert independent in in_debt, "a parent's metric must not cover a child"
    assert feature in in_debt
    assert to_bare_epic in in_debt, "an owner without a metric holds nothing"
    assert delegated not in in_debt
    assert _ids(covered) == {delegated}
    assert covered[0]["ownership"]["state"] == "covered"
    assert covered[0]["ownership"]["owner_task_id"] == feature
    own = next(r for rows in debt.values() for r in rows if r["task_id"] == independent)
    assert own["ownership"]["state"] == "independent"
    assert own["ownership"]["suggested_owner_task_id"] == feature, (
        "the ancestor's metric is a hint for triage, not coverage"
    )
    bare = next(
        r for rows in debt.values() for r in rows if r["task_id"] == to_bare_epic
    )
    assert bare["ownership"]["state"] == "invalid_owner"


async def test_invalid_owner_never_silently_hides_obligation(db: aiosqlite.Connection):
    """AC-2. Недействительная связь видна и не освобождает; починка возвращает покрытие."""
    root = await _task(db, "Root epic", task_type="epic", metric="")
    other_root = await _task(db, "Other epic", task_type="epic", metric="")
    kinds = {
        "owner_not_completed": ("running", "m", False),
        "owner_rejected": ("rejected", "m", False),
        "owner_archived": ("completed", "m", True),
        "owner_no_metric": ("completed", "", False),
    }
    child_of: dict[str, int] = {}
    owner_of: dict[str, int] = {}
    for code, (status, metric, archived) in kinds.items():
        owner = await _task(
            db,
            f"Owner {code}",
            task_type="feature",
            parent_id=root,
            status=status,
            metric=metric,
            archived=archived,
        )
        child_of[code] = await _task(db, f"Child {code}", parent_id=owner)
        owner_of[code] = owner
        await _delegate(db, child_of[code], owner)
    # Перенос: ребёнок ушёл к другому предку, ссылка осталась.
    moved_owner = await _task(
        db, "Owner then left", task_type="feature", parent_id=root
    )
    moved = await _task(db, "Moved child", parent_id=moved_owner)
    await _delegate(db, moved, moved_owner)
    await db.execute("UPDATE tasks SET parent_id=? WHERE id=?", (other_root, moved))
    # Владелец исчез.
    ghost_parent = await _task(db, "Ghost parent", task_type="feature", parent_id=root)
    ghost = await _task(db, "Orphaned promise", parent_id=ghost_parent)
    await _delegate(db, ghost, ghost_parent)
    await db.execute(
        "UPDATE tasks SET outcome_owner_task_id=999999 WHERE id=?", (ghost,)
    )
    # Владелец сам передал обещание выше.
    chain_top = await _task(db, "Chain top", task_type="feature", parent_id=root)
    chain_mid = await _task(db, "Chain mid", parent_id=chain_top)
    chain_low = await _task(db, "Chain low", parent_id=chain_mid)
    await _delegate(db, chain_mid, chain_top)
    await _delegate(db, chain_low, chain_mid)
    await db.commit()

    invalid = {r["task_id"]: r for r in await _rows(db, "invalid_owner")}
    covered = _ids(await _rows(db, "covered"))
    in_debt: set[int] = set()
    for status in ("overdue", "observing", "unknown"):
        in_debt |= _ids(await _rows(db, status))

    expected = {
        child_of["owner_not_completed"]: "owner_not_completed",
        child_of["owner_rejected"]: "owner_not_completed",
        child_of["owner_archived"]: "owner_archived",
        child_of["owner_no_metric"]: "owner_no_metric",
        moved: "owner_not_ancestor",
        ghost: "owner_missing",
        chain_low: "owner_delegates",
    }
    assert {tid: row["ownership"]["reason"] for tid, row in invalid.items()} == expected
    assert set(expected) <= in_debt, "an invalid link must not release the obligation"
    assert not set(expected) & covered
    assert chain_mid in covered, "the valid link of the chain stays valid"

    # Повторная проверка: владелец завершился — связь снова действует.
    await db.execute(
        "UPDATE tasks SET status='completed' WHERE id=?",
        (owner_of["owner_not_completed"],),
    )
    await db.commit()
    assert child_of["owner_not_completed"] in _ids(await _rows(db, "covered"))
    assert child_of["owner_not_completed"] not in _ids(await _rows(db, "invalid_owner"))


async def test_owner_must_be_an_ancestor_when_set(db: aiosqlite.Connection):
    """Запись ссылки отказывает вне цепочки предков, на себя и на пустоту."""
    epic = await _task(db, "Epic", task_type="epic")
    child = await _task(db, "Child", parent_id=epic)
    stranger = await _task(db, "Stranger")

    for bad in (stranger, child):
        with pytest.raises(ValueError):
            await _delegate(db, child, bad)
    with pytest.raises(LookupError):
        await _delegate(db, child, 999999)
    with pytest.raises(LookupError):
        await _delegate(db, 999999, epic)
    await _delegate(db, child, epic)
    await _delegate(db, child, None)
    row = await repo.get_task(db, child)
    assert row is not None and row["outcome_owner_task_id"] is None


async def test_actionable_view_surface_parity_and_paging(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-3. Один срез на всех поверхностях; счётчики сходятся со строками."""
    from hub.services.outcomes import outcome_debt

    epic = await _task(db, "Epic", task_type="epic", metric="")
    feature = await _task(db, "Feature", task_type="feature", parent_id=epic)
    running = await _task(
        db, "Unfinished feature", task_type="feature", parent_id=epic, status="running"
    )
    overdue = [
        await _task(db, f"Overdue {n}", parent_id=feature, released_days_ago=20)
        for n in range(25)
    ]
    observing = await _task(db, "Observing", parent_id=feature, released_days_ago=3)
    unknown = await _task(db, "Unknown", parent_id=feature)
    covered = await _task(db, "Covered", parent_id=feature, released_days_ago=20)
    covered_answered = await _task(db, "Covered, answered", parent_id=feature)
    invalid = await _task(db, "Invalid owner", parent_id=running)
    answered = await _task(db, "Answered", parent_id=feature)
    for tid in (covered, covered_answered):
        await _delegate(db, tid, feature)
    await _delegate(db, invalid, running)
    for tid in (covered_answered, answered):
        await repo.record_outcome_answer(
            db, task_id=tid, verdict="moved", measured_value="0 -> 5"
        )
    await db.commit()
    legacy_before = await outcome_debt(db)

    expected_debt = {feature, observing, unknown, invalid, *overdue}
    first = await outcome_debt(db, view="actionable", status="overdue", limit=10)
    assert first["view"] == "actionable"
    seen: dict[str, set[int]] = {}
    for status in (
        "overdue",
        "observing",
        "unknown",
        "answered",
        "covered",
        "invalid_owner",
    ):
        collected: list[dict[str, Any]] = []
        offset = 0
        while offset is not None:
            page = await outcome_debt(
                db, view="actionable", status=status, limit=10, offset=offset
            )
            collected += page["rows"]
            offset = page["next_offset"]
            assert len(page["rows"]) <= 10
        assert len(collected) == page["total_in_status"], (
            "a page walk equals the counter"
        )
        seen[status] = _ids(collected)
    debt_ids = seen["overdue"] | seen["observing"] | seen["unknown"]
    assert debt_ids == expected_debt
    assert seen["covered"] == {covered, covered_answered}
    assert seen["answered"] == {covered_answered, answered}
    assert seen["invalid_owner"] == {invalid}

    counters = await outcome_debt(db, view="actionable", only_counts=True)
    assert counters["total"] == len(expected_debt)
    assert (
        counters["overdue_total"]
        + counters["observing_total"]
        + counters["unknown_total"]
        == counters["total"]
    )
    assert counters["covered_total"] == 2 and counters["invalid_owner_total"] == 1
    assert counters["answered_total"] == 2
    population = len(legacy_before["items"]) + len(legacy_before["answered"])
    assert (
        counters["independent_total"]
        + counters["covered_total"]
        + counters["invalid_owner_total"]
        == population
        == counters["population_total"]
    )
    assert "rows" not in counters

    # REST: тот же срез, байт в байт.
    for query, kwargs in (
        (
            "status=overdue&limit=10&offset=10",
            {"status": "overdue", "limit": 10, "offset": 10},
        ),
        ("status=covered", {"status": "covered"}),
        ("only_counts=true", {"only_counts": True}),
    ):
        resp = await client.get(f"/api/metrics/outcome-debt?view=actionable&{query}")
        assert resp.status_code == 200
        assert resp.json() == await outcome_debt(db, view="actionable", **kwargs)

    # Карточка и полный бриф: та же классификация.
    for tid, state in (
        (covered, "covered"),
        (invalid, "invalid_owner"),
        (overdue[0], "independent"),
    ):
        card = (await client.get(f"/api/tasks/{tid}")).json()
        brief = (await client.get(f"/api/tasks/{tid}/review-brief")).json()
        assert card["outcome_ownership"]["state"] == state
        assert brief["outcome_ownership"] == card["outcome_ownership"]
    page_html = await client.get(f"/tasks/{covered}")
    assert 'data-outcome-ownership="covered"' in page_html.text
    assert f"#{feature}" in page_html.text

    # Старый режим и контракт #1605 нетронуты.
    assert await outcome_debt(db) == legacy_before
    assert (await client.get("/api/metrics/outcome-debt")).json() == legacy_before
    legacy_page = await outcome_debt(db, status="overdue", limit=5)
    assert "view" not in legacy_page
    assert all("ownership" not in row for row in legacy_page["rows"])

    await _check_cli_and_mcp(db)


async def _check_cli_and_mcp(db: aiosqlite.Connection) -> None:
    from hub import mcp_server
    from hub.services.outcomes import outcome_debt

    rc, api = _run_main(
        ["outcome-debt", "--view", "actionable", "--status", "covered"], api_result={}
    )
    assert rc == 0
    base, _, query = api.call_args.args[1].partition("?")
    assert base == "/api/metrics/outcome-debt"
    assert sorted(query.split("&")) == ["status=covered", "view=actionable"]
    rc, api = _run_main(["outcome-debt"], api_result={})
    assert api.call_args.args[1] == "/api/metrics/outcome-debt", "default stays legacy"

    async def fake_get(path: str, **_: object) -> dict[str, Any]:
        q = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
        return await outcome_debt(
            db,
            view=q.get("view", "all"),
            status=q.get("status"),
            limit=int(q["limit"]) if "limit" in q else None,
            offset=int(q["offset"]) if "offset" in q else None,
            only_counts=q.get("only_counts") == "true",
        )

    with patch.object(mcp_server, "_api_get", AsyncMock(side_effect=fake_get)):
        result = await mcp_server.hub_outcome_debt(view="actionable", status="overdue")
    text = result.content[0].text
    data = result.structuredContent["outcome_debt"]
    assert data["view"] == "actionable" and data["covered_total"] == 2
    assert "covered by an owner" in text and "not answered" in text

    # Нулевой долг в срезе не значит «на всё есть ответ».
    quiet = {
        "view": "actionable",
        "total": 0,
        "covered_total": 3,
        "invalid_owner_total": 0,
        "answered_total": 0,
        "rows": [],
        "next_offset": None,
    }
    with patch.object(mcp_server, "_api_get", AsyncMock(return_value=quiet)):
        result = await mcp_server.hub_outcome_debt(view="actionable")
    quiet_text = result.content[0].text
    assert "has an answer" not in quiet_text and "No outcome debt" not in quiet_text
    assert "3" in quiet_text and "covered" in quiet_text


async def test_ownership_preserves_answer_history(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-4. Покрытие не трогает журнал ответов, авторов и прежние измерения."""
    from hub.services.outcomes import answer_outcome, outcome_debt

    feature = await _task(db, "Feature", task_type="feature")
    child = await _task(db, "Answered child", parent_id=feature)
    legacy = await repo.record_outcome_answer(
        db,
        task_id=child,
        verdict="moved",
        measured_value="old",
        hypothesis_snapshot=None,
    )
    await answer_outcome(
        db,
        task_id=child,
        verdict="moved",
        measured_value="2 -> 1",
        note="human",
        answered_by="denis",
    )
    await answer_outcome(
        db,
        task_id=child,
        verdict="not_moved",
        measured_value="2 -> 2",
        note="agent",
        answered_by="pda_claude",
    )
    journal = "SELECT * FROM outcome_answers ORDER BY id"

    async def snapshot() -> list[tuple[Any, ...]]:
        return [tuple(r) for r in await db.execute_fetchall(journal)]

    before = await snapshot()
    legacy_row = next(
        r for r in (await outcome_debt(db))["answered"] if r["task_id"] == child
    )

    await _delegate(db, child, feature)
    during = await _rows(db, "answered")
    row = next(r for r in during if r["task_id"] == child)
    await _delegate(db, child, None)
    await _delegate(db, child, feature)

    assert await snapshot() == before, "the answer journal is not touched by coverage"
    assert [r[0] for r in before][0] == legacy
    assert row["ownership"]["state"] == "covered"
    for key in ("answers", "latest_answer", "outcome_status", "due_on", "due_assumed"):
        assert row[key] == legacy_row[key], key
    assert row["latest_answer"]["answered_by"] == "pda_claude"
    assert row["outcome_status"] == "refuted"
    covered = await _rows(db, "covered")
    assert child in _ids(covered) and child not in _ids(await _rows(db, "overdue"))
    counters = await outcome_debt(db, view="actionable", only_counts=True)
    assert counters["answered_total"] == 1, (
        "coverage is not an answer, and not its absence"
    )

    # Покрытый владельцем может быть отвечен как раньше.
    again = await client.post(
        f"/api/tasks/{child}/outcome-answers",
        json={"verdict": "unmeasurable", "measured_value": "no data"},
    )
    assert again.status_code == 200 and again.json()["answers"] == 4


async def test_ancestors_are_read_in_one_batch(db: aiosqlite.Connection):
    """Число запросов классификатора не растёт с числом строк (без N+1)."""
    from hub.services.outcome_ownership import classify_ownerships

    epic = await _task(db, "Epic", task_type="epic")
    feature = await _task(db, "Feature", task_type="feature", parent_id=epic)

    async def run(count: int) -> int:
        tasks = []
        for n in range(count):
            tid = await _task(db, f"Leaf {n}", parent_id=feature)
            await _delegate(db, tid, feature)
            tasks.append(tid)
        rows = [dict(r) for r in await repo.list_outcome_debt(db)]
        seen: list[str] = []
        await db.set_trace_callback(seen.append)
        try:
            states = await classify_ownerships(db, rows)
        finally:
            await db.set_trace_callback(None)
        assert all(states[t]["state"] == "covered" for t in tasks)
        return len(seen)

    small = await run(3)
    large = await run(30)
    assert large == small <= 2


async def test_set_owner_rest_validates_and_records(
    db: aiosqlite.Connection, client: AsyncClient
):
    epic = await _task(db, "Epic", task_type="epic")
    child = await _task(db, "Child", parent_id=epic)
    stranger = await _task(db, "Stranger")

    bad = await client.post(
        f"/api/tasks/{child}/outcome-owner", json={"owner_task_id": stranger}
    )
    missing = await client.post(
        "/api/tasks/999999/outcome-owner", json={"owner_task_id": epic}
    )
    good = await client.post(
        f"/api/tasks/{child}/outcome-owner", json={"owner_task_id": epic}
    )
    cleared = await client.post(
        f"/api/tasks/{child}/outcome-owner", json={"owner_task_id": None}
    )

    assert (bad.status_code, missing.status_code) == (400, 404)
    assert good.status_code == 200 and good.json()["owner_task_id"] == epic
    assert cleared.status_code == 200 and cleared.json()["owner_task_id"] is None
    rc, api = _run_main(
        ["outcome-owner", str(child), "--owner", str(epic)], api_result={}
    )
    assert rc == 0
    assert api.call_args.args[:2] == ("POST", f"/api/tasks/{child}/outcome-owner")
    assert api.call_args.args[2] == {"owner_task_id": epic}
    rc, api = _run_main(["outcome-owner", str(child), "--clear"], api_result={})
    assert api.call_args.args[2] == {"owner_task_id": None}
