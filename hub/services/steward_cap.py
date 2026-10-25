"""Суточный потолок стюарда: два счёта и явная передача человеку (#1915).

19.10 default исчерпал потолок 20/20 к 10:17 UTC: 15 суждений и 5 прогонов
советника делили один счёт. Дальше шесть сдач получили отказ ``daily_cap``,
который лёг ТОЛЬКО событием: ни строки ``steward_runs``, ни следа в
``effective_policy`` и во входящих. Маршрут вердикта продолжал называть
решателем стюарда, и задача висела незаметно.

Этот модуль — единственное место, где живут:

* счёт и значение потолка каждой группы (``verdict_dor`` и ``advisor``);
* ЧАСЫ хаба для границы UTC-суток (тесты подменяют ``utcnow``);
* условие «поколение передано человеку из-за потолка до ``reset_at``» —
  ``cap_handoff``. Его зовут маршрут вердикта, диспетчер суждений, заказ
  советников и читатель согласия советника (применение). Копии условия нет:
  копия разъехалась бы ровно в том месте, где задача повисает.

Отказ ``daily_cap`` строкой ``steward_runs`` НЕ пишется: уникальный индекс
(task, generation, kind) заблокировал бы завтрашний заказ. Факт отказа — это
событие ``steward_run_refused`` с ``project_id`` за UTC-сутки.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.services import verdict_owner
from hub.db import fetchall

EVENT_REFUSED = "steward_run_refused"
REFUSED_DAILY_CAP = "daily_cap"
#: Код маршрута вердикта, когда поколение передано человеку из-за потолка.
CODE_DAILY_CAP = "steward_daily_cap"

GROUP_VERDICT_DOR = "verdict_dor"
GROUP_ADVISOR = "advisor"
GROUPS = (GROUP_VERDICT_DOR, GROUP_ADVISOR)

#: Виды заказов (steward_dispatch.KIND_*), которые считает каждая группа.
_GROUP_KINDS = {
    GROUP_VERDICT_DOR: ("verdict", "dor"),
    GROUP_ADVISOR: ("advisor",),
}
#: Виды, чей отказ передаёт человеку вердикт сдачи (dor — про постановку).
VERDICT_KINDS = ("verdict", "advisor")

_STAMP = "%Y-%m-%d %H:%M:%S"
_GROUP_LABEL = {GROUP_VERDICT_DOR: "суждения", GROUP_ADVISOR: "советник"}


def utcnow() -> datetime:
    """Часы хаба для границы суток; тесты подменяют эту функцию, а не спят."""
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    """Метка в формате ``datetime('now')`` — порядок строк равен порядку времени."""
    return moment.astimezone(UTC).strftime(_STAMP)


def utc_day(moment: datetime | None = None) -> str:
    return (moment or utcnow()).astimezone(UTC).strftime("%Y-%m-%d")


def reset_at(day: str) -> str:
    """Когда счёт суток ``day`` обнуляется: следующая полночь UTC."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return stamp(start + timedelta(days=1))


def group_of(kind: str) -> str:
    return GROUP_ADVISOR if kind == "advisor" else GROUP_VERDICT_DOR


def kinds_of(group: str) -> tuple[str, ...]:
    return _GROUP_KINDS[group]


