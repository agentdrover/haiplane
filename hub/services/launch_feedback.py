"""Обратная связь запуска исполнителя на карточке задачи (#1817).

Что видит человек после «Запустить на k8» и «Запустить исполнителя»: сообщение,
плашку шагов и итог с понятной причиной. Здесь же контракт фаз хоста: одна ручка
``POST …/orders/{id}/phase``, фазы только вперёд и только у фактически выкупленного
заказа; в ленту задачи фазы не пишутся.

Шаги слота: создан → отправлен хосту (``delivered`` — доставка без подтверждения,
НЕ «принят») → код выкуплен (фактическое ``state=redeemed``, не ack) → фазы хоста →
итог. У облака заказа и фаз хоста нет, у него свои состояния ``executor_runs``.

Приоритет итога: фактическая сдача > причина закрытия > сообщение хоста. Поздний
report после закрытия принимается и уточняет причину.
"""

from __future__ import annotations

from typing import Any, Final

import aiosqlite

from hub import repository as repo
from hub.db import fetchall, write_transaction

PHASES: Final[tuple[str, ...]] = (
    "vm_starting",
    "agent_running",
    "critic",
    "submitting",
)

PHASE_TEXT: Final[dict[str, str]] = {
    "vm_starting": "хост запускает ВМ",
    "agent_running": "агент работает",
    "critic": "критик проверяет результат",
    "submitting": "хост сдаёт работу",
}

#: Коды причины в report хоста; ``other`` — старый клиент без поля.
REASON_CODE_TEXT: Final[dict[str, str]] = {
    "channel_check_failed": "проверка канала не прошла",
    "setup_failed": "подготовка рабочего места не удалась",
    "redeem_failed": "хост не смог выкупить код",
    "vm_failed": "ВМ не запустилась",
    "busy": "хост занят",
    "over_budget": "превышен бюджет",
    "agent_failed": "агент завершился с ошибкой",
    "critic_failed": "критик отклонил результат",
    "timeout": "вышло время",
    "hub_unavailable": "хост не достучался до хаба",
    "stale_token": "остаток токена прошлого заказа не удалось отозвать",  # nosec B105 - текст причины, не секрет
    "other": "причина не названа",
}
REASON_CODES: Final[tuple[str, ...]] = tuple(REASON_CODE_TEXT)
REASON_MAX: Final[int] = 200

#: Единая таблица: причина закрытия заказа → текст плашки и ленты.
CLOSE_REASON_TEXT: Final[dict[str, str]] = {
    "submitted": "Работа сдана на ревью",
    "expired": "Заказ не выкуплен хостом в срок и истёк",
    "session_gone": "Сессия исполнителя пропала, прогон оборван",
    "run_finished": "Прогон закончился без сдачи",
    "host_disabled": "Хост отключён, заказ закрыт",
    "redeem_failed": "Хост не смог выкупить код, запуск не состоялся",
    "redeem_lost": "Ответ на выкуп потерян, запуск не восстановлен",
    "reported_failed": "Прогон на хосте упал",
    "reported_rescued": "Хост спас работу в отдельную ветку",
}
_UNKNOWN_CLOSE = "Заказ закрыт"


def reason_code_text(code: str) -> str:
    """Человеческий текст кода причины; неизвестный код — как ``other``."""
    return REASON_CODE_TEXT.get(code or "other", REASON_CODE_TEXT["other"])


def close_reason_text(reason: str, reason_code: str = "") -> str:
    """Текст итога по причине закрытия и (если назван) коду причины хоста."""
    base = CLOSE_REASON_TEXT.get(reason, _UNKNOWN_CLOSE)
    if reason_code and reason_code != "other":
        return f"{base}: {reason_code_text(reason_code)}"
    return base


async def landed_by_order(db: aiosqlite.Connection, order: dict[str, Any]) -> bool:
    """Задачу сдал прогон ЭТОГО заказа: поколение дошло до заказного, держатель — его прогон."""
    task = await repo.get_task(db, int(order["task_id"]))
    if task is None:
        return False
    t = dict(task)
    return int(t.get("submission_generation") or 0) >= int(order["generation"]) and (
        t.get("executor_run_id") == order["executor_run_id"]
    )


def close_text(order: dict[str, Any], reason: str) -> str:
    """Текст итоговой записи не-сдачи: причина закрытия, сообщение хоста — уточнение."""
    detail = close_reason_text(reason, order.get("report_reason_code") or "")
    if order.get("report_reason"):
        detail = f"{detail} — {order['report_reason']}"
    return (
        f"Заказ #{order['id']} хосту {order['host']} (сдача {order['generation']}) "
        f"закрыт: {detail}; непотраченный код сожжён."
    )


