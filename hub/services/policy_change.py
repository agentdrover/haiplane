"""Правки политики проекта: ОДИН путь для PATCH и для расписания (#1593).

Слияние присланного куска с сохранённой политикой (#1427), замок #743 и
проверка исполнимости review (#1119/#1188) жили в hub/app.py и вызывались
только PATCH-ом проекта. Отложенная правка, исполняемая поллером, обязана
пройти ТЕ ЖЕ проверки, а не их копию: копия расходится на первой же правке
замка, и расписание стало бы дорогой мимо него. Поэтому проверки вынесены
сюда, и PATCH, создание записи расписания и поллер зовут одни и те же функции.

Сериализация. Ручной PATCH и поллер читают политику и пишут её в ОДНОЙ
write-транзакции (``write_transaction``, BEGIN IMMEDIATE): чтение «до» берётся
уже под write-локом, поэтому ни один из двух не затрёт правку другого, а
запись расписания исполняется ровно один раз при любом числе процессов.

Что здесь НЕ делается: напоминания без правки политики (этап 2), повторяющиеся
записи, планирование агентами.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import write_transaction
from hub.services import project_policy

log = logging.getLogger("hub")

POLICY_CHANGED_EVENT = "project_gate_policy_changed"
REFUSED_EVENT = "scheduled_policy_change_refused"
CREATED_EVENT = "scheduled_policy_change_created"
CANCELLED_EVENT = "scheduled_policy_change_cancelled"

#: Лимит pending-записей на проект: расписание — не очередь задач.
MAX_PENDING_PER_PROJECT = 50
#: Исполнение позже срока на столько секунд и больше помечается опоздавшим
#: (хаб лежал или поллер отстал): правка всё равно исполняется, но событие
#: и результат называют опоздание.
LATE_AFTER_SECONDS = 300
#: Верхняя граница записей за один проход поллера.
MAX_PER_PASS = 100

_STAMP = "%Y-%m-%dT%H:%M:%SZ"


class PolicyRefused(Exception):
    """Отказ общего пути правки: статус HTTP и тело, как у PATCH."""

    def __init__(self, status: int, detail: dict[str, Any]):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def utcnow() -> datetime:
    """Часы расписания; тесты подменяют эту функцию, а не спят."""
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    """Один формат времени в таблице — порядок строк равен порядку времени."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime(_STAMP)


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, _STAMP).replace(tzinfo=UTC)


