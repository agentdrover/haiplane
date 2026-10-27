"""Заказы хостам исполнителя: хаб сам выдаёт код слоту, человек его не переносит (#1712).

Исполнитель на хосте (k8) работает через implementer chat-pair. Раньше код на
каждую задачу выписывал человек кнопкой «Передать в облачный чат» и по ssh
вставлял в launcher. Здесь человек запускает исполнителя на ЗАДАЧУ и ХОСТ, а
код выписывает хаб и отдаёт его хосту зашифрованным на ключ хоста.

Три свойства держат всю схему:

* **Открытый код не хранится нигде.** Он существует в памяти запроса запуска
  ровно до шифрования RSA-OAEP на открытый ключ хоста. В базе — хеш кода (как у
  любого кода chat-pair) и шифротекст; в ответах, аудите, событиях и журнале
  кода нет. Расшифровать может только владелец закрытого ключа — хост.
* **Заказ — бронь слота.** Новый заказ (и переоткрытие) хост получает, только
  пока незакрытых заказов меньше ``executor_hosts.slots`` (#2107): ``_book``
  считает их под замком записи, а триггеры базы отказывают во вставке и
  переоткрытии сверх предела, поэтому два нажатия с двух соединений за
  последнее место не пройдут оба. Снижение ``slots`` открытые заказы не
  закрывает: занятость может быть выше предела, пока они не закроются. Бронь непрерывна от выпуска кода до закрытия, в том числе между
  выкупом и захватом задачи.
* **Любое закрытие, кроме сдачи, сжигает код.** Выкуп кода хоста проверяет в
  той же операции, что его заказ не закрыт и хост включён
  (``chat_pair._SLOT_ORDER_GUARD``), поэтому поздний выкуп старого кода
  невозможен, а гонка закрытия с выкупом имеет ровно один исход.

Ни одна функция записи здесь не делает commit внутри: коммитит владелец
транзакции (``write_transaction`` или обработчик).
"""

from __future__ import annotations

import base64
import itertools
import logging
import re
from dataclasses import dataclass
from typing import Any, Final

import aiosqlite
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub.auth import SLOT_RUNNER_USER_PREFIX
from hub.db import fetchall, write_transaction
from hub.models import _SLOT_NAME
from hub.services import admin as admin_svc
from hub.services import (
    chat_pair,
    executor_triage,
    host_redeem,
    launch_feedback,
    memory_wait,
    order_deadline,
    owner_minutes,
    project_policy,
)

log = logging.getLogger("hub.services.executor_orders")

#: Подстановка кода в тексте заказа: хост меняет её на расшифрованный код.
PAIR_CODE_PLACEHOLDER: Final[str] = "{{PAIR_CODE}}"

STATE_PENDING = "pending"
STATE_DELIVERED = "delivered"
STATE_WAITING_MEMORY = memory_wait.STATE_WAITING_MEMORY  # #1972
STATE_REDEEMED = "redeemed"
STATE_CLOSED = "closed"
_UNREDEEMED = (STATE_PENDING, STATE_DELIVERED, STATE_WAITING_MEMORY)

#: Исходы прогона слота. ``finished`` берётся у облачного опроса.
RUN_OUTCOME_FINISHED = "finished"
RUN_OUTCOME_ABORTED = "aborted"
RUN_OUTCOME_EXPIRED = "expired"

CLOSE_SUBMITTED = "submitted"
CLOSE_EXPIRED = "expired"
CLOSE_SESSION_GONE = "session_gone"
CLOSE_HOST_DISABLED = "host_disabled"
CLOSE_RUN_FINISHED = "run_finished"
CLOSE_MEMORY_WAIT_TIMEOUT = memory_wait.CLOSE_TIMEOUT  # #1972

ACK_REDEEMED = "redeemed"
ACK_REDEEM_FAILED = "redeem_failed"
ACK_REDEEM_LOST = "redeem_lost"
ACK_RESULTS: Final[tuple[str, ...]] = (ACK_REDEEMED, ACK_REDEEM_FAILED, ACK_REDEEM_LOST)

REPORT_OUTCOMES: Final[tuple[str, ...]] = ("submitted", "rescued", "failed")
#: Предел поля note итога прогона (#1712 AC-11).
REPORT_NOTE_MAX: Final[int] = 500

#: Минимум ключа хоста: RSA-3072 (PEM SPKI).
MIN_KEY_BITS: Final[int] = 3072
HOST_KEY_DAYS: Final[int] = 90
#: #2107: незакрытых заказов на хост одновременно — от 1 (по умолчанию) до 4.
HOST_SLOTS_MIN: Final[int] = 1
HOST_SLOTS_MAX: Final[int] = 4

EVENT_ORDER_CLOSED = "executor_order_closed"

REASON_NO_HOST = "хост исполнителя не зарегистрирован"
REASON_HOST_OFF = "хост исполнителя отключён"
REASON_SLOT_BUSY = "слот занят"
REASON_NOT_OPEN = "задача не open"
REASON_OTHER_PROJECT = "задача другого проекта"
REASON_TARGET = "цель запуска: cloud или slot:<имя хоста>"


class HostError(Exception):
    """Отказ операции над хостом: код ответа и текст для человека."""

    def __init__(self, status: int, detail: str | dict[str, str]) -> None:
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------------------
# Шифрование кода
# ---------------------------------------------------------------------------


def load_host_public_key(pem: str) -> rsa.RSAPublicKey:
    """Открытый ключ хоста: RSA не короче 3072 бит, PEM SPKI."""
    try:
        key = serialization.load_pem_public_key((pem or "").encode())
    except Exception as exc:  # noqa: BLE001 - любой разбор отказывает одинаково
        raise HostError(422, "public_key_pem: не PEM открытого ключа (SPKI)") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise HostError(422, "public_key_pem: нужен ключ RSA")
    if key.key_size < MIN_KEY_BITS:
        raise HostError(
            422,
            f"public_key_pem: RSA не короче {MIN_KEY_BITS} бит, получено "
            f"{key.key_size}",
        )
    return key


