"""Разбор прогона k8 владельцу: Входящие, действие «разбор принят», дайджест (#1943).

Рекомендацию пишет #1942 (``executor_triage``), этот модуль её только ПОКАЗЫВАЕТ
и принимает ответ человека. Контракт (его читает #1947, держать явным):

* **Пункт Входящих** = строка ``executor_triage_recommendations`` с
  ``owner_verdict IS NULL``, у которой ``order_id`` - ПОСЛЕДНИЙ host-заказ задачи и
  ``executor_triage.project(snapshot).after_failed_run`` истинно сейчас (задача
  ``open``, не архивная, не взята, нет сдачи). Это чтение текущего состояния для
  ПОКАЗА; исход и совпадение по нему не считаются - они остаются на метках
  ``followup`` из #1942 (долговечные ``executor_orders.created_at`` и
  ``submissions.submitted_at``).
* **Действие владельца** ``record_owner_verdict``: один оператор
  ``UPDATE ... WHERE owner_verdict IS NULL``; значение пишется один раз и не
  перезаписывается (повтор - ``already_decided``). ``followup`` и статус задачи
  не меняются, задача в ``needs_decision`` не переводится, ничего не запускается.
  Событие ``executor_triage_verdict`` НЕ входит в ``HUMAN_GATE_EVENT_KINDS``.
  Кто вправе: только человек (REST ``require_human_or_admin``, веб
  ``_require_human_web``, CSRF для cookie общим middleware).
* **Дайджест** ``digest_sections``: N/K/M считаются по ``created_at``
  рекомендации, X/Y - по ``followup_at`` (время записи исхода свипом, один раз),
  полуинтервал суток UTC. В Y не входят ``submitted``, ``none_72h``, ``unclear``
  (они дают ``unmeasured``), как в ``executor_triage.agreement``.
* Сбой чтения - ``unavailable`` с причиной, не «пусто» и не ноль.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Literal

import aiosqlite
from pydantic import BaseModel

from hub import repository as repo
from hub.db import fetchall, write_transaction
from hub.services import executor_triage as triage

log = logging.getLogger("hub")

VERDICTS: Final[tuple[str, ...]] = ("accepted", "rejected")
EVENT_VERDICT: Final[str] = "executor_triage_verdict"
MEASURED: Final[str] = "measured"
UNMEASURED: Final[str] = "unmeasured"
UNAVAILABLE: Final[str] = "unavailable"

#: Исходы, которых нет в знаменателе совпадения (как ``executor_triage.agreement``).
_UNMEASURED_FOLLOWUPS: Final[frozenset[str]] = frozenset(
    {*triage.STALE_FOLLOWUPS, triage.NO_DECISION_FOLLOWUP, triage.UNCLEAR_FOLLOWUP}
)

#: Рекомендации без ответа владельца по открытым задачам области. ``{where}`` -
#: условия по ``tasks`` (статус, архив, проект, «чьё это») внутри подзапроса,
#: чтобы проект и человек отбирались до всякого показа (#627).
_ITEMS_SQL: Final[str] = """
SELECT r.id AS recommendation_id, r.order_id, r.task_id, r.triage_class,
       r.class_name, r.action, r.reason, r.created_at, t.title
