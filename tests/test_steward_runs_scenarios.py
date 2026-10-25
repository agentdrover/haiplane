"""Многопроходные сценарии свипа steward_runs (#1859, мера Д1).

Все четыре дефекта класса «повтор события или заказ раньше условия» — #1289,
#1290, #1330, #1600 — жили в этом свипе и проявлялись на втором-пятом проходе.
Сценарии идут через настоящий ``Sweep`` из ``SWEEPS`` (оснастка
tests/poller_sweep_support.py) и считают четыре канала раздельно: события,
записи в ленте задач, строки заказов и вызовы провайдера.

Ожидания пишутся по сценарию и каналу. Единственный тест
``test_steward_runs_counts_per_channel`` — ссылка AC-2 и AC-3: именно он обязан
краснеть на мутациях в steward_dispatch и steward_shadow.
"""

from __future__ import annotations

from datetime import timedelta

import aiosqlite
import pytest

from hub import config
from hub.db import fetchall
from hub.services import steward_cap
from hub.services.verdict_owner import EVENT_CHANGED as EVENT_OWNER
from hub.services.steward_dispatch import (
    EVENT_DEFERRED,
    EVENT_ORDERED,
    EVENT_REFUSED,
    RUN_JUDGED,
    RUN_OPEN,
    close_run,
    order_run,
)
from hub.services.steward_shadow import (
    EVENT_RUN_REFUSED,
    EVENT_RUN_STARTED,
    REFUSED_NO_IDENTITY_CHANNEL,
    REFUSED_NOT_CONFIGURED,
)
from tests.poller_sweep_support import (
    configure_steward_env,
    expect,
    freeze_hub_clock,
    frozen_sql_date,
    make_project,
    provider_probe,
    retire_scenario,
    review_dispatch,
    review_settled,
    run_passes,
    runs_of,
    submit_task,
    utc,
)

SWEEP = "steward_runs"
PASSES = 4


@pytest.fixture(autouse=True)
def steward_env(monkeypatch):
    configure_steward_env(monkeypatch)


async def _refusals(db: aiosqlite.Connection, task_id: int, reason: str) -> int:
    rows = await fetchall(
        db,
        "SELECT COUNT(*) AS n FROM events WHERE kind=? AND task_id=? "
        "AND json_extract(payload, '$.reason')=?",
        (EVENT_REFUSED, task_id, reason),
    )
    return int(dict(rows[0])["n"])


# ---------------------------------------------------------------------------
# (а) ревью ожидается: сигнал один, покупок и стартов нет
# ---------------------------------------------------------------------------


async def _scenario_a_review_pending(db: aiosqlite.Connection) -> int:
    """#1289/#1600: пока отчёт ещё придёт, судья не заказывается.

    Два вида ожидания: ревью идёт (строка review_dispatch active) и ревью
    положено, но строки заказа ещё нет. Сигнал ожидания — одна отсрочка на
    сдачу; заказов, стартов и вызовов провайдера нет ни на одном проходе.
    """
    project_id = await make_project(db, "runs-a")
    inflight = await submit_task(db, project_id)
    await review_dispatch(db, inflight, status="active")
    not_yet = await submit_task(db, project_id)  # строки review-dispatch нет

    with provider_probe() as provider:
        log = await run_passes(db, SWEEP, PASSES, provider)

    expect(
        "(а) все проходы",
        log.total,
        events={EVENT_DEFERRED: 2, EVENT_ORDERED: 0, EVENT_RUN_STARTED: 0},
        orders=0,
        provider_calls=0,
    )
    expect(
        "(а) повторы после первого прохода",
        log.after_first,
        events={EVENT_DEFERRED: 0},
        orders=0,
        provider_calls=0,
    )
    assert await runs_of(db, inflight) == [] and await runs_of(db, not_yet) == []
    return inflight


# ---------------------------------------------------------------------------
# (б) после отчёта: один заказ, один старт, повторы не дублируют
# ---------------------------------------------------------------------------


async def _scenario_b_after_the_report(db: aiosqlite.Connection) -> None:
    project_id = await make_project(db, "runs-b")
    task_id = await submit_task(db, project_id)
    await review_dispatch(db, task_id, status="active")
    with provider_probe() as provider:
        await run_passes(db, SWEEP, 2, provider)  # ожидание, как в (а)
        await review_settled(db, task_id)  # отчёт лёг
        log = await run_passes(db, SWEEP, PASSES, provider)

    expect(
        "(б) первый проход после отчёта",
        log.passes[0],
        events={EVENT_ORDERED: 1, EVENT_RUN_STARTED: 1, EVENT_DEFERRED: 0},
        orders=1,
        provider_calls=1,
    )
    expect(
        "(б) повторы после первого прохода",
        log.after_first,
        events={EVENT_ORDERED: 0, EVENT_RUN_STARTED: 0, EVENT_RUN_REFUSED: 0},
        orders=0,
        provider_calls=0,
    )
    runs = await runs_of(db, task_id)
    assert [r["status"] for r in runs] == [RUN_OPEN] and runs[0]["agent_id"]


