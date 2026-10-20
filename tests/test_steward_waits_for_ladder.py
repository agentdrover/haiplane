"""Стюард и неполный отчёт ревью: ждать добора в полёте и называть, почему он не помог (#1925).

20.10 судья стюарда семь раз эскалировал report_incomplete; в #1910 g1 судья
заказан через 18 секунд после неполного lite, пока заказанный deep ещё шёл.

AC-1: неполный отчёт + добор в полёте => судья не заказан, в ленте отсрочка
«ждёт добора»; отчёт добора -> судья; срок -> судья с причиной.
AC-2: эскалация report_incomplete называет причину, почему лестница не помогла;
нет данных — «причина не установлена».
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.db import fetchall
from hub.models import StewardGround, StewardJudgementSubmit
from hub.services import gate_grounds
from hub.services import review_dispatch as dispatcher
from hub.services.ladder_view import (
    UNKNOWN_TEXT,
    WHY_AXIS_EXHAUSTED,
    WHY_AXIS_INCOMPLETE,
    WHY_AXIS_NO_REPORT,
    WHY_CEILING,
    WHY_CIRCLE,
    WHY_DEEP_CAP,
    WHY_ENVIRONMENT,
    WHY_IN_FLIGHT,
    WHY_NOT_INCOMPLETE,
    WHY_ORDER_FAILED,
    WHY_TOP_UP_NO_REPORT,
    WHY_UNKNOWN,
    incomplete_escalation_line,
    ladder_why,
    top_up_in_flight,
)
from hub.services.steward_dispatch import (
    EVENT_DEFERRED,
    EVENT_ORDERED,
    order_due_runs,
    review_wait_gate,
)
from tests.test_steward_dispatch import (
    _due_task,
    _events,
    _submitted_minutes_ago,
)

PRINCIPAL = 7


@pytest.fixture(autouse=True)
def steward_env(monkeypatch):
    from hub.services import steward_ci_wait

    async def _not_waiting(_db, _task):
        return steward_ci_wait.NOT_WAITING

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 50)
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 120)
    monkeypatch.setattr(steward_ci_wait, "ci_wait_view", _not_waiting)


async def _dispatch_row(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    status: str,
    profile: str,
    agent_id: str = "rev-agent",
    model: str = "gpt-5.2",
    principal: int | None = PRINCIPAL,
) -> int:
    cur = await db.execute(
        "INSERT INTO review_dispatches (task_id, submission_generation, agent_id, "
        "run_id, model, profile, status, channel, reviewer_principal_id) "
        "VALUES (?, 1, ?, 'run-1', ?, ?, ?, 'cloud', ?)",
        (task_id, agent_id, model, profile, status, principal),
    )
    await db.commit()
    return int(cur.lastrowid or 0)


async def _report_row(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    incomplete: bool | None,
    profile: str,
    reason: str = "profile",
) -> None:
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        model="gpt-5.2",
        raw_count=3,
        submitted_by="rev-agent",
        incomplete=incomplete,
        incomplete_reason=reason if incomplete else "",
        profile=profile,
        principal_id=PRINCIPAL,
    )
    if incomplete is None:
        await db.execute(
            "UPDATE machine_reviews SET incomplete=NULL WHERE task_id=?", (task_id,)
        )
    await db.commit()


async def _lite_incomplete(
    db: aiosqlite.Connection, slug: str, *, minutes: int = 3
) -> tuple[int, int]:
    """Сдача: лёгкий прогон отдал неполный отчёт (его заказ — done)."""
    project_id, task_id = await _due_task(db, slug)
    await _submitted_minutes_ago(db, task_id, minutes)
    await _dispatch_row(db, task_id, status="done", profile="lite")
    await _report_row(db, task_id, incomplete=True, profile="lite")
    return project_id, task_id


async def _task_dict(db: aiosqlite.Connection, task_id: int) -> dict[str, Any]:
    return dict(await repo.get_task(db, task_id) or {})


async def _why_of_orders(db: aiosqlite.Connection) -> dict[int, str]:
    out: dict[int, str] = {}
    for event in await _events(db, EVENT_ORDERED):
        payload = json.loads(event["payload"])
        row = (
            await fetchall(
                db, "SELECT task_id FROM steward_runs WHERE id=?", (payload["run_id"],)
            )
        )[0]
        out[dict(row)["task_id"]] = payload["why"]
    return out


async def test_steward_waits_for_a_top_up_in_flight(db: aiosqlite.Connection):
    """AC-1 (как #1910 g1): неполный lite + deep в полёте — судью не заказывают."""
    _, flight = await _lite_incomplete(db, "ladder-flight")
    await _dispatch_row(db, flight, status="active", profile="deep")

    assert await order_due_runs(db) == 0
    assert await order_due_runs(db) == 0  # второй тик: тот же ответ, без повтора
    assert list(await fetchall(db, "SELECT 1 FROM steward_runs")) == []
    deferrals = await _events(db, EVENT_DEFERRED)
    assert len(deferrals) == 1, "одна отсрочка на (поколение, причина)"
    payload = json.loads(deferrals[0]["payload"])
    assert payload["wait_kind"] == "top_up"
    assert "ждёт добора" in payload["detail"]

    # Отчёт добора лёг — отсрочка кончается заказом судьи, который читает оба.
    await _report_row(db, flight, incomplete=False, profile="deep")
    assert await order_due_runs(db) == 1

    # Негатив: лёгкий прогон, уже отдавший неполный отчёт и ещё active, — не
    # добор (лестница лишь МОЖЕТ его поставить, граница задачи).
    _, lite_only = await _lite_incomplete(db, "ladder-lite-active")
    await db.execute(
        "UPDATE review_dispatches SET status='active' WHERE task_id=?", (lite_only,)
    )
    await db.commit()
    assert await top_up_in_flight(db, await _task_dict(db, lite_only)) == ""
    assert await order_due_runs(db) == 1

    # Добор упал без отчёта — в полёте его нет, судья заказан.
    _, failed = await _lite_incomplete(db, "ladder-failed-top-up")
    await _dispatch_row(db, failed, status="failed", profile="deep")
    assert await top_up_in_flight(db, await _task_dict(db, failed)) == ""
    assert await order_due_runs(db) == 1

    # Открытый режим: заказ добора без принципала. Прежний lite-отчёт не
    # считается его ответом — ступеней больше, чем отчётов (Codex P2).
    _, unpinned = await _lite_incomplete(db, "ladder-unpinned")
    await db.execute(
        "UPDATE review_dispatches SET reviewer_principal_id=NULL WHERE task_id=?",
        (unpinned,),
    )
    await _dispatch_row(db, unpinned, status="active", profile="deep", principal=None)
    assert await top_up_in_flight(db, await _task_dict(db, unpinned))
    assert await order_due_runs(db) == 0
    await _report_row(db, unpinned, incomplete=False, profile="deep")
    assert await top_up_in_flight(db, await _task_dict(db, unpinned)) == ""

    # Поле incomplete нечитаемо (NULL) — «неполный» не выдумывается, не ждём.
    _, unknown = await _lite_incomplete(db, "ladder-null-flag")
    await db.execute(
        "UPDATE machine_reviews SET incomplete=NULL WHERE task_id=?", (unknown,)
    )
    await _dispatch_row(db, unknown, status="active", profile="deep")
    assert await top_up_in_flight(db, await _task_dict(db, unknown)) == ""
    # Заказаны оба: сдача с NULL-признаком и открытая сдача с лёгшим отчётом.
    assert await order_due_runs(db) == 2


async def test_the_top_up_wait_ends_at_the_ceiling_with_a_named_reason(
    db: aiosqlite.Connection,
):
    """AC-1, срок: по STEWARD_REVIEW_WAIT_MAX судья заказан, причина названа."""
    _, over = await _lite_incomplete(db, "ladder-over", minutes=121)
    await _dispatch_row(db, over, status="active", profile="deep")
    _, under = await _lite_incomplete(db, "ladder-under", minutes=119)
    await _dispatch_row(db, under, status="active", profile="deep")

    assert await order_due_runs(db) == 1
    why = await _why_of_orders(db)
    assert set(why) == {over}
    assert "ждали отчёт 121 мин" in why[over]
    assert "ждёт добора" in why[over]
    # И судья, дойдя до эскалации, называет: добор не завершился за срок.
    line = await incomplete_escalation_line(db, await _task_dict(db, over))
    assert WHY_IN_FLIGHT in line


async def test_the_reason_survives_the_top_up_ending_after_the_order(
    db: aiosqlite.Connection,
):
    """Добор кончился уже после заказа судьи: причина — в строке заказа добора."""
    _, task_id = await _lite_incomplete(db, "ladder-order-basis", minutes=121)
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    assert await order_due_runs(db) == 1
    await db.execute("UPDATE review_dispatches SET status='failed' WHERE id=?", (deep,))
    await db.commit()
    task = await _task_dict(db, task_id)
    assert await top_up_in_flight(db, task) == ""
    why = await ladder_why(db, task)
    assert why is not None and why.code == WHY_TOP_UP_NO_REPORT, why


async def test_deferral_is_deduplicated_per_generation_and_kind(
    db: aiosqlite.Connection,
):
    """Ожидание отчёта и ожидание добора — разные причины, обе в ленте."""
    _, task_id = await _lite_incomplete(db, "ladder-dedup")
    # Прежняя запись (до #1925, без вида) — это отсрочка «отчёт ревью».
    await repo.insert_event(
        db,
        kind=EVENT_DEFERRED,
        task_id=task_id,
        actor="hub",
        payload={"generation": 1, "reason": "review_in_flight", "detail": "старая"},
    )
    await db.commit()
    await _dispatch_row(db, task_id, status="active", profile="deep")

    assert await order_due_runs(db) == 0
    assert await order_due_runs(db) == 0
    assert len(await _events(db, EVENT_DEFERRED)) == 2


async def test_the_wait_reader_stays_a_reader(db: aiosqlite.Connection):
    """Ожидание добора ничего не пишет: снимок таблиц до и после равен."""
    _, task_id = await _lite_incomplete(db, "ladder-reader")
    await _dispatch_row(db, task_id, status="active", profile="deep")
    task = await _task_dict(db, task_id)

    async def snapshot() -> list[list[dict]]:
        out = []
        for table in ("events", "task_updates", "steward_runs", "review_dispatches"):
            rows = await fetchall(db, f"SELECT * FROM {table} ORDER BY id")  # nosec B608
            out.append([dict(r) for r in rows])
        return out

    before = await snapshot()
    gate = await review_wait_gate(db, task)
    assert gate.blocker and gate.wait_kind == "top_up"
    await ladder_why(db, task)
    assert await snapshot() == before


async def _cascade_event(
    db: aiosqlite.Connection, task_id: int, *, dispatch_id: int | None, after: int
) -> None:
    """Событие попытки второй оси так, как его пишет _ask_a_stronger_model."""
    await repo.insert_event(
        db,
        kind=dispatcher.MODEL_CASCADE_EVENT,
        task_id=task_id,
        actor="policy",
        payload={
            "generation": 1,
            "model": "grok-4.6",
            "attempt": 1,
            "dispatch_id": dispatch_id,
            "after_review_id": after,
        },
    )
    await db.commit()


async def _last_report_id(db: aiosqlite.Connection, task_id: int) -> int:
    return int((await repo.machine_reviews_of_generation(db, task_id, 1))[-1]["id"])


async def _alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]


