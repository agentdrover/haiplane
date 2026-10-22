"""Автоповтор доставки принятой (completed) задачи после base_updated (#1999).

Прод 22.10 (#1989): ``deliver_on_disposition`` (decide accept/deliver, доставка
из реестра) получил ``base_updated`` - гейт влил базу в ветку PR, мерж "следующим
циклом". Задача completed, и гейт её больше не трогает (вне конвейера); ручная
доставка снова проигрывает гонку с develop.

Как работает:

1. ``note_refusal`` (из ``deliver_on_disposition``, единой точки трёх входов:
   decide accept, force-complete, доставка из реестра) вооружает задачу строкой
   ``accepted_delivery_retries``, когда отказ - ``base_updated``.
2. ``sweep`` (из ``scan_completed_deliveries``, раз в ``DELIVERY_SCAN_MINUTES``)
   по каждой вооружённой строке: стоп (поколение, PR, одобрение, лимиты) ->
   проба CI головы PR (идёт - ждём без расхода) -> бронь попытки одним UPDATE и
   коммит ДО сети -> ТОТ ЖЕ ``deliver_on_disposition`` (условия гейта, #612, SHA
   сам не перезакрепляется) -> исход.
3. Лимит: 6 попыток и 24 ч от первого вооружения; не сбрасываются повторным
   base_updated, рестартом и вторым соединением (хранятся в строке, проверяются
   в самом UPDATE брони). Любой иной отказ гейта - стоп с алертом и событием.

``delivery_fresh_base_rerun`` (#1956) не дублируется: красный CI при включённой
политике и непотраченном перезапуске проходит в тот же ``deliver_on_disposition``,
где его обслуживает ``delivery_rerun.gate_step`` - второго rerun нет.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from hub import repository as repo
from hub import repository_accepted_delivery_retry as journal
from hub import repository_delivery_rerun as rerun_journal
from hub.db import write_transaction
from hub.integrations.protocols import CIProbeOutcome
from hub.integrations.registry import plugins

log = logging.getLogger("hub")

RETRY_VIA = "accepted_delivery_retry"
STOP_EVENT = "accepted_delivery_retry_stopped"
DELIVER = "deliver"
MAX_ATTEMPTS = journal.MAX_ATTEMPTS
WINDOW_HOURS = journal.WINDOW_HOURS

_CONDITIONS = (
    "Остановится с алертом при красном CI, нетекущем одобрении, авторском push, "
    "смене поколения или исчерпании лимита"
)
_LIMITS = f"не больше {MAX_ATTEMPTS} попыток и {WINDOW_HOURS} ч от вооружения"


def _armed_text(pr_number: int) -> str:
    return (
        f"Автоповтор доставки (#1999): гейт слил базу в ветку PR #{pr_number}, "
        "мерж — после зелёного CI новой головы. Хаб сам повторит доставку теми "
        "же условиями гейта (одобренное ревью, неизменный с апрува код #612, "
        f"зелёный CI): {_LIMITS}. {_CONDITIONS}. Ручной повтор не нужен."
    )


async def note_refusal(
    db: aiosqlite.Connection, task: dict[str, Any], reason: str, via: str
) -> None:
    """Отказ доставки принятой задачи: ``base_updated`` вооружает автоповтор.

    Зовётся из ``deliver_on_disposition`` после ``merge_before_completion`` и
    ничего не коммитит: транзакцию закрывает вызывающий. Сам повтор
    (``via=RETRY_VIA``) не вооружает - лимит принадлежит первому вооружению.
    """
    from hub.services.orchestration import BASE_UPDATED_PREFIX

    if via == RETRY_VIA or not (reason or "").startswith(BASE_UPDATED_PREFIX):
        return
    project = await repo.resolve_project_for_task(db, int(task["id"]))
    created = await journal.arm(
        db,
        int(task["id"]),
        int(task.get("submission_generation") or 0),
        pr_number=int(task.get("pr_number") or 0),
        project_id=dict(project)["id"] if project else None,
        via=via,
        text=reason,
    )
    if created:
        await repo.add_task_update(
            db,
            int(task["id"]),
            "hub",
            "status",
            _armed_text(int(task.get("pr_number") or 0)),
        )


async def _armed_row(
    db: aiosqlite.Connection, task_id: int, reason: str
) -> dict[str, Any] | None:
    """Строка автоповтора для ручного отказа ``base_updated``; иначе None."""
    from hub.services.orchestration import BASE_UPDATED_PREFIX

    if not (reason or "").startswith(BASE_UPDATED_PREFIX):
        return None
    return await journal.latest(db, task_id)


async def refusal_hint(db: aiosqlite.Connection, task_id: int, reason: str) -> str:
    """``actor_hint`` ручного отказа: "none", пока автоповтор вооружён, иначе "human"."""
    row = await _armed_row(db, task_id, reason)
    return "none" if row and row["state"] != journal.CLOSED else "human"


async def refusal_note(db: aiosqlite.Connection, task_id: int, reason: str) -> str:
    """Фраза к ручному отказу ``base_updated``: что хаб повторит сам и когда встанет."""
    row = await _armed_row(db, task_id, reason)
    if row is None:
        return ""
    if row["state"] == journal.CLOSED:
        return f" Автоповтор остановлен ({row['outcome']}): дальше решает человек."
    left = max(MAX_ATTEMPTS - int(row["attempts"]), 0)
    return (
        " Автоповтор вооружён: хаб сам повторит доставку, когда CI новой головы "
        f"позеленеет (осталось попыток: {left} из {MAX_ATTEMPTS}, не дольше "
        f"{WINDOW_HOURS} ч от вооружения). {_CONDITIONS}; руками повторять не "
        "нужно."
    )


# ---- свип -------------------------------------------------------------------


async def sweep(db: aiosqlite.Connection, *, limit: int = 50) -> int:
    """Один проход по вооружённым строкам; число обработанных строк."""
    handled = 0
    try:
        rows = await journal.open_rows(db, limit)
    except Exception:  # noqa: BLE001 - не прочитали строки: остальной скан идёт дальше
        await db.rollback()
        log.exception("accepted delivery retry rows unreadable")
        return handled
    for row in rows:
        try:
            await _step(db, row)
            handled += 1
        except Exception:  # noqa: BLE001 - одна строка не останавливает свип
            await db.rollback()
            log.exception("accepted delivery retry failed for #%s", row["task_id"])
    return handled


def _stop_reason(
    task: dict[str, Any] | None, row: dict[str, Any]
) -> tuple[str, str] | None:
    """Причина стопа по данным без сети: (исход, слова) или None - идём дальше."""
    from hub.services.orchestration import review_approved_for_current_submission

    if task is None:
        return "task_missing", "задачи больше нет"
    if task["status"] != "completed":
        return "task_not_completed", f"задача в статусе {task['status']}"
    if int(task.get("submission_generation") or 0) != int(row["submission_generation"]):
        return "generation_changed", "поколение сдачи сменилось после вооружения"
    if int(task.get("pr_number") or 0) != int(row["pr_number"]):
        return "pr_changed", "номер PR сменился после вооружения"
    if not review_approved_for_current_submission(task):
        return "approval_not_current", "у текущей сдачи нет действующего одобрения"
    if int(row["attempts"]) >= MAX_ATTEMPTS:
        return "attempts_exhausted", f"потрачены все {MAX_ATTEMPTS} попыток"
    if float(row["age_hours"]) >= WINDOW_HOURS:
        return "window_expired", f"прошло {WINDOW_HOURS} ч от вооружения"
    return None


async def _step(db: aiosqlite.Connection, row: dict[str, Any]) -> None:
    task_row = await repo.get_task(db, int(row["task_id"]))
    task = dict(task_row) if task_row else None
    stop = _stop_reason(task, row)
    if task is None or stop is not None:
        await _stop(db, row, *(stop or ("task_missing", "задачи больше нет")))
        return
    if await repo.pipeline_merge_recorded(db, task["id"], int(row["pr_number"])):
        await _delivered(db, row, "доставлено другим путём гейта")
        return
    go = await _ci_allows_attempt(db, task)
    if go == "stop":
        await _stop(db, row, "ci_failed", "CI головы PR красный")
    elif go == "go":
        await _attempt(db, row, task)


async def _ci_allows_attempt(db: aiosqlite.Connection, task: dict[str, Any]) -> str:
    """ "go" - зелёный CI (или красный, который ведёт #1956); "wait"; "stop".

    Непрочитанный CI - "wait", а не "go" и не "stop": неизвестное не равно ни
    зелёному, ни красному.
    """
    from hub.services import orchestration

    pr = int(task["pr_number"])
    try:
        ctx = await orchestration.project_git_context(db, int(task["id"]))
        ci = await plugins.git_ops.check_pr_ci(
            pr,
            repo=ctx.get("repo"),
            gh_repo=ctx.get("gh_repo"),
            forge=ctx.get("forge", ""),
        )
    except Exception:  # noqa: BLE001 - "не прочитали" - повод ждать
        log.exception("CI probe of PR #%s (#%s) failed", pr, task["id"])
        return "wait"
    if ci.outcome == CIProbeOutcome.passed:
        return "go"
    if ci.outcome != CIProbeOutcome.failed:
        return "wait"
    return await _rerun_verdict(db, task)


async def _rerun_verdict(db: aiosqlite.Connection, task: dict[str, Any]) -> str:
    """Красный CI: "go" - перезапуск #1956 ещё может сработать; "stop"; "wait".

    Политика не прочиталась - "wait": неизвестное не равно "выключено".
    """
    from hub.services import delivery_rerun  # поздно: цикл через orchestration

    mode = await delivery_rerun.policy_mode(db, int(task["id"]))
    if mode is None:
        return "wait"
    if not mode:
        return "stop"
    spent = await rerun_journal.get(
        db, int(task["id"]), int(task.get("submission_generation") or 0)
    )
    closed = spent is not None and spent["state"] == rerun_journal.CLOSED
    return "stop" if closed else "go"


async def _attempt(
    db: aiosqlite.Connection, row: dict[str, Any], task: dict[str, Any]
) -> None:
    from hub.services import lifecycle
    from hub.services.orchestration import (
        BASE_UPDATED_PREFIX,
        TRANSIENT_GATE_PREFIXES,
    )

    number = int(row["attempts"]) + 1
    async with write_transaction(db):
        token = await journal.reserve(
            db, int(row["id"]), f"попытка {number} из {MAX_ATTEMPTS}"
        )
    await db.commit()  # бронь закоммичена ДО сети; write-lock на форж не держим
    if not token:
        return
    # Проба CI шла по сети: задача могла стать другой сдачей. Сверка - после
    # брони и прямо перед доставкой (deliver_on_disposition перечитает задачу).
    now_row = await repo.get_task(db, int(task["id"]))
    changed = _stop_reason(
        dict(now_row) if now_row else None, {**row, "attempts": 0, "age_hours": 0}
    )
    if changed is not None:
        await _stop(db, row, *changed, token=token)
        return
    try:
        ok, reason, _ = await lifecycle.deliver_on_disposition(
            db, int(task["id"]), DELIVER, via=RETRY_VIA
        )
    except Exception:
        await db.rollback()
        await _settle(
            db, row, f"попытка {number}: исключение при доставке", False, token
        )
        raise
    if ok:
        await _delivered(db, row, f"попытка {number} из {MAX_ATTEMPTS}", token)
    elif reason.startswith(BASE_UPDATED_PREFIX):
        text = f"попытка {number} из {MAX_ATTEMPTS}: {reason}"
        await _settle(db, row, text, False, token)
    elif reason.startswith(TRANSIENT_GATE_PREFIXES):
        text = f"попытка {number} не состоялась (ждём): {reason}"
        await _settle(db, row, text, True, token)
    else:
        outcome = reason.split(":")[0].strip()[:60] or "refused"
        await _stop(db, row, outcome, reason, token=token)


async def _settle(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    text: str,
    refund: bool,
    token: str,
) -> None:
    async with write_transaction(db):
        settled = await journal.settle(
            db, int(row["id"]), text, refund=refund, token=token
        )
        if settled and not refund:
            await repo.add_task_update(
                db,
                int(row["task_id"]),
                "hub",
                "status",
                f"Автоповтор доставки: {text}. Мерж — после зелёного CI новой головы.",
            )
    await db.commit()


async def _stop(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    outcome: str,
    why: str,
    *,
    token: str = "",
) -> None:
    """Закрыть строку исходом, сказать один раз: алерт в ленту и событие."""
    task_id = int(row["task_id"])
    async with write_transaction(db):
        if not await journal.close(db, int(row["id"]), outcome, why, token=token):
            return
        if outcome == "task_missing":
            return
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Автоповтор остановлен ({outcome}): {why}. Мержа этим путём больше "
            f"нет — дальше решает человек (PR #{row['pr_number']}).",
        )
        await repo.insert_event(
            db,
            kind=STOP_EVENT,
            task_id=task_id,
            actor="hub",
            payload={
                "outcome": outcome,
                "reason": why[:500],
                "pr": int(row["pr_number"]),
                "attempts": int(row["attempts"]),
            },
        )
    await db.commit()


async def _delivered(
    db: aiosqlite.Connection, row: dict[str, Any], how: str, token: str = ""
) -> None:
    """PR влит: перечитать доставку ПЕРЕД записью реестра, потом закрыть строку."""
    from hub.services.delivery_state import task_delivery

    task_id = int(row["task_id"])
    task_row = await repo.get_task(db, task_id)
    answer = await task_delivery(db, dict(task_row)) if task_row else None
    async with write_transaction(db):
        if answer is not None:
            await repo.record_delivery_discrepancy(
                db,
                task_id=task_id,
                state=answer["state"],
                reason=answer["reason"],
                pr_number=answer["pr_number"],
                delivery_path=answer["delivery_path"],
                disposition=DELIVER,
            )
        if await journal.close(db, int(row["id"]), "delivered", how, token=token):
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "status",
                f"Автоповтор доставки: PR #{row['pr_number']} влит ({how}) теми "
                "же условиями гейта.",
            )
    await db.commit()
