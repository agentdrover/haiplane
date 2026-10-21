"""Заказы ролей k8: роль по событию хаба, lease хосту, старт, отчёт (#1973).

Роли k8 были постоянными сессиями на Mac. Здесь хабовая сторона вызова роли по
событию. Заказ роли - отдельная сущность ``role_orders``, а не
заказ исполнителя: тот привязан к задаче и к коду implementer, читается
бронью слота, плашкой и бюджетом. Заказ роли НЕ создаёт прогонов, заказов
исполнителя и кодов парной сессии и ничего из этого не читает: модуль не
упоминает их таблицы, и тест держит это грепом исходника.

Жизнь заказа (каждый переход - условный UPDATE, исход решает ``rowcount``)::

    open --lease--> leased --started--> started --report--> done | failed
      |                |                    \\--срок работы--> failed
      |                \\--срок lease--> open (attempt+1, не больше MAX_REISSUES
      |                                  перевыдач) | failed (lease_exhausted)
      \\--вход в needs_decision устарел--> expired

Перевыдача возможна ТОЛЬКО до ``started``: после старта роль уже могла
действовать, и второй исполнитель недопустим. Старт без отчёта в срок
``RUN_MAX_MINUTES`` - ``failed`` и сообщение стюарду.

Время - ``datetime('now')`` внутри SQL, а не часы процесса: одно значение для
хаба и тестов. Гонки решает write-лок (``write_transaction``) и условный UPDATE.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any, NoReturn

import aiosqlite

from hub import repository as repo
from hub.db import fetchall, write_transaction

log = logging.getLogger("hub.services.role_orders")

ROLE_ROADMAP = "roadmap"
ROLES: tuple[str, ...] = (ROLE_ROADMAP,)
ROLE_LABELS = {ROLE_ROADMAP: "роадмап"}
SOURCE_NEEDS_DECISION = "needs_decision"

#: Заказов ролей в час на всё: сверх квоты событие ждёт, курсор не двигается.
ROLE_ORDERS_PER_HOUR = 12
#: Активных (leased|started) заказов на хост.
ROLE_LEASES_PER_HOST = 1
LEASE_SECONDS = 120
#: Перевыдач lease без старта; сверх - failed.
MAX_REISSUES = 2
#: Срок работы после started; сверх - failed и сообщение стюарду.
RUN_MAX_MINUTES = 60
NOTE_MAX = 500
#: Событий за один проход источника: остаток доберёт следующий тик.
SWEEP_BATCH = 100
#: Открытых заказов за страницу обхода устаревших.
REAP_BATCH = 200
REPORT_OUTCOMES = ("done", "failed")
ACTIVE_STATES = ("open", "leased", "started")

_SECRETISH = re.compile(
    r"\b(?:ahcp|ochk|ghp|gho|sk)[_-][A-Za-z0-9_\-]{12,}|\b[A-Za-z0-9_\-.]{32,}\b"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


class RoleOrderError(Exception):
    """Отказ операции над заказом роли: код ответа и причина для хоста."""

    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.detail = {"reason": reason, "message": message}


def _refuse(status: int, reason: str, message: str) -> NoReturn:
    raise RoleOrderError(status, reason, message)


def label_of(role: str) -> str:
    return ROLE_LABELS.get(role, role)


# ---------------------------------------------------------------------------
# Допуск хоста и проверка владельца
# ---------------------------------------------------------------------------


async def admit_host(
    db: aiosqlite.Connection,
    host: str,
    role: str,
    project_id: int,
    *,
    by: int | None = None,
) -> None:
    """Допустить хост к роли в проекте. Идемпотентно. Роль вне списка - 422."""
    if role not in ROLES:
        _refuse(422, "role_unknown", f"роль {role!r} не из списка {ROLES}")
    async with write_transaction(db):
        known = await fetchall(
            db, "SELECT 1 FROM executor_hosts WHERE name = ?", (host,)
        )
        if not known:
            _refuse(404, "host_unknown", f"хост {host!r} не зарегистрирован")
        await db.execute(
            "INSERT OR IGNORE INTO role_host_admissions (host, role, project_id) "
            "VALUES (?, ?, ?)",
            (host, role, int(project_id)),
        )
        if by is not None:
            from hub.services import admin as admin_svc

            await admin_svc.write_audit(
                db,
                actor_id=by,
                action="role_host_admitted",
                target_type="executor_host",
                target_id=host,
                summary=f"host {host} admitted to role {role} in project {project_id}",
                commit=False,
            )


async def check_host(
    db: aiosqlite.Connection, host: str, principal_id: int | None
) -> None:
    """Хост существует, включён, принципал активен и это ЕГО принципал; иначе 403."""
    rows = await fetchall(
        db,
        "SELECT h.enabled, h.principal_id, p.status FROM executor_hosts h "
        "JOIN principals p ON p.id = h.principal_id WHERE h.name = ?",
        (host,),
    )
    row = rows[0] if rows else None
    if (
        row is None
        or not row["enabled"]
        or row["status"] != "active"
        or principal_id is None
        or int(row["principal_id"]) != int(principal_id)
    ):
        _refuse(403, "host_not_allowed", "хост выключен или ключ не его")


# ---------------------------------------------------------------------------
# Источник: событие needs_decision -> заказ роли
# ---------------------------------------------------------------------------


async def order_role(
    db: aiosqlite.Connection,
    *,
    role: str,
    source_key: str,
    task_id: int,
    source_event_id: int | None = None,
    project_id: int | None = None,
    generation: int = 0,
    entered_at: str = "",
) -> dict[str, Any]:
    """Создать заказ роли; повтор ключа - ``duplicate``, квота - ``quota``.

    Проверка дубля, квоты и вставка - одна ``write_transaction``. Заказ несёт
    только роль, ключ источника и задачу: кода implementer здесь нет и быть не
    может (роль вне ``ROLES`` - 422).
    """
    if role not in ROLES:
        _refuse(422, "role_unknown", f"роль {role!r} не из списка {ROLES}")
    async with write_transaction(db):
        if await fetchall(
            db, "SELECT 1 FROM role_orders WHERE source_key = ?", (source_key,)
        ):
            return {"status": "duplicate"}
        recent = await fetchall(
            db,
            "SELECT COUNT(*) AS n FROM role_orders "
            "WHERE created_at > datetime('now', '-1 hour')",
        )
        if int(recent[0]["n"]) >= ROLE_ORDERS_PER_HOUR:
            return {"status": "quota"}
        cursor = await db.execute(
            "INSERT INTO role_orders (role, source_key, source_event_id, task_id, "
            "project_id, generation, entered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                role,
                source_key,
                source_event_id,
                int(task_id),
                project_id,
                int(generation),
                entered_at,
            ),
        )
        return {"status": "created", "id": cursor.lastrowid}


async def _newest_entry_event(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await fetchall(
        db,
        "SELECT COALESCE(MAX(id), 0) AS n FROM events "
        "WHERE task_id = ? AND kind = 'needs_decision'",
        (task_id,),
    )
    return int(rows[0]["n"])


async def _order_for_event(db: aiosqlite.Connection, event: aiosqlite.Row) -> str:
    """Исход разбора одного события: created | duplicate | quota | stale."""
    task_id = int(event["task_id"] or 0)
    rows = await fetchall(db, "SELECT * FROM tasks WHERE id = ?", (task_id,))
    task = rows[0] if rows else None
    if (
        task is None
        or task["archived"]
        or task["status"] != "needs_decision"
        or await _newest_entry_event(db, task_id) != int(event["id"])
    ):
        return "stale"  # ушла из статуса или вошла снова: ждёт не это событие
    project, _ = await repo.resolve_project_with_source(db, task_id)
    made = await order_role(
        db,
        role=ROLE_ROADMAP,
        source_key=f"{SOURCE_NEEDS_DECISION}:{event['id']}",
        task_id=task_id,
        source_event_id=int(event["id"]),
        project_id=int(project["id"]) if project is not None else None,
        generation=int(task["submission_generation"] or 0),
        entered_at=str(task["status_entered_at"] or ""),
    )
    return str(made["status"])


async def _read_cursor(db: aiosqlite.Connection) -> int:
    rows = await fetchall(db, "SELECT cursor_event_id FROM role_orders_cursor")
    if rows:
        return int(rows[0]["cursor_event_id"])
    # Курсора нет (повреждение): встаём на конец ленты, а не читаем её с нуля.
    await db.execute(
        "INSERT INTO role_orders_cursor (id, cursor_event_id) "
        "SELECT 1, COALESCE(MAX(id), 0) FROM events"
    )
    return await _read_cursor(db)


async def _source_step(db: aiosqlite.Connection) -> str | None:
    """Разобрать одно событие после курсора одной транзакцией; None - лента прочитана."""
    async with write_transaction(db):
        cursor = await _read_cursor(db)
        rows = await fetchall(
            db,
            "SELECT id, task_id FROM events WHERE kind = 'needs_decision' "
            "AND id > ? ORDER BY id LIMIT 1",
            (cursor,),
        )
        if not rows:
            await db.execute(
                "UPDATE role_orders_cursor SET cursor_event_id = "
                "(SELECT COALESCE(MAX(id), 0) FROM events) "
                "WHERE cursor_event_id < (SELECT COALESCE(MAX(id), 0) FROM events)"
            )
            return None
        outcome = await _order_for_event(db, rows[0])
        if outcome == "quota":
            return outcome  # курсор на месте: событие дождётся квоты
        await db.execute(
            "UPDATE role_orders_cursor SET cursor_event_id = ? WHERE id = 1",
            (int(rows[0]["id"]),),
        )
        return outcome


async def sweep_sources(db: aiosqlite.Connection) -> int:
    """Свип источника: события needs_decision после курсора -> заказы; число созданных."""
    created = 0
    for _ in range(SWEEP_BATCH):
        outcome = await _source_step(db)
        if outcome is None or outcome == "quota":
            break
        created += outcome == "created"
    return created


# ---------------------------------------------------------------------------
# Актуальность и истечение
# ---------------------------------------------------------------------------


async def _has_newer_entry(
    db: aiosqlite.Connection, task_id: int, event_id: int
) -> bool:
    rows = await fetchall(
        db,
        "SELECT 1 FROM events WHERE task_id = ? AND kind = 'needs_decision' "
        "AND id > ? LIMIT 1",
        (task_id, event_id),
    )
    return bool(rows)


async def _is_fresh(db: aiosqlite.Connection, order: aiosqlite.Row) -> bool:
    """Задача всё ещё в ТОМ ЖЕ заходе в needs_decision, что породил заказ.

    «Новее» ищется среди событий входа, а не «то же самое событие найдено»:
    очистка лент удаляет старое событие, и заказ, чья задача никуда не уходила,
    не должен от этого стать устаревшим.
    """
    rows = await fetchall(
        db,
        "SELECT status, status_entered_at, submission_generation, archived "
        "FROM tasks WHERE id = ?",
        (int(order["task_id"]),),
    )
    if not rows or rows[0]["archived"] or rows[0]["status"] != "needs_decision":
        return False
    task = rows[0]
    return (
        str(task["status_entered_at"] or "") == str(order["entered_at"] or "")
        and int(task["submission_generation"] or 0) == int(order["generation"])
        and not await _has_newer_entry(
            db, int(order["task_id"]), int(order["source_event_id"] or 0)
        )
    )


async def _expire_stale(db: aiosqlite.Connection, order: aiosqlite.Row) -> bool:
    cursor = await db.execute(
        "UPDATE role_orders SET state = 'expired', outcome = 'stale_entry', "
        "closed_at = datetime('now') WHERE id = ? AND state = 'open'",
        (int(order["id"]),),
    )
    return cursor.rowcount == 1


async def _message_steward(
    db: aiosqlite.Connection, order: aiosqlite.Row, outcome: str
) -> None:
    """Закрытые поля в ящик steward@<проект>; текста хоста и ленты здесь нет."""
    project = (
        await repo.get_project(db, int(order["project_id"]))
        if order["project_id"] is not None
        else None
    )
    if project is None:
        return
    to_ref = f"steward@{str(project['slug']).lower()}"
    body = json.dumps(
        {
            "v": 1,
            "reason": "role_order_failed",
            "role": order["role"],
            "order_id": int(order["id"]),
            "task_id": int(order["task_id"]),
            "source_key": order["source_key"],
            "outcome": outcome,
        },
        ensure_ascii=False,
    )
    message_id = await repo.insert_agent_message(
        db,
        from_principal_id=None,
        from_session_id="",
        from_agent="hub",
        from_model="",
        to_kind="role",
        to_ref=to_ref,
        kind="note",
        body=body,
        related_task_id=int(order["task_id"]),
    )
    await repo.insert_event(
        db,
        kind="message_posted",
        task_id=int(order["task_id"]),
        actor="hub",
        payload={
            "message_id": message_id,
            "to_kind": "role",
            "to_ref": to_ref,
            "kind": "note",
            "from_agent": "hub",
            "from_session_id": "",
            "for_session": "",
        },
    )


async def _notify_failure(
    db: aiosqlite.Connection, order: aiosqlite.Row, outcome: str
) -> None:
    """Сбой без отчёта хоста: запись в ленту задачи и сообщение стюарду."""
    await repo.add_task_update(
        db,
        int(order["task_id"]),
        "hub",
        "alert",
        f"Заказ роли #{order['id']} ({label_of(order['role'])}) не выполнен: "
        f"{outcome}. Роль не перезапускается сама после старта; решение за стюардом.",
    )
    await _message_steward(db, order, outcome)


async def _reap_leases(db: aiosqlite.Connection) -> None:
    """Lease без старта к сроку: перевыдача (не больше MAX_REISSUES) или failed."""
    due = await fetchall(
        db,
        "SELECT * FROM role_orders "
        "WHERE state = 'leased' AND lease_deadline <= datetime('now')",
    )
    for order in due:
        if int(order["attempt"]) <= MAX_REISSUES:
            await db.execute(
                "UPDATE role_orders SET state = 'open', attempt = attempt + 1, "
                "lease_id = NULL, host = NULL, leased_at = NULL, "
                "lease_deadline = NULL WHERE id = ? AND state = 'leased' "
                "AND lease_deadline <= datetime('now')",
                (int(order["id"]),),
            )
            continue
        cursor = await db.execute(
            "UPDATE role_orders SET state = 'failed', outcome = 'lease_exhausted', "
            "closed_at = datetime('now') WHERE id = ? AND state = 'leased' "
            "AND lease_deadline <= datetime('now')",
            (int(order["id"]),),
        )
        if cursor.rowcount == 1:
            await _notify_failure(db, order, "lease_exhausted")


async def _reap_runs(db: aiosqlite.Connection) -> None:
    """Старт без отчёта дольше RUN_MAX_MINUTES: failed без перевыдачи + стюарду."""
    limit = f"-{RUN_MAX_MINUTES} minutes"
    due = await fetchall(
        db,
        "SELECT * FROM role_orders "
        "WHERE state = 'started' AND started_at <= datetime('now', ?)",
        (limit,),
    )
    for order in due:
        cursor = await db.execute(
            "UPDATE role_orders SET state = 'failed', outcome = 'run_timeout', "
            "closed_at = datetime('now') WHERE id = ? AND state = 'started' "
            "AND started_at <= datetime('now', ?)",
            (int(order["id"]), limit),
        )
        if cursor.rowcount == 1:
            await _notify_failure(db, order, "run_timeout")


async def _reap_stale_open(db: aiosqlite.Connection) -> None:
    """Устаревшие открытые заказы; обход постраничный, чтобы хвост очереди не голодал."""
    last = 0
    while True:
        batch = await fetchall(
            db,
            "SELECT * FROM role_orders WHERE state = 'open' AND id > ? "
            "ORDER BY id LIMIT ?",
            (last, REAP_BATCH),
        )
        for order in batch:
            if not await _is_fresh(db, order):
                await _expire_stale(db, order)
        if len(batch) < REAP_BATCH:
            return
        last = int(batch[-1]["id"])


async def reap(db: aiosqlite.Connection) -> None:
    """Истёкшие lease, сорванные сроки работы и устаревшие открытые заказы."""
    async with write_transaction(db):
        await _reap_leases(db)
        await _reap_runs(db)
        await _reap_stale_open(db)


async def run_sweep(db: aiosqlite.Connection) -> None:
    """Один тик поллера: сперва истечения, потом новые заказы по событиям."""
    await reap(db)
    await sweep_sources(db)


# ---------------------------------------------------------------------------
# Маршруты хоста: lease, started, report
# ---------------------------------------------------------------------------


def _lease_view(order: aiosqlite.Row) -> dict[str, Any]:
    """Что видит хост: роль, ключ источника, задача и данные lease - и всё."""
    return {
        "id": int(order["id"]),
        "role": order["role"],
        "source_key": order["source_key"],
        "task_id": int(order["task_id"]),
        "lease_id": order["lease_id"],
        "attempt": int(order["attempt"]),
        "lease_deadline": order["lease_deadline"],
        "run_max_minutes": RUN_MAX_MINUTES,
    }


async def _order(db: aiosqlite.Connection, order_id: int) -> aiosqlite.Row | None:
    rows = await fetchall(db, "SELECT * FROM role_orders WHERE id = ?", (order_id,))
    return rows[0] if rows else None


async def has_work(db: aiosqlite.Connection, host: str) -> bool:
    """Дешёвое чтение для long-poll: есть ли у хоста свой lease или допущенный открытый заказ."""
    rows = await fetchall(
        db,
        "SELECT 1 FROM role_orders o WHERE (o.state = 'leased' AND o.host = ?1) "
        "OR (o.state = 'open' AND EXISTS (SELECT 1 FROM role_host_admissions a "
        "WHERE a.host = ?1 AND a.role = o.role AND a.project_id = o.project_id)) "
        "LIMIT 1",
        (host,),
    )
    return bool(rows)


async def _claim_first(db: aiosqlite.Connection, host: str) -> dict[str, Any] | None:
    candidates = await fetchall(
        db,
        "SELECT o.* FROM role_orders o JOIN role_host_admissions a "
        "ON a.host = ? AND a.role = o.role AND a.project_id = o.project_id "
        "WHERE o.state = 'open' ORDER BY o.id LIMIT 50",
        (host,),
    )
    for order in candidates:
        if not await _is_fresh(db, order):
            await _expire_stale(db, order)
            continue
        cursor = await db.execute(
            "UPDATE role_orders SET state = 'leased', host = ?, lease_id = ?, "
            "leased_at = datetime('now'), "
            "lease_deadline = datetime('now', ?) WHERE id = ? AND state = 'open'",
            (host, uuid.uuid4().hex, f"+{LEASE_SECONDS} seconds", int(order["id"])),
        )
        leased = await _order(db, int(order["id"]))
        if cursor.rowcount == 1 and leased is not None:
            return _lease_view(leased)
    return None


async def lease(
    db: aiosqlite.Connection, host: str, principal_id: int | None
) -> dict[str, Any] | None:
    """Выдать хосту заказ: его текущий lease или первый допущенный свежий открытый.

    Владелец и ``enabled`` проверяются ЗДЕСЬ, в пишущей транзакции: хост,
    выключенный во время long-poll, заказа не получает. Не больше
    ``ROLE_LEASES_PER_HOST`` активных заказов на хост: пока идёт старт или работа,
    второй заказ не выдаётся, а повторный опрос возвращает тот же lease.
    """
    async with write_transaction(db):
        await check_host(db, host, principal_id)
        await _reap_leases(db)
        await _reap_runs(db)
        await _reap_stale_open(db)
        active = await fetchall(
            db,
            "SELECT * FROM role_orders WHERE host = ? AND state IN ('leased', 'started') "
            "ORDER BY id LIMIT ?",
            (host, ROLE_LEASES_PER_HOST),
        )
        if len(active) >= ROLE_LEASES_PER_HOST:
            return _lease_view(active[0]) if active[0]["state"] == "leased" else None
        return await _claim_first(db, host)


def _own_or_refuse(order: aiosqlite.Row | None, host: str) -> aiosqlite.Row:
    """Заказ есть и не чужого хоста; чужой и несуществующий - один 404."""
    if order is None or order["host"] not in (None, host):
        _refuse(404, "role_order_not_found", "заказа нет у этого хоста")
    return order


async def started(
    db: aiosqlite.Connection,
    host: str,
    principal_id: int | None,
    order_id: int,
    lease_id: str,
) -> dict[str, Any]:
    """Хост начал работу: leased -> started одним условным UPDATE; повтор - no-op."""
    async with write_transaction(db):
        await check_host(db, host, principal_id)
        cursor = await db.execute(
            "UPDATE role_orders SET state = 'started', started_at = datetime('now') "
            "WHERE id = ? AND host = ? AND lease_id = ? AND state = 'leased' "
            "AND lease_deadline > datetime('now')",
            (int(order_id), host, lease_id),
        )
        order = _own_or_refuse(await _order(db, int(order_id)), host)
        if cursor.rowcount == 1:
            return {"id": int(order_id), "state": "started", "repeat": False}
        if order["state"] == "started" and order["lease_id"] == lease_id:
            return {"id": int(order_id), "state": "started", "repeat": True}
    _refuse(
        409,
        "lease_not_valid",
        f"lease не годится для старта: заказ в состоянии {order['state']}",
    )


def sanitize_note(text: str) -> str:
    """Заметка роли: без управляющих символов и секретоподобных строк; ≤ NOTE_MAX."""
    clean = _SECRETISH.sub("[скрыто]", _CONTROL.sub(" ", text))
    return " ".join(clean.split())[:NOTE_MAX]


def _digest(outcome: str, lease_id: str, note: str) -> str:
    return hashlib.sha256("\x1f".join((outcome, lease_id, note)).encode()).hexdigest()


async def _close_by_report(
    db: aiosqlite.Connection,
    order: aiosqlite.Row,
    host: str,
    lease_id: str,
    outcome: str,
    note: str,
    digest: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE role_orders SET state = ?, outcome = ?, note = ?, report_digest = ?, "
        "closed_at = datetime('now') WHERE id = ? AND host = ? AND lease_id = ? "
        "AND state = 'started' AND started_at > datetime('now', ?)",
        (
            outcome,
            outcome,
            note,
            digest,
            int(order["id"]),
            host,
            lease_id,
            f"-{RUN_MAX_MINUTES} minutes",
        ),
    )
    if cursor.rowcount != 1:
        return False
    await repo.add_task_update(
        db,
        int(order["task_id"]),
        "hub",
        "status" if outcome == "done" else "alert",
        f"Заказ роли #{order['id']} ({label_of(order['role'])}, {host}): {outcome}. "
        f"Заметка роли (недоверенная цитата): {note or '—'}",
    )
    if outcome == "failed":
        await _message_steward(db, order, "reported_failed")
    return True


async def report(
    db: aiosqlite.Connection,
    host: str,
    principal_id: int | None,
    order_id: int,
    lease_id: str,
    outcome: str,
    note: str,
) -> dict[str, Any]:
    """Итог работы: started -> done|failed; одинаковый повтор - no-op, иной - 409.

    Принимается по (id, host, lease_id, state=started) со сроком, проверенным в
    самой транзакции независимо от того, прошёл ли свип. Повтор сверяется по
    отпечатку ДО отказа по состоянию: закрытый заказ не делает верный повтор
    ошибкой.
    """
    if outcome not in REPORT_OUTCOMES:
        _refuse(422, "role_outcome_invalid", f"outcome: {REPORT_OUTCOMES}")
    if len(note) > NOTE_MAX:
        _refuse(422, "role_note_too_long", f"note не длиннее {NOTE_MAX} символов")
    digest = _digest(outcome, lease_id, note)
    late = False
    async with write_transaction(db):
        await check_host(db, host, principal_id)
        order = _own_or_refuse(await _order(db, int(order_id)), host)
        if order["state"] in ("done", "failed") and order["report_digest"] == digest:
            return {"id": int(order_id), "outcome": outcome, "repeat": True}
        if await _close_by_report(
            db, order, host, lease_id, outcome, sanitize_note(note), digest
        ):
            return {"id": int(order_id), "outcome": outcome, "repeat": False}
        late = order["state"] == "started" and order["lease_id"] == lease_id
    if late:
        await reap(db)  # срок работы вышел до отчёта: фиксируем сбой, не теряем
    _refuse(
        409, "report_not_accepted", "отчёт не принят: заказ не в работе по этому lease"
    )


# ---------------------------------------------------------------------------
# Показ: ProdStateView и карточка задачи
# ---------------------------------------------------------------------------

_VIEW_COLUMNS = (
    "id, role, state, task_id, host, attempt, outcome, created_at, closed_at"
)


def _public(row: aiosqlite.Row) -> dict[str, Any]:
    out = {key: row[key] for key in row.keys()}  # noqa: SIM118 - aiosqlite.Row
    out["label"] = label_of(str(row["role"]))
    return out


async def prod_view(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Все активные заказы и терминальные за сутки по ``closed_at`` (с failed и expired).

    Сбой чтения - одна запись ``unavailable``: это «не знаем», а не «заказов нет».
    """
    try:
        rows = await fetchall(
            db,
            f"SELECT {_VIEW_COLUMNS} FROM role_orders "  # nosec B608
            "WHERE state IN ('open', 'leased', 'started') "
            "OR closed_at >= datetime('now', '-1 day') ORDER BY id DESC",
        )
    except Exception as exc:  # noqa: BLE001 - показ не роняет снимок продакшена
        log.warning("role orders unreadable: %s", exc)
        return [{"unavailable": "заказы ролей не прочитаны"}]
    return [_public(r) for r in rows]


async def card_view(db: aiosqlite.Connection, task_id: int) -> list[dict[str, Any]]:
    """Заказы роли по задаче для карточки; пусто - заказов нет, сбой - ``unavailable``."""
    try:
        rows = await fetchall(
            db,
            f"SELECT {_VIEW_COLUMNS} FROM role_orders "  # nosec B608
            "WHERE task_id = ? ORDER BY id DESC LIMIT 5",
            (int(task_id),),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("role orders of #%s unreadable: %s", task_id, exc)
        return [{"unavailable": "заказы ролей не прочитаны"}]
    return [_public(r) for r in rows]
