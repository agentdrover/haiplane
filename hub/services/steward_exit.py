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
    пара СОГЛАСИЛАСЬ (concur), а потом по задаче случился ЛЮБОЙ из трёх
    фактов — в любой момент после одобрения, без окна и без якоря доставки:
    человеческий ``changes_requested`` на этом или более позднем поколении,
    выход задачи из ``completed`` (переоткрытие), прод-дефект
    ``found_in='prod'`` с ``caused_by_task_id`` на неё. Нужно ноль;
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

ЗАПИСЬ В МОМЕНТ СОБЫТИЯ. Факт пишется в таблицу ``steward_false_approvals``
В ТОЙ ЖЕ ТРАНЗАКЦИИ, что само событие, если у задачи есть пара с concur:
триггеры БД на событие человеческого возврата, на выход статуса из
``completed`` и на prod-дефект с ``caused_by_task_id`` (триггер, а не вызов в
каждом из путей: путей выхода из completed и записи вердикта много, а триггер
ловит и те, о которых никто не вспомнил). События чистятся через 14 дней, а
переоткрытие вообще не оставляет события — поэтому читать историю нельзя, и
читается только таблица. Опрос по тику (``record_false_approvals``) —
страховка для возврата и дефекта, не основной путь.

ЛИПКОСТЬ. Запись остаётся, пока человек явно её не снимет. Снимается КОНКРЕТНЫЙ
случай (задача, источник, ref), а не задача: новый случай — новая запись.

Глобальность. ``effective_mode`` читает одну выборку на хаб, поэтому ошибка на
проекте A возвращает в тень и проект B — по построению, а не по фильтру.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import repository as repo
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
    """Одно ошибочное одобрение: задача, источник, случай и подробность.

    ``ref`` называет КОНКРЕТНЫЙ случай: id события возврата, id дефекта,
    монотонная метка выхода из ``completed``. Липкость и снятие работают по
    (задача, источник, случай), а не по задаче: снятый случай не возвращается,
    а новый случай той же задачи — новая запись.
    """

    task_id: int
    source: str
    generation: int
    detail: str
    ref: str = ""


@dataclass(frozen=True)
class _Pair:
    task_id: int
    generation: int
    approved_at: str
    #: id события «советник ответил» (events.id): монотонный порядок, общий с
    #: событиями возврата. None — события нет (вычищено через 14 дней или
    #: строки заведены мимо контракта): тогда порядок по секундам.
    approval_event_id: int | None = None


async def concur_pairs(db: aiosqlite.Connection) -> list[_Pair]:
    """Пары, которые СОГЛАСИЛИСЬ: approve судьи + concur; с временем одобрения."""
    rows = await fetchall(
        db,
        "SELECT j.task_id, j.generation, a.created_at AS approved_at "
        "FROM steward_judgements j "
        "JOIN steward_judgements a ON a.kind='advisor' AND a.verdict='concur' "
        "AND a.task_id=j.task_id AND a.generation=j.generation "
        "AND a.judged_id=j.id "
        "WHERE j.kind='verdict' AND j.verdict='approve' AND j.contour=? "
        "ORDER BY j.id",
        (CONTOUR_V2,),
    )
    pairs: list[_Pair] = []
    for r in rows:
        item = dict(r)
        task_id, generation = int(item["task_id"]), int(item["generation"])
        events = await fetchall(
            db,
            "SELECT MIN(id) AS id FROM events WHERE kind='steward_advisor_recorded' "
            "AND task_id=? AND json_extract(payload, '$.generation')=? "
            "AND json_extract(payload, '$.verdict')='concur'",
            (task_id, generation),
        )
        event_id = dict(events[0]).get("id") if events else None
        pairs.append(
            _Pair(
                task_id,
                generation,
                str(item["approved_at"] or ""),
                int(event_id) if event_id is not None else None,
            )
        )
    return pairs


