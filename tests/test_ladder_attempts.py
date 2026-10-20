"""Лестница ревью: попытка добора записана долговечно, отчёт привязан к заказу (#1946).

Последствие ревью #1925 gen2. Два дефекта одного корня - связь «отчёт -> заказ ->
исход» держалась на порядке отчётов принципала и на ленте событий, которую чистит
retention (14 дней):

AC-1: два неполных lite-отчёта одного принципала и поставленный deep без своего
отчёта - свип не закрывает deep чужим отчётом, стюард ждёт добор.
AC-2: события добора состарены и вычищены - объяснение остаётся прежним; открытая
попытка после потолка читается «добор не завершился», а не «готовится».
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services import ladder_attempts, report_binding
from hub.services import review_dispatch as dispatcher
from hub.services.ladder_view import (
    UNKNOWN_TEXT,
    WHY_AXIS_NOT_PLACED,
    WHY_TOP_UP_NO_REPORT,
    WHY_TOP_UP_UNFINISHED,
    incomplete_escalation_line,
    ladder_why,
    top_up_in_flight,
)
from hub.services.steward_dispatch import order_due_runs, review_wait_gate
from tests.test_steward_dispatch import (
    _due_task,
    _submitted_minutes_ago,
)
from tests.test_steward_waits_for_ladder import (
    PRINCIPAL,
    _dispatch_row,
    _task_dict,
    _why_of_orders,
)

OTHER = PRINCIPAL + 1


@pytest.fixture(autouse=True)
def steward_env(monkeypatch):
    from hub.services import steward_ci_wait

    async def _not_waiting(_db, _task):
        return steward_ci_wait.NOT_WAITING

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 50)
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 120)
    monkeypatch.setattr(steward_ci_wait, "ci_wait_view", _not_waiting)


async def _report(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    incomplete: bool,
    profile: str,
    dispatch_id: int | None = None,
    principal: int | None = PRINCIPAL,
) -> int:
    """Отчёт поколения 1; ``dispatch_id`` - привязка, как её ставит приём."""
    review_id = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        model="gpt-5.2",
        raw_count=3,
        submitted_by="rev-agent",
        incomplete=incomplete,
        incomplete_reason="profile" if incomplete else "",
        profile=profile,
        principal_id=principal,
    )
    await report_binding.stamp_report(db, review_id, dispatch_id)
    await db.commit()
    return review_id


async def _status(db: aiosqlite.Connection, dispatch_id: int) -> str:
    rows = await fetchall(
        db, "SELECT status FROM review_dispatches WHERE id=?", (dispatch_id,)
    )
    return str(dict(rows[0])["status"])


async def _attempts(db: aiosqlite.Connection, task_id: int) -> list[dict[str, Any]]:
    return await ladder_attempts.attempts_of_generation(db, task_id, 1)


async def _top_up_attempt(
    db: aiosqlite.Connection, task_id: int, *, after: int, dispatch_id: int | None
) -> int:
    """Попытка добора так, как её пишет лестница: открыта, затем закрыта."""
    attempt = await ladder_attempts.open_attempt(
        db, task_id, 1, ladder_attempts.KIND_TOP_UP, after
    )
    if dispatch_id is not None:
        await ladder_attempts.close_attempt(db, attempt, dispatch_id=dispatch_id)
    return attempt


def _no_provider(monkeypatch, status: str = "RUNNING") -> None:
    """Провайдер отвечает «прогон идёт»: свип решает только по связи отчёта."""
    from hub.integrations import cursor_cloud

    async def _run(_agent, _run_id):
        return {"status": status}

    async def _usage(_db, _dispatch):
        return None

    monkeypatch.setattr(cursor_cloud, "get_run", _run)
    monkeypatch.setattr(dispatcher, "_stamp_dispatch_usage", _usage)


async def test_a_report_belongs_to_its_own_dispatch(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-1: два неполных lite одного принципала + deep без отчёта - deep не 'done'."""
    _no_provider(monkeypatch)
    _, task_id = await _due_task(db, "own-dispatch")
    await _submitted_minutes_ago(db, task_id, 3)
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    await _report(db, task_id, incomplete=True, profile="lite", dispatch_id=lite)
    second = await _report(
        db, task_id, incomplete=True, profile="lite", dispatch_id=lite
    )
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    await _top_up_attempt(db, task_id, after=second, dispatch_id=deep)
    task = await _task_dict(db, task_id)

    await dispatcher.sweep_review_dispatches(db)

    assert await _status(db, deep) == "active", "чужой отчёт не закрывает deep"
    assert await top_up_in_flight(db, task), "добор в полёте до своего отчёта deep"
    gate = await review_wait_gate(db, task)
    assert gate.blocker and gate.wait_kind == "top_up"
    assert await order_due_runs(db) == 0, "стюард ждёт добор, судья не заказан"

    # Отчёт deep привязан к deep: теперь заказ закрывается ЕГО отчётом.
    await _report(db, task_id, incomplete=False, profile="deep", dispatch_id=deep)
    await dispatcher.sweep_review_dispatches(db)
    assert await _status(db, deep) == "done"
    assert await top_up_in_flight(db, task) == ""
    assert await order_due_runs(db) == 1


