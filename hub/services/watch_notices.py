"""Наблюдательные уведомления стюарду проекта (#1679).

Хаб сам ничего стюарду не писал: повод жил в ленте без project_id или не
записывался вовсе. Здесь уведомление пишется В МОМЕНТ события, в той же
транзакции, что и событие повода, одной функцией ``notify_steward``. Курсора по
ленте нет: ретенция ленты и рестарт поллера ничего не теряют, а политика
проекта читается в момент вызова — уведомление коммитится вместе с событием или
не пишется вовсе.

Контракт функции:

* Транзакция вызывающего. Собственного commit нет; если транзакции ещё нет,
  она открывается (``BEGIN IMMEDIATE``) и остаётся открытой — иначе RELEASE
  самого внешнего SAVEPOINT закоммитил бы чужую работу.
* Изоляция SAVEPOINT. Ошибка внутри — ROLLBACK TO SAVEPOINT (ни ключа, ни
  сообщения), событие ``watch_notice_failed`` и возврат без исключения:
  поллер перехватывает ошибки без rollback, и проброс оставил бы частичное
  состояние. Уведомление не ломает переход, который его породил.
* Сеть сюда не заходит: функция читает и пишет только базу.
* Дедуп — ключом ``watch_notices.key`` с пространством имён повода; ключ живёт
  вечно, а сообщение пишется только тем вызовом, что вставил ключ.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.models import STEWARD_WATCH_REASONS
from hub.services import project_policy

log = logging.getLogger("hub.watch_notices")

NOTICE_VERSION = 1
FAILED_EVENT = "watch_notice_failed"
_SAVEPOINT = "watch_notice"


async def _project_of(
    db: aiosqlite.Connection, task_id: int | None, project_id: int | None
) -> aiosqlite.Row | None:
    """Проект повода: у задачи — как у карточки; уже известный ``project_id`` берётся как есть.

    Нет ни задачи, ни проекта — None (уведомления нет)."""
    if task_id is not None and project_id is None:
        if await repo.get_task(db, task_id) is None:
            return None
        project, _ = await repo.resolve_project_with_source(db, task_id)
        return project
    if project_id is not None:
        return await repo.get_project(db, project_id)
    return None


def _body(
    reason: str,
    slug: str,
    task_id: int | None,
    source_event_id: int | None,
    source_key: str,
    review_id: int | None,
) -> str:
    """JSON версии 1 с закрытыми полями; свободного текста нет."""
    return json.dumps(
        {
            "v": NOTICE_VERSION,
            "reason": reason,
            "project_slug": slug,
            "task_id": task_id,
            "source_event_id": source_event_id,
            "source_key": source_key,
            "review_id": review_id,
        },
        ensure_ascii=False,
    )


async def _write(
    db: aiosqlite.Connection,
    reason: str,
    *,
    task_id: int | None,
    project_id: int | None,
    source_key: str,
    source_event_id: int | None,
    review_id: int | None,
) -> bool:
    project = await _project_of(db, task_id, project_id)
    if project is None:
        log.warning(
            "watch notice %s skipped: no task/project for %s", source_key, reason
        )
        return False
    policy = project_policy.gate_policy_of(project)
    if reason not in project_policy.steward_watch_of(policy):
        return False
    cur = await db.execute(
        "INSERT OR IGNORE INTO watch_notices (key) VALUES (?)", (source_key,)
    )
    if not cur.rowcount:
        return False  # этот источник уже объявлен
    slug = str(project["slug"]).lower()
    to_ref = f"steward@{slug}"
    message_id = await repo.insert_agent_message(
        db,
        from_principal_id=None,
        from_session_id="",
        from_agent="hub",
        from_model="",
        to_kind="role",
        to_ref=to_ref,
        kind="note",
        body=_body(reason, slug, task_id, source_event_id, source_key, review_id),
        related_task_id=task_id,
    )
    # Как в messaging.send_message: событие несёт адрес и id, не текст.
    await repo.insert_event(
        db,
        kind="message_posted",
        task_id=task_id,
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
    return True


async def _record_failure(
    db: aiosqlite.Connection,
    reason: str,
    task_id: int | None,
    project_id: int | None,
    source_key: str,
    exc: Exception,
) -> None:
    """Событие о сбое уведомления — само под защитой: оно тоже не вправе ронять переход."""
    try:
        await repo.insert_event(
            db,
            kind=FAILED_EVENT,
            task_id=task_id,
            project_id=project_id,
            actor="hub",
            payload={
                "reason": reason,
                "source_key": source_key,
                "error": type(exc).__name__,
            },
        )
    except Exception:  # noqa: BLE001 - сбой записи сбоя уходит только в лог
        log.exception("watch_notice_failed not recorded for %s", source_key)


async def notify_steward(
    db: aiosqlite.Connection,
    reason: str,
    *,
    task_id: int | None = None,
    project_id: int | None = None,
    source_key: str,
    source_event_id: int | None = None,
    review_id: int | None = None,
) -> bool:
    """Положить уведомление в steward@<проект>; True — это вызов записал сообщение.

    Не бросает исключений: ошибка откатывает только своё (SAVEPOINT) и
    остаётся событием ``watch_notice_failed``.
    """
    if reason not in STEWARD_WATCH_REASONS:
        raise ValueError(f"unknown watch reason {reason!r}")  # ошибка кода, не данных
    try:
        if not db.in_transaction:
            await db.execute("BEGIN IMMEDIATE")
        await db.execute(f"SAVEPOINT {_SAVEPOINT}")
    except Exception as exc:  # noqa: BLE001 - без SAVEPOINT изоляции нет, пишем лишь в лог
        log.exception("watch notice %s: no savepoint (%s)", source_key, exc)
        return False
    try:
        sent = await _write(
            db,
            reason,
            task_id=task_id,
            project_id=project_id,
            source_key=source_key,
            source_event_id=source_event_id,
            review_id=review_id,
        )
        await db.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        return sent
    except Exception as exc:  # noqa: BLE001 - уведомление не ломает переход
        log.exception("watch notice %s failed", source_key)
        with contextlib.suppress(Exception):
            await db.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
            await db.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        await _record_failure(db, reason, task_id, project_id, source_key, exc)
        return False


async def emit_needs_decision(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    actor: str = "hub",
    payload: dict[str, Any] | None = None,
) -> int:
    """Событие входа в needs_decision и его уведомление — одной обёрткой.

    Единственное место, где пишется ``insert_event(kind="needs_decision")``:
    тест обходит исходники и падает на любом прямом вызове.
    """
    event_id = await repo.insert_event(
        db, kind="needs_decision", task_id=task_id, actor=actor, payload=payload
    )
    await notify_steward(
        db,
        "needs_decision",
        task_id=task_id,
        source_key=f"needs_decision:{event_id}",
        source_event_id=event_id,
    )
    return event_id