async def _human_returns(
    db: aiosqlite.Connection, task_id: int
) -> list[tuple[int, int, str]]:
    """ВСЕ человеческие возвраты задачи: (id события, поколение, время).

    Каждый возврат — факт; последующий approved его не отменяет (отменяет
    только явное снятие человеком). Акторы автоматики исключены тем же
    списком, что у таблицы тени.
    """
    placeholders, actors = sql_in(NON_HUMAN_GATE_ACTORS)
    rows = await fetchall(
        db,
        "SELECT id, payload, created_at FROM events "  # nosec B608 - placeholders from module constants
        "WHERE kind='review_verdict_recorded' AND task_id=? "
        f"AND actor NOT IN ({placeholders}) ORDER BY id ASC",
        (task_id, *actors),
    )
    out: list[tuple[int, int, str]] = []
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload") or "{}")
        except ValueError:
            continue
        generation = int(payload.get("submission_generation") or 0)
        if generation and payload.get("verdict") == "changes_requested":
            out.append((int(item["id"]), generation, str(item.get("created_at") or "")))
    return out


async def _poll_pair(db: aiosqlite.Connection, pair: _Pair) -> list[FalseApprove]:
    """Страховка: возвраты и прод-дефекты, которых триггер мог не поймать.

    Без окна и без якоря доставки: любой факт ПОСЛЕ одобрения пары. Те же ref,
    что у триггеров, поэтому запись по триггеру и находка опроса — один случай.
    Переоткрытие опросом не находится (оно не оставляет следа) — только
    триггером выхода из completed.
    """
    found: list[FalseApprove] = []
    for event_id, gen, at in await _human_returns(db, pair.task_id):
        # Порядок «возврат после одобрения» — по id событий, а не по секундам:
        # возврат и concur в одну секунду различает только монотонный id.
        after = (
            event_id > pair.approval_event_id
            if pair.approval_event_id is not None
            else at >= pair.approved_at
        )
        if gen >= pair.generation and after:
            found.append(
                FalseApprove(
                    pair.task_id,
                    SOURCE_HUMAN_CHANGES,
                    pair.generation,
                    f"человек вернул поколение {gen} (пара одобрила поколение "
                    f"{pair.generation}), событие #{event_id}",
                    ref=str(event_id),
                )
            )
    defects = await fetchall(
        db,
        "SELECT id FROM tasks WHERE found_in='prod' AND caused_by_task_id=? "
        "AND COALESCE(detected_at, created_at) >= ? ORDER BY id",
        (pair.task_id, pair.approved_at),
    )
    for d in defects:
        defect_id = int(dict(d)["id"])
        found.append(
            FalseApprove(
                pair.task_id,
                SOURCE_PROD_DEFECT,
                pair.generation,
                f"прод-дефект found_in=prod, caused_by_task_id на неё: #{defect_id}",
                ref=str(defect_id),
            )
        )
    return found


async def _recorded_keys(db: aiosqlite.Connection) -> set[tuple[int, str, str]]:
    rows = await fetchall(
        db, "SELECT task_id, source, ref FROM steward_false_approvals"
    )
    return {
        (int(dict(r)["task_id"]), str(dict(r)["source"]), str(dict(r)["ref"]))
        for r in rows
    }


async def detect_false_approvals(db: aiosqlite.Connection) -> list[FalseApprove]:
    """Страховочный опрос: факты, найденные по данным и ещё не записанные.

    Только чтение. Случай, уже внесённый в таблицу (активный или снятый
    человеком), в находки не попадает.
    """
    known = await _recorded_keys(db)
    out: list[FalseApprove] = []
    for pair in await concur_pairs(db):
        for item in await _poll_pair(db, pair):
            if (item.task_id, item.source, item.ref) not in known:
                out.append(item)
    return out


async def sticky_false_approvals(db: aiosqlite.Connection) -> list[FalseApprove]:
    """Записанные и не снятые человеком — ОСНОВНОЙ источник отказа."""
    rows = await fetchall(
        db,
        "SELECT task_id, source, generation, detail, ref FROM steward_false_approvals "
        "WHERE cleared_at IS NULL ORDER BY id",
    )
    return [
        FalseApprove(
            int(dict(r)["task_id"]),
            str(dict(r)["source"]),
            int(dict(r)["generation"]),
            str(dict(r)["detail"]),
            str(dict(r)["ref"]),
        )
        for r in rows
    ]


