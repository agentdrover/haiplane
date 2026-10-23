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
import json
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


class OutcomeAlreadyCounted(Exception):
    """Исход уже связан с другим дайджестом: транзакция записи откатывается (#1997)."""


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

#: Рекомендации, созданные в сутках: счётчики ``created``/``retry``/``owner``.
_DIGEST_ROWS_SQL: Final[str] = """
SELECT id, task_id, action, created_at
FROM executor_triage_recommendations
WHERE created_at >= ? AND created_at < ?
"""

#: Неучтённые исходы с действием до конца суток (#1997). ``action_at`` - время
#: самого действия, выбранного исходом (``followup_action_at``); у строк с исходом
#: до колонки его нет, и они читаются по ``followup_at``. Очередь неучтённых -
#: строки ``triage_digest_outcomes`` с ``digest_id IS NULL`` (частичный индекс):
#: строку заводит триггер при записи ``followup``, учёт закрывает ту же строку, так
#: что объём работы идёт за числом НЕучтённых, а не всей истории. JSON старых
#: дайджестов не читается. Окно по времени обработки исход не ограничивает:
#: пропущенный нечитаемым дайджестом ждёт первого измеренного, а не пропадает.
OUTCOME_ROWS_SQL: Final[str] = """
SELECT r.id, r.task_id, r.action, r.followup,
       COALESCE(r.followup_action_at, r.followup_at) AS action_at
FROM triage_digest_outcomes l
JOIN executor_triage_recommendations r ON r.id = l.recommendation_id
WHERE l.digest_id IS NULL
  AND r.followup IS NOT NULL
  AND COALESCE(r.followup_action_at, r.followup_at) < ?
"""

#: То же для кандидатов ОДНОГО проекта под write-локом: id отобрала проба до лока.
CLAIM_ROWS_SQL: Final[str] = """
SELECT r.id, r.task_id, r.action, r.followup,
       COALESCE(r.followup_action_at, r.followup_at) AS action_at
FROM triage_digest_outcomes l
JOIN executor_triage_recommendations r ON r.id = l.recommendation_id
WHERE l.digest_id IS NULL
  AND r.followup IS NOT NULL
  AND COALESCE(r.followup_action_at, r.followup_at) < ?
  AND l.recommendation_id IN (SELECT value FROM json_each(?))
"""

#: Связь исхода с дайджестом: (id рекомендации, ``day`` | ``late``).
Link = tuple[int, str]


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
        # id рекомендаций, чей исход вошёл в resolved/unmeasured ЭТОГО дайджеста:
        # по ним опоздавший исход отличается от учтённого (а не по времени записи).
        "outcome_ids": [],
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


def _count_created(section: dict[str, Any], row: Any) -> None:
    section["created"] += 1
    retry = row["action"] in triage.RETRY_ACTIONS
    section["retry" if retry else "owner"] += 1


def _count_outcome(section: dict[str, Any], row: Any, window_start: str) -> Link:
    """Учесть неучтённый исход: в сутках - по времени ДЕЙСТВИЯ (не свипа), раньше -
    как опоздавший (``late``, вне X/Y)."""
    section["outcome_ids"].append(int(row["id"]))
    if str(row["action_at"] or "") < window_start:
        section["late"] += 1
        return int(row["id"]), "late"
    followup = row["followup"]
    if followup in _UNMEASURED_FOLLOWUPS:
        section["unmeasured"] += 1
    else:
        section["resolved"] += 1
        section["matched"] += followup in triage.MATCHING.get(
            row["action"], frozenset()
        )
    return int(row["id"]), "day"


class Probe:
    """Что видно до write-лока: разделы по проектам, созданные и кандидаты исходов.

    ``failure`` непуст, если чтение не удалось: проект тогда получает
    ``unavailable`` вместо молчаливого нуля.
    """

    def __init__(self) -> None:
        self.failure = ""
        self.sections: dict[int, dict[str, Any]] = {}
        self.created: dict[int, list[Any]] = {}
        self.candidates: dict[int, list[int]] = {}

    def section_of(self, project_id: int) -> dict[str, Any]:
        if self.failure:
            return unavailable_section(self.failure)
        return self.sections.get(project_id) or empty_section()