# ---------------------------------------------------------------------------
# (в) отказ заказа по суточному потолку (#1330)
# ---------------------------------------------------------------------------


async def _spend_the_cap(db: aiosqlite.Connection, project_id: int) -> None:
    """Соседняя сдача потратила потолок в один прогон (и уже закрыта)."""
    neighbour = await submit_task(db, project_id)
    run = await order_run(db, neighbour, 1)
    assert run is not None
    assert await close_run(db, run, RUN_JUDGED, "соседний прогон")
    await db.execute("UPDATE tasks SET status='open' WHERE id=?", (neighbour,))
    await db.commit()


async def _scenario_c_daily_cap(db: aiosqlite.Connection, monkeypatch) -> None:
    # Независимость двух замков от повтора отказа по потолку (cap_handoff в
    # order_due_runs и дедуп record_refusal) здесь не проверяется: каждый
    # отдельно перекрыт другим. Её держат tests/test_steward_cap.py и
    # tests/test_steward_dispatch.py (прямые вызовы каждого замка).
    clock = freeze_hub_clock(monkeypatch, utc(2026, 10, 20))
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 1)
    project_id = await make_project(db, "runs-c")
    await _spend_the_cap(db, project_id)
    task_id = await submit_task(db, project_id)
    await review_dispatch(db, task_id, status="active")
    await review_settled(db, task_id)

    with provider_probe() as provider:
        # Сутки 1, потолок исчерпан: один отказ и один alert на 4 прохода.
        log = await run_passes(db, SWEEP, PASSES, provider)
        expect(
            "(в) одинаковый отказ",
            log.total,
            events={EVENT_REFUSED: 1, EVENT_ORDERED: 0, EVENT_OWNER: 1},
            notifications=1,
            orders=0,
            provider_calls=0,
        )

        # Новое поколение сдачи — новый факт, новая запись.
        await db.execute(
            "UPDATE tasks SET submission_generation=2 WHERE id=?", (task_id,)
        )
        await db.commit()
        await review_dispatch(db, task_id, generation=2, status="active")
        await review_settled(db, task_id, generation=2)
        log = await run_passes(db, SWEEP, PASSES, provider)
        expect(
            "(в) смена поколения",
            log.total,
            events={EVENT_REFUSED: 1, EVENT_OWNER: 1},
            notifications=1,
            orders=0,
            provider_calls=0,
        )

        # Смена причины на том же заказе: отказ режима вместо потолка — новая
        # запись, повторы молчат. Через свип эта причина недостижима (диспетчер
        # при выключенном режиме не заходит в order_run), поэтому вызов прямой.
        # Дедуп этого отказа отсекает прошлые сутки по date('now') SQLite, а не
        # по часам хаба, поэтому день в SQL зафиксирован (иначе реальная
        # полночь UTC посреди шага давала бы флак).
        monkeypatch.setattr(config, "STEWARD_MODE", "off")
        with frozen_sql_date():
            for _ in range(PASSES):
                assert await order_run(db, task_id, 2) is None
        monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
        assert await _refusals(db, task_id, "steward_off") == 1

        # Новые UTC-сутки, потолок снова исчерпан соседом: новый отказ.
        clock.set(utc(2026, 10, 21))
        await _spend_the_cap(db, project_id)
        log = await run_passes(db, SWEEP, PASSES, provider)
        expect(
            "(в) новые сутки, потолок исчерпан",
            log.total,
            events={EVENT_REFUSED: 1, EVENT_ORDERED: 0},
            notifications=1,
            orders=0,
            provider_calls=0,
        )
        assert await _refusals(db, task_id, steward_cap.REFUSED_DAILY_CAP) == 3

        # Ещё сутки, квота свободна: заказ и старт без нового отказа.
        clock.set(clock.moment + timedelta(days=1))
        log = await run_passes(db, SWEEP, PASSES, provider)
        expect(
            "(в) новые сутки, квота свободна",
            log.total,
            events={
                EVENT_REFUSED: 0,
                EVENT_ORDERED: 1,
                EVENT_RUN_STARTED: 1,
                EVENT_OWNER: 1,  # заказ снял передачу по потолку
            },
            notifications=0,
            orders=1,
            provider_calls=1,
        )


# ---------------------------------------------------------------------------
# (г) #1290: повторяющийся временный отказ старта пишется один раз
# ---------------------------------------------------------------------------


