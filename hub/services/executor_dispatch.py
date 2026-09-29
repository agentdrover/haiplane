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

Человек может остановить прогон из хаба (#1455): та же отмена с повторами,
исход ``cancelled_by_human``. Прогон, закрытый без сдачи (отмена, ошибка,
потолок), снимает задачу из running на решение человеку.

Прогон, закончившийся FINISHED без сдачи своего поколения (#1446), — исход
``finished_without_submission``: в том же проходе alert с причиной и хвостом
итогового текста, затем один повторный прогон «только сдай» (заказ — через
executor_launch, F2.4) или решение человека. Третьего прогона нет.

Первый запуск исполнителя модуль не делает (F2.4).

Молчание провайдера — названная причина, а не ноль и не завершение: строка
сохраняет прежние цифры и остаётся ``running``, пока цена не прочитана.
Закрыть прогон без прочитанного счёта значило бы записать непрочитанную цену
как окончательную.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
#: #1455: остановка человеком из хаба — та же отмена с повторами.
OUTCOME_CANCELLED_BY_HUMAN = "cancelled_by_human"
#: #1446: провайдер сказал FINISHED, а сдачи поколения прогона нет.
OUTCOME_FINISHED_WITHOUT_SUBMISSION = "finished_without_submission"

#: Исходы, которыми прогон кончился без работы до конца (#1455): задача в
#: running без сдачи его поколения уходит на решение. FINISHED без сдачи —
#: другой случай (#1446), taken_down — сдача легла.
STOPPED_OUTCOMES: tuple[str, ...] = (
    OUTCOME_CANCELLED,
    OUTCOME_FAILED,
    OUTCOME_OVER_CEILING,
    OUTCOME_CANCELLED_BY_HUMAN,
)

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
#: #1455: причина needs_decision — прогон остановлен без сдачи.
EVENT_RUN_STOPPED = "executor_run_stopped"
#: #1446: прогон FINISHED без сдачи своего поколения — событие и причина
#: needs_decision, когда повтора «только сдай» не будет.
EVENT_FINISHED_WITHOUT_SUBMISSION = "executor_run_finished_without_submission"

REASON_NO_LIVE_RUN = "по задаче нет идущего прогона исполнителя"


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
        # #1439: у брони запуска без агента причина уже может быть названа
        # (слепая потеря ответа: «агент мог быть создан») — её не затирать
        # общим «нечего опрашивать».
        if not row.get("reason"):
            await repo.update_executor_run(db, row["id"], reason=REASON_NO_RUN_ID)
        return
    run = await _ask(cursor_cloud.get_run, agent_id, run_id)
    if run is None:
        await repo.update_executor_run(db, row["id"], reason=REASON_RUNS_SILENT)
        return
    tokens, cents = cursor_cloud.usage_totals(
        await _ask(cursor_cloud.get_usage, agent_id, run_id)
    )
    # Держать прогон — до проверки молчания /usage: снятие по сдаче и
    # начатая отмена от свежего счёта не зависят, потолок судит по прежнему
    # из строки (_spent).
    if _outcome_of(row, run) is None and await _hold_the_run(db, row, tokens, cents):
        return
    if tokens is None and cents is None:
        await repo.update_executor_run(db, row["id"], reason=REASON_USAGE_SILENT)
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
    if outcome == OUTCOME_FINISHED and not await _submission_landed(db, row):
        # #1446: закончился, не сдав своё поколение, — отдельный исход.
        outcome = OUTCOME_FINISHED_WITHOUT_SUBMISSION
    # Ждать цену — только если её нет ни в ответе, ни в строке: цена,
    # прочитанная опросом во время RUNNING, известна, и конец без cost её не
    # отменяет (COALESCE в update_executor_run её сохранит).
    if outcome is not None and cents is None and row["cents"] is None:
        if not await _wait_for_cost(db, row["id"], tokens, outcome):
            return
    else:
        await repo.update_executor_run(
            db,
            row["id"],
            tokens=tokens,
            cents=cents,
            outcome=outcome or OUTCOME_RUNNING,
            reason="",
            finish=outcome is not None,
        )
    if outcome is not None:
        await _note_human_stop(db, row, outcome)
        await _note_silent_finish(db, row, run, outcome)


async def _note_human_stop(
    db: aiosqlite.Connection, row: dict[str, Any], outcome: str
) -> None:
    """Запись в карточке: остановку человека провайдер подтвердил (#1455)."""
    if outcome != OUTCOME_CANCELLED_BY_HUMAN:
        return
    await repo.add_task_update(
        db,
        int(row["task_id"]),
        "hub",
        "status",
        f"Прогон исполнителя {row['run_id']} (сдача {row['submission_generation']}) "
        f"остановлен по просьбе человека: провайдер подтвердил CANCELLED, исход "
        f"{OUTCOME_CANCELLED_BY_HUMAN} (#1455).",
    )


# ---- #1446: прогон FINISHED без сдачи своего поколения ----

#: Сколько хвоста итогового текста прогона идёт в alert (AC-5).
RESULT_TAIL_CHARS = 500
#: Исход решения по тихому прогону: заказать «только сдай», отдать человеку
#: или только назвать (задачу уже ведёт кто-то другой).
_RETRY, _HUMAN, _NOTE = "retry", "human", "note"


def _result_tail(run: dict[str, Any]) -> str:
    """Хвост итогового текста прогона — поле ``result``, как у review_dispatch."""
    text = str(run.get("result") or "").strip()
    return text[-RESULT_TAIL_CHARS:]


async def _branch_tip(db: aiosqlite.Connection, task_id: int, branch: str) -> str:
    """Вершина ветки, как её видит сам хаб; пусто — ветки нет или не прочитана."""
    from hub.services import lifecycle

    sha, _why = await lifecycle.resolve_branch_tip(db, task_id, branch)
    return sha


async def _tip_ci_red(
    db: aiosqlite.Connection, task_id: int, branch: str, tip: str
) -> bool:
    """Последний прогон CI на вершине закончился неуспехом (красный)."""
    from hub.integrations.registry import plugins
    from hub.services.project_policy import forge_of
    from hub.services.red_base import _FAILED

    project = dict(await repo.resolve_project_for_task(db, task_id) or {})
    runs = await plugins.git_ops.branch_ci_runs(
        branch,
        repo=(project.get("workspace_path") or "").strip() or None,
        gh_repo=(project.get("repo") or "").strip() or None,
        forge=forge_of(project) if project else "",
    )
    on_tip = [r for r in runs or [] if r.get("sha") == tip]
    return bool(on_tip) and on_tip[0].get("conclusion") in _FAILED


async def _earlier_silent_runs(db: aiosqlite.Connection, row: dict[str, Any]) -> int:
    """Сколько прогонов этого поколения уже закончились без сдачи до этого.

    Счёт — по строкам, а не в памяти: повтор один на поколение и после
    перезапуска хаба.
    """
    return sum(
        1
        for r in await repo.list_executor_runs(db, int(row["task_id"]))
        if int(r["id"]) != int(row["id"])
        and r["outcome"] == OUTCOME_FINISHED_WITHOUT_SUBMISSION
        and int(r["submission_generation"] or 0)
        == int(row["submission_generation"] or 0)
    )


async def _budget_short(db: aiosqlite.Connection, task_id: int) -> str:
    """Причина, если бюджет задачи (#1443) не вмещает прогон «только сдай»."""
    budget = await task_budget(db, task_id)
    if budget.unknown or budget.exhausted:
        return f"бюджет исполнителя на задачу не вмещает повтор: {budget.text()}"
    if (
        budget.cents_left < config.EXECUTOR_SUBMIT_ONLY_CENTS_CEILING
        or budget.tokens_left < config.EXECUTOR_SUBMIT_ONLY_TOKEN_CEILING
    ):
        return (
            f"бюджет исполнителя на задачу не вмещает повтор: {budget.text()}, "
            f"а потолок прогона «только сдай» — "
            f"{_num(config.EXECUTOR_SUBMIT_ONLY_CENTS_CEILING)} ¢ и "
            f"{config.EXECUTOR_SUBMIT_ONLY_TOKEN_CEILING} токенов"
        )
    return ""


async def _pushed_work(
    db: aiosqlite.Connection, task: dict[str, Any], project: Any
) -> tuple[str, str, str]:
    """``(ветка, tip, причина)``: есть ли что сдавать и не упрётся ли сдача в
    красный CI (#1405). Пустая причина — сдавать есть что."""
    from hub.services.project_policy import base_branch_of

    task_id = int(task["id"])
    branch = str(task.get("branch") or "").strip()
    tip = await _branch_tip(db, task_id, branch) if branch else ""
    # Вершина прошлой сдачи или базы — ничего нового прогон не запушил.
    unmoved = {str(task.get("submission_sha") or "")}
    if tip:
        unmoved.add(await _branch_tip(db, task_id, base_branch_of(project)))
    if not tip or tip in unmoved:
        where = f"ветка {branch}" if branch else "у задачи нет ветки"
        return (
            branch,
            tip,
            f"прогон не запушил работу ({where}, вершина {tip[:12] or 'нет'})",
        )
    if await _tip_ci_red(db, task_id, branch, tip):
        return branch, tip, f"CI tip {tip[:12]} красный — сдача упрётся в него"
    return branch, tip, ""


async def _silent_decision(
    db: aiosqlite.Connection, row: dict[str, Any], task: dict[str, Any]
) -> tuple[str, str, Any]:
    """Что делать с прогоном без сдачи: ``(решение, причина, заказ)``."""
    from hub.services import executor_launch as el
    from hub.services.project_policy import gate_policy_for_task

    task_id = int(task["id"])
    held = await el.silent_retry_refusal(db, task)
    if held:
        return _NOTE, f"{held}; повтора нет", None
    if await _earlier_silent_runs(db, row):
        return (
            _HUMAN,
            "повторный прогон «только сдай» тоже закончился без сдачи — "
            "третьего прогона нет, решение за человеком",
            None,
        )
    project = await repo.resolve_project_for_task(db, task_id)
    policy = await gate_policy_for_task(db, task_id)
    if el.launch_mode_of(policy) != el.LAUNCH_MANUAL:
        return _HUMAN, f"{el.REASON_OFF} — повтора «только сдай» нет", None
    short = await _budget_short(db, task_id)
    if short:
        return _HUMAN, short, None
    branch, tip, missing = await _pushed_work(db, task, project)
    if missing:
        return _HUMAN, missing, None
    order = el.SubmitOnly(
        run_id=str(row["run_id"]),
        generation=int(row["submission_generation"] or 0),
        branch=branch,
        tip=tip,
        ci="не красный",
    )
    return (
        _RETRY,
        f"хаб заказывает один прогон «только сдай» на {branch} @ {tip[:12]}",
        order,
    )


async def _note_silent_finish(
    db: aiosqlite.Connection, row: dict[str, Any], run: dict[str, Any], outcome: str
) -> None:
    """Прогон FINISHED без сдачи: alert с причиной в том же проходе и одно из
    трёх — повтор «только сдай», решение человеку или только запись (#1446).

    Зовётся один раз на прогон: строка закрывается этим же проходом, и
    следующий опрос её не видит.
    """
    if outcome != OUTCOME_FINISHED_WITHOUT_SUBMISSION:
        return
    task_id = int(row["task_id"])
    found = await repo.get_task(db, task_id)
    if found is None:
        return
    task = dict(found)
    decision, why, order = await _silent_decision(db, row, task)
    tail = _result_tail(run)
    if order is not None:
        order.result_tail = tail
    generation = row["submission_generation"]
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Прогон исполнителя {row['run_id']} (сдача {generation}) завершён без "
        f"сдачи: провайдер отдал FINISHED, а сдачи {generation} у задачи нет. "
        f"Решение хаба: {why} (#1446). Итоговый текст прогона: "
        + (f"«{tail}»" if tail else "итогового текста нет")
        + ".",
    )
    await repo.insert_event(
        db,
        kind=EVENT_FINISHED_WITHOUT_SUBMISSION,
        task_id=task_id,
        actor="hub",
        payload={"executor_run_id": row["id"], "decision": decision, "detail": why},
    )
    if decision == _HUMAN:
        await _silent_to_human(db, task_id, row, why)
    await db.commit()
    if decision == _RETRY:
        await _order_submit_only(db, task_id, row, order)


async def _silent_to_human(
    db: aiosqlite.Connection, task_id: int, row: dict[str, Any], why: str
) -> None:
    """Задача с тихим прогоном — на решение человеку, с причиной (#1446)."""
    task = await repo.get_task(db, task_id)
    if task is None or dict(task).get("status") != "running":
        return
    await repo.update_task(db, task_id, status="needs_decision")
    await repo.insert_event(
        db,
        kind="needs_decision",
        task_id=task_id,
        actor="hub",
        payload={
            "reason": EVENT_FINISHED_WITHOUT_SUBMISSION,
            "outcome": OUTCOME_FINISHED_WITHOUT_SUBMISSION,
            "executor_run_id": row["id"],
            "detail": why,
        },
    )


async def _order_submit_only(
    db: aiosqlite.Connection, task_id: int, row: dict[str, Any], order: Any
) -> None:
    """Заказать повтор; отказ до брони — задача на решение с причиной.

    Отказ ПОСЛЕ брони (провайдер не создал агента) закрывает бронь исходом
    failed, и задачу снимает ``release_stopped_tasks`` (#1455) тем же проходом.
    """
    from hub.services import executor_launch as el

    result = await el.submit_only_executor(db, task_id, order)
    if result.launched or result.row_id is not None:
        return
    why = f"повтор «только сдай» не заказан: {result.reason}"
    await repo.add_task_update(db, task_id, "hub", "alert", f"Хаб: {why} (#1446).")
    await _silent_to_human(db, task_id, row, why)
    await db.commit()


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
) -> bool:
    """Конец прогона без цены: ждать её, но не вечно (#1410).

    Cost у Cursor «eventually consistent» и сразу после конца может не
    прийти. Пока срок не вышел, строка остаётся running с причиной; по
    истечении закрывается исходом провайдера с названной причиной, а центы
    остаются неизвестными (прежнее прочитанное значение не обнуляется).
    True — строка закрыта этим вызовом.
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
        return True
    await repo.update_executor_run(
        db, row_id, tokens=tokens, reason=REASON_COST_PENDING
    )
    return False


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
    """Проход поллера (#1410): опрос прогонов исполнителя, затем задачи,
    чей прогон остановлен без сдачи, — на решение (#1455)."""
    await poll_executor_runs(db)
    await release_stopped_tasks(db)


# ---- #1455: остановка прогона человеком и задача, что не висит ----


async def release_stopped_tasks(db: aiosqlite.Connection) -> int:
    """Задачи в running, чей последний прогон остановлен без сдачи, — в
    needs_decision с причиной и исходом; сколько переведено.

    Читается исход СТРОКИ, а не провайдер: строка, закрытая до выката
    (28.09, #1375 — прогон cancelled внешне, задача осталась running за
    исполнителем), подхватывается первым же проходом. Повтора нет: задача
    уходит из running, и следующий проход её не видит.
    """
    rows = [
        dict(r)
        for r in await repo.stopped_executor_runs_of_running_tasks(db, STOPPED_OUTCOMES)
    ]
    for row in rows:
        await _release_stopped(db, row)
    if rows:
        await db.commit()
    return len(rows)


async def _release_stopped(db: aiosqlite.Connection, row: dict[str, Any]) -> None:
    task_id = int(row["task_id"])
    outcome = str(row["outcome"])
    detail = (
        f"прогон исполнителя {row['run_id'] or row['id']} (сдача "
        f"{row['submission_generation']}) закончился исходом {outcome} без сдачи"
    )
    await repo.update_task(db, task_id, status="needs_decision")
    await repo.insert_event(
        db,
        kind="needs_decision",
        task_id=task_id,
        actor="hub",
        payload={
            "reason": EVENT_RUN_STOPPED,
            "outcome": outcome,
            "executor_run_id": row["id"],
            "detail": detail,
        },
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Задача снята из running на решение: {detail} (#1455). Исполнитель "
        "её больше не делает; вернуть в работу, перезапустить или закрыть "
        "решает человек.",
    )


@dataclass
class StopResult:
    """Ответ на просьбу человека остановить прогон (#1455)."""

    accepted: bool
    reason: str = ""
    row_id: int | None = None
    outcome: str = ""
    cancel_intent: str = ""

    @property
    def confirmed(self) -> bool:
        return self.outcome not in ("", OUTCOME_RUNNING)


async def stop_executor_run(
    db: aiosqlite.Connection, task_id: int, *, actor: str
) -> StopResult:
    """Человек останавливает идущий прогон задачи (#1455).

    Своей отмены здесь нет: ставится ``cancel_intent`` и делается одна
    попытка той же отменой с повторами (``_cancel_step``), что у потолка и
    снятия по сдаче; дальше повторы после паузы держит поллер, а исход
    ``cancelled_by_human`` подтверждает только перечтённый прогон в
    CANCELLED. Отмена, которую хаб уже ведёт по своей причине, не
    переписывается: её исход (например, over_ceiling) остаётся честнее.
    """
    live = [
        dict(r)
        for r in await repo.list_executor_runs(db, task_id)
        if r["outcome"] == OUTCOME_RUNNING and r["agent_id"] and r["run_id"]
    ]
    if not live:
        return StopResult(False, REASON_NO_LIVE_RUN)
    row = live[-1]
    if not row.get("cancel_intent"):
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "status",
            f"{actor} остановил прогон исполнителя {row['run_id']} (сдача "
            f"{row['submission_generation']}): хаб отменяет его с повторами, "
            f"исход {OUTCOME_CANCELLED_BY_HUMAN} — после подтверждения (#1455).",
        )
        # Счёт — до отмены, как у опроса: подтверждённый CANCELLED без цены
        # остался бы ждать её до следующего прохода.
        tokens, cents = cursor_cloud.usage_totals(
            await _ask(cursor_cloud.get_usage, row["agent_id"], row["run_id"])
        )
        await _cancel_step(db, row, OUTCOME_CANCELLED_BY_HUMAN, tokens, cents)
    await db.commit()
    fresh = dict(await repo.get_executor_run(db, int(row["id"])) or row)
    return StopResult(
        True,
        str(fresh.get("reason") or ""),
        int(row["id"]),
        str(fresh["outcome"]),
        str(fresh.get("cancel_intent") or ""),
    )


# ---- #1443 (F5.1): суммарный бюджет исполнителя на задачу ----

TASK_CENTS_CEILING_KEY = "executor_task_cents_ceiling"
TASK_TOKEN_CEILING_KEY = "executor_task_token_ceiling"  # nosec B105 - a policy key name, not a credential


@dataclass
class TaskBudget:
    """Сколько задача уже потратила на исполнителя и сколько ей положено."""

    runs: int
    cents_spent: float
    tokens_spent: int
    cents_ceiling: float
    token_ceiling: int
    #: Прогоны с агентом, чья цена или токены не прочитаны (находка ревью
    #: #1443): «неизвестно» — не ноль (#516, #549, #1410).
    unpriced: int = 0

    @property
    def cents_left(self) -> float:
        return round(max(0.0, self.cents_ceiling - self.cents_spent), 2)

    @property
    def tokens_left(self) -> int:
        return max(0, self.token_ceiling - self.tokens_spent)

    @property
    def unknown(self) -> bool:
        """Сумма неизвестна: остаток не называется, заказ не делается."""
        return self.unpriced > 0

    @property
    def exhausted(self) -> bool:
        return (
            self.cents_spent >= self.cents_ceiling
            or self.tokens_spent >= self.token_ceiling
        )

    def text(self) -> str:
        known = (
            f"потрачено {_num(self.cents_spent)} ¢ из {_num(self.cents_ceiling)} ¢ "
            f"и {self.tokens_spent} токенов из {self.token_ceiling} "
            f"за {self.runs} прогон(ов)"
        )
        if self.unknown:
            return (
                f"{known}; цена {self.unpriced} прогон(ов) не прочитана — "
                "сумма не меньше названной, остаток неизвестен"
            )
        return known


def _positive(value: Any, cast: type) -> Any:
    """Потолок из политики, если он читаем и положителен; иначе ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return cast(value)


async def task_budget(
    db: aiosqlite.Connection, task_id: int, policy: dict | None = None
) -> TaskBudget:
    """Бюджет задачи: сумма ВСЕХ её прогонов против потолка (#1443).

    Одна функция на все заказы — первый запуск (#1412) и круг починки
    (F5.2/F5.3): второй копии правила нет. Прочитанное складывается; прогон
    с агентом, у которого цена или токены не прочитаны, — не ноль, а
    неизвестность (``unpriced``): остаток тогда не называется, и заказ не
    делается. Бронь без агента (заказ не состоялся) стоит ноль честно.
    """
    if policy is None:
        from hub.services.project_policy import gate_policy_for_task

        policy = await gate_policy_for_task(db, task_id)
    rows = [dict(r) for r in await repo.list_executor_runs(db, task_id)]
    cents_ceiling = _positive(policy.get(TASK_CENTS_CEILING_KEY), float)
    token_ceiling = _positive(policy.get(TASK_TOKEN_CEILING_KEY), int)
    return TaskBudget(
        runs=len(rows),
        cents_spent=round(sum(float(r["cents"] or 0) for r in rows), 2),
        tokens_spent=sum(int(r["tokens"] or 0) for r in rows),
        cents_ceiling=cents_ceiling or float(config.EXECUTOR_TASK_CENTS_CEILING),
        token_ceiling=token_ceiling or int(config.EXECUTOR_TASK_TOKEN_CEILING),
        unpriced=sum(
            1
            for r in rows
            if r["agent_id"] and (r["cents"] is None or r["tokens"] is None)
        ),
    )


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
            "stoppable": bool(
                r["outcome"] == OUTCOME_RUNNING and r["agent_id"] and r["run_id"]
            ),
        }
        for r in await repo.list_executor_runs(db, task_id)
    ]
    if not runs:
        return None
    billed = [r["cents"] for r in runs if r["cents"] is not None]
    budget = await task_budget(db, task_id)
    return {
        "runs": runs,
        "count": len(runs),
        # #1455: кнопка остановки — пока у задачи есть идущий прогон.
        "stoppable": any(r["stoppable"] for r in runs),
        "billed": len(billed),
        "cents_total": round(sum(billed), 2) if billed else None,
        # #1443: бюджет задачи рядом с прогонами — сколько ещё можно купить.
        "budget": {
            "cents_spent": budget.cents_spent,
            "cents_ceiling": budget.cents_ceiling,
            "cents_left": budget.cents_left,
            "tokens_spent": budget.tokens_spent,
            "token_ceiling": budget.token_ceiling,
            "tokens_left": budget.tokens_left,
            "exhausted": budget.exhausted,
            "unpriced": budget.unpriced,
            "unknown": budget.unknown,
        },
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