def encrypt_code(public_key_pem: str, code: str) -> str:
    """RSA-OAEP-SHA256 на ключ хоста, base64url без заполнения."""
    key = load_host_public_key(public_key_pem)
    sealed = key.encrypt(
        code.encode(),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.urlsafe_b64encode(sealed).decode().rstrip("=")


# ---------------------------------------------------------------------------
# Реестр хостов
# ---------------------------------------------------------------------------


def host_username(name: str) -> str:
    return f"{SLOT_RUNNER_USER_PREFIX}{name}"


def _host_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"],
        "principal_id": row["principal_id"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "disabled_at": row["disabled_at"],
        "disabled_by": row["disabled_by"],
        "slots": int(row["slots"]) if "slots" in row.keys() else 1,
    }


async def get_host(db: aiosqlite.Connection, name: str) -> dict[str, Any] | None:
    rows = await fetchall(db, "SELECT * FROM executor_hosts WHERE name = ?", (name,))
    return dict(rows[0]) if rows else None


async def list_hosts(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    rows = await fetchall(db, "SELECT * FROM executor_hosts ORDER BY name")
    return [_host_view(dict(r)) for r in rows]


async def enabled_host_names(db: aiosqlite.Connection) -> list[str]:
    rows = await fetchall(
        db,
        "SELECT h.name FROM executor_hosts h JOIN principals p "
        "ON p.id = h.principal_id WHERE h.enabled = 1 AND p.status = 'active' "
        "ORDER BY h.name",
    )
    return [str(r[0]) for r in rows]


async def host_of_principal(db: aiosqlite.Connection, principal_id: int | None) -> str:
    """Имя хоста, чей это принципал; пусто — не принципал хоста."""
    if principal_id is None:
        return ""
    rows = await fetchall(
        db,
        "SELECT name FROM executor_hosts WHERE principal_id = ? AND enabled = 1",
        (int(principal_id),),
    )
    return str(rows[0][0]) if rows else ""


async def register_host(
    db: aiosqlite.Connection,
    *,
    name: str,
    public_key_pem: str,
    created_by: int | None,
    expires_days: int = HOST_KEY_DAYS,
) -> dict[str, Any]:
    """Завести хост: принципал slot_runner, ключ со сроком, запись реестра.

    Одна транзакция: хост без ключа или принципал без записи реестра не
    остаются. Ключ API виден ТОЛЬКО в этом ответе.
    """
    name = (name or "").strip().lower()
    if not _SLOT_NAME.fullmatch(name):
        raise HostError(
            422,
            "name: строчные латинские буквы, цифры и дефис, не длиннее 32 символов",
        )
    load_host_public_key(public_key_pem)
    async with write_transaction(db):
        if await get_host(db, name) is not None:
            raise HostError(409, f"хост {name!r} уже есть; отключённое имя не занимают")
        taken = await fetchall(
            db, "SELECT 1 FROM principals WHERE username = ?", (host_username(name),)
        )
        if taken:
            raise HostError(409, f"принципал {host_username(name)!r} уже есть")
        principal = await admin_svc.create_principal(
            db,
            kind="service",
            username=host_username(name),
            display_name=f"Хост исполнителя {name}",
            notes="создан регистрацией хоста исполнителя (#1712)",
            created_by=created_by,
            role_slug="slot_runner",
            commit=False,
        )
        key = await admin_svc.create_api_key(
            db,
            int(principal["id"]),
            name=f"executor host {name}",
            expires_days=int(expires_days),
            created_by=created_by,
            commit=False,
        )
        await db.execute(
            "INSERT INTO executor_hosts (name, principal_id, public_key_pem, "
            "created_by) VALUES (?, ?, ?, ?)",
            (name, int(principal["id"]), public_key_pem.strip() + "\n", created_by),
        )
        await admin_svc.write_audit(
            db,
            actor_id=created_by,
            action="executor_host_created",
            target_type="executor_host",
            target_id=name,
            summary=f"executor host {name} registered, key valid {expires_days} days",
            commit=False,
        )
    host = await get_host(db, name)
    if host is None:
        raise HostError(500, f"хост {name!r} не записан")
    return {
        **_host_view(host),
        "api_key": key["plaintext_key"],
        "key_prefix": key["key_prefix"],
        "key_expires_at": key["expires_at"],
    }


async def disable_host(
    db: aiosqlite.Connection, name: str, *, by: int | None
) -> dict[str, Any]:
    """Отключить хост одной транзакцией: ключ, коды, заказы, сессии.

    Порядок внутри не важен для исхода, важно, что всё или ничего: выкуп кода
    хоста берёт ту же блокировку записи, поэтому параллельный выкуп случается
    либо до отключения (и его сессию отзывает это же отключение), либо получает
    отказ.
    """
    async with write_transaction(db):
        host = await get_host(db, name)
        if host is None:
            raise HostError(404, f"хост {name!r} не зарегистрирован")
        if not host["enabled"]:
            return _host_view(host)
        await db.execute(
            "UPDATE executor_hosts SET enabled = 0, disabled_at = datetime('now'), "
            "disabled_by = ? WHERE name = ?",
            (by, name),
        )
        await admin_svc.disable_principal(db, int(host["principal_id"]), commit=False)
        await db.execute(
            "DELETE FROM chat_pair_codes WHERE channel = 'slot' AND slot = ? "
            "AND redeemed_at IS NULL",
            (name,),
        )
        await db.execute(
            "UPDATE chat_pair_sessions SET revoked_at = datetime('now') "
            "WHERE channel = 'slot' AND slot = ? AND revoked_at IS NULL",
            (name,),
        )
        for order in await _unclosed(db, host=name):
            await close_order(db, order, CLOSE_HOST_DISABLED, RUN_OUTCOME_ABORTED)
        await admin_svc.write_audit(
            db,
            actor_id=by,
            action="executor_host_disabled",
            target_type="executor_host",
            target_id=name,
            summary=f"executor host {name} disabled: key revoked, codes burnt, "
            "orders closed, sessions revoked",
            commit=False,
        )
    updated = await get_host(db, name)
    if updated is None:
        raise HostError(500, f"хост {name!r} исчез при отключении")
    return _host_view(updated)


# ---------------------------------------------------------------------------
# Закрытие заказа
# ---------------------------------------------------------------------------


async def _unclosed(
    db: aiosqlite.Connection, *, host: str | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM executor_orders WHERE state <> 'closed'"
    params: tuple[Any, ...] = ()
    if host is not None:
        sql += " AND host = ?"
        params = (host,)
    return [dict(r) for r in await fetchall(db, sql + " ORDER BY id", params)]


async def _live_implementer_session(db: aiosqlite.Connection, task_id: int) -> bool:
    rows = await fetchall(
        db,
        "SELECT 1 FROM chat_pair_sessions WHERE kind = 'implementer' "
        "AND bound_task_id = ? AND revoked_at IS NULL "
        "AND expires_at > datetime('now')",
        (int(task_id),),
    )
    return bool(rows)


async def _task_is_this_runs(db: aiosqlite.Connection, order: dict[str, Any]) -> str:
    """Статус задачи, если она всё ещё в руках ЭТОГО прогона; пусто — нет.

    Привязка — четыре условия сразу: сессия этого прогона, то же поколение,
    статус «в работе» и ТЕКУЩИЙ держатель — принципал сессий этого прогона.
    Историческая сессия держателем не считается: задачу, которую после неё
    взял другой агент (отпустили и взяли снова), закрытие заказа не трогает —
    без этого отключение старого хоста снимало бы чужой захват.
    """
    task = await repo.get_task(db, int(order["task_id"]))
    if task is None:
        return ""
    row = dict(task)
    status = str(row.get("status") or "")
    if status not in ("claimed", "running"):
        return ""
    if int(row.get("submission_generation") or 0) + 1 != int(order["generation"]):
        return ""
    holder = row.get("implementer_principal_id")
    if holder is None:
        return ""
    bound = await fetchall(
        db,
        "SELECT 1 FROM chat_pair_sessions WHERE executor_run_id = ? "
        "AND bound_task_id = ? AND principal_id = ?",
        (int(order["executor_run_id"]), int(order["task_id"]), int(holder)),
    )
    return status if bound else ""


async def _return_task(
    db: aiosqlite.Connection, order: dict[str, Any], note: str = ""
) -> bool:
    status = await _task_is_this_runs(db, order)
    if not status or await _live_implementer_session(db, int(order["task_id"])):
        return False
    holders = await fetchall(
        db,
        "SELECT DISTINCT principal_id FROM chat_pair_sessions "
        "WHERE executor_run_id = ? AND bound_task_id = ?",
        (int(order["executor_run_id"]), int(order["task_id"])),
    )
    return await chat_pair.return_task_to_open(
        db,
        int(order["task_id"]),
        holders=frozenset(int(r[0]) for r in holders),
        session_runs=frozenset({int(order["executor_run_id"])}),
        from_status=status,
        note=note
        or f"Заказ хосту {order['host']} закрыт, исполнитель потерян; "
        "задача возвращена в open.",
        event_kind=EVENT_ORDER_CLOSED,
    )


async def close_order(
    db: aiosqlite.Connection,
    order: dict[str, Any],
    reason: str,
    run_outcome: str,
    *,
    only_states: tuple[str, ...] | None = None,
    return_task: bool = True,
    final_text: str = "",
) -> bool:
    """Закрыть заказ; True — закрыл именно этот вызов. Коммит — за вызывающим.

    Условный UPDATE по состоянию — единственная точка гонки с выкупом кода:
    кто успел, тот и исход. Закрывший сжигает непотраченный код, закрывает
    прогон и (если задача ещё у этого прогона и живой сессии нет) возвращает её
    в open. Сжигание — по ЛЮБОМУ закрытию, и сдача не исключение: у потраченного
    кода ``redeemed_at`` уже стоит и строку DELETE не трогает, а у непотраченного
    после сдачи другим путём применения не остаётся.
    """
    # #1817: фактический исход на момент закрытия — до снятия захвата, который
    # уберёт держателя задачи: сдала ли задачу прогон ЭТОГО заказа. Сдача сильнее
    # любой иной причины (в том числе слова хоста failed до закрытия): причина
    # закрытия и исход прогона записываются каноническими «submitted/finished»,
    # слово хоста остаётся отдельно в report_*. Иначе после снятия захвата ответ
    # GET заказа и плашка перевернулись бы на «упал», а k8 объявил бы ложный rescued.
    landed = reason == CLOSE_SUBMITTED or await launch_feedback.landed_by_order(
        db, order
    )
    if landed:
        reason, run_outcome = CLOSE_SUBMITTED, RUN_OUTCOME_FINISHED
    states = only_states or (*_UNREDEEMED, STATE_REDEEMED)
    marks = ", ".join("?" for _ in states)
    cursor = await db.execute(
        "UPDATE executor_orders SET state = 'closed', closed_at = datetime('now'), "
        "close_reason = ?, code_ciphertext = '' "
        f"WHERE id = ? AND state IN ({marks})",  # nosec B608 - placeholders only
        (reason, int(order["id"]), *states),
    )
    if not cursor.rowcount:
        return False
    if order.get("pair_code_id") is not None:
        await db.execute(
            "DELETE FROM chat_pair_codes WHERE id = ? AND redeemed_at IS NULL",
            (int(order["pair_code_id"]),),
        )
    # #1825: закрытый заказ не оставляет живой токен, который ни разу не
    # предъявлен: слот свободен, а исполнитель с ним мог бы встать на задачу.
    # Использованный токен здесь не трогается: его снимают пути, знающие
    # причину (сдача, итог failed/rescued, смерть сессии, отключение хоста).
    await db.execute(
        "UPDATE chat_pair_sessions SET revoked_at = datetime('now') "
        "WHERE executor_run_id = ? AND revoked_at IS NULL AND first_used_at IS NULL",
        (int(order["executor_run_id"]),),
    )
    await db.execute(
        "UPDATE executor_runs SET outcome = ?, reason = ?, "
        "finished_at = datetime('now'), polled_at = datetime('now'), "
        "duration_ms = CAST(ROUND((julianday('now') - julianday(started_at)) "
        "* 86400000) AS INTEGER) "
        "WHERE id = ? AND outcome = 'running'",
        (run_outcome, f"заказ хосту закрыт: {reason}", int(order["executor_run_id"])),
    )
    if reason == CLOSE_SUBMITTED:
        # #1783: принятая сдача слота снимает захват её прогона в этой же
        # транзакции — сессии отозваны, слот закрыт, статус review не тронут.
        from hub.services import run_claim

        await run_claim.release_after_run_closed(
            db, int(order["executor_run_id"]), why=reason
        )
    # #1817: итоговую запись в ленту пишет ТОЛЬКО закрытие заказа, один раз, в
    # транзакции перехода в closed (условный UPDATE выше пропускает один вызов).
    # Исход — фактический на момент закрытия: прогон заказа сдал задачу → сдача,
    # иначе причина закрытия (сообщение хоста — её уточнение). Report хоста, возврат
    # задачи и снятие захвата прогона этого заказа своей записи не пишут.
    if landed:
        task_row = await repo.get_task(db, int(order["task_id"]))
        sha = (dict(task_row).get("submission_sha") if task_row else "") or order.get(
            "report_sha"
        )
        text = (
            f"Заказ #{order['id']} хосту {order['host']} (сдача {order['generation']}) "
            f"закрыт: {launch_feedback.CLOSE_REASON_TEXT[CLOSE_SUBMITTED]}"
            + (f"; sha {sha}." if sha else ".")
        )
    else:
        text = final_text or launch_feedback.close_text(order, reason)
    if return_task:
        await _return_task(db, order)
    await admin_svc.write_audit(
        db,
        actor_id=None,
        action="executor_order_closed",
        target_type="task",
        target_id=str(int(order["task_id"])),
        summary=f"executor order #{order['id']} of host {order['host']} closed: "
        f"{reason}",
        commit=False,
    )
    await repo.add_task_update(db, int(order["task_id"]), "hub", "status", text)
    if reason == CLOSE_MEMORY_WAIT_TIMEOUT:  # #1972: сигнал владельцу, один раз
        await memory_wait.notify_timeout(db, order)
    await db.execute(
        "UPDATE executor_orders SET final_entry_at = datetime('now') WHERE id = ?",
        (int(order["id"]),),
    )
    return True


async def close_for_released_task(db: aiosqlite.Connection, task_id: int) -> int:
    """Reaper вернул задачу в open: её выкупленный заказ закрывается тут же."""
    rows = await fetchall(
        db,
        "SELECT * FROM executor_orders WHERE task_id = ? AND state = 'redeemed'",
        (int(task_id),),
    )
    closed = 0
    for raw in rows:
        if await close_order(
            db,
            dict(raw),
            CLOSE_SESSION_GONE,
            RUN_OUTCOME_ABORTED,
            only_states=(STATE_REDEEMED,),
            return_task=False,
        ):
            closed += 1
    return closed


async def _has_live_run_session(db: aiosqlite.Connection, run_id: int) -> bool:
    rows = await fetchall(
        db,
        "SELECT 1 FROM chat_pair_sessions WHERE executor_run_id = ? "
        "AND revoked_at IS NULL AND expires_at > datetime('now')",
        (int(run_id),),
    )
    return bool(rows)


async def _settled_reason(
    db: aiosqlite.Connection, order: dict[str, Any]
) -> tuple[str, str, tuple[str, ...] | None]:
    """(причина, исход прогона, допустимые состояния) — или пусто, если держится."""
    task = await repo.get_task(db, int(order["task_id"]))
    # #1831: сдача засчитывается заказу, только если задачу держал ЕГО прогон:
    # пересдача облаком или ручным pair того же поколения заказу не принадлежит.
    landed = (
        task is not None
        and int(dict(task).get("submission_generation") or 0)
        >= int(order["generation"])
        and dict(task).get("executor_run_id") == int(order["executor_run_id"])
    )
    if landed:
        return CLOSE_SUBMITTED, RUN_OUTCOME_FINISHED, None
    run = await repo.get_executor_run(db, int(order["executor_run_id"]))
    if run is not None and dict(run)["outcome"] != "running":
        return CLOSE_RUN_FINISHED, dict(run)["outcome"], None
    if order["state"] == STATE_WAITING_MEMORY:
        # #1972: предел ожидания сильнее обычного срока; оба - одним снимком времени.
        what = await memory_wait.verdict(db, order)
        if what == memory_wait.VERDICT_TIMEOUT:
            return CLOSE_MEMORY_WAIT_TIMEOUT, RUN_OUTCOME_EXPIRED, (order["state"],)
        if what == memory_wait.VERDICT_EXPIRED:
            return CLOSE_EXPIRED, RUN_OUTCOME_EXPIRED, _UNREDEEMED
        return "", "", None
    if order["state"] in _UNREDEEMED:
        gone = await fetchall(
            db,
            "SELECT 1 WHERE ? <= datetime('now')",
            (order["expires_at"],),
        )
        if gone:
            return CLOSE_EXPIRED, RUN_OUTCOME_EXPIRED, _UNREDEEMED
        return "", "", None
    if not await _has_live_run_session(db, int(order["executor_run_id"])):
        return CLOSE_SESSION_GONE, RUN_OUTCOME_ABORTED, (STATE_REDEEMED,)
    return "", "", None


async def settle(db: aiosqlite.Connection, host: str | None = None) -> int:
    """Закрыть заказы, чьё время вышло или чья работа кончилась; сколько закрыто.

    Одна функция на все пути: её зовут запуск (бронь не должна висеть на
    сданной задаче), выдача заказов хосту и проход поллера. Так сдача
    освобождает слот сразу, а не к следующему проходу. Коммит — за
    вызывающим.
    """
    closed = 0
    for order in await _unclosed(db, host=host):
        reason, outcome, states = await _settled_reason(db, order)
        if reason and await close_order(db, order, reason, outcome, only_states=states):
            closed += 1
    return closed


async def sweep_executor_orders(db: aiosqlite.Connection) -> int:
    """Проход поллера: закрыть всё, что уже не держится (п.9 постановки)."""
    async with write_transaction(db):
        closed = await settle(db)
    try:
        # #1942: рекомендации разбора — отдельной транзакцией: их сбой не должен
        # отменить закрытие заказов выше и не роняет проход.
        await executor_triage.sweep(db)
    except Exception:  # noqa: BLE001 - разбор в тени не ломает проход поллера
        log.exception("executor triage sweep failed")
    return closed


# ---------------------------------------------------------------------------
# Выпуск заказа
# ---------------------------------------------------------------------------


def parse_target(target: str) -> str:
    """``slot:<имя>`` → имя хоста; ``cloud`` и всё прочее — пусто."""
    if target.startswith("slot:") and _SLOT_NAME.fullmatch(target[5:]):
        return target[5:]
    return ""


def _refused(reason: str, task_id: int | None = None) -> Any:
    from hub.services.executor_launch import LaunchResult

    return LaunchResult(False, reason, task_id)


async def _enabled_host(db: aiosqlite.Connection, name: str) -> dict[str, Any] | str:
    """Хост, если он включён, иначе — причина отказа."""
    host = await get_host(db, name)
    if host is None:
        return f"{REASON_NO_HOST}: {name}"
    principal = await admin_svc.get_principal(db, int(host["principal_id"]))
    if not host["enabled"] or not principal or principal["status"] != "active":
        return f"{REASON_HOST_OFF}: {name}"
    return host


async def _chosen_task(
    db: aiosqlite.Connection, project: Any, task_id: int | None
) -> tuple[dict[str, Any] | None, str]:
    """Задача запуска: названная человеком или голова очереди; иначе причина."""
    from hub.services import executor_launch as el

    if task_id is None:
        picked, why = await el._candidate(db, project)
        if picked is None:
            return None, why
        task_id = picked
    row = await repo.get_task(db, int(task_id))
    if row is None:
        return None, f"задача #{task_id} не найдена"
    task = dict(row)
    owner = await repo.resolve_project_for_task(db, int(task_id))
    if owner is None or int(owner["id"]) != int(project["id"]):
        return None, f"{REASON_OTHER_PROJECT}: #{task_id}"
    if task["status"] not in ("open", "running"):
        return None, f"{REASON_NOT_OPEN}: #{task_id} в статусе {task['status']}"
    return task, ""


async def _task_refusal(db: aiosqlite.Connection, task: dict[str, Any]) -> str:
    """Те же двери-отказы, что у облачного запуска: состояние, не лист, прогон."""
    from hub.services import executor_launch as el
    from hub.services.result_kind import automation_not_applicable

    task_id = int(task["id"])
    if automation_not_applicable(task):
        return f"{el.REASON_STATE_TASK}: #{task_id}"
    not_leaf = await el._not_leaf_refusal(db, task)
    if not_leaf is not None:
        return not_leaf.reason
    # Живая сессия implementer на задаче (ручной код выкуплен раньше) — второй
    # исполнитель на той же ветке: заказ не выдаётся, выкуп кода был бы отказан
    # (#1783, k8), и обе стороны ждали бы друг друга до TTL.
    if await fetchall(
        db,
        "SELECT 1 FROM chat_pair_sessions s WHERE s.kind = 'implementer' "
        "AND s.bound_task_id = ? AND s.revoked_at IS NULL "
        "AND s.claimed_session IS NULL "
        "AND s.expires_at > datetime('now') AND NOT EXISTS ("
        "  SELECT 1 FROM executor_runs r WHERE r.id = s.executor_run_id "
        "  AND (r.outcome <> 'running' OR r.cost_wait_since IS NOT NULL))",
        (task_id,),
    ):
        return f"{el.REASON_ALREADY_RUNNING} #{task_id} (живая сессия implementer)"
    return await el._live_run(db, task_id)


async def _task_busy(db: aiosqlite.Connection, task_id: int) -> str:
    """У задачи уже есть незакрытый заказ (любого хоста): повтор нажатия — отказ с номером."""
    rows = await fetchall(
        db,
        "SELECT id, host, state FROM executor_orders WHERE task_id = ? "
        "AND state <> 'closed' ORDER BY id DESC LIMIT 1",
        (int(task_id),),
    )
    if not rows:
        return ""
    row = dict(rows[0])
    from hub.services.executor_launch import REASON_ALREADY_RUNNING

    return (
        f"{REASON_ALREADY_RUNNING} #{task_id} (слот {row['host']}, "
        f"заказ #{row['id']}, {row['state']})"
    )


async def _slot_busy(db: aiosqlite.Connection, host: str, slots: int = 1) -> str:
    """Отказ, если незакрытых заказов хоста уже ``slots`` (#2107); иначе пусто."""
    rows = await fetchall(
        db,
        "SELECT id, task_id, state FROM executor_orders WHERE host = ? "
        "AND state <> 'closed' ORDER BY id",
        (host,),
    )
    if len(rows) < max(int(slots), 1):
        return ""
    held = ", ".join(f"#{r['task_id']} ({r['state']}, заказ #{r['id']})" for r in rows)
    if len(rows) == 1 and int(slots) <= 1:  # прежний текст для хоста с одним слотом
        row = dict(rows[0])
        return (
            f"{REASON_SLOT_BUSY}: {host} держит задачу #{row['task_id']} "
            f"({row['state']}), заказ #{row['id']}"
        )
    return f"{REASON_SLOT_BUSY}: {host} {len(rows)}/{int(slots)}, задачи {held}"


async def _order_text(
    db: aiosqlite.Connection, project: Any, task: dict[str, Any], skill: Any
) -> tuple[str, str]:
    """(текст заказа с плейсхолдером кода, ссылка старта).

    Тот же промпт, что у облачного пути, но код — подстановка, а не значение:
    текст лежит в базе и уходит хосту открытым.
    """
    from hub.services import executor_launch as el
    from hub.services.review_dispatch import instance_base_url

    base = project_policy.base_branch_of(project)
    extra = ""
    pending = await el._current_findings(db, task)
    if pending:
        extra = el._findings_block(pending)
    text = el._prompt(
        task,
        base,
        PAIR_CODE_PLACEHOLDER,
        instance_base_url().rstrip("/"),
        str(skill["content"]),
        extra,
    )
    return text, el._pushed_branch(task) or base


async def _code_row(db: aiosqlite.Connection, code: str) -> dict[str, Any]:
    rows = await fetchall(
        db,
        "SELECT id, expires_at, code_hash FROM chat_pair_codes WHERE code_hash = ?",
        (chat_pair.hash_pair_code(chat_pair.normalize_pair_code(code)),),
    )
    return dict(rows[0])


@dataclass
class _Booking:
    task: dict[str, Any]
    host: dict[str, Any]
    skill: Any
    #: Круг починки running-задачи (#1998): возврат в open — в SAVEPOINT выпуска.
    returning: bool = False


async def _book(
    db: aiosqlite.Connection,
    project: Any,
    host_name: str,
    task_id: int | None,
    skill: Any,
) -> _Booking | str:
    """Проверки запуска под write-блокировкой; ``str`` — причина отказа."""
    await settle(db, host=host_name)
    host = await _enabled_host(db, host_name)
    if isinstance(host, str):
        return host
    if task_id is not None:
        busy = await _task_busy(db, int(task_id))  # номер заказа важнее статуса задачи
        if busy:
            return busy
    task, why = await _chosen_task(db, project, task_id)
    if task is None:
        return why
    refusal = (
        await _task_busy(db, int(task["id"]))
        or await _task_refusal(db, task)
        or await _slot_busy(db, host_name, int(host.get("slots") or 1))
    )
    if refusal:
        return refusal
    if task["status"] == "running":
        # Круг починки (#1998): то же правило допуска, что у облака. Все отказы —
        # здесь, ДО возврата: строковый отказ после него закоммитил бы open.
        from hub.services import rework_admission

        why = await rework_admission.refusal(db, task)
        if why:
            return f"{REASON_NOT_OPEN}: #{task['id']} в статусе running, {why}"
    return _Booking(task, host, skill, returning=task["status"] == "running")


async def launch_on_slot(
    db: aiosqlite.Connection,
    project: Any,
    *,
    host: str,
    task_id: int | None,
    issuer_principal_id: int,
    issuer_human: bool = False,
) -> Any:
    """Запуск исполнителя на слот хоста по нажатию человека (#1712).

    Проект в режиме manual; облако (github, наблюдение прав токена, модель)
    слоту не нужно. Прогон, код и заказ — ОДНА write-транзакция без внутренних
    commit: любой сбой до вставки заказа откатывает и код, и прогон.
    """
    from hub.services import executor_launch as el
    from hub.services.executor_launch import LaunchResult

    if el.launch_mode_of(project_policy.gate_policy_of(project)) != el.LAUNCH_MANUAL:
        return _refused(el.REASON_OFF)
    skill = await repo.get_active_skill(db, el.DISCIPLINE_SKILL)
    if skill is None:
        return _refused(f"{el.REASON_NO_SKILL} ({el.DISCIPLINE_SKILL})")
    if await chat_pair.get_acting_agent(db) is None:
        return _refused(f"{el.REASON_NO_ACTING_AGENT} (CHAT_PAIR_AGENT)")
    try:
        async with write_transaction(db):
            booked = await _book(db, project, host, task_id, skill)
            if isinstance(booked, str):
                return _refused(booked, task_id)
            order_id, row_id = await _issue_atomically(
                db,
                project,
                booked,
                issuer_principal_id=issuer_principal_id,
                issuer_human=issuer_human,
            )
    except aiosqlite.IntegrityError:
        # Второе соединение заняло последнее место раньше: триггер слотов хоста.
        return _refused(f"{REASON_SLOT_BUSY}: {host}", task_id)
    except HostError as exc:
        # Сохранённый ключ хоста не годится: код не выписан, ничего не осталось.
        return _refused(f"{REASON_HOST_OFF}: {exc.detail}", task_id)
    except HTTPException as exc:
        # Задача ушла из running между проверкой и возвратом: откат уже сделан.
        return _refused(f"{REASON_NOT_OPEN}: {exc.detail}", task_id)
    return LaunchResult(
        True, "", int(booked.task["id"]), row_id=row_id, order_id=order_id, host=host
    )


async def _issue_atomically(
    db: aiosqlite.Connection,
    project: Any,
    booked: _Booking,
    *,
    issuer_principal_id: int,
    issuer_human: bool = False,
) -> tuple[int, int]:
    """``_issue`` под SAVEPOINT: сбой откатывает ЕГО записи, а не только свои.

    ``write_transaction`` вложенного вызова не откатывает ничего — владелец
    транзакции снаружи, и если он закоммитит после отказа, который мы вернули
    значением, выжили бы прогон и код без заказа. Точка сохранения снимает
    это: что бы ни стояло снаружи, до заказа ни кода, ни прогона не остаётся.

    Круг починки running-задачи (#1998): возврат в open — ВНУТРИ этой же точки,
    поэтому сбой выпуска откатывает и его, а не оставляет open без заказа.
    """
    await db.execute("SAVEPOINT executor_slot_issue")
    try:
        if booked.returning:
            await _return_for_repair(db, booked, issuer_principal_id)
        result = await _issue(
            db,
            project,
            booked,
            issuer_principal_id=issuer_principal_id,
            issuer_human=issuer_human,
        )
    except BaseException:
        await db.execute("ROLLBACK TO executor_slot_issue")
        await db.execute("RELEASE executor_slot_issue")
        raise
    await db.execute("RELEASE executor_slot_issue")
    return result


async def _return_for_repair(
    db: aiosqlite.Connection, booked: _Booking, issuer_principal_id: int
) -> None:
    """running → open с записью о том, кто нажал и на какой слот (без commit)."""
    from hub.services import rework_admission, return_running

    who = await admin_svc.get_principal(db, int(issuer_principal_id))
    name = str(who["username"]) if who else f"principal #{issuer_principal_id}"
    await return_running.apply_return_to_open(
        db,
        booked.task,
        actor=name,
        reason=rework_admission.return_reason(
            booked.task,
            where=f"на слоте {booked.host['name']}",
            issue="#1998",
            who=name,
        ),
    )


async def _issue(
    db: aiosqlite.Connection,
    project: Any,
    booked: _Booking,
    *,
    issuer_principal_id: int,
    issuer_human: bool = False,
) -> tuple[int, int]:
    """Прогон, код, шифротекст и заказ — без commit; (id заказа, id прогона)."""
    task, host = booked.task, booked.host
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0) + 1
    text, starting_ref = await _order_text(db, project, task, booked.skill)
    row_id = await repo.create_executor_run(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id="",
        run_id="",
        model="",
        channel="slot",
        slot=host["name"],
        launched_by=issuer_principal_id,
    )
    await repo.set_executor_run_starting_ref(
        db, row_id, starting_ref, int(booked.skill["version"])
    )
    code, _ttl = await chat_pair.issue_run_code(
        db,
        task_id,
        generation,
        issued_by_principal_id=issuer_principal_id,
        executor_run_id=row_id,
        channel="slot",
        slot=host["name"],
        commit=False,
    )
    stored = await _code_row(db, code)
    run_seconds = order_deadline.run_seconds_for(task)  # #1960: снимок срока
    sealed = encrypt_code(host["public_key_pem"], code)
    del code
    cursor = await db.execute(
        "INSERT INTO executor_orders (host, task_id, generation, executor_run_id, "
        "pair_code_id, code_ciphertext, order_text, expires_at, code_hash, "
        "run_seconds, statement_generation, project_slug) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            host["name"],
            task_id,
            generation,
            row_id,
            stored["id"],
            sealed,
            text,
            stored["expires_at"],
            stored["code_hash"],
            run_seconds,
            # #1942: поколение ПОСТАНОВКИ на момент выпуска (не путать с generation —
            # поколением сдачи); по нему считается серия неудач одной постановки.
            int(task.get("statement_generation") or 0),
            # #2122: проект запуска фиксируется при выпуске и не пересчитывается.
            str(project["slug"]),
        ),
    )
    order_id = int(cursor.lastrowid or 0)
    # #1874: заказ принят - событие для учёта минут владельца, в той же транзакции.
    await owner_minutes.record_order_accepted(
        db,
        task_id,
        row_id,
        generation,
        issuer_principal_id,
        "launch",
        issuer_human,
        channel="slot",
        host=host["name"],
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        f"Исполнитель заказан хабом на слот {host['name']} по нажатию человека "
        f"(#1712): заказ #{order_id}, сдача {generation}, от {starting_ref}, "
        f"срок прогона {order_deadline.hours_text(run_seconds)}.",
    )
    return order_id, row_id


# ---------------------------------------------------------------------------
# Что видит хост
# ---------------------------------------------------------------------------


def _order_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "host": row["host"],
        "task_id": row["task_id"],
        "generation": row["generation"],
        "executor_run_id": row["executor_run_id"],
        "state": row["state"],
        "encryption": "RSA-OAEP-SHA256, base64url",
        "code_ciphertext": row["code_ciphertext"],
        "order_text": row["order_text"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "delivered_at": row["delivered_at"],
        "run_seconds": row["run_seconds"],
        # #2122: проект выпуска; NULL у заказов до миграции.
        "project": row["project_slug"],
    }


async def deliver_orders(db: aiosqlite.Connection, host: str) -> list[dict[str, Any]]:
    """Незакрытые невыкупленные заказы хоста; первая отдача ставит delivered.

    Повторная отдача до ack — тот же id: ответ мог потеряться по дороге, и хост
    дедуплицирует по id. Коммитит только то, что изменилось.
    """
    async with write_transaction(db):
        await settle(db, host=host)
        rows = [
            dict(r)
            for r in await fetchall(
                db,
                "SELECT * FROM executor_orders WHERE host = ? "
                f"AND state IN {memory_wait.UNREDEEMED_IN_SQL} ORDER BY id",  # nosec B608 - constant
                (host,),
            )
        ]
        for row in rows:
            if row["state"] != STATE_PENDING:
                continue
            cursor = await db.execute(
                "UPDATE executor_orders SET state = 'delivered', "
                "delivered_at = datetime('now') WHERE id = ? AND state = 'pending'",
                (row["id"],),
            )
            if cursor.rowcount:
                row["state"] = STATE_DELIVERED
                await admin_svc.write_audit(
                    db,
                    actor_id=None,
                    action="executor_order_delivered",
                    target_type="task",
                    target_id=str(row["task_id"]),
                    summary=f"executor order #{row['id']} delivered to host {host}",
                    commit=False,
                )
    return [_order_view(r) for r in rows]


async def _own_order(
    db: aiosqlite.Connection, host: str, order_id: int
) -> dict[str, Any] | None:
    rows = await fetchall(
        db,
        "SELECT * FROM executor_orders WHERE id = ? AND host = ?",
        (int(order_id), host),
    )
    return dict(rows[0]) if rows else None


_SQLITE_MAX_INT = 2**63 - 1
#: Статусы задачи в работе исполнителя: сданный заказ о них молчит (#1831).
_WORK_STATUSES = ("open", "claimed", "running")


async def _fields_hold_any_code(db: aiosqlite.Connection, text: str) -> bool:
    """Назван ли в тексте ЛЮБОЙ код хаба — по хешу, не только код заказа хоста.

    Ветку после закрытия заказа может задать ручной или облачный pair, и её слаг
    способен нести их код без префикса ``AH-``.
    """
    rows = await fetchall(
        db,
        "SELECT code_hash FROM chat_pair_codes UNION "
        "SELECT code_hash FROM executor_orders WHERE code_hash <> ''",
    )
    wanted = {str(r[0]) for r in rows}
    flat = re.sub(r"[^0-9A-Za-z]", "", text).upper()
    size = chat_pair.CODE_LENGTH
    return any(
        chat_pair.hash_pair_code(flat[i : i + size]) in wanted
        for i in range(max(0, len(flat) - size + 1))
    )


async def read_order_outcome(
    db: aiosqlite.Connection, host: str, order_id: int
) -> dict[str, Any] | None:
    """Итог заказа для хоста (#1831); None — чужой или нет такого.

    Только чтение, без кода, шифротекста, хеша и текста заказа: хост узнаёт,
    сдана ли работа, когда сессия прогона уже отозвана при сдаче.
    """
    if not 0 <= int(order_id) <= _SQLITE_MAX_INT:
        return None
    # ОДИН запрос = один снимок: заказ, «есть ли новее» и задача читаются вместе.
    # Два чтения оставляли окно, в которое успевает встать новый заказ.
    rows = await fetchall(
        db,
        "SELECT o.*, t.status AS t_status, t.submission_generation AS t_generation, "
        "t.executor_run_id AS t_holder, "
        "t.submission_sha AS t_sha, t.branch AS t_branch, t.pr_number AS t_pr, "
        "EXISTS (SELECT 1 FROM executor_orders n WHERE n.task_id = o.task_id "
        "AND n.id > o.id) AS newer "
        "FROM executor_orders o LEFT JOIN tasks t ON t.id = o.task_id "
        "WHERE o.id = ? AND o.host = ?",
        (int(order_id), host),
    )
    if not rows:
        return None
    row = dict(rows[0])
    # Сдачу сделал ЭТОТ прогон, если задачу держал он: заказ закрыт причиной
    # «submitted» (её ставит settle только при совпадении держателя) либо ещё
    # не закрыт, а держатель задачи — его прогон. Поколение одно и то же у
    # сдачи другого прогона (облако, ручной pair), поэтому оно не доказательство.
    generation = row["t_generation"] if row["t_generation"] is not None else 0
    attributed = row["close_reason"] == CLOSE_SUBMITTED or (
        row["state"] != STATE_CLOSED and row["t_holder"] == row["executor_run_id"]
    )
    foreign = generation >= int(row["generation"]) and not attributed
    # Закрытая сдачей заказ не говорит о задаче, ушедшей в новую работу (возврат
    # в работу, ручной pair-start): поля задачи тогда принадлежат другому исполнителю.
    moved_on = (
        row["close_reason"] == CLOSE_SUBMITTED and row["t_status"] in _WORK_STATUSES
    )
    superseded = (
        bool(row["newer"]) or generation > int(row["generation"]) or foreign or moved_on
    )
    submitted = not superseded and generation == int(row["generation"])
    branch = str(row["t_branch"] or "") if submitted else ""
    if branch and (
        any(shape.search(branch) for shape in _SECRET_SHAPES)
        or any(str(tok) and str(tok) in branch for tok in config.HUB_TOKENS)
        or await _fields_hold_any_code(db, branch)
    ):
        branch = (
            ""  # слаг ветки задаёт вызывающий pair-start: секрету в ответе не место
        )
    return {
        "id": row["id"],
        "host": row["host"],
        "state": row["state"],
        "close_reason": row["close_reason"],
        "task_id": row["task_id"],
        "generation": row["generation"],
        "superseded": superseded,
        "submitted": submitted,
        "task_status": None if superseded else row["t_status"],
        "submission_generation": generation if submitted else None,
        "submission_sha": (row["t_sha"] or "") if submitted else "",
        "branch": branch,
        "pr_number": row["t_pr"] if submitted else None,
        "report_outcome": row["report_outcome"],
    }


async def ack_order(
    db: aiosqlite.Connection, host: str, order_id: int, result: str
) -> dict[str, Any]:
    """Подтверждение хоста; ``None`` заказа — чужой или нет такого (403 выше).

    ``redeemed`` информативен: бронь не снимает, трату кода хаб сверяет сам.
    ``redeem_failed``/``redeem_lost`` закрывают заказ и сжигают код. Повтор
    идемпотентен.
    """
    if result not in ACK_RESULTS:
        raise HostError(422, f"result: один из {', '.join(ACK_RESULTS)}")
    async with write_transaction(db):
        order = await _own_order(db, host, order_id)
        if order is None:
            raise HostError(403, "заказ не принадлежит этому хосту")
        first = not order["ack_result"]
        if result != ACK_REDEEMED and await host_redeem.token_in_use(
            db, int(order["executor_run_id"])
        ):
            # #1825: токен уже предъявлен — исполнитель работает; «потерян» или
            # «не выкуплен» неверно, заказ и сессия остаются как были.
            raise HostError(
                409,
                {
                    "reason": host_redeem.REASON_IN_USE,
                    "message": f"токен заказа уже используется: {result} отклонён",
                },
            )
        if not first and order["ack_result"] != result:
            # Противоречащий повтор не переписывает слово хоста: поздний
            # redeem_failed после redeemed закрыл бы работающий прогон.
            raise HostError(
                409,
                f"заказ уже подтверждён как {order['ack_result']}: {result} отклонён",
            )
        if first:
            await db.execute(
                "UPDATE executor_orders SET ack_result = ?, "
                "acked_at = datetime('now') WHERE id = ?",
                (result, order["id"]),
            )
            await admin_svc.write_audit(
                db,
                actor_id=None,
                action="executor_order_acked",
                target_type="task",
                target_id=str(order["task_id"]),
                summary=f"executor order #{order['id']} acked by host {host}: {result}",
                commit=False,
            )
        if result != ACK_REDEEMED:
            await close_order(db, order, result, RUN_OUTCOME_ABORTED)
        fresh = await _own_order(db, host, order_id)
    if fresh is None:
        raise HostError(500, "заказ исчез при подтверждении")
    return {
        "id": fresh["id"],
        "state": fresh["state"],
        "close_reason": fresh["close_reason"],
        "ack_result": fresh["ack_result"],
        "duplicate": not first,
    }


#: Только у ``sk-`` есть граница: оно короткое и встречается внутри обычных слов
#: («task-refactoring-…» содержит «sk-refact…»). Остальные шаблоны либо
#: уникальны по префиксу (``ahcp_``, ``ghp_``), либо несут ``=``/``:`` и
#: значение; приставленная впереди буква их не обезвреживает («mypassword=…»,
#: «xghp_…» — всё ещё секрет).
_SECRET_SHAPES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"ahcp_|ochk_|ghp_|github_pat_", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{8}", re.IGNORECASE),
    re.compile(r"\bAH-?[0-9A-HJKMNP-TV-Z]{8}\b", re.IGNORECASE),
    re.compile(r"bearer\s+\S{8,}", re.IGNORECASE),
    re.compile(r"-----BEGIN"),
    re.compile(r"(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S{6,}", re.I),
)
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{32,}")
_HEX = re.compile(r"[0-9a-fA-F]+")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_RESCUE_NUMBER = re.compile(r"[0-9]{1,20}")


