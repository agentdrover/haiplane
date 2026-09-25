"""Прогон облачного исполнителя: строка, опрос, отображение (#1410, F2.2).

В F0 (#1273) прогон исполнителя после сдачи висел около 12 часов, а его цену
($3,22) узнали вручную: хаб стоимость исполнителя не писал и прогон не
опрашивал. Здесь — фундамент, на котором стоят потолок и отмена (F2.3) и
запуск (F2.4): строка ``executor_runs`` и проход поллера, который для каждого
прогона в ``running`` читает ``/runs`` и ``/usage`` провайдера и обновляет
токены, центы (``chargedCents``) и исход.

Чего модуль НЕ делает: не запускает исполнителя (F2.4), не отменяет прогон и
не держит потолок (F2.3). Опрос — только чтение.

Молчание провайдера — названная причина, а не ноль и не завершение: строка
сохраняет прежние цифры и остаётся ``running``, пока цена не прочитана.
Закрыть прогон без прочитанного счёта значило бы записать непрочитанную цену
как окончательную.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.integrations import cursor_cloud

log = logging.getLogger(__name__)

OUTCOME_RUNNING = "running"
OUTCOME_FINISHED = "finished"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_FAILED = "failed"
#: Ставятся F2.3 (потолок) и снятием по факту сдачи; здесь только названы,
#: чтобы словарь исходов жил в одном месте.
OUTCOME_OVER_CEILING = "over_ceiling"
OUTCOME_TAKEN_DOWN = "taken_down"

#: Статус прогона у провайдера → исход строки. Всё, чего здесь нет, —
#: прогон ещё идёт.
_TERMINAL_OUTCOMES = {
    "FINISHED": OUTCOME_FINISHED,
    "CANCELLED": OUTCOME_CANCELLED,
    "ERROR": OUTCOME_FAILED,
    "EXPIRED": OUTCOME_FAILED,
}

REASON_RUNS_SILENT = "провайдер не ответил на /runs — статус прогона неизвестен"
REASON_USAGE_SILENT = (
    "провайдер не ответил на /usage — токены и центы не прочитаны, прогон не закрыт"
)
REASON_NO_RUN_ID = "у строки нет agent_id или run_id — опрашивать нечего"
REASON_COST_PENDING = (
    "прогон закончился, цена ещё не пришла — строка ждёт следующего опроса"
)
REASON_COST_NEVER_CAME = "цена не пришла за {minutes} мин после конца прогона"


async def _ask(call: Any, *args: Any) -> dict[str, Any] | None:
    """Вызов провайдера, где любая авария — молчание, а не падение прохода."""
    try:
        body = await call(*args)
    except Exception as exc:  # noqa: BLE001 — молчание, а не авария свипа
        log.warning("executor run poll: provider call failed: %s", type(exc).__name__)
        return None
    return body if isinstance(body, dict) else None


async def _poll_one(db: aiosqlite.Connection, row: dict[str, Any]) -> None:
    agent_id, run_id = row["agent_id"], row["run_id"]
    if not agent_id or not run_id:
        await repo.update_executor_run(db, row["id"], reason=REASON_NO_RUN_ID)
        return
    run = await _ask(cursor_cloud.get_run, agent_id, run_id)
    if run is None:
        await repo.update_executor_run(db, row["id"], reason=REASON_RUNS_SILENT)
        return
    tokens, cents = cursor_cloud.usage_totals(
        await _ask(cursor_cloud.get_usage, agent_id, run_id)
    )
    if tokens is None and cents is None:
        await repo.update_executor_run(db, row["id"], reason=REASON_USAGE_SILENT)
        return
    outcome = _TERMINAL_OUTCOMES.get(str(run.get("status") or "").upper())
    # Ждать цену — только если её нет ни в ответе, ни в строке: цена,
    # прочитанная опросом во время RUNNING, известна, и конец без cost её не
    # отменяет (COALESCE в update_executor_run её сохранит).
    if outcome is not None and cents is None and row["cents"] is None:
        await _wait_for_cost(db, row["id"], tokens, outcome)
        return
    await repo.update_executor_run(
        db,
        row["id"],
        tokens=tokens,
        cents=cents,
        outcome=outcome or OUTCOME_RUNNING,
        reason="",
        finish=outcome is not None,
    )


async def _wait_for_cost(
    db: aiosqlite.Connection, row_id: int, tokens: int | None, outcome: str
) -> None:
    """Конец прогона без цены: ждать её, но не вечно (#1410).

    Cost у Cursor «eventually consistent» и сразу после конца может не
    прийти. Пока срок не вышел, строка остаётся running с причиной; по
    истечении закрывается исходом провайдера с названной причиной, а центы
    остаются неизвестными (прежнее прочитанное значение не обнуляется).
    """
    minutes = config.EXECUTOR_COST_WAIT_MIN
    if await repo.wait_for_executor_cost(db, row_id, minutes):
        await repo.update_executor_run(
            db,
            row_id,
            tokens=tokens,
            outcome=outcome,
            reason=REASON_COST_NEVER_CAME.format(minutes=minutes),
            finish=True,
        )
        return
    await repo.update_executor_run(
        db, row_id, tokens=tokens, reason=REASON_COST_PENDING
    )


async def poll_executor_runs(db: aiosqlite.Connection) -> int:
    """Один проход опроса: каждая строка в ``running`` — к провайдеру.

    Возвращает число опрошенных строк. Коммит — здесь, один на проход.
    """
    rows = [
        dict(r) for r in await repo.list_executor_runs_in_outcome(db, OUTCOME_RUNNING)
    ]
    for row in rows:
        await _poll_one(db, row)
    if rows:
        await db.commit()
    return len(rows)


async def sweep_executor_runs(db: aiosqlite.Connection) -> None:
    """Проход поллера (#1410): опрос прогонов исполнителя."""
    await poll_executor_runs(db)


async def executor_runs_view(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    """Прогоны исполнителя для карточки задачи; ``None`` — прогонов не было."""
    runs = [
        {
            "generation": int(r["submission_generation"] or 0),
            "model": r["model"] or "",
            "tokens": r["tokens"],
            "cents": r["cents"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "duration_ms": r["duration_ms"],
            "outcome": r["outcome"],
            "reason": r["reason"] or "",
        }
        for r in await repo.list_executor_runs(db, task_id)
    ]
    if not runs:
        return None
    billed = [r["cents"] for r in runs if r["cents"] is not None]
    return {
        "runs": runs,
        "count": len(runs),
        "billed": len(billed),
        "cents_total": round(sum(billed), 2) if billed else None,
    }


async def executor_run_metrics(db: aiosqlite.Connection, since: str) -> dict[str, Any]:
    """Стоимость исполнителя за окно для practice_metrics (#1410).

    ``since`` — модификатор SQLite (``-30 days``), как у соседних метрик.
    Прогоны без прочитанной цены считаются рядом, а не нулём внутри суммы.
    """
    rows = await fetchall(
        db,
        "SELECT COUNT(*) AS runs, "
        "COALESCE(SUM(cents), 0) AS cents_total, "
        "COALESCE(SUM(tokens), 0) AS tokens_total, "
        "COALESCE(SUM(CASE WHEN cents IS NULL THEN 1 ELSE 0 END), 0) "
        "AS runs_without_cents, "
        "COALESCE(SUM(CASE WHEN outcome=? THEN 1 ELSE 0 END), 0) AS runs_running "
        "FROM executor_runs WHERE started_at >= datetime('now', ?)",
        (OUTCOME_RUNNING, since),
    )
    out = dict(rows[0])
    out["cents_total"] = round(float(out["cents_total"]), 2)
    return out
