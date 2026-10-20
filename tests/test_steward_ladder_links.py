"""Лестница добора: исход читается по СВЯЗИ с заказом или попыткой (#1925, круг 2).

Класс ошибки, вернувшийся из ревью: исход добора выводился из косвенного
признака (число ступеней, время строк, маркер алерта, статус) вместо записи,
которая связывает его с КОНКРЕТНЫМ неполным отчётом, заказом и поколением.
Помощники — из tests/test_steward_waits_for_ladder.py.
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import config

from hub import repository as repo
from hub.services import review_dispatch as dispatcher
from hub.services.ladder_view import WHY_UNKNOWN, ladder_why, top_up_in_flight
from hub.services.steward_dispatch import order_due_runs
from tests.test_steward_dispatch import _due_task, _submitted_minutes_ago
from tests.test_steward_waits_for_ladder import (  # noqa: F401 - автоиспользуемая фикстура
    PRINCIPAL,
    _cascade_event,
    _dispatch_row,
    _last_report_id,
    _lite_incomplete,
    _report_row,
    _alerts,
    _task_dict,
    steward_env,
)


# ---------------------------------------------------------------------------
# Круг 2 (CHANGES_REQUESTED gen 1): исход читается по СВЯЗИ с заказом или
# попыткой, а не по косвенному признаку. Коды — строками: тест не должен
# зависеть от констант, которых на момент RED ещё нет.
# ---------------------------------------------------------------------------


async def test_second_door_debt_is_still_a_top_up_in_flight(db: aiosqlite.Connection):
    """Находка 1: неоплаченный долг second_door (agent_id='', принципал NULL).

    Платных ступеней одна и отчёт один — счёт ступеней видел «отвечено». Долг
    поставлен ПОСЛЕ неполного отчёта и своего отчёта не отдал: добор ещё
    должен прийти, судью заказывать рано.
    """
    _, task_id = await _lite_incomplete(db, "round2-second-door-debt")
    await db.execute(
        "UPDATE machine_reviews SET created_at=datetime('now', '-5 seconds') "
        "WHERE task_id=?",
        (task_id,),
    )
    await _dispatch_row(
        db,
        task_id,
        status="second_door",
        profile="deep",
        agent_id="",
        principal=None,
        top_up=True,
    )
    task = await _task_dict(db, task_id)
    assert await top_up_in_flight(db, task), "долг второй двери — добор в пути"
    assert await order_due_runs(db) == 0

    # Хаб закрыл долг (статус заказа сменился) — ожидание кончилось.
    await db.execute(
        "UPDATE review_dispatches SET status='done' WHERE status='second_door'"
    )
    await db.commit()
    assert await top_up_in_flight(db, task) == ""


async def test_a_refused_second_axis_attempt_is_not_the_old_result(
    db: aiosqlite.Connection,
):
    """Находка 2: попытка второй оси отказана (red_ci), старый маркер и старый отчёт.

    Ось не запускалась (dispatch_id пуст, outcome=failed); читатель обязан
    сказать «не поставлена: <причина>», а не «дала такой же неполный отчёт».
    """
    _, task_id = await _lite_incomplete(db, "round2-axis-refused")
    await _dispatch_row(db, task_id, status="done", profile="deep")
    await _report_row(db, task_id, incomplete=True, profile="deep")
    after = (await repo.machine_reviews_of_generation(db, task_id, 1))[-1]["id"]
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        dispatcher.MODEL_CASCADE_MARK.format(generation=1) + " Переспрос 1 из 1",
    )
    await repo.insert_event(
        db,
        kind=dispatcher.MODEL_CASCADE_EVENT,
        task_id=task_id,
        actor="policy",
        payload={
            "outcome": "failed",
            "reason": "red_ci",
            "generation": 1,
            "model": "grok-4.6",
            "attempt": 1,
            "dispatch_id": None,
            "after_review_id": after,
        },
    )
    await db.commit()
    why = await ladder_why(db, await _task_dict(db, task_id))
    assert why is not None
    assert why.code == "second_axis_not_placed", why
    assert "red_ci" in why.line()


async def test_the_reason_survives_the_window_renewal_path(db: aiosqlite.Connection):
    """Находка 3: судья заказан без основания в заказе (путь продления окна).

    Основание не живёт в событии заказа: добор кончился без отчёта — это
    видно по строке его заказа, каким бы путём судью ни заказали и ни стартовали.
    """
    from hub.services.steward_dispatch import EVENT_WINDOW_RENEWED, order_run

    _, task_id = await _lite_incomplete(db, "round2-window-renewal", minutes=121)
    deep = await _dispatch_row(
        db, task_id, status="active", profile="deep", top_up=True
    )
    assert await order_run(db, task_id, 1) is not None  # без why: основания нет
    await repo.insert_event(
        db,
        kind=EVENT_WINDOW_RENEWED,
        task_id=task_id,
        actor="hub",
        payload={"run_id": 1, "generation": 1, "kind": "verdict", "because": "x"},
    )
    await db.execute("UPDATE review_dispatches SET status='failed' WHERE id=?", (deep,))
    await db.commit()
    why = await ladder_why(db, await _task_dict(db, task_id))
    assert why is not None
    assert why.code == "top_up_no_report", why
    assert "failed" in why.line()


# ---------------------------------------------------------------------------
# Круг 2, ревью Codex поверх: тот же класс — связь с заказом, а не признак.
# ---------------------------------------------------------------------------


async def test_a_foreign_or_repeated_report_does_not_answer_the_top_up(
    db: aiosqlite.Connection,
):
    """P2-1, P2-2: чужой неполный отчёт и второй lite-отчёт того же принципала.

    Добор поставлен поступком лестницы после конкретного отчёта; ответом ему
    служит смена статуса заказа, которую ведёт хаб, — не появление любого
    отчёта и не k-й отчёт в счёте строк.
    """
    _, foreign = await _lite_incomplete(db, "round2-foreign-report")
    await _dispatch_row(db, foreign, status="active", profile="deep", top_up=True)
    await repo.insert_machine_review(
        db,
        task_id=foreign,
        submission_generation=1,
        model="other",
        raw_count=1,
        incomplete=True,
        incomplete_reason="profile",
        profile="lite",
        principal_id=PRINCIPAL + 1,
    )
    await db.commit()
    assert await top_up_in_flight(db, await _task_dict(db, foreign))
    assert await order_due_runs(db) == 0

    _, twice = await _lite_incomplete(db, "round2-second-lite")
    await _report_row(db, twice, incomplete=True, profile="lite")  # второй lite
    await _dispatch_row(db, twice, status="active", profile="deep", top_up=True)
    assert await top_up_in_flight(db, await _task_dict(db, twice))


async def test_a_new_alert_does_not_replace_the_outcome_of_a_refused_attempt(
    db: aiosqlite.Connection,
):
    """P2-3: после отказа попытки второй оси лестница пишет «исчерпана» — причина прежняя."""
    _, task_id = await _lite_incomplete(db, "round2-refused-then-exhausted")
    await _dispatch_row(db, task_id, status="done", profile="deep")
    await _report_row(db, task_id, incomplete=True, profile="deep")
    await repo.insert_event(
        db,
        kind=dispatcher.MODEL_CASCADE_EVENT,
        task_id=task_id,
        actor="policy",
        payload={
            "outcome": "failed",
            "reason": "red_ci",
            "generation": 1,
            "attempt": 1,
            "dispatch_id": None,
            "after_review_id": await _last_report_id(db, task_id),
        },
    )
    await _report_row(db, task_id, incomplete=True, profile="deep")  # новый отчёт
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        dispatcher.MODEL_CASCADE_EXHAUSTED_MARK.format(generation=1) + " потолок",
    )
    await db.commit()
    why = await ladder_why(db, await _task_dict(db, task_id))
    assert why is not None and why.code == "second_axis_not_placed", why


async def test_an_alert_of_another_generation_is_not_ours(db: aiosqlite.Connection):
    """P2-4: хвост [сдача N] отделяет поколения; равная секунда ничего не решает."""
    _, task_id = await _lite_incomplete(db, "round2-other-generation")
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"{dispatcher.FORCED_DEEP_CAP_MARK}: потолок"
        + dispatcher.ladder_generation_tag(1),
    )
    await repo.update_task(db, task_id, submission_generation=2)
    await db.execute(
        "INSERT INTO submissions (task_id, generation, sha, submitted_at) "
        "VALUES (?, 2, ?, datetime('now'))",
        (task_id, "b" * 40),
    )
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=2,
        model="gpt-5.2",
        raw_count=1,
        incomplete=True,
        incomplete_reason="profile",
        profile="lite",
        principal_id=PRINCIPAL,
    )
    await db.commit()
    why = await ladder_why(db, await _task_dict(db, task_id))
    assert why is not None and why.code == WHY_UNKNOWN, why


async def test_a_failed_first_order_is_not_called_a_top_up(db: aiosqlite.Connection):
    """P2-5: первичный заказ упал, пришёл чужой неполный отчёт — добора не заказывали."""
    _, task_id = await _due_task(db, "round2-first-order-failed")
    await _submitted_minutes_ago(db, task_id, 3)
    await _dispatch_row(db, task_id, status="failed", profile="lite")
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        model="other",
        raw_count=1,
        incomplete=True,
        incomplete_reason="profile",
        profile="lite",
        principal_id=PRINCIPAL + 1,
    )
    await db.commit()
    task = await _task_dict(db, task_id)
    assert await top_up_in_flight(db, task) == ""
    why = await ladder_why(db, task)
    assert why is not None and why.code == WHY_UNKNOWN, why


async def test_the_ladder_records_the_top_up_it_places(
    db: aiosqlite.Connection, monkeypatch
):
    """Поступок записывает сам хаб: настоящий maybe_top_up_incomplete -> событие со связью."""
    _, task_id = await _lite_incomplete(db, "round2-real-top-up")
    report_id = await _last_report_id(db, task_id)

    async def _placed(_db, _task_id, **_kwargs):
        await _dispatch_row(db, task_id, status="active", profile="deep")
        return True

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _placed)
    assert await dispatcher.maybe_top_up_incomplete(db, task_id) is True
    events = [
        e
        for e in await repo.list_events(db, since=0)
        if e["task_id"] == task_id and e["kind"] == dispatcher.LADDER_TOP_UP_EVENT
    ]
    assert len(events) == 1
    payload = __import__("json").loads(events[0]["payload"])
    assert payload["after_review_id"] == report_id and payload["generation"] == 1
    placed = [
        e
        for e in await repo.list_events(db, since=0)
        if e["task_id"] == task_id
        and e["kind"] == dispatcher.LADDER_TOP_UP_PLACED_EVENT
    ]
    assert len(placed) == 1, "заказ поступка записан после возврата диспетчера"
    assert (
        __import__("json").loads(placed[0]["payload"])["after_review_id"] == report_id
    )
    assert await top_up_in_flight(db, await _task_dict(db, task_id))


async def test_an_act_that_already_produced_a_report_is_not_its_absence(
    db: aiosqlite.Connection,
):
    """Добор дал свой неполный отчёт: «заказ кончился без отчёта» про него неверно."""
    _, task_id = await _lite_incomplete(db, "round2-act-produced-report")
    await _dispatch_row(db, task_id, status="done", profile="deep", top_up=True)
    await _report_row(db, task_id, incomplete=True, profile="deep")
    why = await ladder_why(db, await _task_dict(db, task_id))
    assert why is not None and why.code == WHY_UNKNOWN, why


# ---------------------------------------------------------------------------
# Круг 3 (Codex поверх): записанная связь переживает сбои, замены и смешение
# отчётов; следующая попытка не прячется за исходом прежней.
# ---------------------------------------------------------------------------


async def test_a_debt_left_by_a_crash_mid_order_is_still_waited_for(
    db: aiosqlite.Connection, monkeypatch
):
    """Круг 3, П1: долг второй двери остался, диспетчер упал — поступок уже записан."""
    _, task_id = await _lite_incomplete(db, "round3-crash-mid-order")

    async def _crash(_db, _task_id, **_kwargs):
        await _dispatch_row(
            db,
            task_id,
            status="second_door",
            profile="deep",
            agent_id="",
            principal=None,
        )
        raise RuntimeError("подготовка локальной замены упала")

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _crash)
    with pytest.raises(RuntimeError):
        await dispatcher.maybe_top_up_incomplete(db, task_id)
    task = await _task_dict(db, task_id)
    assert await top_up_in_flight(db, task), "долг есть и поступок записан"
    assert await order_due_runs(db) == 0


async def test_a_local_replacement_report_is_not_called_a_missing_report(
    db: aiosqlite.Connection,
):
    """Круг 3, П2: облачный заказ заменён локальным, у замены свой неполный отчёт."""
    _, task_id = await _lite_incomplete(db, "round3-local-replacement")
    cloud = await _dispatch_row(
        db, task_id, status="failed", profile="deep", top_up=True
    )
    await db.execute(
        "INSERT INTO review_dispatches (task_id, submission_generation, agent_id, "
        "run_id, model, profile, status, channel, reviewer_principal_id, "
        "replaces_dispatch_id) "
        "VALUES (?, 1, 'local:1', 'r', 'qwen', 'deep', 'done', 'local', 9, ?)",
        (task_id, cloud),
    )
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        model="qwen",
        raw_count=1,
        incomplete=True,
        incomplete_reason="profile",
        profile="deep",
        principal_id=9,
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Неполный отчёт профиля «deep»: {dispatcher.NO_CLOUD_AFTER_LOCAL_MARK} — x"
        + dispatcher.ladder_generation_tag(1),
    )
    await db.commit()
    why = await ladder_why(db, await _task_dict(db, task_id))
    assert why is not None and why.code == "local_deep_no_cloud", why
    # Без алерта остаётся одно: у замены свой отчёт, «заказ без отчёта» — неправда.
    await db.execute(
        "DELETE FROM task_updates WHERE task_id=? AND kind='alert'", (task_id,)
    )
    await db.commit()
    again = await ladder_why(db, await _task_dict(db, task_id))
    assert again is not None and again.code == WHY_UNKNOWN, again


async def test_a_complete_axis_report_is_not_turned_into_an_incomplete_one(
    db: aiosqlite.Connection,
):
    """Круг 3, П3: ось дала ПОЛНЫЙ отчёт, позже пришёл чужой неполный."""
    _, task_id = await _lite_incomplete(db, "round3-complete-then-foreign")
    await _dispatch_row(db, task_id, status="done", profile="deep")
    await _report_row(db, task_id, incomplete=True, profile="deep")
    after = await _last_report_id(db, task_id)
    axis = await _dispatch_row(
        db, task_id, status="done", profile="deep", agent_id="r2"
    )
    await _cascade_event(db, task_id, dispatch_id=axis, after=after)
    await _report_row(db, task_id, incomplete=False, profile="deep")
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        model="other",
        raw_count=1,
        incomplete=True,
        incomplete_reason="profile",
        profile="lite",
        principal_id=PRINCIPAL + 1,
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        dispatcher.MODEL_CASCADE_MARK.format(generation=1) + " Переспрос 1 из 1",
    )
    await db.commit()
    why = await ladder_why(db, await _task_dict(db, task_id))
    assert why is not None and why.code == WHY_UNKNOWN, why


async def test_a_new_failed_attempt_is_not_hidden_behind_an_old_refusal(
    db: aiosqlite.Connection, monkeypatch
):
    """Круг 3, П4: первая попытка упёрлась в потолок deep, вторая — в конфигурацию."""
    _, task_id = await _lite_incomplete(db, "round3-old-refusal")
    task = await _task_dict(db, task_id)

    async def _cap_exhausted(_db, _task, _generation):
        return 3

    monkeypatch.setattr(dispatcher, "deep_cap_exhausted", _cap_exhausted)

    async def _refuse_by_cap(_db, _task_id, **_kwargs):
        return not await dispatcher._forced_deep_over_cap(db, task, 1, "deep", "")

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _refuse_by_cap)
    assert await dispatcher.maybe_top_up_incomplete(db, task_id) is False
    cap_feed = "\n".join(await _alerts(db, task_id))
    assert dispatcher.LADDER_ORDER_FAILED_MARK not in cap_feed, (
        "конкретный отказ сам называет причину; общий алерт поверх него не нужен"
    )
    assert (await ladder_why(db, task)).code == "deep_cap"

    async def _refuse_by_config(_db, _task_id, **_kwargs):
        return False  # молчаливый отказ: конкретного алерта нет

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _refuse_by_config)
    assert await dispatcher.maybe_top_up_incomplete(db, task_id) is False
    assert (await ladder_why(db, task)).code == "top_up_not_ordered"


async def test_the_second_axis_is_recorded_before_the_order_too(
    db: aiosqlite.Connection, monkeypatch
):
    """Круг 4, П1: вторая ось: долг остался, вызов упал — поступок уже записан."""
    _, task_id = await _lite_incomplete(db, "round4-axis-crash")
    await _dispatch_row(db, task_id, status="done", profile="deep")
    await _report_row(db, task_id, incomplete=True, profile="deep")
    report = dict((await repo.machine_reviews_of_generation(db, task_id, 1))[-1])

    async def _crash(_db, _task_id, **_kwargs):
        await _dispatch_row(
            db,
            task_id,
            status="second_door",
            profile="deep",
            agent_id="",
            principal=None,
        )
        raise RuntimeError("упало после долга")

    async def _no_block(*_a, **_k):
        return ""

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _crash)
    monkeypatch.setattr(dispatcher, "_second_axis_blocked", _no_block)
    monkeypatch.setattr(dispatcher, "_no_cloud_after_local", _no_block)
    monkeypatch.setattr(dispatcher, "pick_cascade_model", lambda *_a, **_k: "grok-4.6")
    monkeypatch.setattr(config, "REVIEW_MODEL_CASCADE_MAX", 1)
    task = await _task_dict(db, task_id)
    with pytest.raises(RuntimeError):
        await dispatcher._ask_a_stronger_model(db, task, 1, report, 0)
    assert await top_up_in_flight(db, task)


async def test_a_foreign_report_does_not_close_a_second_axis_order(
    db: aiosqlite.Connection,
):
    """Круг 4, П2: заказ оси кончился без своего отчёта, пришёл чужой неполный."""
    _, task_id = await _lite_incomplete(db, "round4-axis-foreign")
    await _dispatch_row(db, task_id, status="done", profile="deep")
    await _report_row(db, task_id, incomplete=True, profile="deep")
    after = await _last_report_id(db, task_id)
    axis = await _dispatch_row(
        db, task_id, status="failed", profile="deep", agent_id="r2"
    )
    await _cascade_event(db, task_id, dispatch_id=axis, after=after)
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        model="other",
        raw_count=1,
        incomplete=True,
        incomplete_reason="profile",
        profile="lite",
        principal_id=PRINCIPAL + 1,
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        dispatcher.MODEL_CASCADE_MARK.format(generation=1) + " Переспрос 1 из 1",
    )
    await db.commit()
    why = await ladder_why(db, await _task_dict(db, task_id))
    assert why is not None and why.code == "second_axis_no_report", why


async def test_an_old_refusal_does_not_cover_a_placed_order_without_a_report(
    db: aiosqlite.Connection, monkeypatch
):
    """Круг 4, П3: первая попытка упёрлась в потолок, вторая поставлена и молчит."""
    _, task_id = await _lite_incomplete(db, "round4-old-cap")
    task = await _task_dict(db, task_id)

    async def _cap_exhausted(_db, _task, _generation):
        return 3

    monkeypatch.setattr(dispatcher, "deep_cap_exhausted", _cap_exhausted)
    assert await dispatcher._forced_deep_over_cap(db, task, 1, "deep", "") is True
    await _dispatch_row(db, task_id, status="failed", profile="deep", top_up=True)
    why = await ladder_why(db, task)
    assert why is not None and why.code == "top_up_no_report", why


async def test_an_unnamed_axis_refusal_is_named_by_its_own_alert(
    db: aiosqlite.Connection, monkeypatch
):
    """Круг 4, П4: отказ оси записан с reason=unknown, а потолок назвал алерт."""
    _, task_id = await _lite_incomplete(db, "round4-unnamed-refusal")
    await _dispatch_row(db, task_id, status="done", profile="deep")
    await _report_row(db, task_id, incomplete=True, profile="deep")
    event = {
        "outcome": "failed",
        "reason": "unknown",
        "generation": 1,
        "attempt": 1,
        "dispatch_id": None,
        "after_review_id": await _last_report_id(db, task_id),
    }
    await repo.insert_event(
        db,
        kind=dispatcher.MODEL_CASCADE_EVENT,
        task_id=task_id,
        actor="policy",
        payload=event,
    )
    await db.commit()
    task = await _task_dict(db, task_id)
    bare = await ladder_why(db, task)
    assert bare is not None and bare.code == "second_axis_not_placed", bare
    # Маркер НАЧАЛА попытки (алерт пишется до вызова) записанный отказ не отменяет.
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        dispatcher.MODEL_CASCADE_MARK.format(generation=1) + " Переспрос 1 из 1",
    )
    await db.commit()
    marked = await ladder_why(db, task)
    assert marked is not None and marked.code == "second_axis_not_placed", marked

    async def _cap_exhausted(_db, _task, _generation):
        return 3

    monkeypatch.setattr(dispatcher, "deep_cap_exhausted", _cap_exhausted)
    assert await dispatcher._forced_deep_over_cap(db, task, 1, "deep", "x") is True
    named = await ladder_why(db, task)
    assert named is not None and named.code == "deep_cap", named


async def test_the_order_being_prepared_is_not_a_finished_review(
    db: aiosqlite.Connection, monkeypatch
):
    """Круг 5, П1: диспетчер готовит заказ, предыдущий уже закрыт — судью заказывать рано."""
    _, task_id = await _lite_incomplete(db, "round5-preparing")
    task = await _task_dict(db, task_id)
    seen: list[str] = []

    async def _slow(_db, _task_id, **_kwargs):
        seen.append(await top_up_in_flight(db, task))
        seen.append(str(await order_due_runs(db)))
        return False

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _slow)
    assert await dispatcher.maybe_top_up_incomplete(db, task_id) is False
    assert seen[0], "поступок записан, закрытия нет — добор готовится"
    assert seen[1] == "0", "судья в окне подготовки не заказан"
    assert await top_up_in_flight(db, task) == "", "закрытие «отказан» снимает ожидание"
    assert await order_due_runs(db) == 1


async def test_a_second_start_on_the_same_report_is_not_closed_by_the_first_close(
    db: aiosqlite.Connection, monkeypatch
):
    """Круг 6, П1: закрытие прошлой попытки по тому же отчёту следующую не закрывает."""
    _, task_id = await _lite_incomplete(db, "round6-second-start")
    task = await _task_dict(db, task_id)

    async def _refuse(_db, _task_id, **_kwargs):
        return False

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _refuse)
    assert await dispatcher.maybe_top_up_incomplete(db, task_id) is False
    assert await top_up_in_flight(db, task) == ""
    seen: list[str] = []

    async def _slow(_db, _task_id, **_kwargs):
        seen.append(await top_up_in_flight(db, task))
        return False

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _slow)
    assert await dispatcher.maybe_top_up_incomplete(db, task_id) is False
    assert seen[0], "вторая попытка по тому же отчёту готовится"
    assert await top_up_in_flight(db, task) == ""


async def test_a_foreign_report_does_not_cancel_the_preparation(
    db: aiosqlite.Connection, monkeypatch
):
    """Круг 6, П2: пока готовится заказ после R1, пришёл чужой неполный R2."""
    _, task_id = await _lite_incomplete(db, "round6-foreign-during-prep")
    task = await _task_dict(db, task_id)
    seen: list[str] = []

    async def _slow(_db, _task_id, **_kwargs):
        await repo.insert_machine_review(
            db,
            task_id=task_id,
            submission_generation=1,
            model="other",
            raw_count=1,
            incomplete=True,
            incomplete_reason="profile",
            profile="lite",
            principal_id=PRINCIPAL + 1,
        )
        await db.commit()
        seen.append(await top_up_in_flight(db, task))
        seen.append(str(await order_due_runs(db)))
        return False

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _slow)
    assert await dispatcher.maybe_top_up_incomplete(db, task_id) is False
    assert seen[0] and seen[1] == "0"


async def test_a_competing_call_refused_by_the_reservation_does_not_end_the_wait(
    db: aiosqlite.Connection, monkeypatch
):
    """Круг 7, П1: B отказан бронью и закрылся, а A всё ещё готовит заказ."""
    _, task_id = await _lite_incomplete(db, "round7-competing-call")
    task = await _task_dict(db, task_id)
    seen: list[str] = []

    async def _refuse(_db, _task_id, **_kwargs):
        return False

    async def _outer(_db, _task_id, **_kwargs):
        monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _refuse)
        assert await dispatcher.maybe_top_up_incomplete(db, task_id) is False
        seen.append(await top_up_in_flight(db, task))
        seen.append(str(await order_due_runs(db)))
        return False

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _outer)
    assert await dispatcher.maybe_top_up_incomplete(db, task_id) is False
    assert seen[0] and seen[1] == "0", "A ещё готовит заказ"
    assert await top_up_in_flight(db, task) == ""


async def test_a_refused_top_up_does_not_make_the_original_run_a_top_up(
    db: aiosqlite.Connection, monkeypatch
):
    """Круг 7, П2: исходный прогон active (лаг свипа), добор отказан — это не добор."""
    _, task_id = await _lite_incomplete(db, "round7-refused-top-up")
    await db.execute(
        "UPDATE review_dispatches SET status='active' WHERE task_id=?", (task_id,)
    )
    await db.commit()

    async def _refuse(_db, _task_id, **_kwargs):
        return False

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _refuse)
    assert await dispatcher.maybe_top_up_incomplete(db, task_id) is False
    task = await _task_dict(db, task_id)
    assert await top_up_in_flight(db, task) == ""
    why = await ladder_why(db, task)
    assert why is not None and why.code != "top_up_in_flight", why