def note_refusal(note: str) -> str:
    """Почему поле note итога нельзя записать в ленту; пусто — можно."""
    if len(note) > REPORT_NOTE_MAX:
        return f"note: не длиннее {REPORT_NOTE_MAX} символов"
    if any(shape.search(note) for shape in _SECRET_SHAPES):
        return "note: похоже на код, токен или ключ — секретам в ленте не место"
    for token in _LONG_TOKEN.findall(note):
        if not _HEX.fullmatch(token):
            return "note: длинная строка похожа на токен"
    return ""


async def _fields_hold_the_code(
    db: aiosqlite.Connection, order: dict[str, Any], *fields: str
) -> bool:
    """Назван ли в полях итога код хоста — по хешу, а не по виду.

    Хаб кода не знает, но хранит хеши: кода этого заказа (потраченный живёт до
    очистки) и ВСЕХ ещё непотраченных кодов этого хоста — итог старого заказа
    не должен печатать в ленту живой код нового. Поля сводятся к буквам и
    цифрам (разделители, дефисы и регистр кода не прячут) и склеиваются во всех
    порядках: код, разорванный по полям (``branch`` и ``note``), находится тем
    же скользящим окном длины кода, что и целый. Видом (``AH-…``) это не
    поймать — код без префикса выглядит как любое слово.
    """
    rows = await fetchall(
        db,
        "SELECT code_hash FROM chat_pair_codes WHERE id = ? "
        "OR (channel = 'slot' AND slot = ? AND redeemed_at IS NULL) "
        # Хеши КАЖДОГО заказа хоста: сожжённый код (строки в chat_pair_codes уже
        # нет) узнаётся по хешу заказа, а не пропускается в ленту без префикса.
        "UNION SELECT code_hash FROM executor_orders "
        "WHERE host = ? AND code_hash <> ''",
        (int(order["pair_code_id"] or 0), order["host"], order["host"]),
    )
    wanted = {str(r[0]) for r in rows}
    if not wanted:
        return False
    size = chat_pair.CODE_LENGTH
    flat = [re.sub(r"[^0-9A-Za-z]", "", f).upper() for f in fields if f]
    for order_of_fields in itertools.permutations(flat):
        text = "".join(order_of_fields)
        for start in range(max(0, len(text) - size + 1)):
            if chat_pair.hash_pair_code(text[start : start + size]) in wanted:
                return True
    return False


