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

Вопрос исполнителя (#1458): прогон, чья задача ушла в needs_info, хаб
отменяет той же отменой с повторами (исход ``awaiting_answer``), а ответ
человека с resume заказывает ровно одно продолжение (executor_launch), пока
строка прошлого прогона не закрыта — ответ ждёт её закрытия. Нет продолжения —
needs_decision с причиной «ответ записан, продолжить некому».

Первый запуск исполнителя модуль не делает (F2.4).

Молчание провайдера — названная причина, а не ноль и не завершение: строка
сохраняет прежние цифры и остаётся ``running``, пока цена не прочитана.
Закрыть прогон без прочитанного счёта значило бы записать непрочитанную цену
как окончательную.
"""

from __future__ import annotations

import json
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
#: #1458: прогон отменён хабом, потому что исполнитель задал вопрос. Не в
#: STOPPED_OUTCOMES: задача остаётся в needs_info и ждёт ответа, а не уходит
#: на решение человеку.
OUTCOME_AWAITING_ANSWER = "awaiting_answer"

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

#: #1458: вопрос остановил прогон / ответ ждёт продолжения / задача разобрана.
EVENT_AWAITING_ANSWER = "executor_run_awaiting_answer"
EVENT_CONTINUATION_WANTED = "executor_continuation_wanted"
EVENT_CONTINUATION_SETTLED = "executor_continuation_settled"
EVENT_ANSWER_NO_CONTINUATION = "executor_answer_no_continuation"
REASON_NO_CONTINUATION = "ответ записан, продолжить некому"


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
        if await _close_abandoned_reservation(db, row):
            return
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


async def _close_abandoned_reservation(
    db: aiosqlite.Connection, row: dict[str, Any]
) -> bool:
    """Бронь без агента, пережившая все попытки заказа, — failed (#1446).

    Раньше её закрывал только следующий запуск (``_live_run``); слепой исход
    заказа «только сдай» оставляет задачу за бронью, и без опроса она
    висела бы в running вечно. Закрытую бронь снимает ``release_stopped_tasks``.
    """
    from hub.services import executor_launch as el

    if row.get("agent_id") or not el._reservation_abandoned(row):
        return False
    await repo.update_executor_run(
        db,
        row["id"],
        outcome=OUTCOME_FAILED,
        reason=el.REASON_RESERVATION_ABANDONED,
        finish=True,
    )
    return True


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
        if outcome == OUTCOME_FINISHED_WITHOUT_SUBMISSION:
            await _note_silent_pending_cost(db, row, run)
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
#: Решение по тихому прогону: заказать «только сдай», ждать CI вершины,
#: отдать человеку или только назвать (задачу уже ведёт кто-то другой).
#: ``pending_cost`` — alert ушёл сразу, решение ждёт цену прогона.
_RETRY, _WAIT_CI, _HUMAN, _NOTE = "retry", "wait_ci", "human", "note"
_PENDING_COST = "pending_cost"
#: Решения, после которых за строкой остаётся долг: заказ ещё не сделан.
_DEBT_DECISIONS = (_RETRY, _WAIT_CI)

#: Состояние CI вершины ветки.
_CI_GREEN, _CI_RED, _CI_PENDING, _CI_UNKNOWN = "green", "red", "pending", "unknown"


def _result_tail(run: dict[str, Any] | None) -> str:
    """Хвост итогового текста прогона — поле ``result``, как у review_dispatch."""
    text = str((run or {}).get("result") or "").strip()
    return text[-RESULT_TAIL_CHARS:]


async def _branch_tip(
    db: aiosqlite.Connection, task_id: int, branch: str
) -> tuple[str, str]:
    """``(sha, причина)`` вершины, как её видит сам хаб; пустой sha —
    вершину прочитать не удалось, и причина говорит почему."""
    from hub.services import lifecycle

    return await lifecycle.resolve_branch_tip(db, task_id, branch)


def _ci_state_of(runs: list[dict[str, Any]] | None, tip: str) -> tuple[str, str]:
    """Состояние CI вершины по прогонам ветки: зелёный — только все
    прогоны вершины завершены успехом; «не прочитан» и «нет прогона» —
    не зелёный (#1446, находка ревью: незнание не разрешает платный заказ)."""
    from hub.services.red_base import _FAILED, _PASSED

    if runs is None:
        return _CI_UNKNOWN, "CI не прочитан"
    on_tip = [r for r in runs if r.get("sha") == tip]
    if not on_tip:
        return _CI_PENDING, "на вершине нет прогона CI"
    if any(r.get("conclusion") in _FAILED for r in on_tip):
        return _CI_RED, "красный"
    if all(
        r.get("status") == "completed" and r.get("conclusion") in _PASSED
        for r in on_tip
    ):
        return _CI_GREEN, "зелёный"
    if all(r.get("status") == "completed" for r in on_tip):
        return _CI_UNKNOWN, "исход CI не распознан"
    return _CI_PENDING, "CI вершины ещё идёт"


async def _tip_ci(
    db: aiosqlite.Connection, task_id: int, branch: str, tip: str
) -> tuple[str, str]:
    """Состояние CI вершины ``(состояние, текст)``."""
    from hub.integrations.registry import plugins
    from hub.services.project_policy import forge_of

    project = dict(await repo.resolve_project_for_task(db, task_id) or {})
    runs = await plugins.git_ops.branch_ci_runs(
        branch,
        repo=(project.get("workspace_path") or "").strip() or None,
        gh_repo=(project.get("repo") or "").strip() or None,
        forge=forge_of(project) if project else "",
    )
    return _ci_state_of(runs, tip)


async def _earlier_silent_runs(db: aiosqlite.Connection, row: dict[str, Any]) -> int:
    """Сколько прогонов этого поколения уже закончились без сдачи до этого.

    Счёт — по строкам, а не в памяти: повтор один на поколение и после
    перезапуска хаба.
    """
    return sum(
        1
        for r in await repo.list_executor_runs(db, int(row["task_id"]))
        if int(r["id"]) < int(row["id"])
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
    """``(ветка, tip, причина)``: есть ли что сдавать. Пустая причина — есть.

    «Не запушил» и «не удалось посмотреть» — разные причины (находка ревью
    #1446): непрочитанная вершина не доказывает, что работы нет.
    """
    from hub.services.project_policy import base_branch_of

    task_id = int(task["id"])
    branch = str(task.get("branch") or "").strip()
    if not branch:
        return "", "", "прогон не запушил работу (у задачи нет ветки)"
    tip, unread = await _branch_tip(db, task_id, branch)
    if not tip:
        return branch, "", f"не удалось проверить ветку {branch}: {unread}"
    # Вершина прошлой сдачи или базы — ничего нового прогон не запушил.
    base_tip, _ = await _branch_tip(db, task_id, base_branch_of(project))
    if tip in {str(task.get("submission_sha") or ""), base_tip}:
        return (
            branch,
            tip,
            f"прогон не запушил работу (ветка {branch}, вершина {tip[:12]} не "
            "сдвинулась)",
        )
    return branch, tip, ""


def _ci_wait_over(row: dict[str, Any]) -> bool:
    """Срок ожидания CI вершины от конца прогона истёк.

    Строка без ``finished_at`` закрывается этим же проходом: прогон только
    что кончился, срок не истёк.
    """
    from datetime import UTC, datetime, timedelta

    if not row.get("finished_at"):
        return False
    try:
        ended = datetime.fromisoformat(str(row.get("finished_at") or "")).replace(
            tzinfo=UTC
        )
    except ValueError:
        return True
    limit = timedelta(minutes=config.EXECUTOR_SUBMIT_ONLY_CI_WAIT_MIN)
    return datetime.now(UTC) - ended > limit


async def _ci_verdict(
    db: aiosqlite.Connection, row: dict[str, Any], task_id: int, branch: str, tip: str
) -> tuple[str, str]:
    """``(решение, причина)`` по CI вершины; пустое решение — CI зелёный."""
    state, text = await _tip_ci(db, task_id, branch, tip)
    if state == _CI_GREEN:
        return "", ""
    if state == _CI_RED:
        return _HUMAN, f"CI tip {tip[:12]} красный — сдача упрётся в него"
    minutes = config.EXECUTOR_SUBMIT_ONLY_CI_WAIT_MIN
    if _ci_wait_over(row):
        return (
            _HUMAN,
            f"CI tip {tip[:12]} не стал зелёным за {minutes} мин ({text}) — "
            "повтор «только сдай» не заказан",
        )
    return (
        _WAIT_CI,
        f"CI tip {tip[:12]} ещё не зелёный ({text}) — хаб ждёт до {minutes} мин "
        "и закажет «только сдай» на зелёном",
    )


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
    decision, why = await _ci_verdict(db, row, task_id, branch, tip)
    if decision:
        return decision, why, None
    order = el.SubmitOnly(
        run_id=str(row["run_id"]),
        generation=int(row["submission_generation"] or 0),
        branch=branch,
        tip=tip,
        ci="зелёный",
    )
    return (
        _RETRY,
        f"хаб заказывает один прогон «только сдай» на {branch} @ {tip[:12]}",
        order,
    )


async def _silent_decisions(db: aiosqlite.Connection, row: dict[str, Any]) -> list[str]:
    """Записанные решения по прогону, по порядку (события #1446)."""
    row_id = int(row["id"])
    rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind=? AND task_id=? ORDER BY id",
        (EVENT_FINISHED_WITHOUT_SUBMISSION, int(row["task_id"])),
    )
    out = []
    for r in rows:
        try:
            payload = json.loads(dict(r)["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if int(payload.get("executor_run_id") or 0) == int(row_id):
            out.append(str(payload.get("decision") or ""))
    return out


async def _record_decision(
    db: aiosqlite.Connection, row: dict[str, Any], decision: str, why: str
) -> None:
    await repo.insert_event(
        db,
        kind=EVENT_FINISHED_WITHOUT_SUBMISSION,
        task_id=int(row["task_id"]),
        actor="hub",
        payload={"executor_run_id": row["id"], "decision": decision, "detail": why},
    )


async def _alert_silent(
    db: aiosqlite.Connection, row: dict[str, Any], run: dict[str, Any], why: str
) -> None:
    """Один alert на прогон: FINISHED без сдачи, причина и хвост итога."""
    generation = row["submission_generation"]
    tail = _result_tail(run)
    await repo.add_task_update(
        db,
        int(row["task_id"]),
        "hub",
        "alert",
        f"Прогон исполнителя {row['run_id']} (сдача {generation}) завершён без "
        f"сдачи: провайдер отдал FINISHED, а сдачи {generation} у задачи нет. "
        f"Решение хаба: {why} (#1446). Итоговый текст прогона: "
        + (f"«{tail}»" if tail else "итогового текста нет")
        + ".",
    )


async def _note_silent_pending_cost(
    db: aiosqlite.Connection, row: dict[str, Any], run: dict[str, Any]
) -> None:
    """Цена ещё не пришла: alert — сразу, решение о повторе — после цены.

    Без цены бюджет задачи неизвестен (#1443), и заказывать нельзя; но
    человек узнаёт о тихом конце в первом же проходе (находка ревью #1446).
    """
    if await _silent_decisions(db, row):
        return
    why = "цена прогона ещё не пришла — о повторе «только сдай» хаб решит после неё"
    await _alert_silent(db, row, run, why)
    await _record_decision(db, row, _PENDING_COST, why)


async def _note_silent_finish(
    db: aiosqlite.Connection, row: dict[str, Any], run: dict[str, Any], outcome: str
) -> None:
    """Прогон FINISHED без сдачи: alert с причиной в том же проходе и одно из
    решений — повтор «только сдай», ожидание CI, человек или только запись.

    Зовётся один раз на закрытие строки. Решение пишется событием ДО заказа
    и коммитится: заказ, оборванный после коммита, остаётся долгом, который
    доводит ``settle_silent_debts`` (находка ревью #1446).
    """
    if outcome != OUTCOME_FINISHED_WITHOUT_SUBMISSION:
        return
    found = await repo.get_task(db, int(row["task_id"]))
    if found is None:
        return
    decision, why, order = await _silent_decision(db, row, dict(found))
    if await _silent_decisions(db, row):
        # Alert уже ушёл, пока прогон ждал цену: решение — записью в ленте.
        await repo.add_task_update(
            db,
            int(row["task_id"]),
            "hub",
            "status",
            f"Решение хаба по прогону {row['run_id']} без сдачи: {why} (#1446).",
        )
    else:
        await _alert_silent(db, row, run, why)
    await _apply_decision(db, row, decision, why, order, _result_tail(run))


async def _apply_decision(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    decision: str,
    why: str,
    order: Any,
    tail: str,
) -> None:
    await _record_decision(db, row, decision, why)
    task_id = int(row["task_id"])
    if decision == _HUMAN:
        await _silent_to_human(db, task_id, row, why)
    await db.commit()
    if decision == _RETRY:
        order.result_tail = tail
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
    failed, и задачу снимает ``release_stopped_tasks`` (#1455) тем же
    проходом. Слепой исход создания (находка ревью #1446): бронь держится —
    агент мог быть создан и оплачен, — задача остаётся за ней; брошенную
    бронь закрывает опрос (``_poll_one``), и тогда задачу снимает #1455.
    """
    from hub.services import executor_launch as el

    result = await el.submit_only_executor(db, task_id, order)
    if result.launched:
        return
    if result.row_id is not None:
        if result.reason.startswith(el.REASON_ANSWER_BLIND):
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "alert",
                "Исход создания прогона «только сдай» неизвестен: "
                f"{result.reason}. Задача остаётся за бронью; если агент так и "
                "не появится, бронь закроется как брошенная и задача уйдёт на "
                "решение человеку (#1446).",
            )
            await db.commit()
        return
    why = f"повтор «только сдай» не заказан: {result.reason}"
    await repo.add_task_update(db, task_id, "hub", "alert", f"Хаб: {why} (#1446).")
    await _silent_to_human(db, task_id, row, why)
    await db.commit()


async def _silent_debt_rows(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Последний прогон задачи в running — закрыт без сдачи своего поколения."""
    rows = await fetchall(
        db,
        "SELECT r.* FROM executor_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE t.status = 'running' AND t.archived = 0 AND r.outcome = ? "
        "AND r.id = (SELECT MAX(x.id) FROM executor_runs x WHERE x.task_id = t.id) "
        "AND COALESCE(t.submission_generation, 0) < r.submission_generation "
        "ORDER BY r.id",
        (OUTCOME_FINISHED_WITHOUT_SUBMISSION,),
    )
    return [dict(r) for r in rows]


async def settle_silent_debts(db: aiosqlite.Connection) -> int:
    """Довести записанное решение «повтор» или «ждать CI» до заказа (#1446).

    Решение пишется и коммитится до заказа; обрыв после коммита (выкат,
    падение) не должен потерять повтор, а закрытую строку опрос больше не
    видит. Долг — прогон, чьё последнее решение ``retry`` или ``wait_ci``,
    остаётся последним прогоном задачи в running: заказ не состоялся. Как
    только заказ сделан, у задачи появляется строка новее — долга нет, и
    второго заказа на поколение этот проход не делает. Возвращает число
    заказов.
    """
    ordered = 0
    for row in await _silent_debt_rows(db):
        decisions = await _silent_decisions(db, row)
        if not decisions or decisions[-1] not in _DEBT_DECISIONS:
            continue
        found = await repo.get_task(db, int(row["task_id"]))
        if found is None:
            continue
        decision, why, order = await _silent_decision(db, row, dict(found))
        if decision == decisions[-1] == _WAIT_CI:
            continue
        if decision != _RETRY or decisions[-1] != _RETRY:
            await repo.add_task_update(
                db,
                int(row["task_id"]),
                "hub",
                "status",
                f"Решение хаба по прогону {row['run_id']} без сдачи: {why} (#1446).",
            )
        run = await _ask(cursor_cloud.get_run, row["agent_id"], row["run_id"])
        await _apply_decision(db, row, decision, why, order, _result_tail(run))
        ordered += decision == _RETRY
    return ordered


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
    await settle_silent_debts(db)
    await settle_answer_debts(db)
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


async def _live_row(db: aiosqlite.Connection, task_id: int) -> dict[str, Any] | None:
    """Последний идущий прогон задачи с агентом и прогоном у провайдера."""
    live = [
        dict(r)
        for r in await repo.list_executor_runs(db, task_id)
        if r["outcome"] == OUTCOME_RUNNING and r["agent_id"] and r["run_id"]
    ]
    return live[-1] if live else None


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
    row = await _live_row(db, task_id)
    if row is None:
        return StopResult(False, REASON_NO_LIVE_RUN)
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


# ---- #1458: вопрос исполнителя останавливает прогон, ответ его продолжает ----

#: Исходы, которыми прогон закончился сам после вопроса: провайдер довёл
#: (FINISHED, с сдачей или без), упал или отменён снаружи. Остановка по
#: потолку и по просьбе человека сюда не входят — это решения человека.
_ENDED_AFTER_QUESTION = (
    OUTCOME_FINISHED,
    OUTCOME_FINISHED_WITHOUT_SUBMISSION,
    OUTCOME_FAILED,
    OUTCOME_CANCELLED,
)
#: Сколько коммитов ветки читается, чтобы назвать сделанное до ответа.
_COMMITS_LOOKBACK = 50


async def _executor_holds(db: aiosqlite.Connection, task: dict[str, Any]) -> bool:
    """Задачу держит исполнитель хаба (или никто), а не чужая сессия."""
    from hub.services import chat_pair

    holder = str(task.get("claimed_by") or "").strip()
    if not holder:
        return True
    acting = await chat_pair.get_acting_agent(db)
    return acting is not None and holder == str(acting["username"])


async def _asked_payload(
    db: aiosqlite.Connection, task_id: int, question_at: str
) -> dict[str, Any]:
    """Payload события «вопрос остановил прогон» ЭТОГО вопроса; пусто — хаб
    прогон на нём не останавливал (событие прошлого вопроса не годится)."""
    rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind=? AND task_id=? AND created_at>=? "
        "ORDER BY id DESC LIMIT 1",
        (EVENT_AWAITING_ANSWER, task_id, question_at),
    )
    if not rows:
        return {}
    try:
        payload = json.loads(dict(rows[0])["payload"] or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


async def pause_run_on_question(db: aiosqlite.Connection, task_id: int) -> None:
    """Вопрос исполнителя при идущем прогоне — отмена с intent awaiting_answer.

    Прогон без вопроса продолжал бы писать код против догадки и жечь деньги
    (SID-20, #1450: пуш b7032f3 через 4 минуты после вопроса). Отмена — та же
    машина с повторами и подтверждением перечтением (#1411), что у потолка и
    остановки человеком. Вопрос локальной сессии (нет строки прогона, или
    задачу держит не исполнитель хаба) не трогается. Лучшее, что можно: вопрос
    уже записан, и сбой отмены его не отменяет — только называется.
    """
    row = await _live_row(db, task_id)
    found = await repo.get_task(db, task_id)
    if row is None or found is None or row.get("cancel_intent"):
        return
    task = dict(found)
    if not await _executor_holds(db, task):
        return
    try:
        await _pause_row(db, row, task)
    except Exception:  # noqa: BLE001 — вопрос записан, сбой отмены не роняет запрос
        log.exception("executor pause on question failed for #%s", task_id)


async def _pause_row(
    db: aiosqlite.Connection, row: dict[str, Any], task: dict[str, Any]
) -> None:
    task_id = int(task["id"])
    branch = str(task.get("branch") or "").strip()
    tip = (await _branch_tip(db, task_id, branch))[0] if branch else ""
    await repo.insert_event(
        db,
        kind=EVENT_AWAITING_ANSWER,
        task_id=task_id,
        actor="hub",
        payload={
            "executor_run_id": row["id"],
            "branch": branch,
            "tip": tip,
            "claim_session_id": str(task.get("claim_session_id") or ""),
        },
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        f"Исполнитель задал вопрос: хаб отменяет прогон {row['run_id']} с "
        f"повторами, исход {OUTCOME_AWAITING_ANSWER}; после ответа хаб закажет "
        f"продолжение (#1458). Вершина ветки на вопросе: {tip[:12] or 'не прочитана'}.",
    )
    tokens, cents = cursor_cloud.usage_totals(
        await _ask(cursor_cloud.get_usage, row["agent_id"], row["run_id"])
    )
    await _cancel_step(db, row, OUTCOME_AWAITING_ANSWER, tokens, cents)
    await db.commit()


def _closed_by_question(last: dict[str, Any], question: dict[str, Any]) -> bool:
    """Последний прогон закрыт вопросом или закончился после него."""
    if last.get("cancel_intent") == OUTCOME_AWAITING_ANSWER:
        return True
    if last["outcome"] not in _ENDED_AFTER_QUESTION:
        return False
    return str(last.get("finished_at") or "") >= str(question["created_at"])


async def _last_update(
    db: aiosqlite.Connection, task_id: int, kind: str
) -> dict[str, Any] | None:
    rows = await fetchall(
        db,
        "SELECT * FROM task_updates WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    )
    return dict(rows[0]) if rows else None


async def answer_for_executor(
    db: aiosqlite.Connection, task: dict[str, Any], *, resume: bool
) -> bool:
    """Ответ на вопрос задачи с прогонами исполнителя; True — ответ ведёт хаб.

    Зовётся после записи ответа. Задача без строки прогона и задача, чей
    последний прогон не связан с вопросом, — False: прежний путь ответа.
    """
    task_id = int(task["id"])
    runs = [dict(r) for r in await repo.list_executor_runs(db, task_id)]
    question = await _last_update(db, task_id, "question")
    answer = await _last_update(db, task_id, "answer")
    if not runs or question is None or answer is None:
        return False
    if not _closed_by_question(runs[-1], question):
        return False
    await _name_commits_after_question(db, task, question)
    if not resume:
        await _answer_to_decision(db, task_id, "ответ без resume")
        return True
    await repo.insert_event(
        db,
        kind=EVENT_CONTINUATION_WANTED,
        task_id=task_id,
        actor="hub",
        payload={
            "question_update_id": question["id"],
            "answer_update_id": answer["id"],
            "claim_session_id": str(task.get("claim_session_id") or ""),
        },
    )
    await db.commit()
    await settle_answer_debts(db, task_id)
    return True


async def _branch_commits(
    db: aiosqlite.Connection, task: dict[str, Any], branch: str, stop_tip: str
) -> list[tuple[str, str]] | None:
    """Коммиты первой линии ветки новее ``stop_tip``: ``[(sha, тема)]``.

    ``None`` — прочитать нельзя (нет рабочей копии проекта, git молчит или
    ``stop_tip`` не найден в прочитанном окне): незнание — не «коммитов нет».
    """
    from hub.integrations.registry import plugins

    project = dict(await repo.resolve_project_for_task(db, int(task["id"])) or {})
    workspace = (project.get("workspace_path") or "").strip()
    if not workspace or not stop_tip:
        return None
    log_text = await plugins.git_ops.first_parent_log(
        workspace, branch, _COMMITS_LOOKBACK
    )
    found: list[tuple[str, str]] = []
    for line in (log_text or "").splitlines():
        sha, _, subject = line.partition("\x1f")
        if sha == stop_tip:
            return found
        found.append((sha, subject.split("\x1f")[0]))
    return None


async def _name_commits_after_question(
    db: aiosqlite.Connection, task: dict[str, Any], question: dict[str, Any]
) -> None:
    """Alert с sha коммитов, запушенных между вопросом и ответом (AC-4).

    Работа, сделанная до ответа, может с ответом разойтись; человек видит её
    в ленте, а заказ продолжения требует её сверить. Сбой чтения — не сбой
    ответа: alert тогда называет то, что прочитано.
    """
    branch = str(task.get("branch") or "").strip()
    if not branch:
        return
    asked = await _asked_payload(db, int(task["id"]), str(question["created_at"]))
    asked_tip = str(asked.get("tip") or "")
    tip = (await _branch_tip(db, int(task["id"]), branch))[0]
    if not tip or tip == asked_tip:
        return
    try:
        commits = await _branch_commits(db, task, branch, asked_tip)
    except Exception:  # noqa: BLE001 — чтение git не должно ронять ответ
        log.exception("commits after question unreadable for #%s", task["id"])
        commits = None
    if commits is None:
        named = (
            f"вершина ветки на ответе {tip}; список коммитов после вопроса не прочитан"
        )
    else:
        named = "; ".join(f"{sha} «{subject}»" for sha, subject in commits)
    await repo.add_task_update(
        db,
        int(task["id"]),
        "hub",
        "alert",
        f"На ветке {branch} после вопроса исполнителя запушена работа — сделано "
        f"до ответа: {named}. Она могла разойтись с ответом; продолжение "
        f"обязано её сверить (#1458).",
    )


async def _answer_to_decision(db: aiosqlite.Connection, task_id: int, why: str) -> None:
    """Продолжить некому: needs_decision с причиной, не running (AC-3)."""
    task = await repo.get_task(db, task_id)
    if task is None or dict(task).get("status") != "needs_info":
        return
    detail = f"{REASON_NO_CONTINUATION}: {why}"
    await repo.update_task(db, task_id, status="needs_decision")
    await repo.insert_event(
        db,
        kind="needs_decision",
        task_id=task_id,
        actor="hub",
        payload={"reason": EVENT_ANSWER_NO_CONTINUATION, "detail": detail},
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Хаб: {detail}. Задачу можно запустить кнопкой запуска исполнителя "
        "из needs_decision или вернуть в работу решением человека (#1458).",
    )
    await settle_continuation(db, task_id, "no_continuation")
    await db.commit()


async def settle_continuation(db: aiosqlite.Connection, task_id: int, how: str) -> None:
    """Закрыть долг продолжения: событие с исходом; коммит — за вызывающим."""
    await repo.insert_event(
        db,
        kind=EVENT_CONTINUATION_SETTLED,
        task_id=task_id,
        actor="hub",
        payload={"how": how},
    )


async def _pending_wanted(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    """Последний нерешённый заказ продолжения; ``None`` — долга нет.

    Долг закрывает событие «решено» позже него, а новый вопрос позже ответа
    делает его устаревшим: продолжать нужно последний ответ, не прошлый.
    """
    rows = await fetchall(
        db,
        "SELECT id, payload FROM events WHERE kind=? AND task_id=? "
        "ORDER BY id DESC LIMIT 1",
        (EVENT_CONTINUATION_WANTED, task_id),
    )
    if not rows:
        return None
    wanted = dict(rows[0])
    closed = await fetchall(
        db,
        "SELECT 1 FROM events WHERE kind=? AND task_id=? AND id>?",
        (EVENT_CONTINUATION_SETTLED, task_id, wanted["id"]),
    )
    payload = json.loads(wanted["payload"] or "{}")
    question = await _last_update(db, task_id, "question")
    if (
        closed
        or question is None
        or int(question["id"]) > int(payload.get("answer_update_id") or 0)
    ):
        return None
    return {**payload, "id": wanted["id"], "task_id": task_id}


async def settle_answer_debts(
    db: aiosqlite.Connection, task_id: int | None = None
) -> int:
    """Довести записанный ответ до заказа продолжения; сколько задач разобрано.

    Долг — событие «нужно продолжение» у задачи в needs_info без решения
    после него. Ответ ждёт закрытия строки прошлого прогона (отмена ещё в
    повторах после 429): два живых прогона на задачу недопустимы. Заказ один
    на ответ и при многих тиках, и после перезапуска хаба: его закрывает
    событие «решено», а долг видит только needs_info.
    """
    where, args = ("AND e.task_id=?", (task_id,)) if task_id else ("", ())
    rows = await fetchall(
        db,
        "SELECT DISTINCT e.task_id FROM events e JOIN tasks t ON t.id=e.task_id "
        f"WHERE e.kind=? AND t.status='needs_info' AND t.archived=0 {where}",
        (EVENT_CONTINUATION_WANTED, *args),
    )
    handled = 0
    for r in rows:
        wanted = await _pending_wanted(db, int(dict(r)["task_id"]))
        if wanted is not None:
            handled += await _continue_after_answer(db, wanted)
    return handled


async def _continue_after_answer(
    db: aiosqlite.Connection, wanted: dict[str, Any]
) -> int:
    """Один долг: 1 — разобран (заказ, решение или alert), 0 — ждёт."""
    task_id = int(wanted["task_id"])
    task = dict(await repo.get_task(db, task_id) or {})
    runs = [dict(r) for r in await repo.list_executor_runs(db, task_id)]
    if not runs:
        await settle_continuation(db, task_id, "no_runs")
        await db.commit()
        return 1
    if runs[-1]["outcome"] == OUTCOME_RUNNING:
        return 0
    taken = await _taken_by_another(db, task, wanted)
    if taken:
        await repo.add_task_update(
            db, task_id, "hub", "alert", f"Продолжение не заказано: {taken} (#1458)."
        )
        await settle_continuation(db, task_id, "taken")
        await db.commit()
        return 1
    order = await _continuation_order(db, task, runs[-1], wanted)
    from hub.services import executor_launch as el

    result = await el.continue_executor(db, task_id, order)
    return await _after_continuation(db, task_id, result)


async def _taken_by_another(
    db: aiosqlite.Connection, task: dict[str, Any], wanted: dict[str, Any]
) -> str:
    """Причина, если задачу с ответа взяла другая сессия; пусто — нет."""
    current = str(task.get("claim_session_id") or "")
    if current != str(wanted.get("claim_session_id") or ""):
        return (
            f"задачу взяла другая сессия ({current or 'без сессии'}), она доводит сама"
        )
    if not await _executor_holds(db, task):
        return f"задачу держит {task.get('claimed_by')}, а не исполнитель хаба"
    return ""


async def _continuation_order(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    last: dict[str, Any],
    wanted: dict[str, Any],
) -> Any:
    from hub.services import executor_launch as el

    question = await repo.get_task_update_by_id(db, int(wanted["question_update_id"]))
    answer = await repo.get_task_update_by_id(db, int(wanted["answer_update_id"]))
    branch = str(task.get("branch") or "").strip()
    tip = (await _branch_tip(db, int(task["id"]), branch))[0] if branch else ""
    asked = await _asked_payload(
        db, int(task["id"]), str(dict(question or {}).get("created_at") or "")
    )
    return el.ContinueOrder(
        run_id=str(last["run_id"]),
        question=str(dict(question or {}).get("content") or ""),
        answer=str(dict(answer or {}).get("content") or ""),
        question_at=str(dict(question or {}).get("created_at") or ""),
        branch=branch,
        pushed=bool(tip),
        asked_tip=str(asked.get("tip") or ""),
    )


async def _after_continuation(
    db: aiosqlite.Connection, task_id: int, result: Any
) -> int:
    """Что делать с исходом заказа продолжения; 1 — долг разобран."""
    from hub.services import executor_launch as el

    if result.launched:
        return 1
    if result.row_id is not None:
        # Бронь названа (отказ провайдера или слепая потеря ответа): задача
        # уже за ней; закрытую failed-бронь снимет release_stopped_tasks.
        if result.reason.startswith(el.REASON_ANSWER_BLIND):
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "alert",
                f"Исход создания прогона-продолжения неизвестен: {result.reason}. "
                "Задача остаётся за бронью (#1458).",
            )
            await db.commit()
        return 1
    if result.reason.startswith(el.REASON_ALREADY_RUNNING):
        return 0  # гонка двух тиков: соседний уже заказал, долг закроет он
    await _answer_to_decision(db, task_id, result.reason)
    return 1


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