async def _project_resolver(db: aiosqlite.Connection):
    owners: dict[int, int | None] = {}

    async def project_of(row: Any) -> int | None:
        task_id = int(row["task_id"])
        if task_id not in owners:
            owner = await repo.resolve_project_for_task(db, task_id)
            owners[task_id] = int(owner["id"]) if owner else None
        return owners[task_id]

    return project_of


async def probe_sections(
    db: aiosqlite.Connection, day_start: str, day_end: str
) -> Probe:
    """Один проход чтения БЕЗ write-лока: разделы всех проектов и кандидаты исходов.

    Решает, у каких проектов есть что собирать, и отбирает им кандидатов: под
    транзакцией проекта читаются только они (#1997). Сбой чтения не поднимается, а
    записывается в ``failure``.
    """
    probe = Probe()
    try:
        created = await fetchall(db, _DIGEST_ROWS_SQL, (day_start, day_end))
        outcomes = await fetchall(db, OUTCOME_ROWS_SQL, (day_end,))
        project_of = await _project_resolver(db)
        for row in created:
            project_id = await project_of(row)
            if project_id is not None:
                probe.created.setdefault(project_id, []).append(row)
                _count_created(
                    probe.sections.setdefault(project_id, empty_section()), row
                )
        for row in outcomes:
            project_id = await project_of(row)
            if project_id is not None:
                probe.candidates.setdefault(project_id, []).append(int(row["id"]))
                _count_outcome(
                    probe.sections.setdefault(project_id, empty_section()),
                    row,
                    day_start,
                )
    except Exception as exc:  # noqa: BLE001 - дайджест выходит без раздела разбора
        log.warning("digest triage section unreadable: %s", exc)
        return _failed(exc)
    return probe


def _failed(exc: Exception) -> Probe:
    probe = Probe()
    probe.failure = f"{type(exc).__name__}: {exc}"
    return probe


async def digest_sections(
    db: aiosqlite.Connection, day_start: str, day_end: str
) -> dict[int, dict[str, Any]]:
    """Разделы по проектам для проверок и показа; сбой чтения пробрасывается."""
    probe = await probe_sections(db, day_start, day_end)
    if probe.failure:
        raise RuntimeError(probe.failure)
    return probe.sections


async def project_section(
    db: aiosqlite.Connection,
    project_id: int,
    day_start: str,
    day_end: str,
    probe: Probe,
) -> tuple[dict[str, Any], list[Link]]:
    """Раздел проекта и связи к записи - ПОД write-локом, только его исходы.

    Созданные берутся из пробы (они не зависят от учёта), исходы - перечитываются
    из очереди неучтённых по id кандидатов проекта и проверяются заново: учтённый
    другим дайджестом или перенесённый в другой проект исход не берётся. Сбой
    чтения - ``unavailable`` без связей: нечитаемый разбор ничего не учитывает.
    """
    if probe.failure:
        return unavailable_section(probe.failure), []
    section = empty_section()
    links: list[Link] = []
    try:
        for row in probe.created.get(project_id, []):
            _count_created(section, row)
        candidates = probe.candidates.get(project_id, [])
        if candidates:
            rows = await fetchall(db, CLAIM_ROWS_SQL, (day_end, json.dumps(candidates)))
            project_of = await _project_resolver(db)
            for row in rows:
                if await project_of(row) == project_id:
                    links.append(_count_outcome(section, row, day_start))
    except Exception as exc:  # noqa: BLE001 - дайджест выходит без раздела разбора
        log.warning("digest triage section unreadable: %s", exc)
        return unavailable_section(f"{type(exc).__name__}: {exc}"), []
    return section, links


async def record_links(
    db: aiosqlite.Connection, digest_id: int, links: list[Link]
) -> None:
    """Закрыть очередь: связать исходы с дайджестом в транзакции его INSERT (#1997).

    Строка очереди (``digest_id IS NULL``) становится учётом. Если исход уже
    учтён другим дайджестом или строки нет, транзакция откатывается целиком
    (дайджест не остаётся без связи).
    """
    for recommendation_id, kind in links:
        cursor = await db.execute(
            "UPDATE triage_digest_outcomes SET digest_id = ?, kind = ? "
            "WHERE recommendation_id = ? AND digest_id IS NULL",
            (digest_id, kind, recommendation_id),
        )
        if cursor.rowcount != 1:
            raise OutcomeAlreadyCounted(recommendation_id)


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