def _check_report_fields(outcome: str, sha: str, branch: str, note: str) -> None:
    """Поля итога прогона до всякой записи: форма и секреты (422)."""
    if outcome not in REPORT_OUTCOMES:
        raise HostError(422, f"outcome: один из {', '.join(REPORT_OUTCOMES)}")
    if sha and not _SHA.fullmatch(sha):
        raise HostError(422, "sha: от 7 до 40 строчных шестнадцатеричных символов")
    why = note_refusal(note)
    if why:
        raise HostError(422, why)


async def _branch_refusal(
    db: aiosqlite.Connection, order: dict[str, Any], branch: str
) -> str:
    """Почему ветку нельзя записать в ленту; пусто — можно.

    Белый список, а не эвристика: ветка — это ветка ЭТОЙ задачи (``tasks.branch``)
    или ``task-<id задачи>/rescue-<цифры>``. Любая другая строка — не ветка, а
    свободный текст в ленте, и угадывать по виду, секрет она или нет, нельзя:
    токен не обязан выглядеть как токен.
    """
    if not branch:
        return ""
    if any(shape.search(branch) for shape in _SECRET_SHAPES):
        # Слаг ветки задаёт вызывающий pair-start: хранимая ветка тоже может
        # быть секретом по виду, и в ленту она не пойдёт.
        return "branch: похоже на токен или ключ"
    task_id = int(order["task_id"])
    row = await repo.get_task(db, task_id)
    own = str(dict(row).get("branch") or "") if row is not None else ""
    if own and branch == own:
        return ""
    prefix = f"task-{task_id}/rescue-"
    if branch.startswith(prefix) and _RESCUE_NUMBER.fullmatch(branch[len(prefix) :]):
        return ""
    return f"branch: только ветка задачи ({own or 'не названа'}) или {prefix}<цифры>"