async def test_incomplete_escalation_names_why_the_ladder_did_not_help(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-2: каждый исход лестницы называет свою причину; нет данных — «не установлена»."""
    cases: dict[str, tuple[int, str]] = {}

    # (а) deep_cap: настоящий отказ диспетчера, не вручную вписанный текст.
    _, cap = await _lite_incomplete(db, "why-cap")
    task = await _task_dict(db, cap)

    async def _cap_exhausted(_db, _task, _generation):
        return 3

    monkeypatch.setattr(dispatcher, "deep_cap_exhausted", _cap_exhausted)
    assert await dispatcher._forced_deep_over_cap(db, task, 1, "deep", "") is True
    cases["cap"] = (cap, WHY_DEEP_CAP)

    # (б) круг ревью #1432: настоящий отказ принудительного deep.
    _, circle = await _lite_incomplete(db, "why-circle")
    task = await _task_dict(db, circle)

    async def _stopped(_db, _task):
        return (3, 3)

    monkeypatch.setattr(dispatcher, "circle_deep_stop", _stopped)
    assert await dispatcher._forced_deep_past_circle(db, task, "deep", "") is True
    cases["circle"] = (circle, WHY_CIRCLE)

    # (а') и круг, и потолок deep — с общим алертом лестницы ПОСЛЕ конкретного
    # (maybe_top_up_incomplete пишет его всегда): конкретная причина не теряется.
    for name in ("cap", "circle"):
        await repo.add_task_update(
            db,
            cases[name][0],
            "hub",
            "alert",
            "Дешёвый прогон сдал неполный отчёт, а добор тяжёлым профилем "
            f"{dispatcher.LADDER_ORDER_FAILED_MARK}. Решение за человеком (#879).",
        )

    # (в) потолок лестницы: настоящий maybe_top_up_incomplete на двух платных
    # прогонах и неполном deep-отчёте (вторая ось выключена).
    monkeypatch.setattr(config, "REVIEW_MODEL_CASCADE_MAX", 0)
    _, ceiling = await _lite_incomplete(db, "why-ceiling")
    await _dispatch_row(db, ceiling, status="done", profile="deep")
    await _report_row(db, ceiling, incomplete=True, profile="deep")
    assert await dispatcher.maybe_top_up_incomplete(db, ceiling) is False
    cases["ceiling"] = (ceiling, WHY_CEILING)

    # (г) вторая ось заказана, отчёта у неё нет.
    _, axis = await _lite_incomplete(db, "why-axis-no-report")
    await _dispatch_row(db, axis, status="done", profile="deep")
    await _report_row(db, axis, incomplete=True, profile="deep")
    await repo.add_task_update(
        db,
        axis,
        "hub",
        "alert",
        dispatcher.MODEL_CASCADE_MARK.format(generation=1) + " Переспрос 1 из 1",
    )
    axis_dispatch = await _dispatch_row(
        db, axis, status="failed", profile="deep", agent_id="rev-2"
    )
    await _cascade_event(
        db, axis, dispatch_id=axis_dispatch, after=await _last_report_id(db, axis)
    )
    cases["axis_no_report"] = (axis, WHY_AXIS_NO_REPORT)

    # (г') та же ось отдала неполный отчёт, потолок оси исчерпан.
    _, spent = await _lite_incomplete(db, "why-axis-exhausted")
    await _dispatch_row(db, spent, status="done", profile="deep")
    await _report_row(db, spent, incomplete=True, profile="deep")
    await repo.add_task_update(
        db,
        spent,
        "hub",
        "alert",
        dispatcher.MODEL_CASCADE_EXHAUSTED_MARK.format(generation=1) + " потолок",
    )
    cases["axis_exhausted"] = (spent, WHY_AXIS_EXHAUSTED)

    # (г'') ось отдала свой неполный отчёт, а следующего алерта ещё нет.
    _, answered = await _lite_incomplete(db, "why-axis-incomplete")
    await _dispatch_row(db, answered, status="done", profile="deep")
    await _report_row(db, answered, incomplete=True, profile="deep")
    await repo.add_task_update(
        db,
        answered,
        "hub",
        "alert",
        dispatcher.MODEL_CASCADE_MARK.format(generation=1) + " Переспрос 1 из 1",
    )
    answered_dispatch = await _dispatch_row(
        db, answered, status="done", profile="deep", agent_id="rev-2"
    )
    before = await _last_report_id(db, answered)
    await _report_row(db, answered, incomplete=True, profile="deep")
    await _cascade_event(db, answered, dispatch_id=answered_dispatch, after=before)
    cases["axis_incomplete"] = (answered, WHY_AXIS_INCOMPLETE)

    # (д) отказ среды — по заявлению самого отчёта.
    _, env = await _lite_incomplete(db, "why-environment")
    await db.execute(
        "UPDATE machine_reviews SET incomplete_reason='environment' WHERE task_id=?",
        (env,),
    )
    await db.commit()
    cases["environment"] = (env, WHY_ENVIRONMENT)

    # (е) нет записей лестницы — «причина не установлена», не пусто.
    _, nothing = await _lite_incomplete(db, "why-unknown")
    cases["unknown"] = (nothing, WHY_UNKNOWN)

    # (е'') только общий алерт «поставить не удалось» — он и есть причина.
    _, failed_order = await _lite_incomplete(db, "why-order-failed")
    await repo.add_task_update(
        db,
        failed_order,
        "hub",
        "alert",
        f"добор тяжёлым профилем {dispatcher.LADDER_ORDER_FAILED_MARK}",
    )
    cases["order_failed"] = (failed_order, WHY_ORDER_FAILED)

    # (е') алерт лестницы ПРЕЖНЕЙ сдачи (раньше метки этой) — не наш.
    _, stale = await _lite_incomplete(db, "why-stale-alert")
    await db.execute(
        "INSERT INTO task_updates (task_id, agent, kind, content, created_at) "
        "VALUES (?, 'hub', 'alert', ?, datetime('now', '-60 minutes'))",
        (stale, f"{dispatcher.LADDER_CEILING_MARK} 2 достигнут"),
    )
    await db.commit()
    cases["stale_alert"] = (stale, WHY_UNKNOWN)

    # (ж) отчёт заявлен полным: неполнота не от лестницы (#1692).
    _, complete = await _lite_incomplete(db, "why-complete")
    await _report_row(db, complete, incomplete=False, profile="deep")
    cases["complete"] = (complete, WHY_NOT_INCOMPLETE)

    seen = set()
    for name, (task_id, code) in cases.items():
        task = await _task_dict(db, task_id)
        why = await ladder_why(db, task)
        assert why is not None and why.code == code, (name, why)
        line = await incomplete_escalation_line(db, task)
        assert why.text in line and code in line, name
        seen.add(why.text)
    assert len(seen) == len({c for _, c in cases.values()}), (
        "у каждого исхода своя фраза"
    )
    assert UNKNOWN_TEXT in (await ladder_why(db, await _task_dict(db, nothing))).text


async def test_escalation_surfaces_carry_the_reason(
    db: aiosqlite.Connection, monkeypatch
):
    """Читатели эскалации: лента суждения, справка владельцу, правило gate_grounds."""
    from hub.services import decision_brief
    from tests.test_decision_brief import _judgement

    _, task_id = await _lite_incomplete(db, "why-surfaces")
    await repo.update_task(db, task_id, status="review")
    await db.commit()
    task = await _task_dict(db, task_id)
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Неполный отчёт после 2 прогон(ов): {dispatcher.LADDER_CEILING_MARK} 2 "
        "достигнут",
    )

    # 1) лента: запись суждения escalate/report_incomplete оставляет алерт.
    await dispatcher_feed_judgement(db, task_id)
    feed = "\n".join(await _alerts(db, task_id))
    assert "Стюард передал решение владельцу: report_incomplete" in feed
    assert WHY_CEILING in feed

    # 2) справка: причина — в why рекомендации.
    await _judgement(db, task_id, 1, "escalate", escalate_reason="report_incomplete")
    brief = await decision_brief.build(db, task, None)
    assert any(WHY_CEILING in line for line in brief["recommend"]["why"]), brief

    # 3) правило gate_grounds: причина из входов; без входов — «не установлена».
    class _Fact:
        def __init__(self, value: dict | None):
            self.value = value or {}
            self.is_absent = value is None
            self.detail = ""

    class _Packet:
        def fact(self, _source):
            return _Fact({"incomplete": True, "confirmed": [], "rejected": []})

    named = gate_grounds.decide(
        _Packet(), gate_grounds.PolicyInputs(ladder_why="ladder_ceiling: потолок")
    )
    assert named.reason == "report_incomplete"
    assert "ladder_ceiling: потолок" in named.detail
    unnamed = gate_grounds.decide(_Packet())
    assert gate_grounds.LADDER_WHY_UNKNOWN in unnamed.detail


async def dispatcher_feed_judgement(db: aiosqlite.Connection, task_id: int) -> None:
    from hub.services.steward_judgement import record_steward_judgement

    await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=1,
            kind="verdict",
            verdict="escalate",
            escalate_reason="report_incomplete",
            confidence="high",
            grounds=[StewardGround(source="machine_review_report")],
        ),
        TokenIdentity("steward-bot", "steward"),
    )


async def test_an_unreadable_ladder_is_named_not_hidden(
    db: aiosqlite.Connection, monkeypatch
):
    """Сбой чтения записей лестницы не маскируется под «нет добора» и не роняет суждение."""
    from hub.services import ladder_view

    _, task_id = await _lite_incomplete(db, "why-broken")
    await repo.update_task(db, task_id, status="review")
    await db.commit()

    async def _boom(*_a, **_k):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(ladder_view, "ladder_why", _boom)
    await dispatcher_feed_judgement(db, task_id)
    feed = "\n".join(await _alerts(db, task_id))
    assert UNKNOWN_TEXT in feed and "RuntimeError" in feed
    assert (
        len(
            await fetchall(
                db, "SELECT 1 FROM steward_judgements WHERE task_id=?", (task_id,)
            )
        )
        == 1
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
        db, task_id, status="second_door", profile="deep", agent_id="", principal=None
    )
    task = await _task_dict(db, task_id)
    assert await top_up_in_flight(db, task), "долг второй двери — добор в пути"
    assert await order_due_runs(db) == 0

    # Нечитаемая метка заказа: связь неизвестна — не «отвечен», ждём (срок закроет).
    await db.execute(
        "UPDATE review_dispatches SET created_at='мусор' WHERE status='second_door'"
    )
    await db.commit()
    assert await top_up_in_flight(db, task)

    # Отчёт, созданный ПОСЛЕ заказа, — его ответ: ожидание кончается.
    await db.execute(
        "UPDATE review_dispatches SET created_at=datetime('now', '-30 seconds') "
        "WHERE status='second_door'"
    )
    await _report_row(db, task_id, incomplete=True, profile="deep")
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
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
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