async def record_phase(
    db: aiosqlite.Connection, host: str, order_id: int, phase: str
) -> dict[str, Any]:
    """Фаза хоста: только свой фактически выкупленный незакрытый заказ без report."""
    from hub.services.executor_orders import STATE_REDEEMED, HostError, _own_order

    if phase not in PHASES:
        raise HostError(422, f"phase: одна из {', '.join(PHASES)}")
    async with write_transaction(db):
        order = await _own_order(db, host, order_id)
        if order is None:
            raise HostError(403, "заказ не принадлежит этому хосту")
        if order["state"] != STATE_REDEEMED:
            raise HostError(
                409, f"фаза возможна у выкупленного заказа: {order['state']}"
            )
        if order["report_outcome"]:
            raise HostError(409, "итог по заказу уже записан: фазы закрыты")
        current = order["phase"] or ""
        if current == phase:
            return {"id": order["id"], "phase": phase, "duplicate": True}
        if current and PHASES.index(phase) < PHASES.index(current):
            raise HostError(409, f"фаза {phase} позади принятой {current}")
        cursor = await db.execute(
            "UPDATE executor_orders SET phase = ?, phase_at = datetime('now') "
            "WHERE id = ? AND state = 'redeemed' AND report_outcome = '' AND phase = ?",
            (phase, order["id"], current),
        )
        if not cursor.rowcount:
            raise HostError(409, "заказ изменился: повторите")
    return {"id": order["id"], "phase": phase, "duplicate": False}


def _step(label: str, at: Any, done: bool) -> dict[str, Any]:
    return {"label": label, "at": str(at) if at else "", "done": done}


async def _slot_plaque(
    db: aiosqlite.Connection, order: dict[str, Any]
) -> dict[str, Any]:
    from hub.services.executor_orders import CLOSE_SUBMITTED, STATE_CLOSED

    host, number = order["host"], order["id"]
    state = order["state"]
    task = await repo.get_task(db, int(order["task_id"]))
    t = dict(task) if task is not None else {}
    landed = int(t.get("submission_generation") or 0) >= int(order["generation"]) and (
        order["close_reason"] == CLOSE_SUBMITTED
        or t.get("executor_run_id") == order["executor_run_id"]
    )
    redeemed = bool(order["redeemed_at"])
    steps = [
        _step(f"Заказ #{number} создан", order["created_at"], True),
        _step("отправлен хосту", order["delivered_at"], bool(order["delivered_at"])),
        _step("код выкуплен", order["redeemed_at"], redeemed),
    ]
    reached = PHASES.index(order["phase"]) if order["phase"] in PHASES else -1
    for i, name in enumerate(PHASES):
        steps.append(
            _step(
                PHASE_TEXT[name],
                order["phase_at"] if i == reached else "",
                i <= reached,
            )
        )
    active = state != STATE_CLOSED and not landed
    outcome = ""
    if landed:
        outcome = CLOSE_REASON_TEXT["submitted"]
    elif state == STATE_CLOSED:
        outcome = close_reason_text(order["close_reason"], order["report_reason_code"])
    if outcome and order["report_reason"] and not landed:  # сдача сильнее слова хоста
        outcome = f"{outcome} — {order['report_reason']}"
    saved = [
        x
        for x in (
            f"ветка {order['report_branch']}" if order["report_branch"] else "",
            f"sha {order['report_sha']}" if order["report_sha"] else "",
        )
        if x
    ]
    if outcome and saved and not landed:
        outcome = f"{outcome} ({', '.join(saved)})"
    if state in ("pending", "delivered"):
        headline = f"Заказ #{number} создан, ждёт {host}"
        step = "отправлен хосту" if state == "delivered" else "создан, ждёт хоста"
    elif not active:
        headline = f"Заказ #{number} на {host} закрыт"
        step = ""
    elif order["phase"] in PHASE_TEXT:
        headline = f"Заказ #{number}: {PHASE_TEXT[order['phase']]}"
        step = PHASE_TEXT[order["phase"]]
    else:
        headline = f"Заказ #{number}: код выкуплен, {host} запускает ВМ"
        step = "код выкуплен"
    return {
        "kind": "slot",
        "order_id": number,
        "host": host,
        "active": active,
        "headline": headline,
        "steps": steps,
        "outcome": outcome,
        "outcome_ok": landed,
        "button_label": step,
    }


def _cloud_plaque(run: dict[str, Any]) -> dict[str, Any]:
    active = run["outcome"] == "running"
    outcome = "" if active else f"Облачный прогон закончен: {run['outcome']}"
    if not active and run.get("reason"):
        outcome += f" — {run['reason']}"
    return {
        "kind": "cloud",
        "order_id": None,
        "host": "",
        "active": active,
        "headline": "Прогон запущен в облаке" if active else "Облачный прогон",
        "steps": [_step("Прогон запущен в облаке", run["started_at"], True)],
        "outcome": outcome,
        "outcome_ok": run["outcome"] == "finished",
        "button_label": "идёт облачный прогон" if active else "",
    }


async def plaque(db: aiosqlite.Connection, task_id: int) -> dict[str, Any] | None:
    """Плашка запуска задачи или None: запусков не было. Читает, не пишет."""
    runs = await fetchall(
        db,
        "SELECT * FROM executor_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (int(task_id),),
    )
    if not runs:
        return None
    run = dict(runs[0])
    if run["channel"] != "slot":
        return _cloud_plaque(run)
    orders = await fetchall(
        db,
        "SELECT * FROM executor_orders WHERE executor_run_id = ? ORDER BY id DESC LIMIT 1",
        (run["id"],),
    )
    return await _slot_plaque(db, dict(orders[0])) if orders else None