def _check_reason_fields(outcome: str, reason_code: str, reason: str) -> str:
    """Код и текст причины report (#1817): форма и секреты; код старого клиента — other."""
    if reason_code and reason_code not in launch_feedback.REASON_CODES:
        raise HostError(
            422, f"reason_code: один из {', '.join(launch_feedback.REASON_CODES)}"
        )
    if len(reason) > launch_feedback.REASON_MAX:
        raise HostError(
            422, f"reason: не длиннее {launch_feedback.REASON_MAX} символов"
        )
    if reason and note_refusal(reason):
        raise HostError(422, note_refusal(reason).replace("note", "reason", 1))
    if any(str(tok) and str(tok) in reason for tok in config.HUB_TOKENS):
        raise HostError(422, "reason: совпадает с настроенным токеном хаба")
    if outcome != "submitted" and not reason_code:
        return "other"
    return reason_code


def _report_text(
    order: dict[str, Any],
    host: str,
    outcome: str,
    sha: str,
    branch: str,
    note: str,
    reason_code: str,
    reason: str,
) -> str:
    """Текст итоговой записи ленты по report хоста."""
    parts = [f"Итог прогона на хосте {host} (сдача {order['generation']}): {outcome}"]
    if sha:
        parts.append(f"sha {sha}")
    if branch:
        parts.append(f"ветка {branch}")
    if note:
        parts.append(f"заметка: {note}")
    if outcome in ("failed", "rescued") and reason_code:
        parts.append(f"причина: {launch_feedback.reason_code_text(reason_code)}")
    if reason:
        parts.append(f"сообщение хоста: {reason}")
    return "; ".join(parts) + "."


