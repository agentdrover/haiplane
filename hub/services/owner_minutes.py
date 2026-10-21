"""Оценка минут владельца, шаг 1 (#1874): нормативы, пас начислений, суммы.

Верхняя цель хаба - польза на час владельца, а его минуты хаб не считал. Точное
время без слежки не измерить, поэтому это ОЦЕНКА по нормативам: каждое записанное
решение человека на гейте даёт одно начисление ``секунды-на-тип`` (``NORMS``).
Не покрыты: таймеры активности, просмотры карточки, чтение без решения.

Кто строит начисления. Поллер (``accrual_pass``), из уже записанных событий
гейтов. Гейт журнал не пишет: сбой или задержка учёта не может помешать самому
решению. Гейты лишь кладут в ``events.payload`` своего события личность
вызывающего (``principal_id`` и ``human``, см. ``stamp``) - из аутентифицированной
личности, а не из ``actor`` и тела запроса. Начисляется только при трёх условиях
сразу: ``human`` в событии, непустой ``principal_id`` и ``principals.kind='human'``
в базе. Нет контекста - нет начисления.

Курсор. ``owner_minutes_state`` хранит ``tracking_since`` и ``cursor_event_id``.
Первый проход создаёт строку атомарно: курсор = MAX(events.id), история до него
не обрабатывается, рестарт её не меняет. Дальше пас читает ``id > курсор``
пакетами по ``BATCH``; начисления пакета и продвижение курсора - ОДНА короткая
write-транзакция без сети, курсор идёт по всем событиям пакета, включая
неучитываемые. ``prune_events`` не удаляет события новее курсора.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

from hub import config
from hub import repository as repo
from hub import repository_owner_minutes as store
from hub.db import write_transaction
from hub.owner_minutes_view import (
    ESTIMATE_LABEL,
    OwnerMinutesDayView,
    OwnerMinutesPrincipal,
    OwnerMinutesView,
)

log = logging.getLogger(__name__)

#: Версия таблицы нормативов. Каждое начисление помнит версию, под которой
#: посчитано: смена нормативов прошлые начисления не переписывает.
NORMS_VERSION = 1

#: Секунды на одно действие (начальные гипотезы; калибровка ручными замерами -
#: следующая задача).
NORMS: dict[str, int] = {
    "approve": 180,
    "review_verdict": 300,
    "decide": 240,
    "answer": 180,
    "launch": 60,
}

#: Событие-источник -> тип начисления. Это ВЕСЬ перечень учитываемого.
EXECUTOR_ORDER_EVENT = "executor_order_accepted"
COUNTED_EVENTS: dict[str, str] = {
    "task_approved": "approve",
    "review_verdict_recorded": "review_verdict",
    "task_decided": "decide",
    "question_answered": "answer",
    EXECUTOR_ORDER_EVENT: "launch",
}

#: Явно исключённое, с причиной: «не учтено» должно читаться как решение, а не
#: как забытый вид события.
EXCLUDED: dict[str, str] = {
    "task_rejected": "reject: отказ - не работа по постановке, шаг 1 его не считает",
    "task_completed": "сопутствует decide(accept) и state APPROVED: то же решение",
    "task_returned_to_work": "return-to-work: сопутствует repair, запуск уже учтён",
    "audit_result": "audit: проверка человеком, не гейт жизненного цикла",
    "disposition_recorded": "disposition находки: не гейт задачи",
    "steward_judgement": "стюард - не человек",
    "steward_applied": "стюард - не человек",
    "steward_escalated": "стюард - не человек",
    "steward_judgement_refused": "стюард - не человек",
}

#: Сколько событий пас берёт за одну транзакцию (потолок list_events - 200).
BATCH = 200

STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"
STATUS_BEHIND = "behind"
STATUS_NOT_STARTED = "not_started"


# ---------------------------------------------------------------------------
# Что гейт кладёт в payload своего события
# ---------------------------------------------------------------------------


def stamp_ids(principal_id: int | None, human: bool) -> dict[str, Any]:
    """Личность для payload события: только человек с принципалом, иначе пусто."""
    if not human or principal_id is None:
        return {}
    return {"principal_id": int(principal_id), "human": True}


def stamp(who: Any) -> dict[str, Any]:
    """То же по ``TokenIdentity`` вызывающего (``None`` - нет контекста)."""
    if who is None:
        return {}
    return stamp_ids(
        getattr(who, "principal_id", None), bool(getattr(who, "is_human", False))
    )


async def record_order_accepted(
    db: aiosqlite.Connection,
    task_id: int,
    run_id: int,
    generation: int,
    principal_id: int | None,
    mode: str,
    human: bool,
    *,
    channel: str = "cloud",
    host: str = "",
) -> None:
    """Событие «заказ исполнителя принят» (#1874); без commit - в транзакции заказа.

    Пишется и для запуска не человеком (автор тогда не проставлен, начисления нет).
    """
    who = stamp_ids(principal_id, human)
    payload: dict[str, Any] = {
        "mode": mode,
        "channel": channel,
        "executor_run_id": run_id,
        "generation": generation,
        **who,
    }
    if host:
        payload["host"] = host
    await repo.insert_event(
        db,
        kind=EXECUTOR_ORDER_EVENT,
        task_id=task_id,
        actor="human" if who else "hub",
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Пас начислений
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    event_id: int
    task_id: int
    kind: str
    principal_id: int
    occurred_at: str


def _candidate_of(event: dict[str, Any]) -> _Candidate | None:
    kind = COUNTED_EVENTS.get(event["kind"])
    if kind is None or event["task_id"] is None:
        return None
    try:
        payload = json.loads(event["payload"] or "{}")
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("human") is not True:
        return None
    principal_id = payload.get("principal_id")
    if isinstance(principal_id, bool) or not isinstance(principal_id, int):
        return None
    if principal_id <= 0:
        return None
    return _Candidate(
        int(event["id"]),
        int(event["task_id"]),
        kind,
        principal_id,
        str(event["created_at"]),
    )


@asynccontextmanager
async def _atomic(db: aiosqlite.Connection) -> AsyncIterator[None]:
    """Атомарная граница пакета - и когда соединение УЖЕ внутри чужой транзакции.

    Соединение поллера живёт весь процесс; предыдущий проход тика мог упасть, не
    закоммитив. Вложенный ``write_transaction`` тогда не владеет транзакцией и не
    откатывает ничего: частичный пакет без курсора коммитил бы следующий проход
    (находка Codex #1874). Поэтому в чужой транзакции пакет идёт под точкой
    сохранения: сбой откатывает ТОЛЬКО его, а чужие записи остаются владельцу.
    Успешные пакеты при этом закоммитит владелец транзакции (в тике это ближайший
    безусловный commit); если он откатит, откатятся вместе и начисления, и курсор -
    следующий тик пройдёт те же события без дублей. Лок записи в этой ветке уже
    держит чужая транзакция, пас новых блокировок не добавляет.
    """
    if not db.in_transaction:
        async with write_transaction(db):
            yield
        return
    await db.execute("SAVEPOINT owner_minutes_batch")
    try:
        yield
    except BaseException:
        await db.execute("ROLLBACK TO SAVEPOINT owner_minutes_batch")
        await db.execute("RELEASE SAVEPOINT owner_minutes_batch")
        raise
    await db.execute("RELEASE SAVEPOINT owner_minutes_batch")


async def _ensure_state(db: aiosqlite.Connection) -> None:
    """Однократная инициализация: tracking_since и курсор MAX(events.id) - вместе."""
    if await store.get_state(db) is not None:
        return
    async with _atomic(db):
        await store.init_state(db)


async def _one_batch(db: aiosqlite.Connection, batch: int) -> tuple[int, bool]:
    """Один пакет: (число начислений, лента кончилась). Одна атомарная граница."""
    async with _atomic(db):
        state = await store.get_state(db)
        if state is None:
            return 0, True
        events = await store.events_after(db, int(state["cursor_event_id"]), batch)
        if not events:
            return 0, True
        found = [c for c in map(_candidate_of, events) if c is not None]
        humans = await store.human_principal_ids(db, {c.principal_id for c in found})
        accrued = 0
        for c in found:
            if c.principal_id not in humans:
                continue
            await store.insert_accrual(
                db,
                task_id=c.task_id,
                principal_id=c.principal_id,
                kind=c.kind,
                occurred_at=c.occurred_at,
                seconds=int(NORMS[c.kind]),
                norms_version=int(NORMS_VERSION),
                source_event_id=c.event_id,
            )
            accrued += 1
        await store.advance_cursor(db, int(events[-1]["id"]))
    return accrued, len(events) < batch


async def accrual_pass(db: aiosqlite.Connection, *, batch: int = BATCH) -> int:
    """Дочитать ленту событий и начислить; возвращает число новых начислений."""
    await _ensure_state(db)
    total = 0
    while True:
        accrued, done = await _one_batch(db, batch)
        total += accrued
        if done:
            return total


# ---------------------------------------------------------------------------
# Суммы
# ---------------------------------------------------------------------------


def timezone() -> ZoneInfo:
    """Применённый часовой пояс; неверное имя - ошибка (при старте её ловит проверка)."""
    return ZoneInfo(config.OWNER_MINUTES_TIMEZONE)


def validate_timezone() -> None:
    """Проверка при старте: пояс из настройки существует."""
    name = config.OWNER_MINUTES_TIMEZONE
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise RuntimeError(
            f"HAIPLANE_OWNER_MINUTES_TIMEZONE={name!r} - неизвестный часовой пояс IANA"
        ) from exc


async def _progress(db: aiosqlite.Connection) -> dict[str, Any]:
    state = await store.get_state(db)
    if state is None:
        return {"tracking_since": None, "through": 0, "caught_up": False}
    through = int(state["cursor_event_id"])
    return {
        "tracking_since": state["tracking_since"],
        "through": through,
        "caught_up": through >= await store.max_event_id(db),
    }


def _status(progress: dict[str, Any], accruals: int) -> str:
    if progress["tracking_since"] is None:
        return STATUS_NOT_STARTED
    if not progress["caught_up"]:
        return STATUS_BEHIND
    return STATUS_OK if accruals else STATUS_NO_DATA


def _with_line(view: Any) -> Any:
    view.line = describe(view.model_dump())
    return view


async def task_estimate(db: aiosqlite.Connection, task_id: int) -> OwnerMinutesView:
    """Оценка минут по задаче: все учтённые люди с разбивкой по principal."""
    # Порядок чтения несущий (находка Codex #1874): СНАЧАЛА курсор, потом суммы.
    # Курсор, прочитанный позже сумм, мог обогнать их, если пас отработал между
    # чтениями: ответ «нет данных, пас догнал» при уже начисленных секундах.
    # Так суммы всегда включают всё, что лежит не дальше прочитанного курсора.
    progress = await _progress(db)
    split = await store.task_split(db, task_id)
    accruals = sum(int(p["accruals"]) for p in split)
    return _with_line(
        OwnerMinutesView(
            status=_status(progress, accruals),
            total_seconds=sum(int(p["seconds"]) for p in split) if accruals else None,
            accruals=accruals,
            by_principal=[OwnerMinutesPrincipal(**p) for p in split],
            tracking_since=progress["tracking_since"],
            processed_through_event_id=progress["through"],
            caught_up=progress["caught_up"],
            norms_version=int(NORMS_VERSION),
            label=ESTIMATE_LABEL,
            timezone=str(config.OWNER_MINUTES_TIMEZONE),
        )
    )


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


class DayOutOfRange(ValueError):
    """Дата, чьи границы дня не представимы (0001-01-01, 9999-12-31 и подобные)."""


def local_day_bounds(day: date) -> tuple[str, str]:
    """[начало, конец) локального дня в UTC-строках events.created_at."""
    tz = timezone()
    try:
        start = datetime.combine(day, time.min, tzinfo=tz)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
        return _utc_text(start), _utc_text(end)
    except OverflowError as exc:
        raise DayOutOfRange(f"date {day} is outside the supported range") from exc


def today_local() -> date:
    return datetime.now(UTC).astimezone(timezone()).date()


async def day_estimate(
    db: aiosqlite.Connection, principal_id: int, day: date
) -> OwnerMinutesDayView:
    """Оценка минут принципала за локальный день по всем проектам."""
    start, end = local_day_bounds(day)
    progress = await _progress(db)  # курсор раньше сумм, см. task_estimate
    seconds, accruals = await store.principal_window(db, principal_id, start, end)
    return _with_line(
        OwnerMinutesDayView(
            status=_status(progress, accruals),
            total_seconds=seconds if accruals else None,
            accruals=accruals,
            tracking_since=progress["tracking_since"],
            processed_through_event_id=progress["through"],
            caught_up=progress["caught_up"],
            norms_version=int(NORMS_VERSION),
            label=ESTIMATE_LABEL,
            principal_id=principal_id,
            date=day.isoformat(),
            timezone=str(config.OWNER_MINUTES_TIMEZONE),
        )
    )


# ---------------------------------------------------------------------------
# Показ
# ---------------------------------------------------------------------------


def format_duration(seconds: int) -> str:
    minutes, rest = divmod(int(seconds), 60)
    if not minutes:
        return f"{rest} с"
    return f"{minutes} мин" + (f" {rest} с" if rest else "")


def describe(estimate: dict[str, Any]) -> str:
    """Одна строка для карточки, MCP и CLI - из объекта, без пересчёта."""
    status = estimate.get("status")
    total = estimate.get("total_seconds")
    head = format_duration(total) if total is not None else "нет данных"
    if status == STATUS_NOT_STARTED:
        head = "учёт не включён"
    note = "; пас отстаёт от ленты" if status == STATUS_BEHIND else ""
    return f"{head} ({estimate.get('label') or ESTIMATE_LABEL}){note}"
