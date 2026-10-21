"""Исход прогона k8 по классам и состояние «после неудачного прогона» (#1941).

Первая половина модуля - два чистых чтения, которые ничего не пишут:

* класс причины неудачи по коду ``reason_code`` из закрытого списка
  ``launch_feedback.REASON_CODES`` (таблица ниже — единственная в коде; такая же
  лежит в ``docs/agent-context/contracts.md``);
* проекция ``last_executor_order`` для карточки задачи, в том числе признак
  ``after_failed_run`` — «последний host-заказ закрыт неудачей, и после неё в
  задаче ничего не произошло».

Признак считается ОДНИМ оператором SELECT: заказ, задача, живые сессии и прогоны
читаются одним снимком, поэтому событие между чтениями не может дать смесь
«до» и «после». ``executor_launch._live_run`` здесь не зовётся — он пишет
(снимает брошенную бронь). Сбой чтения — не «нет» и не «да»: проекция несёт
``unavailable`` с причиной и не несёт ``after_failed_run`` вовсе.

Проекция безопасна: в ней нет кода заказа, его хеша и шифротекста. Запуск
исполнителя она не решает — инвариант «код слоту только по executor-launch
человека» не меняется.

Вторая половина (#1942) - рекомендация разбора в тени. После закрытия заказа
исходом rescued/failed хаб по классу причины и серии неудач пишет ОДНУ
рекомендацию на заказ (``executor_triage_recommendations``), событие
``executor_triage_recommended`` и запись ленты - в транзакции вызывающего, без
commit. Хаб при этом ничего не запускает, не выпускает код, не меняет статус и
не переводит задачу в needs_decision: решение остаётся за человеком. Свип
(``sweep``) добирает пропущенные рекомендации и проставляет ``followup`` -
что случилось в задаче после рекомендации; по нему считается ``agreement``.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import aiosqlite

from hub import repository as repo
from hub.db import fetchall, write_transaction

log = logging.getLogger(__name__)

CLASS_UNCLASSIFIED: Final[int] = 0
CLASS_NAMES: Final[dict[int, str]] = {
    CLASS_UNCLASSIFIED: "unclassified",
    1: "infra",
    2: "no_new_head",
    3: "critic_exhausted",
    4: "integrity",
}

#: Код причины -> класс. Нет в таблице (в том числе неизвестный сохранённый
#: код) = «не классифицировано»: critic_failed, agent_failed, other.
CODE_CLASS: Final[dict[str, int]] = {
    "critic_unavailable": 1,
    "critic_delivery_failed": 1,
    "channel_check_failed": 1,
    "setup_failed": 1,
    "redeem_failed": 1,
    "vm_failed": 1,
    "hub_unavailable": 1,
    "stale_token": 1,  # nosec B105 - код причины, не секрет
    "busy": 1,
    "over_budget": 1,
    "timeout": 1,
    "no_new_head": 2,
    "critic_cycles_exhausted": 3,
    "critic_time_exhausted": 3,
    "integrity_violation": 4,
}

FAILED_OUTCOMES: Final[tuple[str, ...]] = ("rescued", "failed")

#: Один оператор = один снимок. Последний host-заказ задачи (по id) вместе со
#: всем, что гасит признак: статус и архив задачи, держатель, поколение сдачи,
#: живая implementer-сессия, живой или забронированный прогон (в том числе
#: облачная бронь без агента; прогон, ждущий только цену, не считается).
_SNAPSHOT_SQL: Final[str] = """
SELECT o.id AS order_id, o.task_id, o.state, o.generation, o.report_outcome,
       o.report_reason_code, o.statement_generation AS order_statement_generation,
       t.statement_generation AS task_statement_generation,
       t.status, t.archived, t.claimed_by, t.claim_session_id,
       t.implementer_principal_id, t.submission_generation,
       EXISTS (SELECT 1 FROM chat_pair_sessions s
               WHERE s.kind = 'implementer' AND s.bound_task_id = t.id
                 AND s.revoked_at IS NULL
                 AND s.expires_at > datetime('now')) AS live_session,
       EXISTS (SELECT 1 FROM executor_runs r
               WHERE r.task_id = t.id AND r.outcome = 'running'
                 AND r.cost_wait_since IS NULL) AS live_run