async def report_outcome(
    db: aiosqlite.Connection,
    host: str,
    order_id: int,
    *,
    outcome: str,
    sha: str = "",
    branch: str = "",
    note: str = "",
    principal_id: int | None = None,
    reason_code: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Итог прогона от хоста: сохраняется в заказе, статус задачи не меняется.

    В ленту report НЕ пишет (#1817): итоговую запись пишет закрытие заказа. Для
    failed/rescued report сам закрывает заказ, и его текст (исход, sha, ветка,
    заметка, причина) становится этой записью; submitted заказ не закрывает.
    Идемпотентно: тот же итог повторно — тот же ответ без второй записи;
    другой итог по тому же заказу — 409 (первое слово хоста не переписывается).
    Работает и на закрытом заказе: сдача закрывает его раньше, чем хост
    успевает сказать, чем кончился прогон.
    """
    _check_report_fields(outcome, sha, branch, note)
    reason_code = _check_reason_fields(outcome, reason_code, reason)
    async with write_transaction(db):
        order = await _own_order(db, host, order_id)
        if order is None:
            raise HostError(403, "заказ не принадлежит этому хосту")
        if not order["delivered_at"]:
            raise HostError(409, "заказ ещё не выдан хосту: итогу прогона рано")
        if order["report_outcome"]:
            same = (
                order["report_outcome"],
                order["report_sha"],
                order["report_branch"],
                order["report_reason_code"],
            ) == (
                outcome,
                sha,
                branch,
                reason_code,
            )
            if not same:
                raise HostError(
                    409,
                    f"итог по заказу уже записан: {order['report_outcome']}",
                )
            return {"id": order["id"], "outcome": outcome, "duplicate": True}
        if outcome in ("failed", "rescued"):
            # #1942: граница включения разбора закрепляется ДО закрытия заказа и вне
            # SAVEPOINT рекомендации: откат разбора её не уносит, а закрытие этого
            # заказа не может оказаться «историей» из-за ленивого создания.
            await executor_triage.pin_activation_safely(db)
        refusal = await _branch_refusal(db, order, branch)
        if refusal:
            raise HostError(422, refusal)
        if await _fields_hold_the_code(db, order, note, branch, sha, reason):
            raise HostError(422, "в полях итога код этого заказа")
        await db.execute(
            "UPDATE executor_orders SET report_outcome = ?, report_sha = ?, "
            "report_branch = ?, report_note = ?, report_reason_code = ?, "
            "report_reason = ?, reported_at = datetime('now') "
            "WHERE id = ? AND report_outcome = ''",
            (outcome, sha, branch, note, reason_code, reason, order["id"]),
        )
        text = _report_text(
            order, host, outcome, sha, branch, note, reason_code, reason
        )
        # Report хоста в ленту не пишет никогда (ни до закрытия, ни после): итог
        # заказа пишет его закрытие; здесь исход и причина только сохранены для плашки.
        if outcome in ("failed", "rescued"):
            # Прогон на хосте кончился без сдачи: бронь не ждёт конца сессии
            # (решение владельца 16.10). Сдача (submitted) заказ не закрывает —
            # его закрывает сама сдача.
            # Хост сообщил, что прогон кончился: сессия этого прогона больше не
            # исполнитель, и пока она жива, слот нельзя отдавать второму.
            await db.execute(
                "UPDATE chat_pair_sessions SET revoked_at = datetime('now') "
                "WHERE executor_run_id = ? AND revoked_at IS NULL",
                (int(order["executor_run_id"]),),
            )
            await close_order(
                db,
                order,
                f"reported_{outcome}",
                RUN_OUTCOME_ABORTED,
                final_text=text,
            )
            # #1942: разбор в тени после сохранённого report и возврата задачи — и
            # когда закрыл этот вызов, и когда заказ закрыт раньше (поздний report).
            # Свежая строка заказа читается внутри; запуска и смены статуса нет.
            await executor_triage.recommend_safely(db, int(order["id"]))
    return {"id": order["id"], "outcome": outcome, "duplicate": False}
