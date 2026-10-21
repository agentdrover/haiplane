"""Выкуп кода заказа хоста самим хостом, с безопасным повтором (#1825).

Хост k8 забирает заказ, расшифровывает код и выкупает его. Раньше выкуп шёл
публично (``POST /api/auth/chat-pair/redeem``): credential — только код, хост не
проверялся, а потерянный ответ делал код «потраченным» без токена в руках — запуск
пропадал. Здесь для заказов хоста есть маршрут с ключом хоста:

* первый выкуп — тот же ``chat_pair.redeem_code_explained`` (одно общее место
  траты кода), но только для кода ЭТОГО заказа ЭТОГО хоста;
* повтор после неопределённого ответа — ротация хеша токена ТОЙ ЖЕ сессии, и
  только пока токен ни разу не предъявлен, окно (от первого выкупа, не
  продлевается) не вышло, сессия жива, заказ не закрыт. Использованный токен
  ротация не отзывает: хост уже работает под ним (``already_redeemed_in_use``).

Единственное место допуска токена — ``chat_pair.resolve_session``; он зовёт
``admit_first_use`` отсюда, и этот UPDATE сериализуется с ротацией замком записи.
Ни код, ни токен не попадают в лог, аудит и ленту.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

import aiosqlite

from hub import config
from hub.auth import LoginRateLimiter
from hub.db import fetchall, write_transaction
from hub.hub_instance import hub_base_url
from hub.services import admin as admin_svc
from hub.services import chat_pair
from hub.services import executor_orders as orders
from hub.services import memory_wait

log = logging.getLogger("hub.services.host_redeem")

REASON_IN_USE = "already_redeemed_in_use"
REASON_EXPIRED = "redeem_retry_expired"
REASON_LIMITED = "redeem_retry_limited"
REASON_UNAVAILABLE = "order_unavailable"

#: Попытки выкупа одного хоста в окне; 429 заказ не закрывает.
host_redeem_limiter = LoginRateLimiter(max_attempts=60, window_seconds=300)

_LIVE = "revoked_at IS NULL AND expires_at > datetime('now')"


def _refuse(status: int, reason: str, message: str) -> orders.HostError:
    return orders.HostError(status, {"reason": reason, "message": message})


async def is_host_order_code(db: aiosqlite.Connection, row: dict[str, Any]) -> bool:
    """Код слота зарегистрированного хоста — заказ хоста, а не ручной слот."""
    return row.get("channel") == "slot" and bool(
        await fetchall(
            db,
            "SELECT 1 FROM executor_hosts WHERE name = ?",
            (row.get("slot") or "",),
        )
    )


async def admit_first_use(
    db: aiosqlite.Connection, token_hash: str, dsn: str | None
) -> bool:
    """Допуск ПЕРВОГО запроса с токеном: ``first_used_at`` ставится атомарно.

    UPDATE называет хеш токена: ротация меняет хеш под тем же замком записи,
    поэтому запрос со СТАРЫМ токеном либо ставит метку первым (ротация тогда
    откажет), либо не находит строки. Своё соединение, когда известен DSN:
    общее для коммитов не предназначено.
    """
    from hub.db import connect

    conn = await connect(dsn) if dsn else db
    live = f"token_hash = ? AND {_LIVE}"
    try:
        cursor = await conn.execute(
            "UPDATE chat_pair_sessions SET first_used_at = datetime('now') "  # nosec B608
            f"WHERE {live} AND first_used_at IS NULL",
            (token_hash,),
        )
        stamped = bool(cursor.rowcount)
        await conn.commit()
        if stamped:
            return True
        # Другой запрос успел поставить метку между чтением и записью: допуск.
        return bool(
            await fetchall(
                conn,
                f"SELECT 1 FROM chat_pair_sessions WHERE {live}",  # nosec B608
                (token_hash,),
            )
        )
    finally:
        if dsn:
            await conn.close()


async def token_in_use(db: aiosqlite.Connection, run_id: int) -> bool:
    """Живая сессия прогона, чей токен уже предъявлен хотя бы раз."""
    return bool(
        await fetchall(
            db,
            f"SELECT 1 FROM chat_pair_sessions WHERE executor_run_id = ? "  # nosec B608
            f"AND first_used_at IS NOT NULL AND {_LIVE}",
            (int(run_id),),
        )
    )


def _answer(session: dict[str, Any], order: dict[str, Any], version: int) -> dict:
    implementer = session.get("kind") == "implementer"
    return {
        "token": session["token"],
        "expires_at": session["expires_at"],
        "username": session["username"],
        "role": "agent" if implementer else "human",
        "base_url": hub_base_url(),
        "permissions": sorted(
            config.CHAT_PAIR_IMPLEMENTER_PERMS
            if implementer
            else config.CHAT_PAIR_PERMS
        ),
        "kind": session.get("kind") or "intake",
        "bound_task_id": session.get("bound_task_id"),
        "order_id": order["id"],
        "token_version": version,
        "retry_until": order["retry_until"],
    }


async def _retry(db: aiosqlite.Connection, host: str, order_id: int) -> dict:
    """Повтор: токен ТОЙ ЖЕ сессии заменяется новым, версия растёт."""
    async with write_transaction(db):
        order = await orders._own_order(db, host, order_id)
        if order is None or order["state"] != orders.STATE_REDEEMED:
            raise _refuse(409, "order_closed", "заказ закрыт или не выкуплен")
        found = await fetchall(
            db,
            "SELECT s.*, p.username AS acting_name, "
            "s.expires_at <= datetime('now') AS dead, "
            "o.retry_until <= datetime('now') AS late, "
            "r.outcome AS run_outcome "
            "FROM executor_orders o "
            "JOIN chat_pair_sessions s ON s.id = o.session_id "
            "JOIN principals p ON p.id = s.acting_principal_id "
            "JOIN executor_runs r ON r.id = o.executor_run_id "
            "WHERE o.id = ?",
            (order["id"],),
        )
        host_row = await orders._enabled_host(db, host)
        if isinstance(host_row, str):
            raise _refuse(409, "host_disabled", host_row)
        sess = dict(found[0]) if found else None
        bound = sess is not None and (
            sess["bound_task_id"] == order["task_id"]
            and sess["bound_generation"] == order["generation"]
            and sess["executor_run_id"] == order["executor_run_id"]
            and (sess["channel"], sess["slot"]) == ("slot", host)
            and sess["kind"] == "implementer"
        )
        if sess is None or not bound or sess["revoked_at"] or sess["dead"]:
            raise _refuse(409, "session_gone", "сессия заказа отозвана или истекла")
        if sess["run_outcome"] != "running":
            raise _refuse(409, "order_closed", "прогон заказа закончен")
        if sess["late"]:
            raise _refuse(409, REASON_EXPIRED, "окно повтора выкупа вышло")
        if sess["first_used_at"] is not None:
            raise _refuse(409, REASON_IN_USE, "токен уже используется")
        if int(order["redeem_rotations"]) >= config.HOST_REDEEM_MAX_ROTATIONS:
            raise _refuse(429, REASON_LIMITED, "слишком много повторов выкупа")
        token = chat_pair.generate_session_token()
        cursor = await db.execute(
            "UPDATE chat_pair_sessions SET token_hash = ?, "
            "token_version = token_version + 1 WHERE id = ? "
            f"AND first_used_at IS NULL AND {_LIVE}",  # nosec B608
            (chat_pair.hash_pair_code(token), sess["id"]),
        )
        if not cursor.rowcount:
            raise _refuse(409, REASON_IN_USE, "токен уже используется")
        await db.execute(
            "UPDATE executor_orders SET redeem_rotations = redeem_rotations + 1 "
            "WHERE id = ?",
            (order["id"],),
        )
        await admin_svc.write_audit(
            db,
            actor_id=None,
            action="executor_order_redeem_retried",
            target_type="task",
            target_id=str(order["task_id"]),
            summary=f"executor order #{order['id']} redeem retried by host {host} "
            f"(token version {int(sess['token_version']) + 1})",
            commit=False,
        )
    session = {
        "token": token,
        "expires_at": chat_pair._as_iso(sess["expires_at"]),
        "username": sess["acting_name"],
        "kind": "implementer",
        "bound_task_id": sess["bound_task_id"],
    }
    return _answer(session, order, int(sess["token_version"]) + 1)


async def redeem_for_host(
    db: aiosqlite.Connection, host: str, order_id: int, raw_code: str
) -> dict:
    """Тело ``POST /api/executor-hosts/{name}/orders/{id}/redeem``.

    Любое несовпадение хоста, заказа и кода — один отказ ``order_unavailable``
    (403): по нему нельзя узнать, существует ли код.
    """
    if host_redeem_limiter.is_blocked(host):
        raise _refuse(429, REASON_LIMITED, "слишком много попыток выкупа")
    # Попытка считается сразу, до первого await: отказы тоже стоят замка записи.
    host_redeem_limiter.record(host)
    code_hash = chat_pair.hash_pair_code(chat_pair.normalize_pair_code(raw_code))
    order = await orders._own_order(db, host, order_id)
    if order is None or not hmac.compare_digest(str(order["code_hash"]), code_hash):
        raise _refuse(403, REASON_UNAVAILABLE, "заказ недоступен для этого кода")
    if not order["first_redeemed_at"]:
        if order["state"] not in orders._UNREDEEMED:
            raise _refuse(409, REASON_EXPIRED, "повтор невозможен: окно не открыто")
        session, refusal = await chat_pair.redeem_code_explained(
            db, raw_code, via_host=True
        )
        if session is not None:
            fresh = await orders._own_order(db, host, order_id)
            if fresh is None:
                raise _refuse(500, "order_gone", "заказ исчез при выкупе")
            return _answer(session, fresh, 1)
        # Перекрывающийся повтор: первый выкуп успел между чтением и тратой —
        # это повтор после потерянного ответа, а не окончательный отказ.
        order = await orders._own_order(db, host, order_id)
        if order is None or not order["first_redeemed_at"]:
            reason = refusal.reason if refusal is not None else "refused"
            # #1972: предел ожидания и срок называются своими словами, не spent.
            named = reason if reason in memory_wait.SPEND_REASONS else "redeem_refused"
            raise _refuse(409, named, f"код не принят: {reason}")
    return await _retry(db, host, order_id)
