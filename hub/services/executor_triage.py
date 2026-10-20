"""Исход прогона k8 по классам и состояние «после неудачного прогона» (#1941).

Два чистых чтения, которые ничего не пишут:

* класс причины неудачи по коду ``reason_code`` из закрытого списка
  ``launch_feedback.REASON_CODES`` (таблица ниже — единственная в коде; такая же
  лежит в ``docs/agent-context/contracts.md``);
* проекция ``last_executor_order`` для карточки задачи, в том числе признак
  ``after_failed_run`` — «последний host-заказ закрыт неудачей, и после неё в
  задаче ничего не произошло».

Признак считается ОДНИМ оператором SELECT: заказ, задача, живые сессии и прогоны
читаются одним снимком, поэтому событие между чтениями не может дать смесь
«до» и «после». ``executor_launch._live_run`` здесь не зовётся — он пишет
(снимает брошенную бронь). Сбой чтения — не «нет» и не «да»: проекция несёт
``unavailable`` с причиной и не несёт ``after_failed_run`` вовсе.

Проекция безопасна: в ней нет кода заказа, его хеша и шифротекста. Запуск
исполнителя она не решает — инвариант «код слоту только по executor-launch
человека» не меняется.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import aiosqlite

from hub.db import fetchall

log = logging.getLogger(__name__)

CLASS_UNCLASSIFIED: Final[int] = 0
CLASS_NAMES: Final[dict[int, str]] = {
    CLASS_UNCLASSIFIED: "unclassified",
    1: "infra",
    2: "no_new_head",
    3: "critic_exhausted",
    4: "integrity",
}

#: Код причины -> класс. Нет в таблице (в том числе неизвестный сохранённый
#: код) = «не классифицировано»: critic_failed, agent_failed, other.
CODE_CLASS: Final[dict[str, int]] = {
    "critic_unavailable": 1,
    "critic_delivery_failed": 1,
    "channel_check_failed": 1,
    "setup_failed": 1,
    "redeem_failed": 1,
    "vm_failed": 1,
    "hub_unavailable": 1,
    "stale_token": 1,  # nosec B105 - код причины, не секрет
    "busy": 1,
    "over_budget": 1,
    "timeout": 1,
    "no_new_head": 2,
    "critic_cycles_exhausted": 3,
    "critic_time_exhausted": 3,
    "integrity_violation": 4,
}

FAILED_OUTCOMES: Final[tuple[str, ...]] = ("rescued", "failed")

#: Один оператор = один снимок. Последний host-заказ задачи (по id) вместе со
#: всем, что гасит признак: статус и архив задачи, держатель, поколение сдачи,
#: живая implementer-сессия, живой или забронированный прогон (в том числе
#: облачная бронь без агента; прогон, ждущий только цену, не считается).
_SNAPSHOT_SQL: Final[str] = """
SELECT o.id AS order_id, o.state, o.generation, o.report_outcome,
       o.report_reason_code,
       t.status, t.archived, t.claimed_by, t.claim_session_id,
       t.implementer_principal_id, t.submission_generation,
       EXISTS (SELECT 1 FROM chat_pair_sessions s
               WHERE s.kind = 'implementer' AND s.bound_task_id = t.id
                 AND s.revoked_at IS NULL
                 AND s.expires_at > datetime('now')) AS live_session,
       EXISTS (SELECT 1 FROM executor_runs r
               WHERE r.task_id = t.id AND r.outcome = 'running'
                 AND r.cost_wait_since IS NULL) AS live_run
FROM executor_orders o JOIN tasks t ON t.id = o.task_id
WHERE o.task_id = ?
ORDER BY o.id DESC LIMIT 1
"""


def class_of(reason_code: str) -> tuple[int, str]:
    """(номер класса, имя) по коду причины; неизвестный код — не классифицировано."""
    number = CODE_CLASS.get(reason_code or "", CLASS_UNCLASSIFIED)
    return number, CLASS_NAMES[number]


async def _read_snapshot(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    rows = await fetchall(db, _SNAPSHOT_SQL, (int(task_id),))
    return dict(rows[0]) if rows else None


def _quiet(snap: dict[str, Any]) -> bool:
    """В задаче после неудачного прогона ничего не произошло."""
    return (
        snap["status"] == "open"
        and not snap["archived"]
        and not snap["claimed_by"]
        and not snap["claim_session_id"]
        and snap["implementer_principal_id"] is None
        and not snap["live_session"]
        and not snap["live_run"]
        # Каноническая сдача сильнее report failed/rescued: поколение дошло до
        # заказного (заказ создаётся на поколение сдачи + 1).
        and int(snap["submission_generation"] or 0) < int(snap["generation"])
    )


def project(snap: dict[str, Any]) -> dict[str, Any]:
    """Проекция последнего host-заказа из снимка; чистая функция."""
    outcome = snap["report_outcome"] or ""
    failed = outcome in FAILED_OUTCOMES
    code = snap["report_reason_code"] or ""
    number, name = class_of(code) if failed else (None, None)
    return {
        "order_id": int(snap["order_id"]),
        "state": snap["state"],
        "outcome": outcome,
        "reason_code": code,
        "class": number,
        "class_name": name,
        "after_failed_run": bool(failed and snap["state"] == "closed" and _quiet(snap)),
    }


async def last_executor_order_view(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    """Проекция для карточки; ``None`` — у задачи нет host-заказов.

    Сбой чтения даёт ``{"unavailable": <причина>}`` без ``after_failed_run``.
    """
    try:
        snap = await _read_snapshot(db, task_id)
    except Exception as exc:  # noqa: BLE001 - сбой чтения называется, а не глотается
        log.warning("last_executor_order #%s: %s", task_id, exc)
        return {"unavailable": f"чтение заказов не удалось: {type(exc).__name__}"}
    return None if snap is None else project(snap)