async def test_the_wait_for_the_top_up_ends_at_the_ceiling(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-1, потолок: добор без своего отчёта держит судью до STEWARD_REVIEW_WAIT_MAX."""
    _no_provider(monkeypatch)
    _, task_id = await _due_task(db, "own-dispatch-ceiling")
    await _submitted_minutes_ago(db, task_id, 121)
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    await _report(db, task_id, incomplete=True, profile="lite", dispatch_id=lite)
    last = await _report(db, task_id, incomplete=True, profile="lite", dispatch_id=lite)
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    await _top_up_attempt(db, task_id, after=last, dispatch_id=deep)

    await dispatcher.sweep_review_dispatches(db)

    assert await _status(db, deep) == "active"
    assert await order_due_runs(db) == 1
    assert "ждали отчёт 121 мин" in (await _why_of_orders(db))[task_id]


async def test_unbound_reports_still_pair_by_rung(db: aiosqlite.Connection):
    """Отчёты без привязки (история, ключ API) сопоставляются по порядку ступеней."""
    _, task_id = await _due_task(db, "unbound-rungs")
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    first = await _report(db, task_id, incomplete=True, profile="lite")
    row = dict(
        (await fetchall(db, "SELECT * FROM review_dispatches WHERE id=?", (lite,)))[0]
    )
    assert (await dispatcher.report_of_dispatch(db, task_id, 1, row))["id"] == first
    row = dict(
        (await fetchall(db, "SELECT * FROM review_dispatches WHERE id=?", (deep,)))[0]
    )
    assert await dispatcher.report_of_dispatch(db, task_id, 1, row) is None
    second = await _report(db, task_id, incomplete=False, profile="deep")
    assert (await dispatcher.report_of_dispatch(db, task_id, 1, row))["id"] == second


async def test_a_bound_report_does_not_take_a_rung_of_an_unbound_dispatch(
    db: aiosqlite.Connection,
):
    """Привязанный к первому заказу отчёт не сдвигает ступени: второй отчёт - второму."""
    _, task_id = await _due_task(db, "bound-and-unbound")
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    await _report(db, task_id, incomplete=True, profile="lite", dispatch_id=lite)
    await _report(db, task_id, incomplete=True, profile="lite", dispatch_id=lite)
    deep_row = dict(
        (await fetchall(db, "SELECT * FROM review_dispatches WHERE id=?", (deep,)))[0]
    )
    assert await dispatcher.report_of_dispatch(db, task_id, 1, deep_row) is None
    unbound = await _report(db, task_id, incomplete=False, profile="deep")
    own = await dispatcher.report_of_dispatch(db, task_id, 1, deep_row)
    assert own is not None and own["id"] == unbound


async def test_the_link_survives_event_retention(db: aiosqlite.Connection):
    """AC-2: отказанная вторая ось и состаренные события - объяснение прежнее."""
    _, task_id = await _due_task(db, "retention")
    await _submitted_minutes_ago(db, task_id, 3)
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    first = await _report(
        db, task_id, incomplete=True, profile="lite", dispatch_id=lite
    )
    attempt = await ladder_attempts.open_attempt(
        db, task_id, 1, ladder_attempts.KIND_SECOND_AXIS, first, model="grok-4.6"
    )
    for kind, payload in (
        (dispatcher.LADDER_TOP_UP_EVENT, {"generation": 1, "after_review_id": first}),
        (
            dispatcher.MODEL_CASCADE_EVENT,
            {
                "outcome": dispatcher.MODEL_CASCADE_FAILED,
                "reason": "red_ci",
                "generation": 1,
                "model": "grok-4.6",
                "dispatch_id": None,
                "after_review_id": first,
            },
        ),
    ):
        await repo.insert_event(
            db, kind=kind, task_id=task_id, actor="policy", payload=payload
        )
    await ladder_attempts.close_attempt(db, attempt, dispatch_id=None, reason="red_ci")
    task = await _task_dict(db, task_id)
    before = await ladder_why(db, task)
    assert before is not None and before.code == WHY_AXIS_NOT_PLACED

    # Лента состарена на 15 дней и вычищена retention-ом.
    await db.execute("UPDATE events SET created_at = datetime('now', '-15 days')")
    await db.execute("UPDATE owner_minutes_state SET cursor_event_id = 1000000")
    await db.commit()
    assert await repo.prune_events(db) >= 2
    assert not await fetchall(
        db,
        "SELECT 1 FROM events WHERE kind IN (?, ?)",
        (dispatcher.LADDER_TOP_UP_EVENT, dispatcher.MODEL_CASCADE_EVENT),
    )

    after = await ladder_why(db, task)
    assert after is not None and after.code == WHY_AXIS_NOT_PLACED
    assert "вторая ось не поставлена: red_ci" in after.text
    line = await incomplete_escalation_line(db, task)
    assert "вторая ось не поставлена: red_ci" in line
    assert "тоже" not in line, "не «тоже не дочитала»"

    # Открытая попытка после потолка ожидания читается «добор не завершился».
    _, stuck = await _due_task(db, "retention-open")
    await _submitted_minutes_ago(db, stuck, 121)
    lite2 = await _dispatch_row(db, stuck, status="done", profile="lite")
    first2 = await _report(
        db, stuck, incomplete=True, profile="lite", dispatch_id=lite2
    )
    await _top_up_attempt(db, stuck, after=first2, dispatch_id=None)
    assert await order_due_runs(db) >= 1, "потолок вышел: судья заказан"
    unfinished = await ladder_why(db, await _task_dict(db, stuck))
    assert unfinished is not None and unfinished.code == WHY_TOP_UP_UNFINISHED
    assert "добор не завершился" in unfinished.text


async def test_an_open_attempt_after_the_ceiling_reads_as_unfinished(
    db: aiosqlite.Connection,
):
    """AC-2: открытая попытка после потолка - «добор не завершился», не «готовится»."""
    _, task_id = await _due_task(db, "unfinished")
    await _submitted_minutes_ago(db, task_id, 121)
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    first = await _report(
        db, task_id, incomplete=True, profile="lite", dispatch_id=lite
    )
    await _top_up_attempt(db, task_id, after=first, dispatch_id=None)  # не закрыта
    task = await _task_dict(db, task_id)

    # Внутри окна читатель ожидания держит: «готовится».
    assert "готовится" in await top_up_in_flight(db, task)
    # Срок вышел: судья заказан, и объяснение - «не завершился».
    assert await order_due_runs(db) == 1
    why = await ladder_why(db, task)
    assert why is not None and why.code == WHY_TOP_UP_UNFINISHED
    assert "добор не завершился" in why.text
    assert "добор не завершился" in await incomplete_escalation_line(db, task)


async def test_the_ladder_writes_a_durable_attempt(
    db: aiosqlite.Connection, monkeypatch
):
    """maybe_top_up_incomplete: открыть попытку до диспетчера и закрыть заказом."""
    _, task_id = await _due_task(db, "writes-attempt")
    await _submitted_minutes_ago(db, task_id, 3)
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    first = await _report(
        db, task_id, incomplete=True, profile="lite", dispatch_id=lite
    )
    seen: dict[str, Any] = {}

    async def _placed(_db, _task_id, **_kwargs):
        seen["during"] = await _attempts(db, task_id)
        seen["deep"] = await _dispatch_row(db, task_id, status="active", profile="deep")
        return True

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _placed)
    assert await dispatcher.maybe_top_up_incomplete(db, task_id) is True

    assert [a["closed_at"] for a in seen["during"]] == [None], "открыта ДО диспетчера"
    (done,) = await _attempts(db, task_id)
    assert done["kind"] == ladder_attempts.KIND_TOP_UP
    assert done["after_review_id"] == first
    assert done["outcome"] == ladder_attempts.OUTCOME_PLACED
    assert done["dispatch_id"] == seen["deep"] and done["closed_at"] is not None


async def test_a_refused_top_up_is_closed_as_refused(
    db: aiosqlite.Connection, monkeypatch
):
    _, task_id = await _due_task(db, "refused-attempt")
    await _submitted_minutes_ago(db, task_id, 3)
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    await _report(db, task_id, incomplete=True, profile="lite", dispatch_id=lite)

    async def _refused(_db, _task_id, **_kwargs):
        return False

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _refused)
    assert await dispatcher.maybe_top_up_incomplete(db, task_id) is False
    (done,) = await _attempts(db, task_id)
    assert done["outcome"] == ladder_attempts.OUTCOME_REFUSED
    assert done["dispatch_id"] is None and done["closed_at"] is not None


async def test_a_failing_dispatcher_leaves_the_attempt_open(
    db: aiosqlite.Connection, monkeypatch
):
    """Упавший диспетчер: попытка осталась открытой, а не стёрлась и не «не было»."""
    _, task_id = await _due_task(db, "crash-attempt")
    await _submitted_minutes_ago(db, task_id, 3)
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    await _report(db, task_id, incomplete=True, profile="lite", dispatch_id=lite)

    async def _boom(_db, _task_id, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _boom)
    with pytest.raises(RuntimeError):
        await dispatcher.maybe_top_up_incomplete(db, task_id)
    (left,) = await _attempts(db, task_id)
    assert left["closed_at"] is None and left["outcome"] == ""
    why = await ladder_why(db, await _task_dict(db, task_id))
    assert why is not None and why.code == WHY_TOP_UP_UNFINISHED


async def test_an_unreadable_attempt_table_is_not_an_absent_top_up(
    db: aiosqlite.Connection,
):
    """Сбой чтения записи - не «добора нет»: читатель поднимает, объяснение называет сбой."""
    _, task_id = await _due_task(db, "unreadable")
    await _submitted_minutes_ago(db, task_id, 3)
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    first = await _report(
        db, task_id, incomplete=True, profile="lite", dispatch_id=lite
    )
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    await _top_up_attempt(db, task_id, after=first, dispatch_id=deep)
    task = await _task_dict(db, task_id)

    # Таблицы попыток «нет» (сбой чтения): это не «добора не было».
    await db.execute("ALTER TABLE ladder_attempts RENAME TO ladder_attempts_lost")
    await db.commit()
    with pytest.raises(aiosqlite.OperationalError):
        await top_up_in_flight(db, task)
    line = await incomplete_escalation_line(db, task)
    assert "чтение записей лестницы не удалось" in line
    assert UNKNOWN_TEXT in line


async def test_a_task_from_before_the_table_reads_the_events(db: aiosqlite.Connection):
    """Сдача без строк попыток (до миграции) читается по событиям, как раньше."""
    _, task_id = await _due_task(db, "pre-migration")
    await _submitted_minutes_ago(db, task_id, 3)
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    await _report(db, task_id, incomplete=True, profile="lite", dispatch_id=lite)
    await _dispatch_row(db, task_id, status="active", profile="deep", top_up=True)
    assert await _attempts(db, task_id) == []
    assert await top_up_in_flight(db, await _task_dict(db, task_id))


async def _deep_incomplete(db: aiosqlite.Connection, slug: str, monkeypatch):
    """Сдача с неполным deep-отчётом и подготовленной второй осью."""
    _, task_id = await _due_task(db, slug)
    await _submitted_minutes_ago(db, task_id, 3)
    deep = await _dispatch_row(db, task_id, status="done", profile="deep")
    await _report(db, task_id, incomplete=True, profile="deep", dispatch_id=deep)
    report = dict((await repo.machine_reviews_of_generation(db, task_id, 1))[-1])

    async def _no_block(*_a, **_k):
        return ""

    monkeypatch.setattr(dispatcher, "_second_axis_blocked", _no_block)
    monkeypatch.setattr(dispatcher, "_no_cloud_after_local", _no_block)
    monkeypatch.setattr(dispatcher, "pick_cascade_model", lambda *_a, **_k: "grok-4.6")
    monkeypatch.setattr(config, "REVIEW_MODEL_CASCADE_MAX", 1)
    return task_id, report


async def test_the_second_axis_writes_its_refusal_into_the_attempt(
    db: aiosqlite.Connection, monkeypatch
):
    task_id, report = await _deep_incomplete(db, "axis-refused", monkeypatch)

    async def _refuse(_db, _task_id, **_kwargs):
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Кросс-модельное ревью НЕ вызвано: red_ci. Вердикт остаётся человеку",
        )
        return False

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _refuse)
    task = await _task_dict(db, task_id)
    assert await dispatcher._ask_a_stronger_model(db, task, 1, report, 0) is False
    (attempt,) = await _attempts(db, task_id)
    assert attempt["kind"] == ladder_attempts.KIND_SECOND_AXIS
    assert attempt["model"] == "grok-4.6"
    assert attempt["outcome"] == ladder_attempts.OUTCOME_REFUSED
    assert attempt["reason"] == "red_ci" and attempt["closed_at"] is not None
    why = await ladder_why(db, task)
    assert why is not None and why.code == WHY_AXIS_NOT_PLACED
    assert "вторая ось не поставлена: red_ci" in why.text


async def test_the_second_axis_writes_its_order_into_the_attempt(
    db: aiosqlite.Connection, monkeypatch
):
    task_id, report = await _deep_incomplete(db, "axis-placed", monkeypatch)
    ordered: dict[str, int] = {}

    async def _place(_db, _task_id, **_kwargs):
        ordered["id"] = await _dispatch_row(
            db, task_id, status="active", profile="deep"
        )
        return True

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _place)
    task = await _task_dict(db, task_id)
    assert await dispatcher._ask_a_stronger_model(db, task, 1, report, 0) is True
    (attempt,) = await _attempts(db, task_id)
    assert attempt["kind"] == ladder_attempts.KIND_SECOND_AXIS
    assert attempt["outcome"] == ladder_attempts.OUTCOME_PLACED
    assert attempt["dispatch_id"] == ordered["id"]
    assert attempt["after_review_id"] == report["id"]
    assert await top_up_in_flight(db, task), "заказ второй оси без отчёта - в полёте"


async def test_readers_of_the_outcome_skip_reports_bound_to_other_orders(
    db: aiosqlite.Connection,
):
    """Исход попытки: чужой привязанный отчёт после after_review_id ей не отвечает."""
    _, task_id = await _due_task(db, "outcome-readers")
    await _submitted_minutes_ago(db, task_id, 3)
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    first = await _report(
        db, task_id, incomplete=True, profile="lite", dispatch_id=lite
    )
    await _report(db, task_id, incomplete=True, profile="lite", dispatch_id=lite)
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    await _top_up_attempt(db, task_id, after=first, dispatch_id=deep)
    task = await _task_dict(db, task_id)

    # Второй lite-отчёт ляжет «после» первого, но заказу deep не принадлежит.
    await db.execute("UPDATE review_dispatches SET status='failed' WHERE id=?", (deep,))
    await db.commit()
    why = await ladder_why(db, task)
    assert why is not None and why.code == WHY_TOP_UP_NO_REPORT, why

    event = {
        "task_id": task_id,
        "payload": json.dumps(
            {
                "generation": 1,
                "after_review_id": first,
                "dispatch_id": deep,
                "model": "grok-4.6",
            }
        ),
    }
    assert await dispatcher.cascade_outcome(db, event) == "pending"
    await _report(db, task_id, incomplete=False, profile="deep", dispatch_id=deep)
    assert await dispatcher.cascade_outcome(db, event) == "complete"
