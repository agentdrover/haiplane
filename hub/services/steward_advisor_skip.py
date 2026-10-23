"""Когда советник стюарда не нужен: approve всё равно не применится (#2024).

Прогон советника стоит денег и минут, а на части сдач одобрение стюарда уже
исключено причиной, которая от ответа советника не зависит:

* целостность CI этой сдачи не чиста: дифф прочитан и меняет входы CI;
* класс задачи выше обычного потолка проекта, а исключение двойной чистоты
  (#1986) для этого класса недостижимо.

Этот модуль — единственный читатель таких причин для ЗАКАЗА. Он намеренно не
зовёт привратник применения ``steward_apply.apply_refusals``: тот спрашивает
``advisor_refusal`` и двойную чистоту, а ей нужен уже полученный concur, то есть
заказ ждал бы собственного результата. Привратник остаётся у применения и
отказывает по тем же причинам сам: пропуск заказа не ослабляет ни одно правило и
НИКОГДА не считается согласием. Правило «обязательный обоснованный concur»
здесь не затронуто.

Пропускается заказ только там, где причина прочитана и однозначна. Теневой
режим, режим act без выданной выборки пар, проект без делегирования вердикта
стюарду, непросмотренный или нечитаемый дифф, неизвестный класс или потолок и
любой сбой чтения оставляют заказ как был: набор пар для выхода в act
(``steward_exit``) живёт на ответах советника, и молчаливое сужение заказа его
бы остановило.

Причина записывается один раз на (задача, поколение, код): событие и строка
ленты. Строки ``steward_runs`` пропуск не пишет — её уникальный индекс
заблокировал бы заказ, когда причина снимется. Снята причина — следующий тик
заказывает советника обычным путём, ровно один раз.
"""

from __future__ import annotations

import json
import logging

import aiosqlite

from hub import repository as repo
from hub.db import fetchall

log = logging.getLogger(__name__)

EVENT_SKIPPED = "steward_advisor_skipped"
CODE_CI_INTEGRITY = "ci_integrity"
CODE_CLASS_CEILING = "class_above_policy_ceiling"
#: Начало метки в строке ленты: события чистятся по сроку, лента живёт с задачей.
MARK_PREFIX = "[пропуск-советника g="


def mark_of(generation: int, code: str) -> str:
    """Стабильный ключ (поколение, код) в тексте ленты - не зависит от причины."""
    return f"{MARK_PREFIX}{generation} код={code}]"


async def unapprovable_reason(
    db: aiosqlite.Connection, task_id: int
) -> tuple[str, str] | None:
    """``(код, причина)``, если approve стюарда этой сдачи не применится без советника.

    ``None`` — заказывать как обычно. Любой сбой чтения тоже ``None``: «не смог
    прочитать» не причина пропускать платный заказ.
    """
    try:
        return await _reason(db, task_id)
    except Exception as exc:  # noqa: BLE001 - непрочитанное не пропускает заказ
        log.warning("advisor skip not decided for #%s: %s", task_id, exc)
        return None


async def _reason(db: aiosqlite.Connection, task_id: int) -> tuple[str, str] | None:
    from hub.services import steward_shadow
    from hub.services.steward_dispatch import verdict_delegated_to_steward

    if (await steward_shadow.mode_report(db))["effective"] != "act":
        return None  # тень и недостаточная выборка пар: советники нужны как раньше
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None or not verdict_delegated_to_steward(project):
        return None
    row = await repo.get_task(db, task_id)
    if row is None:
        return None
    task = dict(row)
    return await _integrity(db, task) or await _ceiling(db, task)


async def _integrity(db: aiosqlite.Connection, task: dict) -> tuple[str, str] | None:
    from hub.services import ci_integrity

    standing = await ci_integrity.standing_of(db, task)
    if standing.scan.state != ci_integrity.SIGNS or standing.gap is None:
        return None  # не просмотрено или нечитаемо: apply-путь пересканирует
    return CODE_CI_INTEGRITY, f"целостность CI: {standing.reason}"


async def _ceiling(db: aiosqlite.Connection, task: dict) -> tuple[str, str] | None:
    from hub.models import RiskClass
    from hub.services import project_policy
    from hub.services.steward_act_readiness import BLOCKED, ceiling_reason
    from hub.services.steward_apply import current_ceiling
    from hub.services.steward_double_clean import frozen_pair, may_apply_to_class

    key = "steward_approve_max_class"
    task_id = int(task["id"])
    ceiling = await current_ceiling(db, task_id, key)
    if ceiling is None or ceiling == project_policy.STEWARD_CEILING_UNREADABLE:
        return None
    try:
        RiskClass(str(task.get("risk_class") or "").strip())
    except ValueError:
        return None  # класс не вычислен: неизвестное не равно «выше потолка»
    found = ceiling_reason(ceiling, task.get("risk_class"), key=key)
    if found is None or found[0] != BLOCKED:
        return None
    generation = int(task.get("submission_generation") or 0)
    if await may_apply_to_class(
        db, task_id, task.get("risk_class")
    ) and await frozen_pair(db, task_id, generation):
        # Двойная чистота достижима: настройки её допускают, а пара отчётов
        # заморожена при заказе судьи. Остальное решат её условия и советник.
        return None
    return CODE_CLASS_CEILING, found[1]["detail"]  # пустая пара: исключения нет


async def record_skip(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    code: str,
    detail: str,
) -> bool:
    """Записать пропуск один раз на (задача, поколение, код); коммитит вызывающий.

    ``False`` — такая запись уже есть, ленту не повторяем.
    """
    project = await repo.resolve_project_for_task(db, task_id)
    text = (
        f"Советник не заказан для сдачи {generation}: {detail}. Одобрение стюардом "
        "невозможно независимо от ответа советника (привратник применения "
        "отказал бы по той же причине); пропуск не согласие, вердикт за "
        "человеком. Если причина снимется, заказ восстановится сам. "
        + mark_of(generation, code)
    )
    payload = json.dumps(
        {"generation": generation, "code": code, "detail": detail},
        ensure_ascii=False,
    )
    # Проверка и запись — один оператор: два тика не запишут причину дважды. Метка
    # «уже записано» — ключ в строке ленты, а не событие: события чистятся по
    # сроку, лента живёт столько же, сколько задача.
    cursor = await db.execute(
        "INSERT INTO events (kind, task_id, project_id, actor, payload) "
        "SELECT ?, ?, ?, 'hub', ? WHERE NOT EXISTS ("
        "SELECT 1 FROM task_updates WHERE task_id=? AND instr(content, ?) > 0)",
        (
            EVENT_SKIPPED,
            task_id,
            int(project["id"]) if project is not None else None,
            payload,
            task_id,
            mark_of(generation, code),
        ),
    )
    if cursor.rowcount != 1:
        return False
    await repo.add_task_update(db, task_id, "hub", "status", text, author_kind="hub")
    return True


async def recorded_reason(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> str:
    """Текст последнего записанного пропуска этого поколения или пусто."""
    rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind=? AND task_id=? "
        "AND json_extract(payload, '$.generation')=? ORDER BY id DESC LIMIT 1",
        (EVENT_SKIPPED, task_id, generation),
    )
    if rows:
        try:
            return str(json.loads(dict(rows[0])["payload"]).get("detail") or "")
        except ValueError:
            return ""
    # событие вычищено по сроку, а запись в ленте осталась
    fed = await fetchall(
        db,
        "SELECT 1 FROM task_updates WHERE task_id=? AND instr(content, ?) > 0 LIMIT 1",
        (task_id, f"{MARK_PREFIX}{generation} "),
    )
    return "причина записана в ленте задачи" if fed else ""