FROM executor_triage_recommendations r JOIN tasks t ON t.id = r.task_id
WHERE r.owner_verdict IS NULL AND r.task_id IN (SELECT id FROM tasks WHERE {where})
ORDER BY r.id
"""


class TriageVerdictBody(BaseModel):
    """Тело ``POST /api/executor-triage/{id}/verdict``: значение вне списка - 422."""

    verdict: Literal["accepted", "rejected"]


class VerdictError(Exception):
    """Отказ действия владельца: ``status`` - код HTTP, ``code`` - машинное имя."""

    def __init__(self, status: int, code: str, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.code = code
        self.text = text


def _scope_sql(scoped: dict[str, Any]) -> tuple[str, list[Any]]:
    conditions = ["status = 'open'", "archived = 0"]
    params: list[Any] = []
    if scoped.get("project_id") is not None:
        conditions.append(repo.PROJECT_SUBTREE_CONDITION)
        params.append(scoped["project_id"])
    repo._append_person_filters(
        conditions,
        params,
        human_owner=scoped.get("human_owner"),
        claimed_by=scoped.get("claimed_by"),
        mine=scoped.get("mine"),
    )
    return " AND ".join(conditions), params


def _item(row: Any) -> dict[str, Any]:
    action = str(row["action"])
    return {
        "recommendation_id": int(row["recommendation_id"]),
        "order_id": int(row["order_id"]),
        "task_id": int(row["task_id"]),
        "title": str(row["title"] or ""),
        "class": int(row["triage_class"]),
        "class_name": str(row["class_name"] or ""),
        "action": action,
        "recommendation": triage.ACTION_TEXT.get(action, action),
        "reason": str(row["reason"] or ""),
        "created_at": str(row["created_at"] or ""),
    }


async def _still_after_failed_run(db: aiosqlite.Connection, row: Any) -> bool:
    snap = await triage._read_snapshot(db, int(row["task_id"]))
    return (
        snap is not None
        and int(snap["order_id"]) == int(row["order_id"])
        and bool(triage.project(snap)["after_failed_run"])
    )


async def inbox_items(
    db: aiosqlite.Connection, scoped: dict[str, Any], *, cap: int
) -> dict[str, Any]:
    """Неразобранные рекомендации для Входящих.

    ``{"items": [...], "unavailable": "", "note": ""}``. Сбой чтения даёт
    ``unavailable`` с причиной и пустые ``items``: «не смог прочитать» и
    «прочитал, разбирать нечего» - разные исходы.
    """
    where, params = _scope_sql(scoped)
    try:
        rows = await fetchall(
            db,
            _ITEMS_SQL.format(where=where),  # nosec B608 - where собран из констант
            tuple(params),
        )
        items = [_item(r) for r in rows if await _still_after_failed_run(db, r)]
    except Exception as exc:  # noqa: BLE001 - входящие рендерятся без разбора
        log.warning("triage inbox unreadable: %s", exc)
        return {
            "items": [],
            "unavailable": f"{type(exc).__name__}: {exc}"[:200],
            "note": "",
        }
    note = f"показано {cap} из {len(items)}" if len(items) > cap else ""
    return {"items": items[:cap], "unavailable": "", "note": note}


async def record_owner_verdict(
    db: aiosqlite.Connection,
    recommendation_id: int,
    verdict: str,
    *,
    who: str,
    principal_id: int | None = None,
) -> dict[str, Any]:
    """Записать «разбор принят / отклонён»; значение пишется один раз."""
    if verdict not in VERDICTS:
        raise VerdictError(422, "bad_verdict", f"verdict must be one of {VERDICTS}")
    async with write_transaction(db):
        rows = await fetchall(
            db,
            "SELECT id, order_id, task_id, action, owner_verdict "
            "FROM executor_triage_recommendations WHERE id = ?",
            (int(recommendation_id),),
        )
        if not rows:
            raise VerdictError(404, "not_found", "рекомендация не найдена")
        row = rows[0]
        cursor = await db.execute(
            "UPDATE executor_triage_recommendations SET owner_verdict = ?, "
            "owner_verdict_at = datetime('now') "
            "WHERE id = ? AND owner_verdict IS NULL",
            (verdict, int(recommendation_id)),
        )
        if not cursor.rowcount:
            raise VerdictError(
                409,
                "already_decided",
                f"решение уже записано: {row['owner_verdict']}",
            )
        task_id = int(row["task_id"])
        project_row = await repo.resolve_project_for_task(db, task_id)
        await repo.insert_event(
            db,
            kind=EVENT_VERDICT,
            task_id=task_id,
            project_id=int(project_row["id"]) if project_row else None,
            actor=who,
            payload={
                "recommendation_id": int(recommendation_id),
                "order_id": int(row["order_id"]),
                "verdict": verdict,
            },
        )
        word = "принят" if verdict == "accepted" else "отклонён"
        await repo.add_task_update(
            db,
            task_id,
            who,
            "status",
            f"Разбор прогона k8 {word} владельцем (рекомендация "
            f"#{recommendation_id}, заказ #{row['order_id']}, "
            f"действие {row['action']}).",
            principal_id=principal_id,
            author_kind="principal",
        )
    return {
        "recommendation_id": int(recommendation_id),
        "task_id": task_id,
        "owner_verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Дайджест
# ---------------------------------------------------------------------------

#: ``action_at`` - время самого действия, выбранного исходом (``followup_action_at``);
#: у строк с исходом до колонки его нет, и они читаются по ``followup_at``.
_DIGEST_ROWS_SQL: Final[str] = """
SELECT task_id, action, created_at, followup, followup_at,
       COALESCE(followup_action_at, followup_at) AS action_at
FROM executor_triage_recommendations
WHERE (created_at >= ? AND created_at < ?)
   OR (followup IS NOT NULL
       AND (COALESCE(followup_action_at, followup_at) >= ?
            AND COALESCE(followup_action_at, followup_at) < ?
            OR followup_at >= ? AND followup_at < ?))
