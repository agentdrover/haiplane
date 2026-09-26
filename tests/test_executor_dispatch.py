"""Строка прогона облачного исполнителя и опрос прогона хабом (#1410, F2.2).

В F0 прогон исполнителя висел около 12 часов, а его цену ($3,22) узнали
вручную: хаб не писал стоимость и не опрашивал прогон. Здесь проверяется
строка прогона (токены, центы ``chargedCents``, длительность, исход), её
обновление опросом и то, что молчание провайдера остаётся названной причиной,
а не нулём и не завершением. Провайдер подменён — сети в тестах нет.
"""

from __future__ import annotations

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.integrations import cursor_cloud
from hub.services.executor_dispatch import (
    OUTCOME_CANCELLED,
    OUTCOME_FINISHED,
    OUTCOME_OVER_CEILING,
    OUTCOME_RUNNING,
    OUTCOME_TAKEN_DOWN,
    REASON_COST_NEVER_CAME,
    REASON_COST_PENDING,
    REASON_RUNS_SILENT,
    REASON_USAGE_SILENT,
    poll_executor_runs,
)
from hub.services.orchestration import practice_metrics

#: Не похоже на настоящий ключ нарочно: secret_scan читает дерево.
_FAKE_KEY = "not-a-real-cursor-key-1410"


@pytest.fixture(autouse=True)
def cursor_key(monkeypatch):
    monkeypatch.setattr(config, "CURSOR_API_KEY", _FAKE_KEY)


async def _task(db: aiosqlite.Connection, title: str = "исполнитель") -> int:
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()
    return task_id


async def _run(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    agent_id: str = "bc-exec-1",
    run_id: str = "run-1",
    generation: int = 1,
    started_ago_s: int = 746,
) -> int:
    row_id = await repo.create_executor_run(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id=agent_id,
        run_id=run_id,
        model="gpt-5.3-codex",
    )
    await db.execute(
        "UPDATE executor_runs SET started_at=datetime('now', ?) WHERE id=?",
        (f"-{started_ago_s} seconds", row_id),
    )
    await db.commit()
    return row_id


def _provider(monkeypatch, *, run: dict | None, usage: dict | None) -> list[str]:
    calls: list[str] = []

    async def _get_run(agent_id: str, run_id: str):
        calls.append(f"run:{agent_id}:{run_id}")
        return run

    async def _get_usage(agent_id: str, run_id: str | None = None):
        calls.append(f"usage:{agent_id}:{run_id}")
        return usage

    monkeypatch.setattr(cursor_cloud, "get_run", _get_run)
    monkeypatch.setattr(cursor_cloud, "get_usage", _get_usage)
    return calls


def _usage(tokens: int, cents: float) -> dict:
    return {"totalUsage": {"totalTokens": tokens, "chargedCents": cents}}


async def _row(db: aiosqlite.Connection, row_id: int) -> dict:
    row = await repo.get_executor_run(db, row_id)
    assert row is not None
    return dict(row)


def _no_key_in(row: dict) -> None:
    assert not any(_FAKE_KEY in str(v) for v in row.values()), (
        "ключ Cursor не должен попадать в строку прогона"
    )


# ---- AC-1: опрос пишет токены, центы и исход ----


async def test_polling_records_tokens_cents_and_outcome(db, monkeypatch):
    task_id = await _task(db)
    row_id = await _run(db, task_id)

    calls = _provider(
        monkeypatch, run={"id": "run-1", "status": "RUNNING"}, usage=_usage(1000, 1.5)
    )
    assert await poll_executor_runs(db) == 1
    assert "run:bc-exec-1:run-1" in calls and "usage:bc-exec-1:run-1" in calls
    row = await _row(db, row_id)
    assert row["tokens"] == 1000
    assert row["cents"] == pytest.approx(1.5)
    assert row["outcome"] == OUTCOME_RUNNING
    assert row["finished_at"] is None
    assert row["duration_ms"] is None

    # F0, шаг 0: FINISHED за 746 с, 1 332 835 токенов, 47.1 цента.
    _provider(
        monkeypatch,
        run={"id": "run-1", "status": "FINISHED"},
        usage=_usage(1_332_835, 47.1),
    )
    assert await poll_executor_runs(db) == 1
    row = await _row(db, row_id)
    assert row["tokens"] == 1_332_835
    assert row["cents"] == pytest.approx(47.1)
    assert row["outcome"] == OUTCOME_FINISHED
    assert row["finished_at"]
    assert 740_000 <= row["duration_ms"] <= 800_000
    assert row["reason"] == ""
    _no_key_in(row)

    # Закрытый прогон больше не опрашивается.
    calls = _provider(monkeypatch, run=None, usage=None)
    assert await poll_executor_runs(db) == 0
    assert calls == []


