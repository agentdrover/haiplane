"""Критерий выхода стюарда из тени, версия 2 (#1601).

Прежний критерий требовал десять человеческих ``changes_requested``,
сопоставленных с неэскалированными суждениями. За 60 дней таких набралось три:
доработку ловит машинное ревью раньше человека. Критерий был недостижим по
устройству, а не по качеству судьи.

Теперь выход считается по ФАКТАМ нового контура «судья + советник-критик»:

``pairs``
    approve судьи, на который пришёл ответ советника (concur или object).
    Нужно не меньше :data:`ACT_MIN_PAIRS`;
``false_approve``
    пара СОГЛАСИЛАСЬ (concur), а потом по задаче случилось одно из трёх:
    человеческий возврат на этом или более позднем поколении, переоткрытие
    или прод-дефект. Нужно ноль, и найденное не пропадает — см. ниже;
доля ``substantive``
    эскалации судьи по существу плюс возражения советника, делённые на все
    суждения нового контура без процедурных. Нужно от 5 до 50%.

Процедурные эскалации — только :data:`PROCEDURAL_ESCALATE_REASONS`: нет
отчёта и неполный отчёт. Это «судить не из чего», а не мнение о коде.
``precondition_failed`` процедурным НЕ считается: под ним красный CI, дрейф
ветки и выход за области — то, что судья обязан видеть и называть.

Выборка новая: считаются только суждения с ``contour=2``, то есть записанные
после выката. Старые одиночные суждения (``contour=1``) не зачитываются, и
этим же отсечены суждения, у которых советника быть не могло.

ЛИПКОСТЬ. Ошибочное одобрение пишется в таблицу ``steward_false_approvals`` и
остаётся там, пока человек явно его не снимет. Таблица, а не событие: события
чистятся через 14 дней, а окно проверки — 30 дней после доставки. Снятие
исключает задачу из проверки целиком: человек рассудил её, и повторное
обнаружение того же факта не должно отменять его решение.

Глобальность. ``effective_mode`` читает одну выборку на хаб, поэтому ошибка на
проекте A возвращает в тень и проект B — по построению, а не по фильтру.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub.db import fetchall
from hub.services.gate_events import NON_HUMAN_GATE_ACTORS, sql_in

#: Минимум пар (approve судьи с полученным ответом советника).
ACT_MIN_PAIRS = 20
#: Коридор доли «по существу» — прежний (#1107): ниже пола судья штампует,
#: выше потолка возвращает человеку почти всё.
ACT_ESCALATION_FLOOR = 0.05
ACT_ESCALATION_CEILING = 0.50

#: Процедурные эскалации судьи: судить было не из чего. Ровно два кода.
PROCEDURAL_ESCALATE_REASONS: frozenset[str] = frozenset(
    {"no_current_report", "report_incomplete"}
)

#: Окно проверки пары после доставки, дней.
FALSE_APPROVE_WINDOW_DAYS = 30

SOURCE_HUMAN_CHANGES = "human_changes_requested"
SOURCE_REOPENED = "reopened"
SOURCE_PROD_DEFECT = "prod_defect"
FALSE_APPROVE_SOURCES: tuple[str, ...] = (
    SOURCE_HUMAN_CHANGES,
    SOURCE_REOPENED,
    SOURCE_PROD_DEFECT,
)

#: Контур выборки: суждения, записанные после выката #1601.
CONTOUR_V2 = 2

REASON_SAMPLE_TOO_SMALL = "sample_too_small"
REASON_FALSE_APPROVE = "false_approve"
REASON_STAMPING = "escalations_below_floor"
REASON_OVER_ESCALATING = "escalations_above_ceiling"
REASON_NO_SAMPLE = "no_sample"


@dataclass(frozen=True)
class ContourCounts:
    """Счётчики нового контура за окно (или за всё время, если окна нет)."""

    judged: int = 0
    procedural: int = 0
    judge_approve: int = 0
    judge_changes: int = 0
    judge_escalate_substantive: int = 0
    concur: int = 0
    object: int = 0
    advisor_timeout: int = 0
    advisor_refused: int = 0
    advisor_pending: int = 0
    #: номера задач по процедурным эскалациям — отдельной строкой в сводке
    procedural_by_reason: dict[str, int] = field(default_factory=dict)

    @property
    def pairs(self) -> int:
        return self.concur + self.object

    @property
    def denominator(self) -> int:
        """Все суждения судьи нового контура минус процедурные."""
        return self.judged - self.procedural

    @property
    def substantive(self) -> int:
        """Эскалации судьи по существу плюс возражения советника."""
        return self.judge_escalate_substantive + self.object

    @property
    def share(self) -> float | None:
        """Доля по существу, или None, если делить не на что (не 0.0, #762)."""
        return (self.substantive / self.denominator) if self.denominator else None


async def contour_counts(
    db: aiosqlite.Connection, since_days: int | None = None
) -> ContourCounts:
    """Посчитать выборку нового контура одним запросом по суждениям судьи.

    Советник присоединяется ПО ПРИВЯЗКЕ ``judged_id``, а не по номеру задачи и
    поколения: ответ, не привязанный к этому суждению, парой не считается.
    """
    where = "j.kind='verdict' AND j.contour=?"
    params: list[Any] = [CONTOUR_V2]
    if since_days is not None:
        where += " AND j.created_at >= datetime('now', ?)"
        params.append(f"-{int(since_days)} days")
    rows = await fetchall(
        db,
        "SELECT j.verdict, j.escalate_reason, a.verdict AS adv_verdict, "  # nosec B608 - constant fragments, values are params
        "r.status AS run_status "
        "FROM steward_judgements j "
        "LEFT JOIN steward_judgements a ON a.kind='advisor' "
        "AND a.task_id=j.task_id AND a.generation=j.generation "
        "AND a.judged_id=j.id "
        "LEFT JOIN steward_runs r ON r.kind='advisor' "
        "AND r.task_id=j.task_id AND r.generation=j.generation "
        f"WHERE {where}",
        tuple(params),
    )
    c: dict[str, int] = {
        "judged": 0,
        "procedural": 0,
        "judge_approve": 0,
        "judge_changes": 0,
        "judge_escalate_substantive": 0,
        "concur": 0,
        "object": 0,
        "advisor_timeout": 0,
        "advisor_refused": 0,
        "advisor_pending": 0,
    }
    by_reason: dict[str, int] = {}
    for row in rows:
        item = dict(row)
        c["judged"] += 1
        verdict = str(item.get("verdict") or "")
        if verdict == "escalate":
            reason = str(item.get("escalate_reason") or "")
            if reason in PROCEDURAL_ESCALATE_REASONS:
                c["procedural"] += 1
                by_reason[reason] = by_reason.get(reason, 0) + 1
            else:
                c["judge_escalate_substantive"] += 1
        elif verdict == "changes_requested":
            c["judge_changes"] += 1
        elif verdict == "approve":
            c["judge_approve"] += 1
            adv = str(item.get("adv_verdict") or "")
            status = str(item.get("run_status") or "")
            if adv == "concur":
                c["concur"] += 1
            elif adv == "object":
                c["object"] += 1
            elif status in ("timeout", "never_started"):
                c["advisor_timeout"] += 1
            elif status in ("refused", "superseded"):
                c["advisor_refused"] += 1
            else:
                c["advisor_pending"] += 1
    return ContourCounts(**c, procedural_by_reason=by_reason)


# ---------------------------------------------------------------------------
# Ошибочное одобрение пары
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FalseApprove:
    """Одно найденное ошибочное одобрение: задача, источник и подробность."""

    task_id: int
    source: str
    generation: int
    detail: str


async def concur_pairs(db: aiosqlite.Connection) -> list[tuple[int, int]]:
    """(задача, поколение) пар, которые СОГЛАСИЛИСЬ: approve судьи + concur."""
    rows = await fetchall(
        db,
        "SELECT j.task_id, j.generation FROM steward_judgements j "
        "JOIN steward_judgements a ON a.kind='advisor' AND a.verdict='concur' "
        "AND a.task_id=j.task_id AND a.generation=j.generation "
        "AND a.judged_id=j.id "
        "WHERE j.kind='verdict' AND j.verdict='approve' AND j.contour=? "
        "ORDER BY j.id",
        (CONTOUR_V2,),
    )
    return [(int(dict(r)["task_id"]), int(dict(r)["generation"])) for r in rows]


async def _human_returns(db: aiosqlite.Connection, task_id: int) -> dict[int, str]:
    """Последний человеческий вердикт по каждому поколению → время события.

    Только ``changes_requested`` в итоге: человек, вернувший и затем
    одобривший то же поколение, ошибки пары не доказал. Акторы автоматики
    (policy, steward, hub) исключены тем же списком, что у таблицы тени.
    """
    placeholders, actors = sql_in(NON_HUMAN_GATE_ACTORS)
    rows = await fetchall(
        db,
        "SELECT payload, created_at FROM events "  # nosec B608 - placeholders from module constants
        "WHERE kind='review_verdict_recorded' AND task_id=? "
        f"AND actor NOT IN ({placeholders}) ORDER BY id ASC",
        (task_id, *actors),
    )
    last: dict[int, tuple[str, str]] = {}
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload") or "{}")
        except ValueError:
            continue
        generation = int(payload.get("submission_generation") or 0)
        verdict = str(payload.get("verdict") or "").strip()
        if generation and verdict in ("approved", "changes_requested"):
            last[generation] = (verdict, str(item.get("created_at") or ""))
    return {g: at for g, (v, at) in last.items() if v == "changes_requested"}


async def _within(db: aiosqlite.Connection, stamp: str, horizon: str | None) -> bool:
    """Событие не позже горизонта (доставка + окно); без горизонта — всегда."""
    if horizon is None or not stamp:
        return True
    rows = await fetchall(db, "SELECT ? <= ? AS ok", (stamp, horizon))
    return bool(dict(rows[0]).get("ok")) if rows else True


async def _detect_for_pair(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> list[FalseApprove]:
    row = await fetchall(
        db,
        "SELECT status, completed_at, status_entered_at, "
        "datetime(completed_at, ?) AS horizon FROM tasks WHERE id=?",
        (f"+{FALSE_APPROVE_WINDOW_DAYS} days", task_id),
    )
    if not row:
        return []
    task = dict(row[0])
    horizon = task.get("horizon") or None
    found: list[FalseApprove] = []

    for gen, at in sorted((await _human_returns(db, task_id)).items()):
        if gen >= generation and await _within(db, at, horizon):
            found.append(
                FalseApprove(
                    task_id,
                    SOURCE_HUMAN_CHANGES,
                    generation,
                    f"человек вернул поколение {gen} (пара одобрила "
                    f"поколение {generation})",
                )
            )
            break

    if (
        task.get("completed_at")
        and task.get("status") != "completed"
        and await _within(db, str(task.get("status_entered_at") or ""), horizon)
    ):
        found.append(
            FalseApprove(
                task_id,
                SOURCE_REOPENED,
                generation,
                f"задача доставлена ({task['completed_at']}) и снова в статусе "
                f"{task.get('status')}",
            )
        )

    defects = await fetchall(
        db,
        "SELECT id, COALESCE(detected_at, created_at) AS at FROM tasks "
        "WHERE found_in='prod' AND caused_by_task_id=? ORDER BY id",
        (task_id,),
    )
    ids = [
        int(dict(d)["id"])
        for d in defects
        if await _within(db, str(dict(d).get("at") or ""), horizon)
    ]
    if ids:
        found.append(
            FalseApprove(
                task_id,
                SOURCE_PROD_DEFECT,
                generation,
                "прод-дефект found_in=prod, caused_by_task_id на неё: "
                + ", ".join(f"#{i}" for i in ids),
            )
        )
    return found


async def _cleared_tasks(db: aiosqlite.Connection) -> set[int]:
    rows = await fetchall(
        db,
        "SELECT DISTINCT task_id FROM steward_false_approvals "
        "WHERE cleared_at IS NOT NULL",
    )
    return {int(dict(r)["task_id"]) for r in rows}


async def detect_false_approvals(db: aiosqlite.Connection) -> list[FalseApprove]:
    """Что находится по данным СЕЙЧАС. Только чтение; снятые человеком — вне."""
    cleared = await _cleared_tasks(db)
    out: list[FalseApprove] = []
    for task_id, generation in await concur_pairs(db):
        if task_id in cleared:
            continue
        out.extend(await _detect_for_pair(db, task_id, generation))
    return out


async def sticky_false_approvals(db: aiosqlite.Connection) -> list[FalseApprove]:
    """Записанные и не снятые человеком."""
    rows = await fetchall(
        db,
        "SELECT task_id, source, generation, detail FROM steward_false_approvals "
        "WHERE cleared_at IS NULL ORDER BY id",
    )
    return [
        FalseApprove(
            int(dict(r)["task_id"]),
            str(dict(r)["source"]),
            int(dict(r)["generation"]),
            str(dict(r)["detail"]),
        )
        for r in rows
    ]


async def current_false_approvals(db: aiosqlite.Connection) -> list[FalseApprove]:
    """Липкие плюс найденные сейчас, без повторов по (задача, источник)."""
    seen: set[tuple[int, str]] = set()
    out: list[FalseApprove] = []
    for item in [*await sticky_false_approvals(db), *await detect_false_approvals(db)]:
        key = (item.task_id, item.source)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


async def record_false_approvals(db: aiosqlite.Connection) -> int:
    """Закрепить найденное: с этого момента оно не зависит от данных.

    Возвращает число новых записей. ``INSERT OR IGNORE`` по (задача, источник):
    повторное обнаружение не плодит строк и не сбрасывает дату первого.
    """
    added = 0
    for item in await detect_false_approvals(db):
        cursor = await db.execute(
            "INSERT OR IGNORE INTO steward_false_approvals "
            "(task_id, source, generation, detail) VALUES (?, ?, ?, ?)",
            (item.task_id, item.source, item.generation, item.detail),
        )
        added += cursor.rowcount or 0
    await db.commit()
    return added


async def clear_false_approval(
    db: aiosqlite.Connection, task_id: int, human: str, note: str = ""
) -> int:
    """Явное событие человека: снять ошибочное одобрение с задачи.

    Если строк по задаче ещё нет (найдено «сейчас», но не закреплено), они
    закрепляются и тут же снимаются: иначе следующее обнаружение вернуло бы
    отказ, который человек уже рассудил. Возвращает число снятых строк.
    """
    await record_false_approvals(db)
    cursor = await db.execute(
        "UPDATE steward_false_approvals SET cleared_by=?, cleared_at=datetime('now'), "
        "clear_note=? WHERE task_id=? AND cleared_at IS NULL",
        (human, note, task_id),
    )
    cleared = cursor.rowcount or 0
    if not cleared:
        # Нечего снимать — но решение человека всё равно фиксируется, чтобы
        # будущее обнаружение по этой задаче не вернуло отказ.
        await db.execute(
            "INSERT OR IGNORE INTO steward_false_approvals "
            "(task_id, source, generation, detail, cleared_by, cleared_at, "
            "clear_note) VALUES (?, 'human_cleared', 0, ?, ?, datetime('now'), ?)",
            (task_id, "снято человеком до обнаружения", human, note),
        )
    await db.commit()
    return cleared


# ---------------------------------------------------------------------------
# Критерий выхода
# ---------------------------------------------------------------------------


async def act_refusals_v2(db: aiosqlite.Connection) -> list[tuple[str, str]]:
    """Какие критерии выхода не выполнены, по именам. Пусто — выполнены все."""
    counts = await contour_counts(db)
    out: list[tuple[str, str]] = []
    if counts.pairs < ACT_MIN_PAIRS:
        out.append(
            (
                REASON_SAMPLE_TOO_SMALL,
                f"пар судья+советник в выборке {counts.pairs}, нужно "
                f"{ACT_MIN_PAIRS}: считаются approve судьи нового контура с "
                "полученным ответом советника; прежние одиночные суждения не "
                "зачитываются",
            )
        )
    false_approves = await current_false_approvals(db)
    if false_approves:
        listing = "; ".join(
            f"#{f.task_id} [{f.source}] {f.detail}" for f in false_approves
        )
        out.append(
            (
                REASON_FALSE_APPROVE,
                f"ошибочных одобрений пары: {len({f.task_id for f in false_approves})}"
                f" — {listing}. Снимается только явным решением человека",
            )
        )
    share = counts.share
    if share is None:
        out.append(
            (
                REASON_NO_SAMPLE,
                "суждений судьи нового контура (без процедурных) нет вовсе: "
                "доля по существу не измерена, а не равна нулю",
            )
        )
        return out
    seen = (
        f"{counts.substantive} из {counts.denominator} ({share:.0%}): эскалации "
        f"судьи по существу {counts.judge_escalate_substantive} плюс возражения "
        f"советника {counts.object}"
    )
    if share < ACT_ESCALATION_FLOOR:
        out.append(
            (
                REASON_STAMPING,
                f"доля по существу {seen} ниже {ACT_ESCALATION_FLOOR:.0%}: "
                "судья с советником соглашаются со всем подряд, то есть штампуют",
            )
        )
    if share > ACT_ESCALATION_CEILING:
        out.append(
            (
                REASON_OVER_ESCALATING,
                f"доля по существу {seen} выше {ACT_ESCALATION_CEILING:.0%}: "
                "человеку возвращается почти всё, и смысла в паре нет",
            )
        )
    return out


async def contour_report(db: aiosqlite.Connection) -> dict[str, Any]:
    """Сводка нового контура для practice_metrics и сводки политики. Чтение."""
    counts = await contour_counts(db)
    false_approves = await current_false_approvals(db)
    return {
        "pairs": counts.pairs,
        "concur": counts.concur,
        "object": counts.object,
        "timeout": counts.advisor_timeout,
        "advisor_refused": counts.advisor_refused,
        "advisor_pending": counts.advisor_pending,
        "judged": counts.judged,
        "judge_approve": counts.judge_approve,
        "judge_changes_requested": counts.judge_changes,
        "judge_escalate_substantive": counts.judge_escalate_substantive,
        "procedural_escalations": counts.procedural,
        "procedural_by_reason": dict(counts.procedural_by_reason),
        "denominator": counts.denominator,
        "substantive": counts.substantive,
        # None — «не измерено», а не 0.0 (#762).
        "substantive_share": counts.share,
        "min_pairs": ACT_MIN_PAIRS,
        "false_approve": len({f.task_id for f in false_approves}),
        "false_approve_tasks": [
            {"task_id": f.task_id, "source": f.source, "detail": f.detail}
            for f in false_approves
        ],
    }
