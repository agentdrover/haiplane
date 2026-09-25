"""Прогон облачного исполнителя: строка, опрос, отображение (#1410, F2.2).

В F0 (#1273) прогон исполнителя после сдачи висел около 12 часов, а его цену
($3,22) узнали вручную: хаб стоимость исполнителя не писал и прогон не
опрашивал. Здесь — фундамент, на котором стоят потолок и отмена (F2.3) и
запуск (F2.4): строка ``executor_runs`` и проход поллера, который для каждого
прогона в ``running`` читает ``/runs`` и ``/usage`` провайдера и обновляет
токены, центы (``chargedCents``) и исход.

Поверх опроса (#1411, F2.3) хаб прогон держит: отменяет его, когда у задачи
легла сдача его поколения (исход ``taken_down``) или когда usage съел запас
до потолка токенов или центов (``over_ceiling``, эскалация человеку с
цифрами). Отмена — не одна попытка: в F0 она пять раз подряд получила 429 и
взяла с шестой. Каждый проход делает не больше одной попытки после паузы, до
потолка попыток; исход подтверждает только перечтённый прогон в CANCELLED.

Чего модуль НЕ делает: не запускает исполнителя (F2.4).

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
#: Исходы отмены хабом (#1411): прогон, прочитанный CANCELLED после отмены
#: хаба, закрывается причиной отмены, а не голым ``cancelled``.
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
REASON_CANCEL_PAUSE = "отмена {attempt} из {limit} не подтверждена — ждёт паузы"
REASON_CANCEL_TRIED = (
    "отмена {attempt} из {limit} ({intent}): {answer}; прогон читается как {status}"
)
REASON_CANCEL_EXHAUSTED = (
    "отмена не подтверждена за {limit} попыток — прогон не остановлен, "
    "решение за человеком"
)

EVENT_OVER_CEILING = "executor_run_over_ceiling"
EVENT_CANCEL_EXHAUSTED = "executor_run_cancel_exhausted"


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
    if _outcome_of(row, run) is None and await _hold_the_run(db, row, tokens, cents):
        return
    await _settle(db, row, run, tokens, cents)


def _outcome_of(row: dict[str, Any], run: dict[str, Any]) -> str | None:
    """Исход строки по статусу провайдера; ``None`` — прогон ещё идёт.

    CANCELLED после отмены хаба — это причина отмены (#1411), а не голый
    ``cancelled``: иначе снятие по сдаче и остановка по потолку были бы
    неотличимы от отмены кем-то ещё.
    """
    outcome = _TERMINAL_OUTCOMES.get(str(run.get("status") or "").upper())
    if outcome == OUTCOME_CANCELLED and row.get("cancel_intent"):
        return str(row["cancel_intent"])
    return outcome


async def _settle(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    run: dict[str, Any],
    tokens: int | None,
    cents: float | None,
) -> None:
    outcome = _outcome_of(row, run)
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


# ---- #1411 (F2.3): держать прогон — отмена, потолок, снятие по сдаче ----


def _ceilings(row: dict[str, Any]) -> tuple[int, float]:
    """Потолки строки; NULL (строка до миграции) — умолчание конфигурации."""
    tokens = row.get("token_ceiling")
    cents = row.get("cents_ceiling")
    return (
        int(config.EXECUTOR_TOKEN_CEILING if tokens is None else tokens),
        float(config.EXECUTOR_CENTS_CEILING if cents is None else cents),
    )


def _spent(
    row: dict[str, Any], tokens: int | None, cents: float | None
) -> tuple[int | None, float | None]:
    """Прочитанное сейчас, а если провайдер не назвал, — прежнее из строки."""
    return (
        row.get("tokens") if tokens is None else tokens,
        row.get("cents") if cents is None else cents,
    )


def _reached(spent: float | None, ceiling: float, share: float) -> bool:
    return spent is not None and spent >= ceiling * share


def _near_ceiling(row: dict[str, Any], tokens: int | None, cents: float | None) -> bool:
    """Съеден ли запас до любого из потолков.

    Отмена стартует ДО предела: в F0 она пять раз подряд получила 429, и
    отмена ровно на пределе остановила бы прогон уже за ним.
    """
    share = 1 - config.EXECUTOR_CEILING_MARGIN_PCT / 100
    token_ceiling, cents_ceiling = _ceilings(row)
    spent_tokens, spent_cents = _spent(row, tokens, cents)
    return _reached(spent_tokens, token_ceiling, share) or _reached(
        spent_cents, cents_ceiling, share
    )


async def _submission_landed(db: aiosqlite.Connection, row: dict[str, Any]) -> bool:
    """У задачи легла сдача поколения, которое делает этот прогон."""
    generation = int(row["submission_generation"] or 0)
    if generation < 1:
        return False
    task = await repo.get_task(db, int(row["task_id"]))
    if task is None:
        return False
    return int(dict(task).get("submission_generation") or 0) >= generation


async def _cancel_intent(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    tokens: int | None,
    cents: float | None,
) -> str:
    """Почему хаб должен отменить идущий прогон; пусто — не должен.

    Потолок раньше сдачи: деньги называются человеку, даже если сдача уже
    легла.
    """
    if row.get("cancel_intent"):
        return str(row["cancel_intent"])
    if _near_ceiling(row, tokens, cents):
        return OUTCOME_OVER_CEILING
    if await _submission_landed(db, row):
        return OUTCOME_TAKEN_DOWN
    return ""


async def _hold_the_run(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    tokens: int | None,
    cents: float | None,
) -> bool:
    """Отменить прогон, если хабу есть за что; True — строка уже записана."""
    intent = await _cancel_intent(db, row, tokens, cents)
    if not intent:
        return False
    if intent == OUTCOME_OVER_CEILING and not row.get("cancel_intent"):
        await _escalate_over_ceiling(db, row, tokens, cents)
    await _cancel_step(db, row, intent, tokens, cents)
    return True


def _cancel_limit() -> int:
    return max(1, config.EXECUTOR_CANCEL_MAX_ATTEMPTS)


async def _cancel_step(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    intent: str,
    tokens: int | None,
    cents: float | None,
) -> None:
    """Одна попытка отмены за проход — после паузы и до потолка попыток."""
    limit = _cancel_limit()
    attempts = int(row.get("cancel_attempts") or 0)
    if attempts >= limit:
        reason = REASON_CANCEL_EXHAUSTED.format(limit=limit)
    elif await repo.executor_cancel_paused(
        db, row["id"], config.EXECUTOR_CANCEL_PAUSE_S
    ):
        reason = REASON_CANCEL_PAUSE.format(attempt=attempts, limit=limit)
    else:
        await _try_cancel(db, row, intent, tokens, cents)
        return
    await repo.update_executor_run(
        db, row["id"], tokens=tokens, cents=cents, reason=reason
    )


async def _try_cancel(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    intent: str,
    tokens: int | None,
    cents: float | None,
) -> None:
    """Попросить отмену и перечитать прогон: только CANCELLED — отмена."""
    attempt = await repo.record_executor_cancel_attempt(db, row["id"], intent)
    _, refusal = await cursor_cloud.cancel_run(row["agent_id"], row["run_id"])
    run = await _ask(cursor_cloud.get_run, row["agent_id"], row["run_id"])
    held = {**row, "cancel_intent": intent}
    if run is not None and _outcome_of(held, run) is not None:
        await _settle(db, held, run, tokens, cents)
        return
    limit = _cancel_limit()
    status = str((run or {}).get("status") or "неизвестно")
    await repo.update_executor_run(
        db,
        row["id"],
        tokens=tokens,
        cents=cents,
        reason=REASON_CANCEL_TRIED.format(
            attempt=attempt,
            limit=limit,
            intent=intent,
            answer=_answer_text(refusal),
            status=status,
        ),
    )
    if attempt >= limit:
        await _name_exhausted_cancel(db, held, attempt, status)


def _answer_text(refusal: cursor_cloud.Refusal | None) -> str:
    if refusal is None:
        return "провайдер принял просьбу"
    code = f" {refusal.code}" if refusal.code else ""
    return f"отказ HTTP {refusal.status or 'без ответа'}{code}"


def _num(value: float | None) -> str:
    if value is None:
        return "не прочитано"
    return f"{value:.2f}".rstrip("0").rstrip(".")


async def _escalate_over_ceiling(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    tokens: int | None,
    cents: float | None,
) -> None:
    """Прогон упёрся в потолок: человеку — с цифрами, задача — на решение."""
    token_ceiling, cents_ceiling = _ceilings(row)
    spent_tokens, spent_cents = _spent(row, tokens, cents)
    crossed = _reached(spent_tokens, token_ceiling, 1) or _reached(
        spent_cents, cents_ceiling, 1
    )
    margin = _num(config.EXECUTOR_CEILING_MARGIN_PCT)
    task_id = int(row["task_id"])
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Прогон исполнителя {row['run_id']} (сдача {row['submission_generation']}) "
        f"остановлен по потолку: токены {_num(spent_tokens)} при потолке "
        f"{token_ceiling}, центы {_num(spent_cents)} при потолке "
        f"{_num(cents_ceiling)} — "
        + ("потолок пересечён" if crossed else f"запас {margin}% до потолка съеден")
        + ". Хаб отменяет прогон (исход over_ceiling); что делать с задачей, "
        "решает человек (#1411).",
    )
    await repo.insert_event(
        db,
        kind=EVENT_OVER_CEILING,
        task_id=task_id,
        actor="hub",
        payload={
            "executor_run_id": row["id"],
            "tokens": spent_tokens,
            "cents": spent_cents,
            "token_ceiling": token_ceiling,
            "cents_ceiling": cents_ceiling,
            "crossed": crossed,
        },
    )
    await _to_human_decision(db, task_id)
    await db.commit()


async def _to_human_decision(db: aiosqlite.Connection, task_id: int) -> None:
    """Задача, чей исполнитель остановлен, — на решение человеку.

    Только из running: сдача, которая уже легла, идёт своей дорогой ревью, и
    менять её хаб здесь не берётся.
    """
    task = await repo.get_task(db, task_id)
    if task is None or dict(task).get("status") != "running":
        return
    await repo.update_task(db, task_id, status="needs_decision")
    await repo.insert_event(
        db,
        kind="needs_decision",
        task_id=task_id,
        actor="hub",
        payload={"reason": EVENT_OVER_CEILING},
    )


async def _name_exhausted_cancel(
    db: aiosqlite.Connection, row: dict[str, Any], attempts: int, status: str
) -> None:
    """Потолок попыток отмены исчерпан — один алерт человеку."""
    await repo.add_task_update(
        db,
        int(row["task_id"]),
        "hub",
        "alert",
        f"Отмена прогона исполнителя {row['run_id']} не подтверждена: попытка "
        f"{attempts} из {_cancel_limit()}, прогон читается как {status}. "
        f"Хаб больше не просит; прогон может тратить дальше — остановите его "
        f"у провайдера или решите по задаче (#1411).",
    )
    await repo.insert_event(
        db,
        kind=EVENT_CANCEL_EXHAUSTED,
        task_id=int(row["task_id"]),
        actor="hub",
        payload={
            "executor_run_id": row["id"],
            "attempts": attempts,
            "intent": row.get("cancel_intent") or "",
        },
    )
    await db.commit()


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
