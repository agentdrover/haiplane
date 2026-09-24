"""Очередь исполнения: какую задачу проекта хаб взял бы следующей (#1274).

Первое звено оркестратора (эпик #1272), и только читатель. Хаб называет
следующую задачу и объясняет, почему остальные ждут; запуск исполнителя —
F2 (#1365). До этой задачи порядок держала память сессии-стюарда: #1260,
#1262 и #1254 правят один review_dispatch.py и были выстроены вручную.

Кандидат — задача проекта, которая:

* ``open``, не эпик, не в архиве, с ``dor_passed``;
* все её ``depends_on`` ДОСТАВЛЕНЫ. Читатель тот же, что у готовности и
  доказательств стюарда (``with_cached_delivery``, #484/#885/#1281): второй
  копии правила «доставлено» здесь нет. «Узнать не удалось» (``None``) —
  не доставлено: незнание не снимает зависимость;
* объявила ``affected_areas`` — пустая декларация не «ни с чем не
  пересекается», а «неизвестно с чем», и такой кандидат пропускается с
  причиной;
* ни один её путь не пересекается с путями задач проекта в running/review.

И проект целиком — WIP (running + review) ниже ``wip_limit`` политики.
Порядок кандидатов: priority, затем position, затем id.

Пересечение — префиксное, в ОБЕ стороны и по границе пути. В одну сторону
(как ``ladder_hits``) широкая декларация «hub/» у кандидата проходила бы
мимо узкого «hub/poller.py» в работе (#1147); по голой строке «hub» задевал
бы «hubble/». Глоб «*» режет путь до себя и сравнивается как префикс.

Ни одна функция здесь не пишет в задачу. Единственная запись —
``announce_once``: событие ``orchestrator_next_candidate`` в ленту проекта,
и только при смене ответа.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import deserialize_str_list, fetchall
from hub.services import project_policy
from hub.services.delivery_state import with_cached_delivery

EVENT_NEXT_CANDIDATE = "orchestrator_next_candidate"

SKIP_DEPENDENCY = "dependency_undelivered"
SKIP_UNDECLARED = "area_undeclared"
SKIP_OVERLAP = "area_overlap"

#: Что занимает WIP и с чем сверяются области. Ровно то, что названо в
#: постановке: задача в работе и задача, ждущая ревью.
ACTIVE_STATUSES: tuple[str, ...] = ("running", "review")

_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _covers(outer: str, inner: str) -> bool:
    """Покрывает ли объявленный путь ``outer`` путь ``inner``."""
    if "*" in outer:
        return inner.startswith(outer.split("*", 1)[0])
    base = outer.rstrip("/")
    return inner == base or inner.startswith(base + "/")


def _normalized(path: str) -> str:
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def overlap(a: str, b: str) -> bool:
    """Пересекаются ли два объявленных пути — в любую сторону."""
    a, b = _normalized(a), _normalized(b)
    if not a or not b:
        return False
    return _covers(a, b) or _covers(b, a)


def _sort_key(task: dict[str, Any]) -> tuple[int, int, int]:
    return (
        _PRIORITY_RANK.get(str(task.get("priority") or ""), len(_PRIORITY_RANK)),
        int(task.get("position") or 0),
        int(task["id"]),
    )


async def _project_tasks(
    db: aiosqlite.Connection, project_id: int
) -> list[dict[str, Any]]:
    """Живые задачи проекта в open/running/review.

    Принадлежность проекту — через ``resolve_project_for_task``, как у
    очереди review (#1264): поддерево теряет задачи, которые default держит
    по умолчанию, а они — большая часть его очереди.
    """
    rows = await fetchall(
        db,
        "SELECT id, title, status, task_type, priority, position, dor_passed, "
        "affected_areas FROM tasks WHERE archived=0 "
        "AND status IN ('open', 'running', 'review') ORDER BY id",
    )
    tasks: list[dict[str, Any]] = []
    for row in rows:
        project = await repo.resolve_project_for_task(db, int(row["id"]))
        if project is not None and int(project["id"]) == project_id:
            task = dict(row)
            task["areas"] = [
                p for p in deserialize_str_list(task.get("affected_areas")) if p.strip()
            ]
            tasks.append(task)
    return tasks


async def _undelivered_blockers(
    db: aiosqlite.Connection, task_id: int
) -> list[dict[str, Any]]:
    edges = await repo.list_task_dependencies(db, task_id)
    blockers = await with_cached_delivery(
        db, [dict(e) for e in edges.get("blocked_by", [])]
    )
    return [
        {
            "task_id": int(b["task_id"]),
            "delivered": b.get("delivered"),
            "reason": b.get("reason") or "",
        }
        for b in blockers
        if b.get("delivered") is not True
    ]


def _first_overlap(
    candidate: dict[str, Any], active: list[dict[str, Any]]
) -> dict[str, Any] | None:
    for path in candidate["areas"]:
        for other in active:
            for other_path in other["areas"]:
                if overlap(path, other_path):
                    return {
                        "path": path,
                        "with_task_id": int(other["id"]),
                        "with_path": other_path,
                    }
    return None


async def _skip_reason(
    db: aiosqlite.Connection, candidate: dict[str, Any], active: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Почему этот кандидат ждёт; ``None`` — не ждёт."""
    task_id = int(candidate["id"])
    blockers = await _undelivered_blockers(db, task_id)
    if blockers:
        listed = ", ".join(f"#{b['task_id']}" for b in blockers)
        return {
            "task_id": task_id,
            "reason": SKIP_DEPENDENCY,
            "detail": f"зависимость не доставлена: {listed}",
            "blockers": blockers,
        }
    if not candidate["areas"]:
        return {
            "task_id": task_id,
            "reason": SKIP_UNDECLARED,
            "detail": "область не объявлена: affected_areas пусты, "
            "пересечение с работой в полёте не проверить",
        }
    hit = _first_overlap(candidate, active)
    if hit is not None:
        return {
            "task_id": task_id,
            "reason": SKIP_OVERLAP,
            "detail": (
                f"{hit['path']} пересекается с {hit['with_path']} "
                f"задачи #{hit['with_task_id']} в работе"
            ),
            **hit,
        }
    return None


def _summary(answer: dict[str, Any]) -> str:
    if answer["wip_full"]:
        held = ", ".join(f"#{t}" for t in answer["in_progress"])
        return (
            f"Следующей нет: WIP проекта «{answer['project']}» "
            f"{len(answer['in_progress'])} при лимите {answer['wip_limit']} ({held})."
        )
    skipped = "; ".join(f"#{s['task_id']} — {s['detail']}" for s in answer["skipped"])
    tail = f" Пропущены: {skipped}." if skipped else ""
    if answer["next_task_id"] is None:
        return f"Следующей нет: свободного кандидата в «{answer['project']}» нет.{tail}"
    return (
        f"Следующей взял бы #{answer['next_task_id']}: open, DoR пройден, "
        f"зависимости доставлены, области свободны.{tail}"
    )


async def next_task(db: aiosqlite.Connection, project: Any) -> dict[str, Any]:
    """Ответ очереди для одного проекта. Ничего не пишет."""
    policy = project_policy.gate_policy_of(project)
    limit = project_policy.wip_limit_of(policy)
    tasks = await _project_tasks(db, int(project["id"]))
    active = [t for t in tasks if t["status"] in ACTIVE_STATUSES]
    candidates = sorted(
        (
            t
            for t in tasks
            if t["status"] == "open" and t["dor_passed"] and t["task_type"] != "epic"
        ),
        key=_sort_key,
    )
    answer: dict[str, Any] = {
        "project": str(project["slug"]),
        "project_id": int(project["id"]),
        "mode": project_policy.queue_mode_of(policy),
        "wip_limit": limit,
        "in_progress": [int(t["id"]) for t in active],
        "wip_full": limit is not None and len(active) >= limit,
        "next_task_id": None,
        "candidates": [
            {"task_id": int(t["id"]), "priority": t["priority"]} for t in candidates
        ],
        "skipped": [],
    }
    if not answer["wip_full"]:
        for candidate in candidates:
            skip = await _skip_reason(db, candidate, active)
            if skip is None:
                answer["next_task_id"] = int(candidate["id"])
                break
            answer["skipped"].append(skip)
    answer["summary"] = _summary(answer)
    return answer


async def next_tasks(
    db: aiosqlite.Connection, slug: str | None = None
) -> list[dict[str, Any]] | None:
    """Один проект по slug (``None`` — нет такого) или все проекты в тени."""
    if slug:
        project = await repo.get_project_by_slug(db, slug)
        return None if project is None else [await next_task(db, project)]
    return [
        await next_task(db, project)
        for project in await repo.list_projects(db)
        if project_policy.queue_mode_of(project_policy.gate_policy_of(project))
        == project_policy.QUEUE_SHADOW
    ]


def _answer_key(answer: dict[str, Any]) -> dict[str, Any]:
    """То, по чему ответ считается тем же самым: выбор и причины пропусков."""
    return {
        "next_task_id": answer["next_task_id"],
        "wip_full": answer["wip_full"],
        "in_progress": answer["in_progress"] if answer["wip_full"] else [],
        "skipped": [
            [
                s["task_id"],
                s["reason"],
                s.get("with_task_id"),
                s.get("path"),
                [b["task_id"] for b in s.get("blockers", [])],
            ]
            for s in answer["skipped"]
        ],
    }


async def _last_key(db: aiosqlite.Connection, project_id: int) -> dict[str, Any]:
    rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind=? AND project_id=? "
        "ORDER BY id DESC LIMIT 1",
        (EVENT_NEXT_CANDIDATE, project_id),
    )
    if not rows:
        return {}
    try:
        payload = json.loads(rows[0]["payload"] or "{}")
    except ValueError:
        return {}
    key = payload.get("key") if isinstance(payload, dict) else None
    return key if isinstance(key, dict) else {}


async def announce_once(db: aiosqlite.Connection, answer: dict[str, Any]) -> bool:
    """Событие кандидата в ленту проекта — только когда ответ сменился.

    Дедуп по образцу ``steward_shadow._announce_refusal_once``: сравнивается
    ключ ответа с ключом последнего такого события проекта. Пишется одно
    событие и больше ничего — ни статуса, ни claim, ни воркспейса.
    """
    key = _answer_key(answer)
    if await _last_key(db, answer["project_id"]) == key:
        return False
    await repo.insert_event(
        db,
        kind=EVENT_NEXT_CANDIDATE,
        task_id=answer["next_task_id"],
        project_id=answer["project_id"],
        actor="hub",
        payload={**answer, "key": key},
    )
    await db.commit()
    return True


async def shadow_pass(db: aiosqlite.Connection) -> int:
    """Проход поллера: ответ каждого проекта в тени; сколько событий записано."""
    written = 0
    for answer in await next_tasks(db) or []:
        if await announce_once(db, answer):
            written += 1
    return written
