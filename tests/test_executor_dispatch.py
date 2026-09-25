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
    OUTCOME_RUNNING,
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