# ---- AC-2: молчание провайдера — причина, не ноль и не завершение ----


async def test_a_silent_provider_is_named_not_zeroed(db, monkeypatch):
    task_id = await _task(db)
    row_id = await _run(db, task_id)
    _provider(
        monkeypatch, run={"id": "run-1", "status": "RUNNING"}, usage=_usage(5000, 3.0)
    )
    await poll_executor_runs(db)

    # /usage молчит, а /runs говорит FINISHED: цена не прочитана — прогон
    # не закрывается, прежние цифры не обнуляются.
    _provider(monkeypatch, run={"id": "run-1", "status": "FINISHED"}, usage=None)
    await poll_executor_runs(db)
    row = await _row(db, row_id)
    assert row["tokens"] == 5000
    assert row["cents"] == pytest.approx(3.0)
    assert row["outcome"] == OUTCOME_RUNNING
    assert row["finished_at"] is None
    assert row["reason"] == REASON_USAGE_SILENT

    # /runs молчит: то же — причина названа, цифры на месте.
    _provider(monkeypatch, run=None, usage=_usage(9000, 7.0))
    await poll_executor_runs(db)
    row = await _row(db, row_id)
    assert row["tokens"] == 5000
    assert row["cents"] == pytest.approx(3.0)
    assert row["outcome"] == OUTCOME_RUNNING
    assert row["finished_at"] is None
    assert row["reason"] == REASON_RUNS_SILENT
    _no_key_in(row)

    # Прогон, о котором провайдер не сказал ни разу: неизвестно, а не ноль.
    silent_id = await _run(db, task_id, agent_id="bc-exec-2", run_id="run-2")
    _provider(monkeypatch, run=None, usage=None)
    await poll_executor_runs(db)
    silent = await _row(db, silent_id)
    assert silent["tokens"] is None
    assert silent["cents"] is None
    assert silent["outcome"] == OUTCOME_RUNNING
    assert silent["reason"]

    # Провайдер ответил снова — причина снимается.
    _provider(
        monkeypatch, run={"id": "run-1", "status": "FINISHED"}, usage=_usage(9000, 7.0)
    )
    await poll_executor_runs(db)
    row = await _row(db, row_id)
    assert row["outcome"] == OUTCOME_FINISHED
    assert row["tokens"] == 9000
    assert row["reason"] == ""


# ---- находки ревью сдачи 1: где лежит цена и когда прогон закрывается ----


def _sdk_usage(tokens: int, cents: float | None) -> dict:
    """Тело /usage по типам Cloud Agents API/SDK: деньги в соседнем ``cost``."""
    body: dict = {"totalUsage": {"totalTokens": tokens}}
    if cents is not None:
        body["cost"] = {"rawCostCents": 50.0, "chargedCents": cents}
    return body


async def test_cost_is_read_from_the_sdk_cost_object(db, monkeypatch):
    assert cursor_cloud.usage_totals(_sdk_usage(1_332_835, 47.1)) == (
        1_332_835,
        47.1,
    )
    task_id = await _task(db)
    row_id = await _run(db, task_id)
    _provider(
        monkeypatch,
        run={"id": "run-1", "status": "FINISHED"},
        usage=_sdk_usage(1_332_835, 47.1),
    )
    await poll_executor_runs(db)
    row = await _row(db, row_id)
    assert row["cents"] == pytest.approx(47.1)
    assert row["outcome"] == OUTCOME_FINISHED