"""


def empty_section() -> dict[str, Any]:
    """Раздел дня без разбора: измерено, ноль."""
    return {
        "state": MEASURED,
        "created": 0,
        "retry": 0,
        "owner": 0,
        "resolved": 0,
        "matched": 0,
        "unmeasured": 0,
        "late": 0,
    }


def section_active(section: dict[str, Any]) -> bool:
    """В сутках что-то создано, решено или разбор не прочитан: день стоит дайджеста.

    Нечитаемый разбор - не «пусто»: без дайджеста владелец не узнал бы о сбое.
    """
    return bool(
        section.get("state") == UNAVAILABLE
        or section.get("created")
        or section.get("resolved")
        or section.get("unmeasured")
        or section.get("late")
    )


async def _counted_in_its_day(
    db: aiosqlite.Connection, project_id: int, row: Any
) -> bool:
    """Дайджест дня действия собран ПОСЛЕ обработки исхода: исход в нём уже есть."""
    found = await fetchall(
        db,
        "SELECT created_at FROM autopilot_digests "
        "WHERE project_id = ? AND digest_date = ?",
        (project_id, str(row["action_at"])[:10]),
    )
    return bool(found) and str(found[0]["created_at"]) > str(row["followup_at"])


async def _count(
    db: aiosqlite.Connection,
    section: dict[str, Any],
    row: Any,
    project_id: int,
    window: tuple[str, str],
) -> None:
    """Учесть строку в сутках ``window``: создание - по created_at, исход - по
    времени ДЕЙСТВИЯ; исход, обработанный в этих сутках после сборки дайджеста дня
    действия, идёт отдельным числом ``late`` и в X/Y не входит."""
    start, end = window
    if start <= str(row["created_at"]) < end:
        section["created"] += 1
        retry = row["action"] in triage.RETRY_ACTIONS
        section["retry" if retry else "owner"] += 1
    followup = row["followup"]
    if followup is None:
        return
    if start <= str(row["action_at"] or "") < end:
        if followup in _UNMEASURED_FOLLOWUPS:
            section["unmeasured"] += 1
            return
        section["resolved"] += 1
        hit = followup in triage.MATCHING.get(row["action"], frozenset())
        section["matched"] += hit
    elif (
        start <= str(row["followup_at"] or "") < end
        and str(row["action_at"] or "") < start
    ):
        if not await _counted_in_its_day(db, project_id, row):
            section["late"] += 1


async def digest_sections(
    db: aiosqlite.Connection, day_start: str, day_end: str
) -> dict[int, dict[str, Any]]:
    """Раздел «разбор» по проектам за [day_start, day_end); ключ - id проекта.

    Исключение пробрасывается: вызывающий пишет ``unavailable_section`` во все
    дайджесты вместо молчаливого нуля.
    """
    rows = await fetchall(db, _DIGEST_ROWS_SQL, (day_start, day_end) * 3)
    sections: dict[int, dict[str, Any]] = {}
    owners: dict[int, int | None] = {}
    for row in rows:
        task_id = int(row["task_id"])
        if task_id not in owners:
            owner = await repo.resolve_project_for_task(db, task_id)
            owners[task_id] = int(owner["id"]) if owner else None
        project_id = owners[task_id]
        if project_id is None:
            continue
        await _count(
            db,
            sections.setdefault(project_id, empty_section()),
            row,
            project_id,
            (day_start, day_end),
        )
    return sections


async def section_map(
    db: aiosqlite.Connection, day_start: str, day_end: str
) -> tuple[dict[int, dict[str, Any]], str]:
    """(разделы по проектам, причина сбоя); сбой не превращается в ноль."""
    try:
        return await digest_sections(db, day_start, day_end), ""
    except Exception as exc:  # noqa: BLE001 - дайджест выходит без раздела разбора
        log.warning("digest triage section unreadable: %s", exc)
        return {}, f"{type(exc).__name__}: {exc}"


def section_for(
    project_id: int, sections: dict[int, dict[str, Any]], failure: str
) -> dict[str, Any]:
    """Раздел проекта: данные, пустой день или ``unavailable`` при сбое чтения."""
    if failure:
        return unavailable_section(failure)
    return sections.get(project_id) or empty_section()


def unavailable_section(reason: str) -> dict[str, Any]:
    return {**empty_section(), "state": UNAVAILABLE, "reason": reason[:200]}


def section_state(payload: dict[str, Any]) -> str:
    """Считал ли дайджест разбор: ``measured``, ``unavailable`` или ``unmeasured``.

    Дайджест старше раздела ключа не несёт: ноль там не измерялся.
    """
    section = payload.get("triage")
    if not isinstance(section, dict):
        return UNMEASURED
    return str(section.get("state") or UNMEASURED)


def section_line(section: dict[str, Any]) -> str:
    """Строка дайджеста: «разобрано N, рекомендован повтор K, к владельцу M; ...»."""
    verdicts = (
        f"совпало с вашим решением {section['matched']} из {section['resolved']}"
        if section["resolved"]
        else "решений владельца для сверки пока нет"
    )
    tail = f"; не измерено {section['unmeasured']}" if section["unmeasured"] else ""
    if section.get("late"):
        tail += f"; досчитано позже {section['late']}"
    return (
        f"разобрано {section['created']}, рекомендован повтор {section['retry']}, "
        f"к владельцу {section['owner']}; {verdicts}{tail}"
    )