def policy_version(project: Any) -> str:
    """Отпечаток сохранённой политики: меняется ровно при смене её содержимого.

    Форма проекта несёт политику целиком; открытая ДО исполнения правки и
    отправленная ПОСЛЕ, она вернула бы старые значения поверх новых. Версия,
    с которой форма составлена, сверяется внутри write-транзакции.
    """
    canonical = json.dumps(
        project_policy.gate_policy_of(project), sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


async def refuse_unrunnable_review(db, before, fields: dict) -> None:
    """Не хранить как исполнимую политику, исполнить которую нельзя (#1119).

    ``review=dispatch`` там, где ревью НЕЧЕМ добыть, — не «попробуем», а
    гарантированный отказ на КАЖДОЙ сдаче. Отказать на записи дешевле: тут
    есть кому прочитать причину.

    #1188: вопрос «исполнимо ли здесь» задаётся общему читателю
    (review_dispatch.review_reach) — тому же, которого спрашивают диспетчер и
    форма проекта. Это инвариант, а не проверка поля: оба значения берутся
    ПОСЛЕ патча, каждое из патча, если оно там есть, иначе из строки, —
    поэтому PATCH только ``forge`` проходит ту же проверку (отчёт #201).
    """
    from hub.services.review_dispatch import review_reach

    if "forge" not in fields and "gate_policy" not in fields:
        return
    if "gate_policy" in fields and fields["gate_policy"] is not None:
        review_after = str(
            json.loads(fields["gate_policy"]).get("review") or ""
        ).strip()
    else:
        review_after = str(
            project_policy.gate_policy_of(before).get("review") or ""
        ).strip()
    forge_after = str(fields.get("forge") or project_policy.forge_of(before)).strip()
    if review_after != "dispatch":
        return
    reach = await review_reach(db, forge_after)
    if reach.runnable:
        return
    raise PolicyRefused(
        422,
        {
            # Код сменился вместе со смыслом (#1188): «форж не тот» было
            # единственной причиной, пока способ добычи был один.
            "error": "review_unrunnable_here",
            "hint": (
                reach.reason + ". Пока способа нет, поле review принимает только off; "
                "вердикт в любом случае остаётся за человеком"
            ),
        },
    )


def merged_gate_policy(
    before: Any, sent: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Слить присланный кусок gate_policy с сохранённой политикой (#1427).

    PATCH остаётся PATCH и внутри gate_policy: присланный ключ заменяется,
    отсутствующий остаётся, ``null`` у ключа удаляет его. Слияние
    одноуровневое: ``risk_map`` — одно значение.

    Итоговая политика проверяется ЦЕЛИКОМ, и замок #743 смотрит на неё же:
    кусок может быть чистым, а результат — нет (делегат, лежавший в строке,
    записался бы заново как одобренный).

    Возвращает (итоговая политика, {"changed": [...], "removed": [...]}).
    """
    from hub.models import validated_gate_policy

    stored = project_policy.gate_policy_of(before)
    merged = {**stored, **{k: v for k, v in sent.items() if v is not None}}
    for key, value in sent.items():
        if value is None:
            merged.pop(key, None)
    try:
        merged = validated_gate_policy(merged)
    except ValueError as exc:
        raise PolicyRefused(
            422,
            {
                "error": "gate_policy_invalid",
                "hint": f"итоговая политика после слияния не проходит проверку: {exc}",
            },
        ) from exc
    # #743: хаб не ослабляет надзор над собой — проект default (репозиторий
    # самого хаба) не принимает ДЕЛЕГИРУЮЩЕГО значения на гейтах, ни от какого
    # токена и ни по какому расписанию. Правило здесь, а не в модели, потому
    # что ему нужно знать, КАКОЙ проект правят. #760/#1151: проверка по именам
    # двух гейтов и по общему перечню делегатов.
    # #1602: исключение — явный список пар, сейчас одна: verdict=steward.
    violations = project_policy.gate_lock_violations(before["slug"], merged)
    if violations:
        allowed = project_policy.gate_lock_allowed_pairs()
        raise PolicyRefused(
            422,
            {
                "error": "default_project_gate_locked",
                "violations": violations,
                "allowed": allowed,
                "hint": (
                    "замок #743: проект default (сам хаб) не принимает "
                    f"{', '.join(violations)}; разрешено только "
                    f"{', '.join(allowed)} "
                    f"({project_policy.GATE_LOCK_OWNER_DECISION}); "
                    "остальное на default — human; правка не записана"
                ),
            },
        )
    delta = {
        "changed": sorted(
            k for k in merged if k not in stored or stored[k] != merged[k]
        ),
        "removed": sorted(k for k in stored if k not in merged),
    }
    return merged, delta


def was_now(
    stored: dict[str, Any], merged: dict[str, Any], delta: dict[str, list[str]]
) -> dict[str, dict[str, Any]]:
    """«Было → стало» по каждому реально изменённому или снятому ключу."""
    out: dict[str, dict[str, Any]] = {}
    for key in delta["changed"]:
        out[key] = {"was": stored.get(key), "now": merged.get(key)}
    for key in delta["removed"]:
        out[key] = {"was": stored.get(key), "now": None}
    return out


def render_changes(changes: dict[str, dict[str, Any]]) -> str:
    """«deep_daily_cap: 2 → 4; steward_shadow: true → false» одной строкой."""

    def show(value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value)

    return "; ".join(
        f"{key}: {show(pair['was'])} → {show(pair['now'])}"
        for key, pair in sorted(changes.items())
    )


async def apply_project_fields(
    db: aiosqlite.Connection,
    before: Any,
    fields: dict[str, Any],
    *,
    actor: str,
    by: str = "",
    schedule: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Применить правку проекта: слияние, замок, исполнимость, запись, событие.

    Вызывающий владеет ``write_transaction`` и вызывает функцию ПОСЛЕ чтения
    ``before`` внутри неё. Коммита здесь нет: политика, событие и — у
    расписания — итог записи фиксируются вместе или не фиксируются вовсе.

    ``fields`` — поля ProjectPatch после model_dump; ``gate_policy`` — кусок
    (dict), не итог. Возвращает «было → стало» по ключам политики.
    """
    fields = dict(fields)
    expected = fields.pop("policy_version", None)
    if expected is not None and expected != policy_version(before):
        raise PolicyRefused(
            422,
            {
                "error": "policy_version_stale",
                "hint": (
                    "политика проекта изменилась после того, как форма была "
                    "открыта (например, исполнилась отложенная правка); "
                    "НИЧЕГО не сохранено — откройте проект заново и повторите правку"
                ),
            },
        )
    delta: dict[str, list[str]] | None = None
    changes: dict[str, dict[str, Any]] = {}
    if fields.get("gate_policy") is not None:
        merged, delta = merged_gate_policy(before, fields["gate_policy"])
        changes = was_now(project_policy.gate_policy_of(before), merged, delta)
        fields["gate_policy"] = json.dumps(merged)
    await refuse_unrunnable_review(db, before, fields)
    if fields:
        await repo.update_project(db, int(before["id"]), **fields)
    changed = bool(delta and (delta["changed"] or delta["removed"]))
    # Исполнение расписания видно в событиях ВСЕГДА: запись, значение которой
    # человек успел поставить руками, всё равно стала applied, и без события
    # это исполнение исчезало бы из ленты и из last_change. Ручной PATCH без
    # изменений, как и раньше, событий не пишет.
    if delta is not None and (changed or schedule):
        payload: dict[str, Any] = {
            "slug": before["slug"],
            **delta,
            "changes": changes,
            "by": by,
            "actor": actor,
        }
        if not changed:
            payload["unchanged"] = True
        if schedule:
            payload.update(schedule)
        await repo.insert_event(
            db,
            kind=POLICY_CHANGED_EVENT,
            project_id=int(before["id"]),
            actor=actor,
            payload=payload,
        )
    return changes


# ---------------------------------------------------------------------------
# Расписание
# ---------------------------------------------------------------------------


async def create_scheduled_change(
    db: aiosqlite.Connection,
    project: Any,
    *,
    at: datetime,
    patch: dict[str, Any],
    note: str,
    created_by: str,
    now: datetime | None = None,
) -> int:
    """Принять запись расписания: полная валидация на ТЕКУЩЕЙ политике.

    Проверяется то же, что проверит исполнение: слияние, замок #743,
    исполнимость review. Это ранний отказ, а не гарантия — к моменту ``at``
    политика, форж или локальная конфигурация могут стать другими, и тогда
    исполнение откажет с причиной (refused), а не применит силой.
    """
    now = now or utcnow()
    at_s = stamp(at)
    if at_s <= stamp(now):
        raise PolicyRefused(
            422,
            {
                "error": "schedule_at_in_past",
                "hint": f"at {at_s} не в будущем (сейчас {stamp(now)} UTC)",
            },
        )
    async with write_transaction(db):
        fresh = await repo.get_project(db, int(project["id"]))
        if fresh is None:
            raise PolicyRefused(404, {"error": "project_not_found", "hint": ""})
        pending = await repo.count_pending_scheduled_policy_changes(
            db, int(fresh["id"])
        )
        if pending >= MAX_PENDING_PER_PROJECT:
            raise PolicyRefused(
                422,
                {
                    "error": "schedule_limit_reached",
                    "hint": (
                        f"у проекта уже {pending} ожидающих правок "
                        f"(предел {MAX_PENDING_PER_PROJECT}); отмените лишние"
                    ),
                },
            )
        merged, _ = merged_gate_policy(fresh, patch)
        await refuse_unrunnable_review(db, fresh, {"gate_policy": json.dumps(merged)})
        change_id = await repo.insert_scheduled_policy_change(
            db,
            project_id=int(fresh["id"]),
            at=at_s,
            patch=patch,
            note=note,
            created_by=created_by,
        )
        await repo.insert_event(
            db,
            kind=CREATED_EVENT,
            project_id=int(fresh["id"]),
            actor="human",
            payload={
                "slug": fresh["slug"],
                "schedule_id": change_id,
                "at": at_s,
                "patch": patch,
                "by": created_by,
            },
        )
    return change_id


async def cancel_scheduled_change(
    db: aiosqlite.Connection, project: Any, change_id: int, *, by: str
) -> None:
    async with write_transaction(db):
        row = await repo.get_scheduled_policy_change(db, change_id)
        if row is None or int(row["project_id"]) != int(project["id"]):
            raise PolicyRefused(
                404, {"error": "schedule_not_found", "hint": f"запись {change_id}"}
            )
        if not await repo.cancel_scheduled_policy_change(
            db, change_id, result={"outcome": "cancelled", "by": by}
        ):
            raise PolicyRefused(
                409,
                {
                    "error": "schedule_not_pending",
                    "hint": (
                        f"запись {change_id} уже в состоянии {row['state']}: "
                        "отменить можно только ожидающую"
                    ),
                },
            )
        await repo.insert_event(
            db,
            kind=CANCELLED_EVENT,
            project_id=int(project["id"]),
            actor="human",
            payload={"slug": project["slug"], "schedule_id": change_id, "by": by},
        )


async def _settle_refused(
    db: aiosqlite.Connection,
    row: Any,
    project: Any,
    refusal: PolicyRefused,
    now_s: str,
    late: dict[str, Any],
) -> dict[str, Any]:
    reason = str(refusal.detail.get("hint") or refusal.detail.get("error") or "")
    result = {
        "outcome": "refused",
        "error": refusal.detail.get("error", ""),
        "reason": reason,
        **late,
    }
    await repo.settle_scheduled_policy_change(
        db, int(row["id"]), state="refused", executed_at=now_s, result=result
    )
    slug = project["slug"] if project is not None else "?"
    await repo.insert_event(
        db,
        kind=REFUSED_EVENT,
        project_id=int(row["project_id"]),
        actor="schedule",
        payload={
            "slug": slug,
            "schedule_id": int(row["id"]),
            "at": row["at"],
            "patch": json.loads(row["patch"]),
            "error": result["error"],
            "reason": reason,
        },
    )
    await db.execute(
        "INSERT INTO activity_log (kind, summary, detail) VALUES (?, ?, ?)",
        (
            REFUSED_EVENT,
            f"{slug}: отложенная правка политики #{row['id']} отклонена"[:200],
            reason,
        ),
    )
    return result


async def execute_one(
    db: aiosqlite.Connection, row: Any, now: datetime
) -> dict[str, Any]:
    """Исполнить уже прочитанную ПОД write-локом запись; итог — в той же транзакции."""
    now_s = stamp(now)
    late_seconds = int((now - parse_stamp(row["at"])).total_seconds())
    late = {"late": late_seconds > LATE_AFTER_SECONDS}
    if late["late"]:
        late["late_seconds"] = late_seconds  # type: ignore[assignment]
    project = await repo.get_project(db, int(row["project_id"]))
    if project is None:
        return await _settle_refused(
            db,
            row,
            None,
            PolicyRefused(
                422, {"error": "project_not_found", "hint": "проект не найден"}
            ),
            now_s,
            late,
        )
    try:
        changes = await apply_project_fields(
            db,
            project,
            {"gate_policy": json.loads(row["patch"])},
            actor="schedule",
            by=f"schedule #{row['id']}",
            schedule={"schedule_id": int(row["id"]), "note": row["note"], **late},
        )
    except PolicyRefused as refusal:
        return await _settle_refused(db, row, project, refusal, now_s, late)
    result = {
        "outcome": "applied",
        "changes": changes,
        "summary": render_changes(changes) or "без изменений",
        **late,
    }
    await repo.settle_scheduled_policy_change(
        db, int(row["id"]), state="applied", executed_at=now_s, result=result
    )
    return result


async def run_due(
    db: aiosqlite.Connection, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Один проход поллера: исполнить просроченные записи в порядке (at, id).

    Одна запись — одна write-транзакция. Запись читается ЗАНОВО под write-
    локом, поэтому при двух процессах вторая транзакция видит итог первой и
    берёт следующую запись, а не ту же. Сбой между записью политики/события
    и фиксацией итога откатывает всё: запись остаётся pending и исполняется
    следующим проходом. Проход после сбоя прекращается, чтобы более поздняя
    запись того же ключа не обогнала упавшую.
    """
    now = now or utcnow()
    now_s = stamp(now)
    outcomes: list[dict[str, Any]] = []
    if not await repo.has_due_scheduled_policy_change(db, now_s):
        return outcomes
    for _ in range(MAX_PER_PASS):
        try:
            async with write_transaction(db):
                row = await repo.next_due_scheduled_policy_change(db, now_s)
                if row is None:
                    return outcomes
                outcome = await execute_one(db, row, now)
        except Exception:
            log.exception("Poll: scheduled policy change failed — stays pending")
            return outcomes
        outcomes.append({"id": int(row["id"]), **outcome})
    return outcomes


def view_row(row: Any) -> dict[str, Any]:
    """Строка таблицы → словарь для API/CLI/MCP."""

    def load(raw: str | None) -> dict[str, Any]:
        try:
            value = json.loads(raw or "{}")
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    return {
        "id": int(row["id"]),
        "project_id": int(row["project_id"]),
        "at": row["at"],
        "patch": load(row["patch"]),
        "note": row["note"] or "",
        "created_by": row["created_by"] or "",
        "created_at": row["created_at"] or "",
        "state": row["state"],
        "executed_at": row["executed_at"],
        "result": load(row["result"]),
    }


def format_schedule(slug: str, rows: list[dict[str, Any]]) -> list[str]:
    """Строки списка расписания — общие для CLI и MCP."""
    if not rows:
        return [f"Policy schedule of project {slug}: empty"]
    lines = [f"Policy schedule of project {slug}"]
    for item in rows:
        head = f"  #{item['id']} {item['state']} at {item['at']}: " + json.dumps(
            item["patch"], ensure_ascii=False, sort_keys=True
        )
        if item["note"]:
            head += f" — {item['note']}"
        lines.append(head)
        result = item.get("result") or {}
        if item["state"] == "applied":
            lines.append(
                f"    {result.get('summary', '')}"
                + (" (late)" if result.get("late") else "")
            )
        elif item["state"] == "refused":
            lines.append(f"    refused: {result.get('reason', '')}")
    return lines