async def test_a_finished_run_without_cost_waits_for_it(db, monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_COST_WAIT_MIN", 30)
    task_id = await _task(db)
    row_id = await _run(db, task_id)

    # Прогон кончился, токены пришли, цена — ещё нет: не закрывать.
    _provider(
        monkeypatch,
        run={"id": "run-1", "status": "FINISHED"},
        usage=_sdk_usage(1_332_835, None),
    )
    await poll_executor_runs(db)
    row = await _row(db, row_id)
    assert row["tokens"] == 1_332_835
    assert row["cents"] is None
    assert row["outcome"] == OUTCOME_RUNNING
    assert row["finished_at"] is None
    assert row["reason"] == REASON_COST_PENDING

    # Цена пришла на следующем опросе — закрыт с центами.
    _provider(
        monkeypatch,
        run={"id": "run-1", "status": "FINISHED"},
        usage=_usage(1_332_835, 47.1),
    )
    await poll_executor_runs(db)
    row = await _row(db, row_id)
    assert row["outcome"] == OUTCOME_FINISHED
    assert row["cents"] == pytest.approx(47.1)
    assert row["finished_at"]
    assert row["reason"] == ""


async def test_a_price_read_while_running_closes_the_run_at_once(db, monkeypatch):
    """Цена уже в строке с опроса во время RUNNING: конец без cost её не теряет."""
    monkeypatch.setattr(config, "EXECUTOR_COST_WAIT_MIN", 30)
    task_id = await _task(db)
    row_id = await _run(db, task_id)
    _provider(
        monkeypatch, run={"id": "run-1", "status": "RUNNING"}, usage=_usage(1000, 1.5)
    )
    await poll_executor_runs(db)

    _provider(
        monkeypatch,
        run={"id": "run-1", "status": "FINISHED"},
        usage=_sdk_usage(1200, None),
    )
    await poll_executor_runs(db)
    row = await _row(db, row_id)
    assert row["outcome"] == OUTCOME_FINISHED
    assert row["cents"] == pytest.approx(1.5)
    assert row["tokens"] == 1200
    assert row["finished_at"]
    assert row["reason"] == ""


async def test_cost_wait_has_a_ceiling_and_names_it(db, monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_COST_WAIT_MIN", 30)
    task_id = await _task(db)
    row_id = await _run(db, task_id)
    _provider(
        monkeypatch,
        run={"id": "run-1", "status": "FINISHED"},
        usage=_sdk_usage(1000, None),
    )
    await poll_executor_runs(db)
    assert (await _row(db, row_id))["outcome"] == OUTCOME_RUNNING

    # Ещё не срок: 29 минут ожидания.
    await db.execute(
        "UPDATE executor_runs SET cost_wait_since=datetime('now', '-29 minutes') "
        "WHERE id=?",
        (row_id,),
    )
    await db.commit()
    await poll_executor_runs(db)
    assert (await _row(db, row_id))["outcome"] == OUTCOME_RUNNING

    # Срок исчерпан: закрыт исходом провайдера, центы неизвестны, причина названа.
    await db.execute(
        "UPDATE executor_runs SET cost_wait_since=datetime('now', '-31 minutes') "
        "WHERE id=?",
        (row_id,),
    )
    await db.commit()
    await poll_executor_runs(db)
    row = await _row(db, row_id)
    assert row["outcome"] == OUTCOME_FINISHED
    assert row["cents"] is None
    assert row["tokens"] == 1000
    assert row["finished_at"]
    assert row["reason"] == REASON_COST_NEVER_CAME.format(minutes=30)


# ---- AC-3: карточка и practice_metrics ----


def _auth(monkeypatch) -> dict[str, str]:
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {"human-token": TokenIdentity("denis", "human", principal_id=1)},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {"Authorization": "Bearer human-token"}


async def test_executor_cost_is_visible_on_the_card_and_in_metrics(
    db, client: AsyncClient, monkeypatch
):
    headers = _auth(monkeypatch)
    task_id = await _task(db)
    first = await _run(db, task_id, run_id="run-1", generation=1)
    second = await _run(db, task_id, agent_id="bc-exec-2", run_id="run-2", generation=2)
    await repo.update_executor_run(
        db, first, tokens=1_332_835, cents=47.1, outcome=OUTCOME_FINISHED, finish=True
    )
    await repo.update_executor_run(
        db,
        second,
        tokens=10_909_965,
        cents=322.5,
        outcome=OUTCOME_CANCELLED,
        finish=True,
    )
    # Прогон вне окна метрики не считается.
    other = await _task(db, "старая задача")
    old = await _run(db, other, agent_id="bc-old", run_id="run-old")
    await repo.update_executor_run(
        db, old, tokens=1, cents=999.0, outcome=OUTCOME_FINISHED, finish=True
    )
    await db.execute(
        "UPDATE executor_runs SET started_at=datetime('now', '-100 days') WHERE id=?",
        (old,),
    )
    await db.commit()

    page = await client.get(f"/tasks/{task_id}", headers=headers)
    assert page.status_code == 200
    html = page.text
    assert "Прогоны исполнителя" in html
    assert "47.1" in html and "322.5" in html
    assert OUTCOME_FINISHED in html and OUTCOME_CANCELLED in html
    assert _FAKE_KEY not in html

    metrics = (await practice_metrics(db, since_days=30))["executor_runs"]
    assert metrics["runs"] == 2
    assert metrics["cents_total"] == pytest.approx(369.6)
    assert metrics["tokens_total"] == 1_332_835 + 10_909_965
    assert metrics["runs_without_cents"] == 0


# ---- #1411 (F2.3): отмена с повторами, потолок, снятие по факту сдачи ----


def _cancelling_provider(
    monkeypatch,
    *,
    usage: dict | None,
    refusals: int = 0,
    stops: bool = True,
) -> dict:
    """Провайдер, у которого отмена сначала отвечает 429 ``refusals`` раз.

    ``stops=False`` — отмена отвечает 2xx, а прогон так и читается RUNNING:
    ответ на POST — не подтверждение.
    """
    state: dict = {"cancel_calls": 0, "cancelled": set(), "run_reads": 0}

    async def _get_run(agent_id: str, run_id: str):
        state["run_reads"] += 1
        return {
            "id": run_id,
            "status": "CANCELLED" if run_id in state["cancelled"] else "RUNNING",
        }

    async def _get_usage(agent_id: str, run_id: str | None = None):
        return usage

    async def _cancel_run(agent_id: str, run_id: str):
        state["cancel_calls"] += 1
        if state["cancel_calls"] <= refusals:
            return None, cursor_cloud.Refusal(
                status=429, code="rate_limit_exceeded", detail="rate limited"
            )
        if stops:
            state["cancelled"].add(run_id)
        return {"id": run_id}, None

    monkeypatch.setattr(cursor_cloud, "get_run", _get_run)
    monkeypatch.setattr(cursor_cloud, "get_usage", _get_usage)
    monkeypatch.setattr(cursor_cloud, "cancel_run", _cancel_run)
    return state


async def _submitted(db: aiosqlite.Connection, task_id: int, generation: int) -> None:
    await db.execute(
        "UPDATE tasks SET submission_generation=?, status='review' WHERE id=?",
        (generation, task_id),
    )
    await db.commit()


async def _pause_passed(db: aiosqlite.Connection, row_id: int) -> None:
    await db.execute(
        "UPDATE executor_runs SET cancel_last_at=datetime('now', '-1 hour') WHERE id=?",
        (row_id,),
    )
    await db.commit()


async def _alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]


async def test_a_run_is_taken_down_once_its_submission_lands(db, monkeypatch):
    """AC-1: сдача этого поколения легла, прогон RUNNING — хаб его снимает."""
    # Потолки с большим запасом: снимает сдача, а не потолок.
    monkeypatch.setattr(config, "EXECUTOR_TOKEN_CEILING", 50_000_000)
    monkeypatch.setattr(config, "EXECUTOR_CENTS_CEILING", 100_000)
    task_id = await _task(db)
    row_id = await _run(db, task_id, generation=1)

    # Сдачи ещё нет: прогон работает, отмены нет.
    state = _cancelling_provider(monkeypatch, usage=_usage(10_000, 5.0))
    await poll_executor_runs(db)
    assert state["cancel_calls"] == 0
    assert (await _row(db, row_id))["outcome"] == OUTCOME_RUNNING

    # Прогон следующего поколения сдачей первого не снимается.
    later = await _run(db, task_id, agent_id="bc-exec-2", run_id="run-2", generation=2)

    # F0: после сдачи прогон висел RUNNING на 10 909 965 токенах и 322.5 цента.
    await _submitted(db, task_id, 1)
    state = _cancelling_provider(monkeypatch, usage=_usage(10_909_965, 322.5))
    await poll_executor_runs(db)
    assert state["cancel_calls"] == 1
    row = await _row(db, row_id)
    assert row["outcome"] == OUTCOME_TAKEN_DOWN
    assert row["cents"] == pytest.approx(322.5)
    assert row["tokens"] == 10_909_965
    assert row["finished_at"]
    assert row["duration_ms"] is not None
    _no_key_in(row)
    assert (await _row(db, later))["outcome"] == OUTCOME_RUNNING
    assert not any(_FAKE_KEY in a for a in await _alerts(db, task_id))


async def test_cancel_retries_through_rate_limits(db, monkeypatch):
    """AC-2: пять 429 подряд, шестая успешна — отмена доведена до CANCELLED."""
    monkeypatch.setattr(config, "EXECUTOR_CANCEL_MAX_ATTEMPTS", 8)
    monkeypatch.setattr(config, "EXECUTOR_CANCEL_PAUSE_S", 60)
    task_id = await _task(db)
    row_id = await _run(db, task_id, generation=1)
    await _submitted(db, task_id, 1)
    state = _cancelling_provider(monkeypatch, usage=_usage(2000, 1.0), refusals=5)

    for attempt in range(1, 6):
        await poll_executor_runs(db)
        assert state["cancel_calls"] == attempt
        row = await _row(db, row_id)
        assert row["outcome"] == OUTCOME_RUNNING, "429 — не отмена"
        assert "429" in row["reason"]
        # Пауза не вышла — следующий проход отмену не повторяет.
        await poll_executor_runs(db)
        assert state["cancel_calls"] == attempt
        await _pause_passed(db, row_id)

    await poll_executor_runs(db)
    assert state["cancel_calls"] == 6
    row = await _row(db, row_id)
    assert row["outcome"] == OUTCOME_TAKEN_DOWN
    assert row["finished_at"]
    _no_key_in(row)


async def test_a_2xx_cancel_is_confirmed_by_reading_the_run(db, monkeypatch):
    """Ответ 2xx на отмену — не отмена, пока прогон не читается CANCELLED."""
    monkeypatch.setattr(config, "EXECUTOR_CANCEL_PAUSE_S", 60)
    task_id = await _task(db)
    row_id = await _run(db, task_id, generation=1)
    await _submitted(db, task_id, 1)
    state = _cancelling_provider(monkeypatch, usage=_usage(2000, 1.0), stops=False)

    await poll_executor_runs(db)
    assert state["cancel_calls"] == 1
    row = await _row(db, row_id)
    assert row["outcome"] == OUTCOME_RUNNING
    assert row["finished_at"] is None
    assert "RUNNING" in row["reason"]

    # Прогон так и не остановился — хаб пробует снова после паузы.
    await _pause_passed(db, row_id)
    await poll_executor_runs(db)
    assert state["cancel_calls"] == 2


async def test_a_silent_usage_does_not_stop_holding_the_run(db, monkeypatch):
    """Молчание /usage не обрывает снятие по сдаче и начатую отмену (#1411)."""
    monkeypatch.setattr(config, "EXECUTOR_CANCEL_PAUSE_S", 60)
    task_id = await _task(db)
    row_id = await _run(db, task_id, generation=1)
    await _submitted(db, task_id, 1)

    # Сдача легла, /usage молчит — снятие всё равно просится.
    state = _cancelling_provider(monkeypatch, usage=None, refusals=1)
    await poll_executor_runs(db)
    assert state["cancel_calls"] == 1
    row = await _row(db, row_id)
    assert row["cancel_intent"] == OUTCOME_TAKEN_DOWN
    assert "429" in row["reason"]

    # Пауза вышла, /usage всё ещё молчит — отмена повторяется и доводится.
    await _pause_passed(db, row_id)
    await poll_executor_runs(db)
    assert state["cancel_calls"] == 2
    # Прогон отменён; цены нет — строка ждёт её, а не отменяет снова.
    assert (await _row(db, row_id))["reason"] == REASON_COST_PENDING
    await _pause_passed(db, row_id)
    await poll_executor_runs(db)
    assert state["cancel_calls"] == 2


async def test_cancel_exhaustion_is_named_to_a_human(db, monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_CANCEL_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(config, "EXECUTOR_CANCEL_PAUSE_S", 60)
    task_id = await _task(db)
    row_id = await _run(db, task_id, generation=1)
    await _submitted(db, task_id, 1)
    state = _cancelling_provider(monkeypatch, usage=_usage(2000, 1.0), refusals=99)

    for _ in range(5):
        await poll_executor_runs(db)
        await _pause_passed(db, row_id)
    assert state["cancel_calls"] == 3
    row = await _row(db, row_id)
    assert row["outcome"] == OUTCOME_RUNNING
    exhausted = [a for a in await _alerts(db, task_id) if "3 из 3" in a]
    assert len(exhausted) == 1, "исчерпание называется человеку один раз"
    assert "run-1" in exhausted[0]


async def test_crossing_the_ceiling_cancels_and_escalates(db, monkeypatch):
    """AC-3: usage пересёк потолок — отмена, over_ceiling, эскалация с цифрами."""
    monkeypatch.setattr(config, "EXECUTOR_CEILING_MARGIN_PCT", 15)
    task_id = await _task(db)
    row_id = await repo.create_executor_run(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="bc-exec-1",
        run_id="run-1",
        model="gpt-5.3-codex",
        token_ceiling=8_000_000,
        cents_ceiling=3500,
    )
    await db.commit()

    # Далеко от потолка: ничего не происходит.
    state = _cancelling_provider(monkeypatch, usage=_usage(1_000_000, 40.0))
    await poll_executor_runs(db)
    assert state["cancel_calls"] == 0

    # F0: 10 909 965 токенов при потолке 8M.
    state = _cancelling_provider(monkeypatch, usage=_usage(10_909_965, 322.5))
    await poll_executor_runs(db)
    assert state["cancel_calls"] == 1
    row = await _row(db, row_id)
    assert row["outcome"] == OUTCOME_OVER_CEILING
    assert row["tokens"] == 10_909_965
    assert row["cents"] == pytest.approx(322.5)
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision"
    alerts = [a for a in await _alerts(db, task_id) if "потол" in a]
    assert len(alerts) == 1
    for number in ("10909965", "8000000", "322.5", "3500"):
        assert number in alerts[0], number
    assert _FAKE_KEY not in alerts[0]

    # Повторный проход не эскалирует второй раз.
    await poll_executor_runs(db)
    assert len([a for a in await _alerts(db, task_id) if "потол" in a]) == 1


async def test_the_ceiling_cancel_starts_before_the_limit(db, monkeypatch):
    """Отмена стартует с запасом: 429 на отмене не должен пропустить предел."""
    monkeypatch.setattr(config, "EXECUTOR_CEILING_MARGIN_PCT", 15)
    task_id = await _task(db)
    row_id = await repo.create_executor_run(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="bc-exec-1",
        run_id="run-1",
        model="gpt-5.3-codex",
        token_ceiling=8_000_000,
        cents_ceiling=3500,
    )
    await db.commit()
    # 88% токенов: предел не пересечён, но запас уже съеден.
    state = _cancelling_provider(monkeypatch, usage=_usage(7_040_000, 300.0))
    await poll_executor_runs(db)
    assert state["cancel_calls"] == 1
    assert (await _row(db, row_id))["outcome"] == OUTCOME_OVER_CEILING

    # По деньгам — так же: 3000 из 3500 центов.
    other = await _task(db, "дорогая")
    money_row = await repo.create_executor_run(
        db,
        task_id=other,
        submission_generation=1,
        agent_id="bc-exec-3",
        run_id="run-3",
        model="gpt-5.3-codex",
        token_ceiling=8_000_000,
        cents_ceiling=3500,
    )
    await db.commit()
    state = _cancelling_provider(monkeypatch, usage=_usage(100_000, 3000.0))
    await poll_executor_runs(db)
    assert state["cancel_calls"] == 1
    assert (await _row(db, money_row))["outcome"] == OUTCOME_OVER_CEILING


async def test_ceilings_default_from_config_at_order_time(db, monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_TOKEN_CEILING", 1234)
    monkeypatch.setattr(config, "EXECUTOR_CENTS_CEILING", 56.0)
    task_id = await _task(db)
    row = await _row(db, await _run(db, task_id))
    assert row["token_ceiling"] == 1234
    assert row["cents_ceiling"] == pytest.approx(56.0)
