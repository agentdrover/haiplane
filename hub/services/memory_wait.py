"""«Ждёт памяти»: заказ хосту честно ждёт память до выкупа (#1972).

Допуск по памяти на k8 (#1949) и очередь тяжёлых (#1951) задерживают подъём ВМ
ДО выкупа кода. Раньше хаб видел «выдан, не выкуплен», а код и заказ сгорали по
своим срокам. Теперь хост сообщает фазу ``waiting_memory`` (существующая ручка
``/phase``, тот же ключ слота) с цифрами памяти, и заказ живёт, пока идёт heartbeat.

Правила (их сводка - в ``docs/agent-context/invariants.md``):

* состояние ``waiting_memory`` существует только ДО выкупа; выход из него -
  обычный выкуп кода (или закрытие);
* ``waiting_since`` пишется один раз и не сбрасывается ни heartbeat'ом, ни
  повторной доставкой;
* каждый принятый heartbeat в ОДНОЙ транзакции продлевает срок заказа и его кода
  до ``min(now + MEMORY_WAIT_HEARTBEAT_MINUTES, waiting_since + MEMORY_WAIT_MAX_HOURS)``;
  просроченный заказ, отсутствующий или потраченный код не оживляются;
* на границе ``MEMORY_WAIT_MAX_HOURS`` heartbeat и выкуп отказаны, а ``settle``
  закрывает заказ причиной ``memory_wait_timeout`` (ровно один раз: запись ленты,
  сигнал владельцу и событие пишет закрывший);
* время - только серверное (``datetime('now')``), часам хоста веры нет.

Модуль - лист: ни один сервис заказов на верхнем уровне не импортируется.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import aiosqlite

from hub import repository as repo
from hub.db import fetchall, write_transaction
from hub.services import admin as admin_svc

STATE_WAITING_MEMORY: Final[str] = "waiting_memory"
#: Имя фазы в теле ``/phase`` совпадает с именем состояния заказа.
PHASE_WAITING_MEMORY: Final[str] = STATE_WAITING_MEMORY
CLOSE_TIMEOUT: Final[str] = "memory_wait_timeout"
EVENT_TIMEOUT: Final[str] = "executor_memory_wait_timeout"

#: Срок заказа и кода после heartbeat; хост шлёт не реже раза в 5 минут.
MEMORY_WAIT_HEARTBEAT_MINUTES: Final[int] = 10
#: Предел ожидания от первого сообщения; дальше - отказ и закрытие.
MEMORY_WAIT_MAX_HOURS: Final[int] = 6
#: Потолок цифр памяти: 1 ТиБ в MiB.
MIB_MAX: Final[int] = 1_048_576

#: Состояния «ещё не выкуплен»: на них смотрят выкуп, доставка и settle.
UNREDEEMED_IN_SQL: Final[str] = "('pending', 'delivered', 'waiting_memory')"
#: Граница ожидания по часам базы: позже этого момента ждать больше нельзя.
_LIMIT_SQL: Final[str] = f"datetime('now', '-{MEMORY_WAIT_MAX_HOURS} hours')"
#: Выкуп допустим: до ожидания всё как было, из ожидания - пока срок жив и 6 ч не вышли.
#: Предикат стоит в самом UPDATE траты кода (``chat_pair._SLOT_ORDER_GUARD``).
REDEEMABLE_SQL: Final[str] = (
    "(o.state IN ('pending', 'delivered') OR (o.state = 'waiting_memory' "
    "AND o.expires_at > datetime('now') "
    f"AND o.waiting_since > {_LIMIT_SQL}))"
)

_READ_SQL: Final[str] = (
    "SELECT o.state, o.task_id, o.waiting_since, o.expires_at <= datetime('now') AS lapsed, "  # nosec B608
    f"(o.waiting_since IS NOT NULL AND o.waiting_since <= {_LIMIT_SQL}) AS over_limit, "
    "c.id AS code_id, c.redeemed_at AS code_spent, "
    "c.expires_at <= datetime('now') AS code_lapsed "
    "FROM executor_orders o LEFT JOIN chat_pair_codes c ON c.id = o.pair_code_id "
    "WHERE o.id = ? AND o.host = ?"
)

#: Срок после принятого heartbeat. Правая часть UPDATE читает СТАРУЮ строку,
#: поэтому ``COALESCE(waiting_since, now)`` - это и первое сообщение, и повтор.
_EXPIRES_SQL: Final[str] = (
    f"min(datetime('now', '+{MEMORY_WAIT_HEARTBEAT_MINUTES} minutes'), "
    f"datetime(COALESCE(waiting_since, datetime('now')), "
    f"'+{MEMORY_WAIT_MAX_HOURS} hours'))"
)


def _host_error(status: int, detail: Any) -> Exception:
    from hub.services.executor_orders import HostError

    return HostError(status, detail)


def _mib(name: str, value: Any, low: int) -> int:
    """Строгое целое: ни строк, ни boolean, ни дробей - ничего не приводится."""
    if type(value) is not int or not low <= value <= MIB_MAX:
        raise _host_error(422, f"{name}: целое число от {low} до {MIB_MAX}")
    return value


def validate_mib(need_mib: Any, free_mib: Any) -> tuple[int, int]:
    """(нужно, свободно) в MiB; 422 на всё, что не строгое целое в границах."""
    return _mib("need_mib", need_mib, 1), _mib("free_mib", free_mib, 0)


async def record_waiting(
    db: aiosqlite.Connection,
    host: str,
    order_id: int,
    need_mib: Any,
    free_mib: Any,
) -> dict[str, Any]:
    """Фаза ``waiting_memory`` или heartbeat: заказ и код живут дальше.

    Все проверки - чтением под write-lock ДО записей: отказ ничего не меняет, и
    вложенный вызов (без собственного отката) не оставляет полупродления.
    """
    need, free = validate_mib(need_mib, free_mib)
    async with write_transaction(db):
        rows = await fetchall(db, _READ_SQL, (int(order_id), host))
        if not rows:
            raise _host_error(403, "заказ не принадлежит этому хосту")
        got = dict(rows[0])
        if got["state"] not in ("pending", "delivered", STATE_WAITING_MEMORY):
            raise _host_error(
                409, f"ожидание памяти возможно до выкупа: заказ {got['state']}"
            )
        if got["over_limit"]:
            raise _host_error(
                409,
                {
                    "reason": "memory_wait_limit",
                    "message": f"ожидание памяти длится {MEMORY_WAIT_MAX_HOURS} ч: "
                    "предел вышел, заказ будет закрыт",
                },
            )
        if (
            got["lapsed"]
            or got["code_id"] is None
            or got["code_spent"]
            or got["code_lapsed"]
        ):
            raise _host_error(
                409,
                {
                    "reason": "memory_wait_lapsed",
                    "message": "срок заказа или кода вышел: heartbeat не оживляет",
                },
            )
        first = got["state"] != STATE_WAITING_MEMORY
        await db.execute(
            "UPDATE executor_orders SET state = 'waiting_memory', "  # nosec B608 - constants
            "delivered_at = COALESCE(delivered_at, datetime('now')), "
            "last_seen_at = datetime('now'), need_mib = ?, free_mib = ?, "
            f"expires_at = {_EXPIRES_SQL}, "
            "waiting_since = COALESCE(waiting_since, datetime('now')) "
            "WHERE id = ?",
            (need, free, int(order_id)),
        )
        await db.execute(
            "UPDATE chat_pair_codes SET expires_at = "
            "(SELECT expires_at FROM executor_orders WHERE id = ?) WHERE id = ?",
            (int(order_id), int(got["code_id"])),
        )
        if first:
            await admin_svc.write_audit(
                db,
                actor_id=None,
                action="executor_order_waiting_memory",
                target_type="task",
                target_id=str(int(got["task_id"])),
                summary=f"executor order #{int(order_id)} of host {host} waits for "
                f"memory: need {need} MiB, free {free} MiB",
                commit=False,
            )
        fresh = (
            await fetchall(
                db,
                "SELECT waiting_since, last_seen_at, expires_at FROM executor_orders "
                "WHERE id = ?",
                (int(order_id),),
            )
        )[0]
    return {
        "id": int(order_id),
        "phase": PHASE_WAITING_MEMORY,
        "state": STATE_WAITING_MEMORY,
        "heartbeat": not first,
        "waiting_since": fresh["waiting_since"],
        "last_seen_at": fresh["last_seen_at"],
        "expires_at": fresh["expires_at"],
    }


VERDICT_TIMEOUT: Final[str] = "timeout"
VERDICT_EXPIRED: Final[str] = "expired"


async def verdict(db: aiosqlite.Connection, order: dict[str, Any]) -> str:
    """Что делать ``settle`` с ждущим заказом: ``timeout`` | ``expired`` | пусто.

    Предел и обычный срок читаются ОДНИМ оператором, то есть при одном и том же
    ``datetime('now')``: срок заказа у предела равен ``waiting_since + 6 ч``, и две
    проверки в разные моменты закрыли бы заказ ``expired`` без сигнала владельцу.
    Граница включена: ровно 6 ч - уже предел, и он сильнее обычного срока.
    """
    rows = await fetchall(
        db,
        f"SELECT (? IS NOT NULL AND ? <= {_LIMIT_SQL}) AS over, "
        "(? <= datetime('now')) AS lapsed",
        (
            order.get("waiting_since"),
            order.get("waiting_since"),
            order["expires_at"],
        ),
    )
    if rows[0]["over"]:
        return VERDICT_TIMEOUT
    return VERDICT_EXPIRED if rows[0]["lapsed"] else ""


#: Причины отказа выкупа, названные по условиям допуска; ``spent`` - тоже названа.
SPEND_REASONS: Final[tuple[str, ...]] = ("memory_wait_limit", "expired", "spent")


async def why(db: aiosqlite.Connection, code_row: dict[str, Any], default: str) -> str:
    """Почему выкуп кода слота отказан: ``spent`` | ``memory_wait_limit`` | ``expired`` | ``default``.

    Те же условия, что в ``REDEEMABLE_SQL``, и ОДНО чтение (один ``datetime('now')``),
    от ЗАКАЗА, а не от строки кода: закрытие по пределу сжигает код, и от кода
    причина пропала бы. Порядок: потрачен, предел (в том числе закрытие
    ``memory_wait_timeout``), срок; всё прочее - ``default``. Зовут оба места
    отказа ``chat_pair``: ранняя проверка срока кода (``default=expired``) и
    нулевой rowcount траты (``default=spent``), чтобы граница 6 ч, где срок кода
    и предел совпадают, называлась одним словом.
    """
    rows = await fetchall(
        db,
        "SELECT c.redeemed_at IS NOT NULL AS spent, o.state AS state, "
        "o.close_reason AS close_reason, "
        f"(o.waiting_since IS NOT NULL AND o.waiting_since <= {_LIMIT_SQL}) AS over, "
        "(o.expires_at <= datetime('now') OR c.expires_at <= datetime('now')) AS lapsed "
        "FROM executor_orders o LEFT JOIN chat_pair_codes c ON c.id = o.pair_code_id "
        "WHERE o.pair_code_id = ?",  # nosec B608 - constant
        (int(code_row["id"]),),
    )
    if not rows:
        return default
    got = rows[0]
    if got["spent"]:
        return "spent"
    if got["close_reason"] == CLOSE_TIMEOUT:
        return "memory_wait_limit"
    if got["state"] != STATE_WAITING_MEMORY:
        return default
    if got["over"]:
        return "memory_wait_limit"
    return "expired" if got["lapsed"] else default


async def notify_timeout(db: aiosqlite.Connection, order: dict[str, Any]) -> None:
    """Сигнал владельцу о конце предела: alert в ленте и событие; один раз.

    Зовёт ``close_order`` в транзакции закрытия, после условного UPDATE - поэтому
    повторный проход сюда не попадает.
    """
    task = await repo.get_task(db, int(order["task_id"]))
    project_id = dict(task).get("project_id") if task is not None else None
    await repo.add_task_update(
        db,
        int(order["task_id"]),
        "hub",
        "alert",
        f"Ожидание памяти на {order['host']} длилось {MEMORY_WAIT_MAX_HOURS} ч, "
        f"заказ #{order['id']} закрыт, код сожжён. Запуск исполнителя нужен "
        "заново, когда на хосте освободится память.",
    )
    await repo.insert_event(
        db,
        kind=EVENT_TIMEOUT,
        task_id=int(order["task_id"]),
        project_id=project_id,
        actor="hub",
        payload={
            "order_id": int(order["id"]),
            "host": order["host"],
            "waiting_since": order.get("waiting_since"),
            "need_mib": order.get("need_mib"),
            "free_mib": order.get("free_mib"),
            "max_hours": MEMORY_WAIT_MAX_HOURS,
        },
    )


def _gib(mib: Any) -> str:
    """MiB -> «ГиБ» с запятой и без хвостовых нулей: 6144 -> «6», 3000 -> «2,9»."""
    return f"{round(int(mib) / 1024, 1):g}".replace(".", ",")


def _hhmm(stamp: Any) -> str:
    """Время заказа (UTC в базе) в часовом поясе владельца, «HH:MM»."""
    from hub.services import owner_minutes

    moment = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return moment.astimezone(owner_minutes.timezone()).strftime("%H:%M")


def line(host: str, snap: dict[str, Any]) -> str:
    """Единая строка ожидания для REST, MCP, CLI, страницы и плашки; собирает хаб."""
    return (
        f"ждёт памяти на {host} с {_hhmm(snap['waiting_since'])}: "
        f"нужно {_gib(snap['need_mib'])} ГиБ, свободно {_gib(snap['free_mib'])} ГиБ, "
        f"последний сигнал {_hhmm(snap['last_seen_at'])}"
    )


def projection(host: str, snap: dict[str, Any]) -> dict[str, Any]:
    """Блок ``waiting_memory`` проекции ``last_executor_order``; без кода и хеша."""
    return {
        "host": host,
        "since": snap["waiting_since"],
        "last_seen_at": snap["last_seen_at"],
        "need_mib": snap["need_mib"],
        "free_mib": snap["free_mib"],
        "line": line(host, snap),
    }