async def _scenario_d_start_refusal(db: aiosqlite.Connection, monkeypatch) -> None:
    from hub.services import steward_shadow as sh

    project_id = await make_project(db, "runs-d")
    task_id = await submit_task(db, project_id)
    await review_dispatch(db, task_id, status="active")
    await review_settled(db, task_id)
    monkeypatch.setattr(config, "CURSOR_API_KEY", "")  # старт отказывает временно

    with provider_probe() as provider:
        log = await run_passes(db, SWEEP, PASSES, provider)
        expect(
            "(г) повторяющийся отказ старта",
            log.total,
            events={EVENT_ORDERED: 1, EVENT_RUN_REFUSED: 1, EVENT_RUN_STARTED: 0},
            orders=1,
            provider_calls=0,
        )
        run = (await runs_of(db, task_id))[0]
        assert run["status"] == RUN_OPEN and run["agent_id"] == "", "слот открыт"
        assert await sh.waiting_refusal_code(db, task_id, run["id"]) == (
            REFUSED_NOT_CONFIGURED
        )

        # Прошли сутки: прежний отказ «вчерашний», но причина та же — записи
        # нет. День в ключе дедупа не участвует (steward_shadow, #1290).
        await db.execute(
            "UPDATE events SET created_at=datetime('now', '-1 day') WHERE kind=?",
            (EVENT_RUN_REFUSED,),
        )
        await db.commit()
        log = await run_passes(db, SWEEP, PASSES, provider)
        expect(
            "(г) вчерашний отказ той же причины",
            log.total,
            events={EVENT_RUN_REFUSED: 0},
            orders=0,
            provider_calls=0,
        )

        # Смена причины — новая запись, повторы молчат.
        monkeypatch.setattr(config, "CURSOR_API_KEY", "cursor-key")

        async def _no_channel(_db, _task_id, _generation, _base_url, **_kw):
            return None

        monkeypatch.setattr(sh, "identity_delivery", _no_channel)
        log = await run_passes(db, SWEEP, PASSES, provider)
        expect(
            "(г) смена причины",
            log.total,
            events={EVENT_RUN_REFUSED: 1, EVENT_RUN_STARTED: 0},
            orders=0,
            provider_calls=0,
        )
        assert (
            await sh.waiting_refusal_code(db, task_id, run["id"])
            == REFUSED_NO_IDENTITY_CHANNEL
        )

        # Причина устранена — старт.
        async def _delivery(_db, _task_id, _generation, _base_url, **_kw):
            return "код доступа: ABC-123"

        monkeypatch.setattr(sh, "identity_delivery", _delivery)
        log = await run_passes(db, SWEEP, PASSES, provider)
        expect(
            "(г) причина устранена",
            log.total,
            events={EVENT_RUN_STARTED: 1, EVENT_RUN_REFUSED: 0},
            orders=0,
            provider_calls=1,
        )


# ---------------------------------------------------------------------------
# (д) #1600: заказ есть, ревью снова ожидается — старт не покупается
# ---------------------------------------------------------------------------


async def _scenario_e_start_guard(db: aiosqlite.Connection) -> None:
    project_id = await make_project(db, "runs-e")
    task_id = await submit_task(db, project_id)
    await review_dispatch(db, task_id, status="done", channel="local")
    # Заказ лёг, пока отчёт был не нужен (прямой вызов: минуя страж заказа).
    run = await order_run(db, task_id, 1)
    assert run is not None
    # К повтору старта ревью снова ожидается: ушёл переспрос / встал новый заказ.
    await review_dispatch(db, task_id, status="active", model="grok-4.6")

    with provider_probe() as provider:
        log = await run_passes(db, SWEEP, PASSES, provider)
        expect(
            "(д) ревью ожидается",
            log.total,
            events={EVENT_RUN_STARTED: 0, EVENT_DEFERRED: 1, EVENT_RUN_REFUSED: 0},
            orders=0,
            provider_calls=0,
        )
        slot = (await runs_of(db, task_id))[0]
        assert slot["status"] == RUN_OPEN and slot["agent_id"] == "", (
            "слот не должен быть занят, пока отчёт ещё ждут"
        )

        await review_settled(db, task_id)
        log = await run_passes(db, SWEEP, PASSES, provider)
        expect(
            "(д) отчёт лёг",
            log.total,
            events={EVENT_RUN_STARTED: 1},
            orders=0,
            provider_calls=1,
        )


async def test_steward_runs_counts_per_channel(db: aiosqlite.Connection, monkeypatch):
    """AC-2/AC-3: сценарии (а)–(д), количества по каждому каналу раздельно."""
    await _scenario_a_review_pending(db)
    await retire_scenario(db)
    await _scenario_b_after_the_report(db)
    await retire_scenario(db)
    await _scenario_c_daily_cap(db, monkeypatch)
    await retire_scenario(db)
    await _scenario_d_start_refusal(db, monkeypatch)
    await retire_scenario(db)
    await _scenario_e_start_guard(db)