async def current_false_approvals(db: aiosqlite.Connection) -> list[FalseApprove]:
    """Активные записи плюс то, что опрос нашёл и ещё не закрепил."""
    return [*await sticky_false_approvals(db), *await detect_false_approvals(db)]


async def record_false_approvals(db: aiosqlite.Connection) -> int:
    """Закрепить найденное опросом. Зовётся тиком поллера и при запросе act.

    Основной путь записи — триггеры в момент события; это страховка.
    ``INSERT OR IGNORE`` по (задача, источник, случай) повторов не плодит.
    """
    added = 0
    for item in await detect_false_approvals(db):
        cursor = await db.execute(
            "INSERT OR IGNORE INTO steward_false_approvals "
            "(task_id, source, generation, detail, ref) VALUES (?, ?, ?, ?, ?)",
            (item.task_id, item.source, item.generation, item.detail, item.ref),
        )
        added += cursor.rowcount or 0
    await db.commit()
    return added


async def clear_false_approval(
    db: aiosqlite.Connection, task_id: int, human: str, note: str = ""
) -> int:
    """Явное решение человека: снять АКТИВНЫЕ ошибочные одобрения задачи.

    Снимаются конкретные случаи, которые есть сейчас (найденные опросом, но не
    закреплённые, закрепляются и тут же снимаются). Нечего снимать — 0, и
    никакого запаса на будущее: новый случай той же задачи станет новой
    активной записью. Возвращает число снятых случаев.
    """
    await record_false_approvals(db)
    cursor = await db.execute(
        "UPDATE steward_false_approvals SET cleared_by=?, cleared_at=datetime('now'), "
        "clear_note=? WHERE task_id=? AND cleared_at IS NULL",
        (human, note, task_id),
    )
    await db.commit()
    return cursor.rowcount or 0


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


#: Окно счётчика фактических вердиктов стюарда. Лента событий чистится через
#: EVENTS_RETENTION_DAYS, и окно длиннее показывало бы то, чего уже нет, под
#: видом нуля: «0 за 90 дней» при данных за 14 — неправда в безопасную сторону.
STEWARD_VERDICT_WINDOW_DAYS = repo.EVENTS_RETENTION_DAYS


async def actual_steward_verdicts(
    db: aiosqlite.Connection, since_days: int = STEWARD_VERDICT_WINDOW_DAYS
) -> dict[str, int]:
    """Фактические вердикты стюарда по проектам: ``{slug: число}`` (#1602).

    Считается ОДНО событие: ``review_verdict_recorded`` с actor=steward — тот
    самый вердикт, который стюард записал на задачу (``steward_applied.py``).
    Событие ``steward_applied`` сюда не годится: оно пишется на любое
    неэскалированное суждение, тень и DoR тоже. Один вердикт на поколение
    сдачи: повтор той же пары (задача, поколение) считается один раз.
    Проект задачи — по цепочке до эпика, как у решателя.
    """
    rows = await fetchall(
        db,
        "SELECT task_id, payload FROM events "
        "WHERE kind='review_verdict_recorded' AND actor='steward' "
        "AND task_id IS NOT NULL AND created_at >= datetime('now', ?)",
        (f"-{int(since_days)} days",),
    )
    seen: set[tuple[int, Any]] = set()
    slugs: dict[int, str] = {}
    counts: dict[str, int] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        generation = (
            payload.get("submission_generation") if isinstance(payload, dict) else None
        )
        key = (int(row["task_id"]), generation)
        if key in seen:
            continue
        seen.add(key)
        task_id = int(row["task_id"])
        if task_id not in slugs:
            project = await repo.resolve_project_for_task(db, task_id)
            slugs[task_id] = project["slug"] if project is not None else "default"
        counts[slugs[task_id]] = counts.get(slugs[task_id], 0) + 1
    return counts


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
            {
                "task_id": f.task_id,
                "source": f.source,
                "ref": f.ref,
                "detail": f.detail,
            }
            for f in false_approves
        ],
    }
