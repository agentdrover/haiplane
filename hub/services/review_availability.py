"""Есть ли у сдачи ревью и отвечает ли провайдер ревью вообще (#1262).

10–13.09.2026 облачный провайдер ревью отказывал трое суток подряд
(HTTP 400 usage_limit_exceeded): у 11 из 15 задач в review текущее поколение
не получило ни одного полного отчёта. Хаб знал каждый отказ, но писал его
алертом в карточку ОДНОЙ задачи, и картину «ревьюер недоступен, очередь стоит»
собрал вручную стюард.

Здесь два читателя одних и тех же фактов, без новых записей в базу:

- :func:`generation_review` — ответ по одной задаче: у текущего поколения
  есть ревью (кем, по какому sha, полное ли) или его нет и почему;
- :func:`provider_outage` — ответ по хабу: провайдер отказывает подряд
  дольше порога, и какие задачи в review из-за этого стоят без ревью.

Отказ провайдера читается из алерта, который пишет
``review_dispatch.maybe_dispatch_review`` (текст собирает
``_lost_call_detail``): синхронный отказ создания не оставляет другой
долговечной записи. Связь с текстом закреплена тестом, который проводит
настоящий отказ через диспетчер и читает его отсюда.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.models import GenerationReview
from hub.services.orchestration import report_has_evidence

#: Начала алертов ``maybe_dispatch_review``, означающих «провайдер агента не
#: создал»: прямой отказ и ответ, который не дошёл при подтверждённой пустоте.
#: Отказ по конфигурации хаба («не хватает конфигурации») сюда не входит —
#: это не провайдер.
REFUSAL_ALERT_PREFIXES: tuple[str, ...] = (
    "Кросс-модельное ревью НЕ вызвано: провайдер отказал",
    "Кросс-модельное ревью НЕ вызвано: ответ провайдера не дошёл",
)

#: Событие хаба «ревьюер недоступен» и его снятие.
REVIEWER_UNAVAILABLE = "reviewer_unavailable"
REVIEWER_AVAILABLE = "reviewer_available"
#: Провайдер, о котором говорит сигнал. Один сегодня, но ключ дедупа —
#: провайдер, а не задача: сигнал не зависит от одной карточки.
PROVIDER = "cursor_cloud"

_REASON_TEXT = {
    "provider_refused": "провайдер ревью отказал",
    "incomplete_report": "отчёт есть, но назван неполным",
    "no_execution_evidence": "отчёт есть, но исполнения не видно (#750/#841)",
    "run_failed": "прогон ревьюера кончился без отчёта",
    "in_flight": "ревью в полёте, отчёта ещё нет",
    "not_dispatched": "ревью не вызывалось",
}


#: Параметры к ``substr(content, 1, ?) = ?`` в запросах ниже: длина и начало
#: каждого из двух алертов, по порядку ``REFUSAL_ALERT_PREFIXES``. Запросы
#: написаны литералом на два начала: третье начало — правка и запросов тоже.
_REFUSAL_PARAMS: tuple[Any, ...] = tuple(
    value for prefix in REFUSAL_ALERT_PREFIXES for value in (len(prefix), prefix)
)


def refusal_detail(content: str) -> str:
    """Текст отказа без общего начала алерта и хвоста про вердикт."""
    text = content.split(": ", 1)[-1]
    return text.split(". Вердикт", 1)[0].strip()


async def _latest_refusal(db, task_id: int, since: str | None) -> dict[str, Any] | None:
    args: tuple[Any, ...] = (task_id, *_REFUSAL_PARAMS, since or "")
    rows = await fetchall(
        db,
        "SELECT content, created_at FROM task_updates WHERE task_id=? "
        "AND kind='alert' AND (substr(content, 1, ?) = ? OR substr(content, 1, ?) = ?) "
        "AND created_at >= ? ORDER BY id DESC LIMIT 1",
        args,
    )
    return dict(rows[0]) if rows else None


async def _first_refusal_at(db, task_id: int, since: str) -> str:
    """Когда начались отказы этой задачи после ``since`` — пусто, если не было."""
    rows = await fetchall(
        db,
        "SELECT MIN(created_at) AS at FROM task_updates WHERE task_id=? "
        "AND kind='alert' AND (substr(content, 1, ?) = ? OR substr(content, 1, ?) = ?) "
        "AND created_at >= ?",
        (task_id, *_REFUSAL_PARAMS, since),
    )
    return str(dict(rows[0]).get("at") or "") if rows else ""


async def _latest_real_dispatch(
    db, task_id: int, generation: int
) -> dict[str, Any] | None:
    """Последний заказ поколения, где агент действительно создан.

    Пустой ``agent_id`` — заглушка долга второй двери при синхронном отказе
    (#1266): она ничего не создала и о прогоне не говорит.
    """
    rows = await fetchall(
        db,
        "SELECT * FROM review_dispatches WHERE task_id=? "
        "AND submission_generation=? AND agent_id != '' ORDER BY id DESC LIMIT 1",
        (task_id, generation),
    )
    return dict(rows[0]) if rows else None


async def _principal_name(db, report: dict[str, Any]) -> str:
    pid = report.get("principal_id")
    if pid is not None:
        rows = await fetchall(
            db, "SELECT username FROM principals WHERE id=?", (int(pid),)
        )
        if rows:
            return str(dict(rows[0])["username"])
    return str(report.get("submitted_by") or "")


async def _previous_generation(db, task_id: int, generation: int) -> int | None:
    rows = await fetchall(
        db,
        "SELECT MAX(submission_generation) AS g FROM machine_reviews "
        "WHERE task_id=? AND submission_generation < ?",
        (task_id, generation),
    )
    value = dict(rows[0]).get("g") if rows else None
    return int(value) if value is not None else None


def _is_review(report: dict[str, Any]) -> bool:
    return not report.get("incomplete") and report_has_evidence(report)


async def _absence_reason(
    db, task_id: int, generation: int, reports: list[dict[str, Any]], since: str | None
) -> tuple[str, str]:
    """Почему у поколения нет ревью — причина наблюдённая, по старшинству."""
    if reports:
        if any(r.get("incomplete") for r in reports):
            return "incomplete_report", ""
        return "no_execution_evidence", ""
    dispatch = await _latest_real_dispatch(db, task_id, generation)
    refusal = await _latest_refusal(db, task_id, since)
    if refusal is not None and (
        dispatch is None or str(refusal["created_at"]) > str(dispatch["created_at"])
    ):
        return "provider_refused", refusal_detail(str(refusal["content"]))
    if dispatch is None:
        return "not_dispatched", ""
    if dispatch.get("status") == "active":
        return "in_flight", str(dispatch.get("model") or "")
    return "run_failed", str(dispatch.get("run_status") or dispatch.get("status"))


def _headline(view: GenerationReview) -> str:
    if view.has_review:
        completeness = "полный" if view.complete else "полнота не заявлена"
        return (
            f"ревью поколения {view.generation} есть: "
            f"{view.reviewer_principal or 'принципал не назван'} "
            f"({view.reviewer_model or 'модель не названа'}), "
            f"sha {view.sha[:12] or 'не закреплён'}, отчёт {completeness}"
        )
    text = f"ревью у поколения {view.generation} НЕТ: {_REASON_TEXT[view.reason]}"
    if view.reason_detail:
        text += f" ({view.reason_detail})"
    if view.previous_generation is not None:
        text += (
            f"; отчёт поколения {view.previous_generation} — отчёт прошлой "
            "сдачи, не ревью текущей"
        )
    return text


async def generation_review(db, task_row: dict[str, Any]) -> GenerationReview:
    """Ответ «есть ли ревью у текущего поколения» для одной задачи."""
    view, _since = await _generation_review_since(db, task_row)
    return view


async def _generation_review_since(
    db, task_row: dict[str, Any]
) -> tuple[GenerationReview, str]:
    """Тот же ответ плюс момент сдачи текущего поколения (пусто — не записан).

    Одно правило на двоих: бриф и сторож очереди читают ответ отсюда, и
    второго определения «у сдачи нет ревью из-за провайдера» не заводится.
    """
    task_id = int(task_row["id"])
    generation = int(task_row.get("submission_generation") or 0)
    submission = await repo.get_submission(db, task_id, generation)
    pinned = str(dict(submission).get("sha") or "") if submission is not None else ""
    sha = pinned or str(task_row.get("submission_sha") or "")
    since = str(dict(submission)["submitted_at"]) if submission is not None else None
    reports = [
        dict(r)
        for r in await repo.machine_reviews_of_generation(db, task_id, generation)
    ]
    view = GenerationReview(generation=generation, sha=sha)
    reviewed = [r for r in reports if _is_review(r)]
    if reviewed:
        report = reviewed[-1]
        view.has_review = True
        view.reviewer_principal = await _principal_name(db, report)
        view.reviewer_model = str(report.get("model") or "")
        incomplete = report.get("incomplete")
        view.complete = None if incomplete is None else not bool(incomplete)
    else:
        view.reason, view.reason_detail = await _absence_reason(
            db, task_id, generation, reports, since
        )
        view.previous_generation = await _previous_generation(db, task_id, generation)
    view.headline = _headline(view)
    return view, since or ""


@dataclass
class Outage:
    """Провайдер отказывает подряд: с какого момента и кого это держит."""

    since: str
    refused_tasks: list[int]
    waiting: list[dict[str, Any]] = field(default_factory=list)


async def _last_provider_success(db) -> str:
    rows = await fetchall(
        db,
        "SELECT MAX(created_at) AS at FROM review_dispatches "
        "WHERE channel='cloud' AND agent_id != ''",
    )
    return str(dict(rows[0]).get("at") or "") if rows else ""


async def _review_queue(
    db, last_success: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Сдачи в review без ревью текущего поколения и начала их отказов.

    Отказ засчитывается провайдеру, только если ``generation_review`` этой
    сдачи называет его причиной (отказ текущего поколения, ревью нет) и он
    случился после последнего созданного облачного агента: отказ до успеха —
    уже не «подряд».
    """
    rows = await fetchall(
        db,
        "SELECT * FROM tasks WHERE status='review' AND archived=0 ORDER BY id",
    )
    waiting: list[dict[str, Any]] = []
    starts: list[str] = []
    for row in rows:
        task = dict(row)
        view, submitted_at = await _generation_review_since(db, task)
        if view.has_review:
            continue
        waiting.append({"task_id": int(task["id"]), "reason": view.reason})
        if view.reason != "provider_refused":
            continue
        began = await _first_refusal_at(
            db, int(task["id"]), max(submitted_at, last_success)
        )
        if began and began > last_success:
            waiting[-1]["refused_since"] = began
            starts.append(began)
    return waiting, starts


async def provider_outage(db, *, now: datetime | None = None) -> Outage | None:
    """Провайдер держит очередь review дольше порога, или None.

    Считаются только сдачи, которые СЕЙЧАС в review и у которых текущее
    поколение стоит без ревью из-за отказа провайдера после его последнего
    успеха. Нужно минимум две такие сдачи — отказ на одной может быть
    свойством сдачи (validation_error у #1214), а не провайдера. Как только
    их меньше двух или отказы моложе порога, ответ None — и сигнал снимается.
    """
    hours = config.REVIEWER_UNAVAILABLE_HOURS
    if hours <= 0:
        return None
    waiting, starts = await _review_queue(db, await _last_provider_success(db))
    if len(starts) < 2:
        return None
    since = min(starts)
    now = now or datetime.now(UTC)
    threshold = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    if since > threshold:
        return None
    return Outage(
        since=since,
        refused_tasks=[w["task_id"] for w in waiting if "refused_since" in w],
        waiting=waiting,
    )


async def signal_state(db) -> str:
    """Последнее слово сторожа: поднят ли сигнал, и когда."""
    rows = await fetchall(
        db,
        "SELECT kind, created_at FROM events WHERE kind IN (?, ?) "
        "ORDER BY id DESC LIMIT 1",
        (REVIEWER_UNAVAILABLE, REVIEWER_AVAILABLE),
    )
    return str(dict(rows[0])["kind"]) if rows else ""