def cap_of(group: str) -> int:
    """Потолок группы. Советник без своего значения берёт общий потолок."""
    if group == GROUP_ADVISOR:
        raw = str(config.STEWARD_ADVISOR_DAILY_CAP or "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass  # нечитаемое значение читается как «не задано», не как ноль
    return int(config.STEWARD_DAILY_CAP)


async def used_today(
    db: aiosqlite.Connection, project_id: int | None, group: str
) -> int:
    from hub.services.steward_dispatch import runs_today

    return await runs_today(db, project_id, group)


async def _refusals(
    db: aiosqlite.Connection,
    *,
    task_id: int | None = None,
    project_id: int | None = None,
    generation: int | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Отказы ``daily_cap`` текущих UTC-суток, старые первыми."""
    where = [
        "kind=?",
        "json_extract(payload, '$.reason')=?",
        "created_at >= ?",  # диапазон, а не date(): так работает индекс событий
        "created_at < ?",
    ]
    day = utc_day()
    params: list[Any] = [
        EVENT_REFUSED,
        REFUSED_DAILY_CAP,
        f"{day} 00:00:00",
        reset_at(day),
    ]
    if task_id is not None:
        where.append("task_id=?")
        params.append(task_id)
    if project_id is not None:
        where.append("project_id=?")
        params.append(project_id)
    if generation is not None:
        where.append("json_extract(payload, '$.generation')=?")
        params.append(generation)
    if kind is not None:
        where.append("json_extract(payload, '$.kind')=?")
        params.append(kind)
    rows = await fetchall(
        db,
        "SELECT id, task_id, project_id, created_at, "  # nosec B608 - placeholders only
        "json_extract(payload, '$.kind') AS order_kind "
        f"FROM events WHERE {' AND '.join(where)} ORDER BY id ASC",
        tuple(params),
    )
    return [dict(r) for r in rows]


async def refused_today(
    db: aiosqlite.Connection, task_id: int, generation: int, kind: str
) -> bool:
    """Был ли уже отказ ``daily_cap`` на (задача, поколение, вид) в эти сутки.

    Дедуп ленты идёт по НАЛИЧИЮ именно этого отказа, а не по последнему
    событию: чередование причин daily_cap → другая → daily_cap не должно
    рождать второй alert.
    """
    return bool(await _refusals(db, task_id=task_id, generation=generation, kind=kind))


async def record_refusal(
    db: aiosqlite.Connection,
    task_id: int,
    project_id: int | None,
    generation: int,
    kind: str,
    detail: str,
) -> bool:
    """Записать отказ по потолку один раз на (задача, поколение, вид, сутки).

    Событие (с ``project_id`` и меткой часов хаба) и alert в ленте задачи
    пишутся в одной транзакции вызывающего; коммитит он. ``False`` — такой
    отказ сегодня уже есть, писать нечего. Строки ``steward_runs`` нет: её
    уникальный индекс заблокировал бы завтрашний заказ.
    """
    if await refused_today(db, task_id, generation, kind):
        return False
    moment = utcnow()
    await db.execute(
        "INSERT INTO events (kind, task_id, project_id, actor, payload, created_at) "
        "VALUES (?, ?, ?, 'hub', ?, ?)",
        (
            EVENT_REFUSED,
            task_id,
            project_id,
            json.dumps(
                {
                    "reason": REFUSED_DAILY_CAP,
                    "detail": detail,
                    "generation": generation,
                    "kind": kind,
                },
                ensure_ascii=False,
            ),
            stamp(moment),
        ),
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        alert_text(kind, generation, detail, utc_day(moment)),
        author_kind="hub",
    )
    # #2052: потолок исчерпан - вердикт сдачи у человека (запись временная:
    # новый заказ на этой сдаче вернёт стюарда).
    # Причина - та же строка с временем сброса, что показывает маршрут.
    handoff = await cap_handoff(db, task_id, generation)
    await verdict_owner.hand_to_human(
        db,
        task_id,
        verdict_owner.reason_of(
            CODE_DAILY_CAP, handoff.reason if handoff is not None else detail
        ),
        generation=generation,
    )
    return True


@dataclass(frozen=True)
class CapHandoff:
    """Поколение передано человеку: потолок исчерпан, сутки не кончились."""

    kind: str
    group: str
    used: int
    cap: int
    reset_at: str

    @property
    def reason(self) -> str:
        return (
            f"суточный потолок стюарда исчерпан ({_GROUP_LABEL[self.group]}: "
            f"{self.used}/{self.cap}); вердикт у человека до {self.reset_at} UTC"
        )


async def _ordered_since(
    db: aiosqlite.Connection, task_id: int, generation: int, group: str, since: str
) -> bool:
    """Есть ли прогон группы для этого поколения, созданный после отказа."""
    kinds = kinds_of(group)
    marks = ",".join("?" for _ in kinds)
    rows = await fetchall(
        db,
        "SELECT 1 FROM steward_runs WHERE task_id=? AND generation=? "  # nosec B608
        f"AND kind IN ({marks}) AND created_at >= ? LIMIT 1",
        (task_id, generation, *kinds, since),
    )
    return bool(rows)


async def cap_handoff(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    kinds: tuple[str, ...] = VERDICT_KINDS,
) -> CapHandoff | None:
    """Одно условие «передано человеку из-за потолка до reset_at».

    Верно, когда за текущие UTC-сутки этой сдаче отказали по ``daily_cap``,
    потолок группы всё ещё исчерпан И после отказа прогон этой группы для
    этого поколения не заказан. Потолок поднят среди суток — задача
    возвращается стюарду; а заказанное после отказа поколение возвращается
    насовсем: исчерпание потолка другими задачами на него уже не действует.
    Чистое чтение; проект берётся по задаче, а не из события (старые события
    его не несут).
    """
    day = utc_day()
    for event in await _refusals(db, task_id=task_id, generation=generation):
        kind = str(event.get("order_kind") or "")
        if kind not in kinds:
            continue
        project = await repo.resolve_project_for_task(db, task_id)
        project_id = int(project["id"]) if project is not None else None
        group = group_of(kind)
        if await _ordered_since(db, task_id, generation, group, event["created_at"]):
            continue
        used = await used_today(db, project_id, group)
        cap = cap_of(group)
        if used >= cap:
            return CapHandoff(kind, group, used, cap, reset_at(day))
    return None


async def project_report(
    db: aiosqlite.Connection, project_id: int
) -> list[dict[str, Any]]:
    """used/cap/utc_day/reset_at/first_refused_at по двум группам проекта."""
    day = utc_day()
    events = await _refusals(db, project_id=project_id)
    out = []
    for group in GROUPS:
        firsts = [
            e["created_at"]
            for e in events
            if group_of(str(e.get("order_kind") or "")) == group
        ]
        out.append(
            {
                "kind_group": group,
                "used": await used_today(db, project_id, group),
                "cap": cap_of(group),
                "utc_day": day,
                "reset_at": reset_at(day),
                "first_refused_at": firsts[0] if firsts else None,
            }
        )
    return out


async def banner_rows(
    db: aiosqlite.Connection, project_id: int | None = None
) -> list[dict[str, Any]]:
    """Баннер входящих: проекты с verdict=steward, где сегодня был отказ по потолку.

    Читается из событий отказа за UTC-сутки — отдельного хранения уведомлений
    нет, поэтому дублей и потерь доставки быть не может. Событие без
    ``project_id`` баннера не рождает: проект неизвестен.
    """
    from hub.services.steward_dispatch import verdict_delegated_to_steward

    seen: set[tuple[int, str]] = set()
    for event in await _refusals(db, project_id=project_id):
        if event["project_id"] is not None:
            seen.add(
                (int(event["project_id"]), group_of(str(event["order_kind"] or "")))
            )
    if not seen:
        return []
    projects = {int(p["id"]): p for p in await repo.list_projects(db)}
    day = utc_day()
    rows = []
    for pid, group in sorted(seen):
        project = projects.get(pid)
        if project is None or not verdict_delegated_to_steward(project):
            continue
        used, cap = await used_today(db, pid, group), cap_of(group)
        rows.append(
            {
                "slug": str(project["slug"]),
                "kind_group": group,
                "used": used,
                "cap": cap,
                "reset_at": reset_at(day),
                "line": (
                    "стюард: сегодня были отказы из-за суточного потолка на "
                    f"{project['slug']} ({_GROUP_LABEL[group]}); сейчас "
                    f"{used}/{cap}; сброс {reset_at(day)} UTC"
                ),
            }
        )
    return rows


def alert_text(kind: str, generation: int, detail: str, day: str) -> str:
    """Запись ленты задачи на отказ по потолку."""
    what = (
        "заказ чтения постановки повторится после сброса"
        if kind == "dor"
        else "до тех пор вердикт за человеком"
    )
    return (
        f"Суточный потолок стюарда исчерпан: прогон «{kind}» для сдачи/ревизии "
        f"{generation} не заказан. {detail}. Сброс {reset_at(day)} UTC; {what}."
    )