FROM executor_orders o JOIN tasks t ON t.id = o.task_id
WHERE o.task_id = ?
ORDER BY o.id DESC LIMIT 1
"""


def class_of(reason_code: str) -> tuple[int, str]:
    """(номер класса, имя) по коду причины; неизвестный код — не классифицировано."""
    number = CODE_CLASS.get(reason_code or "", CLASS_UNCLASSIFIED)
    return number, CLASS_NAMES[number]


async def _read_snapshot(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    rows = await fetchall(db, _SNAPSHOT_SQL, (int(task_id),))
    return dict(rows[0]) if rows else None


def _quiet(snap: dict[str, Any]) -> bool:
    """В задаче после неудачного прогона ничего не произошло."""
    return (
        snap["status"] == "open"
        and not snap["archived"]
        and not snap["claimed_by"]
        and not snap["claim_session_id"]
        and snap["implementer_principal_id"] is None
        and not snap["live_session"]
        and not snap["live_run"]
        # Каноническая сдача сильнее report failed/rescued: поколение дошло до
        # заказного (заказ создаётся на поколение сдачи + 1).
        and int(snap["submission_generation"] or 0) < int(snap["generation"])
    )


def project(snap: dict[str, Any]) -> dict[str, Any]:
    """Проекция последнего host-заказа из снимка; чистая функция."""
    outcome = snap["report_outcome"] or ""
    failed = outcome in FAILED_OUTCOMES
    code = snap["report_reason_code"] or ""
    number, name = class_of(code) if failed else (None, None)
    return {
        "order_id": int(snap["order_id"]),
        "state": snap["state"],
        "outcome": outcome,
        "reason_code": code,
        "class": number,
        "class_name": name,
        "after_failed_run": bool(failed and snap["state"] == "closed" and _quiet(snap)),
    }


async def last_executor_order_view(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    """Проекция для карточки; ``None`` — у задачи нет host-заказов.

    Сбой чтения даёт ``{"unavailable": <причина>}`` без ``after_failed_run``.
    """
    try:
        snap = await _read_snapshot(db, task_id)
    except Exception as exc:  # noqa: BLE001 - сбой чтения называется, а не глотается
        log.warning("last_executor_order #%s: %s", task_id, exc)
        return {"unavailable": f"чтение заказов не удалось: {type(exc).__name__}"}
    return None if snap is None else project(snap)


# ---------------------------------------------------------------------------
# Рекомендация разбора (#1942)
# ---------------------------------------------------------------------------

EVENT_RECOMMENDED: Final[str] = "executor_triage_recommended"

ACTION_RETRY_AFTER_STATUS: Final[str] = "retry_after_status"
ACTION_REFINE_AND_RETRY: Final[str] = "refine_hints_and_retry"
ACTION_RETRY_ONCE: Final[str] = "retry_once"
ACTION_REWRITE_OR_SPLIT: Final[str] = "rewrite_or_split"
ACTION_TO_OWNER: Final[str] = "to_owner"
RETRY_ACTIONS: Final[tuple[str, ...]] = (
    ACTION_RETRY_AFTER_STATUS,
    ACTION_REFINE_AND_RETRY,
    ACTION_RETRY_ONCE,
)
ACTION_TEXT: Final[dict[str, str]] = {
    ACTION_RETRY_AFTER_STATUS: "повторить запуск после проверки k8-launch status",
    ACTION_REFINE_AND_RETRY: "уточнить technical_hints по находке P1 и повторить один раз",
    ACTION_RETRY_ONCE: "повторить запуск один раз",
    ACTION_REWRITE_OR_SPLIT: (
        "переписать AC/scope или разделить задачу; повторять только после нового DoR"
    ),
    ACTION_TO_OWNER: "решить владельцу",
}

#: Не больше двух рекомендаций «повтор» на задачу за скользящие 24 часа: третья
#: превращается в «к владельцу».
RETRY_CAP: Final[int] = 2
RETRY_WINDOW_SQL: Final[str] = "-24 hours"
#: Рекомендация без действия дольше этого срока закрывается как none_72h.
NO_DECISION_HOURS: Final[int] = 72

_SAVEPOINT: Final[str] = "executor_triage_rec"

_HISTORY_SQL: Final[str] = """
SELECT id, generation, statement_generation, report_outcome, report_reason_code
FROM executor_orders WHERE task_id = ? AND id <= ? ORDER BY id DESC
"""
_RETRIES_SQL: Final[str] = (
    "SELECT COUNT(*) FROM executor_triage_recommendations "
    "WHERE task_id = ? AND action IN (?, ?, ?) "
    "AND created_at > datetime('now', ?)"
)


def series_length(rows: list[dict[str, Any]]) -> int:
    """Сколько подряд заказов (от текущего назад) той же постановки, того же класса.

    ``rows[0]`` - текущий заказ, дальше - более ранние. Серию обрывает: другое или
    неизвестное поколение постановки, заказ без неудачи (в том числе сдача), другой
    класс и другое поколение СДАЧИ (между заказами задачу сдавали).
    """
    current = rows[0]
    klass = class_of(current["report_reason_code"])[0]
    length = 1
    newer = current
    for row in rows[1:]:
        if (
            row["statement_generation"] is None
            or row["statement_generation"] != current["statement_generation"]
            or row["generation"] != newer["generation"]
            or row["report_outcome"] not in FAILED_OUTCOMES
            or class_of(row["report_reason_code"])[0] != klass
        ):
            break
        length += 1
        newer = row
    return length


def decide(
    klass: int, *, generation_known: bool, run_length: int, recent_retries: int
) -> tuple[str, str]:
    """(код действия, причина) по правилу постановки; чистая функция."""
    if not generation_known:
        return ACTION_TO_OWNER, "поколение постановки заказа неизвестно"
    if klass in (CLASS_UNCLASSIFIED, 4):
        return ACTION_TO_OWNER, "причина не классифицирована или нарушена целостность"
    repeated = run_length >= 2
    if klass == 1:
        action, why = (
            (ACTION_TO_OWNER, "вторая подряд неудача инфраструктуры")
            if repeated
            else (ACTION_RETRY_AFTER_STATUS, "сбой окружения, не постановки")
        )
    elif klass == 2:
        action, why = (
            (ACTION_TO_OWNER, "новой головы нет и после уточнения")
            if repeated
            else (ACTION_REFINE_AND_RETRY, "прогон не дал новой головы ветки")
        )
    else:
        action, why = (
            (ACTION_REWRITE_OR_SPLIT, "критик исчерпан второй раз подряд")
            if repeated
            else (ACTION_RETRY_ONCE, "критик исчерпан впервые в серии")
        )
    if action in RETRY_ACTIONS and recent_retries >= RETRY_CAP:
        return ACTION_TO_OWNER, f"уже {recent_retries} рекомендации повтора за 24 часа"
    return action, why


async def _decision(db: aiosqlite.Connection, snap: dict[str, Any]) -> tuple[str, str]:
    """Действие и причина; сбой чтения истории даёт «к владельцу» с названной причиной."""
    klass = class_of(snap["report_reason_code"] or "")[0]
    known = snap["order_statement_generation"] is not None
    try:
        rows = [
            dict(r)
            for r in await fetchall(
                db, _HISTORY_SQL, (int(snap["task_id"]), int(snap["order_id"]))
            )
        ]
        run_length = series_length(rows) if rows else 1
        counted = await fetchall(
            db,
            _RETRIES_SQL,
            (int(snap["task_id"]), *RETRY_ACTIONS, RETRY_WINDOW_SQL),
        )
        retries = int(counted[0][0])
    except Exception as exc:  # noqa: BLE001 - нечитаемая история не равна пустой
        log.warning("triage history #%s: %s", snap["order_id"], exc)
        return ACTION_TO_OWNER, f"история прогонов не прочиталась: {type(exc).__name__}"
    return decide(
        klass, generation_known=known, run_length=run_length, recent_retries=retries
    )


def _entry_text(snap: dict[str, Any], action: str, why: str) -> str:
    number, name = class_of(snap["report_reason_code"] or "")
    return (
        f"Разбор прогона k8 (тень, #1942): заказ #{snap['order_id']}, класс {number} "
        f"({name}). Рекомендация: {ACTION_TEXT[action]} ({why}). "
        "Хаб ничего не запустил; решение за человеком."
    )


async def recommend_for_order(
    db: aiosqlite.Connection, order_id: int
) -> dict[str, Any] | None:
    """Записать рекомендацию по заказу; ``None`` - её нет или она уже есть.

    Без commit: транзакция - вызывающего. Читает свежую строку заказа и задачи,
    а не снимок вызывающего. Рекомендация нужна, только если заказ - последний у
    задачи и признак ``after_failed_run`` (#1941) на нём истинен; каноническая
    сдача и задача, уже взятая человеком, его гасят. Постановка, изменившаяся
    после выпуска заказа, тоже снимает рекомендацию: владелец уже решил.
    """
    ref = await fetchall(
        db,
        "SELECT task_id, closed_at FROM executor_orders WHERE id = ?",
        (int(order_id),),
    )
    if not ref:
        return None
    # Граница включения (#1942): заказ, закрытый до активации, - история, а не
    # свежая неудача; рекомендации на него нет, и свип её не создаёт.
    if str(ref[0]["closed_at"] or "") < await activated_at(db):
        return None
    snap = await _read_snapshot(db, int(ref[0]["task_id"]))
    if (
        snap is None
        or int(snap["order_id"]) != int(order_id)
        or not project(snap)["after_failed_run"]
    ):
        return None
    ordered = snap["order_statement_generation"]
    if ordered is not None and int(snap["task_statement_generation"] or 0) > ordered:
        return None
    action, why = await _decision(db, snap)
    number, name = class_of(snap["report_reason_code"] or "")
    cursor = await db.execute(
        "INSERT OR IGNORE INTO executor_triage_recommendations "
        "(order_id, task_id, triage_class, class_name, action, reason, child_floor) "
        "VALUES (?, ?, ?, ?, ?, ?, "
        "COALESCE((SELECT MAX(id) FROM tasks WHERE parent_id = ?), 0))",
        (
            int(order_id),
            int(snap["task_id"]),
            number,
            name,
            action,
            why,
            int(snap["task_id"]),
        ),
    )
    if not cursor.rowcount:
        return None  # вторая вставка: событие и запись ленты уже есть у первой
    task_id = int(snap["task_id"])
    project_row = await repo.resolve_project_for_task(db, task_id)
    await repo.insert_event(
        db,
        kind=EVENT_RECOMMENDED,
        task_id=task_id,
        project_id=int(project_row["id"]) if project_row else None,
        actor="hub",
        payload={"order_id": int(order_id), "class": number, "action": action},
    )
    await repo.add_task_update(
        db, task_id, "hub", "status", _entry_text(snap, action, why)
    )
    return {"order_id": int(order_id), "class": number, "action": action}


async def recommend_safely(
    db: aiosqlite.Connection, order_id: int
) -> dict[str, Any] | None:
    """``recommend_for_order`` под SAVEPOINT: сбой откатывает только свои записи.

    Report хоста важнее рекомендации: если разбор упал, итог прогона всё равно
    сохранён, а свип добирает рекомендацию позже.
    """
    await db.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        result = await recommend_for_order(db, order_id)
    except BaseException as exc:
        # Отмена тоже откатывает точку: иначе владелец транзакции, поймавший её и
        # закоммитивший, сохранил бы рекомендацию и событие без записи ленты.
        await db.execute(f"ROLLBACK TO {_SAVEPOINT}")
        await db.execute(f"RELEASE {_SAVEPOINT}")
        if not isinstance(exc, Exception):
            raise
        log.exception("executor triage #%s failed", order_id)
        return None
    await db.execute(f"RELEASE {_SAVEPOINT}")
    return result


# ---------------------------------------------------------------------------
# Разобранность и замер совпадения
# ---------------------------------------------------------------------------

FOLLOWUPS: Final[tuple[str, ...]] = (
    "new_order",
    "statement_changed",
    "split",
    "submitted",
    "none_72h",
    "unclear",
)
#: Вне знаменателя совпадения: «устарела» (сдача, архив) и «без решения».
STALE_FOLLOWUPS: Final[frozenset[str]] = frozenset({"submitted"})
NO_DECISION_FOLLOWUP: Final[str] = "none_72h"
#: action -> follow-up, который считается совпадением. owner_verdict на совпадение
#: не влияет: доля принятых считается отдельно.
MATCHING: Final[dict[str, frozenset[str]]] = {
    ACTION_RETRY_AFTER_STATUS: frozenset({"new_order"}),
    ACTION_RETRY_ONCE: frozenset({"new_order"}),
    ACTION_REFINE_AND_RETRY: frozenset({"new_order"}),
    ACTION_REWRITE_OR_SPLIT: frozenset({"statement_changed", "split"}),
    ACTION_TO_OWNER: frozenset({"statement_changed", "split"}),
}

_PENDING_SQL: Final[str] = """
SELECT r.id, r.order_id, r.task_id, r.action, r.child_floor,
       datetime(r.created_at, '+72 hours') AS deadline_at,
       o.generation AS order_generation,
       o.statement_generation AS order_sg,
       t.statement_generation AS task_sg,
       t.archived, t.submission_generation
FROM executor_triage_recommendations r
JOIN executor_orders o ON o.id = r.order_id
JOIN tasks t ON t.id = r.task_id
WHERE r.followup IS NULL
"""
_LATER_ORDER_SQL: Final[str] = (
    "SELECT created_at, statement_generation FROM executor_orders "
    "WHERE task_id = ? AND id > ? ORDER BY id LIMIT 1"
)
#: Разделение - ТОЛЬКО подзадача, созданная человеком: у неё есть долговечная метка
#: (``created_at``) и она сама есть решение. Агентский ребёнок в любом статусе (draft,
#: одобрен, отклонён) предложением остаётся, а когда его одобрили - в данных нет.
_HUMAN_CHILD_SQL: Final[str] = (
    "SELECT MIN(created_at) FROM tasks WHERE parent_id = ? AND id > ? "
    "AND source = 'human'"
)
_AGENT_CHILD_SQL: Final[str] = (
    "SELECT 1 FROM tasks WHERE parent_id = ? AND id > ? AND source <> 'human' LIMIT 1"
)
_SUBMISSION_SQL: Final[str] = (
    "SELECT MIN(submitted_at) FROM submissions WHERE task_id = ? AND generation >= ?"
)
UNCLEAR_FOLLOWUP: Final[str] = "unclear"

#: Ничья по секунде: логический порядок действий владельца. Заказ раньше
#: разделения, сдача - после; срок 72 часа - последним.
_RANK: Final[dict[str, int]] = {
    "statement_changed": 0,
    "new_order": 1,
    "split": 2,
    "submitted": 3,
    NO_DECISION_FOLLOWUP: 5,
    UNCLEAR_FOLLOWUP: 5,
}


def classify_followup(f: dict[str, Any]) -> str | None:
    """Первое по времени действие с ДОЛГОВЕЧНОЙ меткой; ``None`` - пока ничего.

    ``f["events"]`` - кандидаты ``(время, исход)`` только из таблиц, где время
    записано самим действием: заказ (``executor_orders.created_at``), сдача
    (``submissions.submitted_at``), подзадача человека (``tasks.created_at``) и
    срок 72 часа. Время свипа в хронологию не входит, поэтому исход не зависит от
    того, когда прошёл свип. При равенстве - ``_RANK``; исход текущей секунды
    откладывается до следующего прохода (событие той же секунды ещё может прийти).
    """
    events = [(when, _RANK[kind], kind) for when, kind in f["events"]]
    if not events:
        return None
    first = min(events)
    return None if first[0] >= f["now"] else first[2]


def _later_order_event(f: dict[str, Any], later: Any) -> list[tuple[str, str]]:
    """Заказ после рекомендации; поколение постановки записано в самом заказе."""
    if later is None:
        return []
    ordered, retry = f["order_sg"], f["action"] in RETRY_ACTIONS
    bumped = ordered is not None and later[1] is not None and later[1] > ordered
    return [(later[0], "statement_changed" if bumped and not retry else "new_order")]


async def _durable_events(
    db: aiosqlite.Connection, f: dict[str, Any]
) -> list[tuple[str, str]]:
    task_id = int(f["task_id"])
    later = await fetchall(db, _LATER_ORDER_SQL, (task_id, int(f["order_id"])))
    found = _later_order_event(f, later[0] if later else None)
    child = (await fetchall(db, _HUMAN_CHILD_SQL, (task_id, int(f["child_floor"]))))[0][
        0
    ]
    if child:
        found.append((child, "split"))
    sub = (await fetchall(db, _SUBMISSION_SQL, (task_id, int(f["order_generation"]))))[
        0
    ][0]
    if sub:
        found.append((sub, "submitted"))
    return found


async def _only_swept(db: aiosqlite.Connection, f: dict[str, Any]) -> bool:
    """Есть ли действие, видимое только свипу (без долговечной метки времени).

    Архив, правка постановки без заказа, агентский ребёнок и сдача без строки
    ``submissions``. Они не входят в замер: срок рекомендации кончается исходом
    ``unclear`` вместо ``none_72h``.
    """
    if f["archived"]:
        return True
    ordered = f["order_sg"]
    if ordered is not None and int(f["task_sg"] or 0) > ordered:
        return True
    args = (int(f["task_id"]), int(f["child_floor"]))
    if await fetchall(db, _AGENT_CHILD_SQL, args):
        return True
    return int(f["submission_generation"] or 0) >= int(f["order_generation"])


async def _followup_events(
    db: aiosqlite.Connection, raw: Any, now: str
) -> list[tuple[str, str]]:
    f = dict(raw)
    found = await _durable_events(db, f)
    if f["deadline_at"] <= now:
        kind = UNCLEAR_FOLLOWUP if await _only_swept(db, f) else NO_DECISION_FOLLOWUP
        found.append((f["deadline_at"], kind))
    return found


async def _resolve_pending(db: aiosqlite.Connection) -> int:
    now = (await fetchall(db, "SELECT datetime('now')"))[0][0]
    resolved = 0
    for raw in await fetchall(db, _PENDING_SQL):
        events = await _followup_events(db, raw, now)
        followup = classify_followup({"events": events, "now": now})
        if followup is None:
            continue
        cursor = await db.execute(
            "UPDATE executor_triage_recommendations SET followup = ?, "
            "followup_at = datetime('now'), resolved_by = 'hub' "
            "WHERE id = ? AND followup IS NULL",
            (followup, int(raw["id"])),
        )
        resolved += int(cursor.rowcount or 0)
    return resolved


async def activated_at(db: aiosqlite.Connection) -> str:
    """Момент включения разбора: одна строка состояния, пишется один раз.

    Первый вызов (свип или первая рекомендация) запоминает его; дальше он не
    меняется. Рекомендации получают только заказы, закрытые после него, так что
    выкат не публикует историю как свежие неудачи.
    """
    await db.execute(
        "INSERT OR IGNORE INTO executor_triage_state (id, activated_at) "
        "VALUES (1, datetime('now'))"
    )
    rows = await fetchall(db, "SELECT activated_at FROM executor_triage_state")
    return str(rows[0][0])


async def _recover_missing(db: aiosqlite.Connection) -> int:
    """Закрытые неудачей заказы без рекомендации (сбой при report, рестарт).

    Нижняя граница - момент активации, верхней нет: срок ожидания действия
    владельца (72 часа) и срок восстановления отсутствующей записи - разные
    вещи. Берётся последний заказ открытой задачи.
    """
    rows = await fetchall(
        db,
        "SELECT o.id FROM executor_orders o JOIN tasks t ON t.id = o.task_id "
        "WHERE o.state = 'closed' AND o.report_outcome IN ('rescued', 'failed') "
        "AND o.closed_at >= ? AND t.status = 'open' AND t.archived = 0 "
        "AND NOT EXISTS (SELECT 1 FROM executor_triage_recommendations r "
        "                WHERE r.order_id = o.id) "
        "AND o.id = (SELECT MAX(n.id) FROM executor_orders n "
        "            WHERE n.task_id = o.task_id)",
        (await activated_at(db),),
    )
    made = 0
    for row in rows:
        if await recommend_safely(db, int(row["id"])):
            made += 1
    return made


async def sweep(db: aiosqlite.Connection) -> dict[str, int]:
    """Проход поллера: добрать пропущенные рекомендации и разобрать готовые."""
    async with write_transaction(db):
        created = await _recover_missing(db)
        resolved = await _resolve_pending(db)
    return {"created": created, "resolved": resolved}


async def agreement(db: aiosqlite.Connection) -> dict[str, Any]:
    """Замер совпадения рекомендации с действием владельца (формула - в docs).

    Знаменатель - рекомендации с followup, кроме «устарела» (submitted, archived) и
    «без решения» (none_72h) и unclear. Совпадение - followup из ``MATCHING[action]``.
    ``accepted_share`` - доля принятых среди рекомендаций с owner_verdict и от
    совпадения не зависит.
    """
    rows = await fetchall(
        db,
        "SELECT triage_class, action, followup, owner_verdict "
        "FROM executor_triage_recommendations",
    )
    total = matched = pending = no_decision = stale = unclear = 0
    by_class: dict[int, list[int]] = {}
    accepted = rejected = 0
    for row in rows:
        verdict = row["owner_verdict"]
        accepted += verdict == "accepted"
        rejected += verdict == "rejected"
        followup = row["followup"]
        if followup is None:
            pending += 1
        elif followup == NO_DECISION_FOLLOWUP:
            no_decision += 1
        elif followup == UNCLEAR_FOLLOWUP:
            unclear += 1
        elif followup in STALE_FOLLOWUPS:
            stale += 1
        else:
            hit = followup in MATCHING.get(row["action"], frozenset())
            total += 1
            matched += hit
            counts = by_class.setdefault(int(row["triage_class"]), [0, 0])
            counts[0] += 1
            counts[1] += hit
    judged = accepted + rejected
    return {
        "total": total,
        "matched": matched,
        "rate": (matched / total) if total else None,
        "by_class": {
            k: {"total": v[0], "matched": v[1], "rate": v[1] / v[0]}
            for k, v in sorted(by_class.items())
        },
        "pending": pending,
        "no_decision": no_decision,
        "unclear": unclear,
        # none_72h и unclear - «исхода с долговечной меткой нет». Их разбивка
        # зависит от того, что свип видел на сроке, поэтому устойчивая цифра - сумма.
        "unmeasured": no_decision + unclear,
        "stale": stale,
        "accepted_share": (accepted / judged) if judged else None,
    }
