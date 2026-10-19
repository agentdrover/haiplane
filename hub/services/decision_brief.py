"""Справка для решения без LLM: что произошло, что нужно, риски, рекомендация, проверки (#1810).

Данные для решения лежат в разных читателях: итог машинного ревью, маршрут
вердикта, доверие к CI, целостность CI, доставка, суждение стюарда. Эта сборка
ничего не вычисляет заново: каждый вопрос она задаёт тому же читателю, что
решатель, и только называет ответ простыми словами. Новых таблиц нет, LLM нет:
справка собирается на запрос, поэтому у карточки и у входящих один текст.

Правило безопасности одно: «Принять» советуется ТОЛЬКО когда доказательства
чисты целиком. Любой признак сомнения — «Проверить» или «На доработку» с
названной причиной: ложное «Принять» хуже ложного «Проверить». Отсутствие
данных — не данные: непрочитанное называется «неизвестно», а не «чисто».
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.models import FindingOutcome, MachineReviewView
from hub.services import (
    ci_contract,
    ci_integrity,
    ci_trust,
    inbox_decisions,
    state_review,
    report_fit,
    review_evidence,
    verdict_evidence,
)
from hub.services.finding_identity import unresolved_uids
from hub.services.finding_outcome import KIND_UNRESOLVED
from hub.services.readiness import parse_risks_from_row
from hub.services.result_kind import automation_not_applicable
from hub.services.review_availability import generation_review
from hub.services.verdict_route import DECIDER_HUMAN, DECIDER_STEWARD, verdict_route

ACCEPT = "Принять"
CHECK = "Проверить"
REWORK = "На доработку"
HUMAN_CALL = "Решить вам"
APPROVE_DRAFT = "Одобрить"
FIX_STATEMENT = "Доработать постановку"
RERUN = "Повторный прогон"
WORKING = "Идёт работа"
DELIVERED = "Доставлено"
MERGED_NOT_SHIPPED = "Влито, выкат не подтверждён"
NO_ACTION = "Действий нет"

#: Словарь формы вердикта (то, что принимает карточка).
VERDICT_APPROVED = "approved"
VERDICT_CHANGES = "changes_requested"

MAX_RISKS = 3
_WEIGHT = {"high": 3, "medium": 2, "low": 1}
#: Класс риска, с которого справка называет его риском (R3 и выше).
_HIGH_CLASSES = frozenset({"R3", "R4", "R5"})
_STEWARD_ACTIONS = {
    "approve": (ACCEPT, VERDICT_APPROVED),
    "changes_requested": (REWORK, VERDICT_CHANGES),
    "escalate": (HUMAN_CALL, ""),
}


CODE_CI = "ci"
CODE_INTEGRITY = "integrity"
CODE_REPORT = "report"


class UnreadableReport(Exception):
    """Строка отчёта не разобралась в модель чтения: «неизвестно», а не «чисто»."""

    def __init__(self, report_id: int) -> None:
        super().__init__(report_id)
        self.report_id = report_id


@dataclass(frozen=True)
class Doubt:
    """Признак сомнения: из-за него «Принять» невозможно."""

    level: str  # REWORK или CHECK
    severity: str
    risk: str
    why: str
    #: Группа проверки (CODE_*): по ней, а не по тексту, строятся «проверки».
    code: str = "report"


def _recommend(
    action: str, verdict: str, why: list[str], alt: str = ""
) -> dict[str, Any]:
    out: dict[str, Any] = {"action": action, "verdict": verdict, "why": why}
    if alt:
        out["alt"] = alt
    return out


def _brief(
    happened: str,
    need: str,
    recommend: dict[str, Any],
    *,
    risks: list[dict[str, str]] | None = None,
    checks: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "happened": happened,
        "need": need,
        "risks": _top_risks(risks or []),
        "recommend": recommend,
        "checks": checks or [],
    }


def _top_risks(risks: list[dict[str, str]]) -> list[dict[str, str]]:
    unique = list({r["text"]: r for r in reversed(risks)}.values())[::-1]
    # sorted устойчив: при равной тяжести порядок появления сохраняется.
    ordered = sorted(unique, key=lambda r: -_WEIGHT.get(r["severity"], 0))
    return ordered[:MAX_RISKS]


def _risk(severity: str, text: str, detail: str = "") -> dict[str, str]:
    return {"severity": severity, "text": text, "detail": detail}


def _check(name: str, state: str, detail: str) -> dict[str, str]:
    return {"name": name, "state": state, "detail": detail}


# ---- проверки CI и целостности ----------------------------------------------


async def _ci_doubt(db: aiosqlite.Connection, task: dict[str, Any]) -> Doubt | None:
    sha = str(task.get("submission_sha") or "").strip()
    if not sha:
        return Doubt(
            CHECK, "high", "CI не доказан: коммит сдачи не закреплён", "", CODE_CI
        )
    gap = await ci_trust.pass_gap(db, int(task["id"]), sha)
    if gap is None:
        return None
    row = await repo.get_ci_run_report(db, int(task["id"]), sha)
    fingerprint = await ci_contract.fingerprint_for_task(db, int(task["id"]))
    if row is not None and ci_trust.is_verified(dict(row), fingerprint):
        status = str(dict(row).get("validation_status") or "").strip()
        if status == "fail":
            return Doubt(REWORK, "high", "CI красный на коммите сдачи", gap, CODE_CI)
    return Doubt(
        CHECK,
        "high",
        "CI не доказан: нет подтверждённого зелёного отчёта",
        gap,
        CODE_CI,
    )


async def _integrity_doubt(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> Doubt | None:
    gap = (await ci_integrity.standing_of(db, task)).gap
    if gap is None:
        return None
    return Doubt(
        CHECK,
        "high",
        "Изменены входы CI или целостность не проверена",
        gap,
        CODE_INTEGRITY,
    )


# ---- отчёт машинного ревью ---------------------------------------------------


async def _unanswered_unresolved(
    db: aiosqlite.Connection, task_id: int, view: MachineReviewView
) -> int:
    """Неотвеченные unresolved ОДНОГО отчёта: исход ищется по тому uid, под которым он пишется."""
    left = 0
    for uid in unresolved_uids(view.unresolved):
        outcome = await repo.finding_outcome_for_uid(
            db, task_id, uid, finding_kind=KIND_UNRESOLVED
        )
        # «Не рассудил» — не закрытие: это отсутствие судьбы, а не она.
        left += outcome is None or (
            str(dict(outcome).get("outcome") or "") == FindingOutcome.not_judged.value
        )
    return left


async def _generation_views(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> list[MachineReviewView]:
    """ВСЕ отчёты текущего поколения как модели чтения: «Принять» — про каждый."""
    rows = await repo.machine_reviews_of_generation(
        db, int(task["id"]), int(task.get("submission_generation") or 0)
    )
    views: list[MachineReviewView] = []
    for row in rows:
        try:
            views.append(MachineReviewView(**dict(row)))
        except (TypeError, ValueError) as exc:  # нечитаемый отчёт — не чистый
            raise UnreadableReport(int(row["id"])) from exc
    return views


async def _report_doubts(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> tuple[list[Doubt], list[MachineReviewView]]:
    """Сомнения по отчётам поколения: белый список ``report_fit`` на КАЖДЫЙ отчёт."""
    try:
        views = await _generation_views(db, task)
    except UnreadableReport as bad:
        text = f"Отчёт ревью #{bad.report_id} не читается: проверка не доказана"
        return [Doubt(CHECK, "high", text, "")], []
    if not views:
        generation = await generation_review(db, task)
        headline = str(generation.headline)
        return [
            Doubt(CHECK, "high", "Отчёта машинного ревью по этой сдаче нет", headline)
        ], views
    found: dict[str, tuple[str, list[int]]] = {}
    for view in views:
        left = await _unanswered_unresolved(db, int(task["id"]), view)
        for code, text in report_fit.report_gaps(view, left):
            found.setdefault(code, (text, []))[1].append(view.id)
    doubts = []
    for code, (text, ids) in found.items():
        level = REWORK if code == report_fit.GAP_CONFIRMED else CHECK
        which = ", ".join(f"#{i}" for i in ids)
        doubts.append(Doubt(level, "high", text, f"отчёты: {which}", CODE_REPORT))
    return doubts, views


async def _review_doubts(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> tuple[list[Doubt], list[MachineReviewView]]:
    """Все признаки сомнения по задаче в review и отчёты, на которых они стоят."""
    doubts, views = await _report_doubts(db, task)
    # Платный прогон ещё идёт (или должен открыться вторая дверь): отчёт
    # поколения может измениться, а совет «Принять» опирается на него.
    flight = await review_evidence.inflight_view(db, task, include_owed=True)
    if flight is not None:
        doubts.append(
            Doubt(CHECK, "high", "Ревью этой сдачи ещё идёт", str(flight.headline))
        )
    for found in (await _ci_doubt(db, task), await _integrity_doubt(db, task)):
        if found is not None:
            doubts.append(found)
    return doubts, views


def _context_risks(task: dict[str, Any]) -> list[dict[str, str]]:
    risks = [
        _risk(r.severity.value, r.description, r.mitigation)
        for r in parse_risks_from_row(task.get("risks"))
    ]
    if int(task.get("review_cycle") or 0) >= 2:
        risks.append(_risk("medium", f"Уже {task['review_cycle']}-й круг ревью"))
    if str(task.get("risk_class") or "") in _HIGH_CLASSES:
        risks.append(_risk("medium", f"Высокий класс риска {task['risk_class']}"))
    return risks


# ---- рекомендация в review ---------------------------------------------------


async def _steward_row(db: aiosqlite.Connection, task: dict[str, Any]) -> Any:
    generation = int(task.get("submission_generation") or 0)
    if not generation:
        return None
    return await repo.get_steward_judgement(db, int(task["id"]), generation, "verdict")


def _route_why(route: Any) -> str:
    if route.final == DECIDER_STEWARD:
        return f"Вердикт применит стюард, {route.condition or 'без условий'}"
    if route.final == DECIDER_HUMAN:
        return f"Вердикт за вами: {route.reason}"
    return ""


def _ground_sources(raw: Any) -> list[str]:
    """Только имена источников оснований. Текст ``detail`` — из логов и диффов,
    а справка уходит во входящие дословно: код или секрет оттуда не выводим."""
    names: list[str] = []
    for ground in verdict_evidence.parse_grounds(raw):
        source = ground.get("source") if isinstance(ground, dict) else None
        if isinstance(source, str) and source.strip():
            names.append(f"основание стюарда: {source.strip()[:80]}")
    return names


def _from_steward(
    judged: Any, doubts: list[Doubt], route: Any
) -> dict[str, Any] | None:
    mapped = _STEWARD_ACTIONS.get(str(judged["verdict"]))
    if mapped is None:
        return None
    action, verdict = mapped
    why = _ground_sources(judged["grounds"])
    if action == HUMAN_CALL:
        why.insert(0, "стюард передал решение вам: причина в суждении на карточке")
    if action == ACCEPT and doubts:
        # Стюард сказал approve, но есть сомнение: правила сильнее суждения.
        return None
    if tail := _route_why(route):
        why.append(tail)
    return _recommend(action, verdict, why or ["стюард без пояснений"])


def _from_rules(doubts: list[Doubt], route: Any) -> dict[str, Any]:
    if not doubts:
        why = [
            "CI доверенно зелёный на коммите сдачи",
            "отчёт ревью полный, независимый, находок нет",
        ]
        if tail := _route_why(route):
            why.append(tail)
        return _recommend(ACCEPT, VERDICT_APPROVED, why)
    rework = any(d.level == REWORK for d in doubts)
    action, verdict = (REWORK, VERDICT_CHANGES) if rework else (CHECK, "")
    alt = "" if rework else "На доработку, если проверка подтвердит сомнение"
    return _recommend(action, verdict, [d.risk for d in doubts], alt)


async def _review_brief(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    doubts, views = await _review_doubts(db, task)
    route = ctx.get("route") or await verdict_route(db, int(task["id"]))
    judged = await _steward_row(db, task)
    recommend = _from_steward(judged, doubts, route) if judged is not None else None
    if recommend is None:
        recommend = _from_rules(doubts, route)
        if judged is not None and doubts:
            recommend["why"].append("суждение стюарда не принято: есть сомнения")
    risks = [_risk(d.severity, d.risk, d.why) for d in doubts] + _context_risks(task)
    sha = str(task.get("submission_sha") or "")[:12]
    return _brief(
        f"Работа сдана на ревью (коммит {sha or 'не закреплён'}).",
        "Нужен ваш вердикт.",
        recommend,
        risks=risks,
        checks=_review_checks(doubts, views),
    )


def _review_checks(
    doubts: list[Doubt], views: list[MachineReviewView]
) -> list[dict[str, str]]:
    def of(code: str) -> list[Doubt]:
        return [d for d in doubts if d.code == code]

    ci, integrity, report = of(CODE_CI), of(CODE_INTEGRITY), of(CODE_REPORT)
    return [
        _check(
            "CI", "doubt" if ci else "ok", ci[0].why or ci[0].risk if ci else "зелёный"
        ),
        _check(
            "Целостность CI",
            "doubt" if integrity else "ok",
            integrity[0].why if integrity else "входы CI не менялись",
        ),
        _check(
            "Машинное ревью",
            "doubt" if report or not views else "ok",
            "; ".join(d.risk for d in report) or "отчёт текущей сдачи полный",
        ),
    ]


# ---- задача-состояние --------------------------------------------------------


async def _state_brief(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> dict[str, Any]:
    view = await state_review.state_view_of(db, task)
    have = f"доказательства поколения {view.generation} по {len(view.evidence)} AC"
    checks = [
        _check("Доказательства", "ok" if view.evidence_complete else "doubt", have),
        _check(
            "Откат (rollback)",
            "ok" if view.rollback else "doubt",
            view.rollback or "откат не назван",
        ),
    ]
    doubts = [c for c in checks if c["state"] == "doubt"]
    risks = [_risk("high", f"{c['name']}: {c['detail']}") for c in doubts]
    if doubts:
        recommend = _recommend(
            CHECK, "", [view.headline] + [c["detail"] for c in doubts]
        )
    else:
        recommend = _recommend(
            ACCEPT, VERDICT_APPROVED, [have, "откат назван, комплект полон"]
        )
    return _brief(
        "Состояние изменено руками, доказательства приложены.",
        "Нужен ваш вердикт.",
        recommend,
        risks=risks + _context_risks(task),
        checks=checks,
    )


# ---- остальные состояния -----------------------------------------------------


async def _draft_brief(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> dict[str, Any]:
    if task.get("dor_passed"):
        score = task.get("readiness_score")
        why = [f"постановка готова (readiness {score if score is not None else '?'})"]
        return _brief(
            "Черновик задачи ждёт одобрения.",
            "Нужно одобрить запуск или вернуть на доработку.",
            _recommend(APPROVE_DRAFT, "", why),
        )
    missing = await _missing_fields(db, task)
    return _brief(
        "Черновик задачи не доведён до готовности.",
        "Постановку нужно доработать.",
        _recommend(FIX_STATEMENT, "", [f"не хватает: {missing}"]),
        risks=[_risk("medium", "Постановка не прошла проверку готовности")],
    )


async def _missing_fields(db: aiosqlite.Connection, task: dict[str, Any]) -> str:
    from hub.services.dor import evaluate_dor

    try:
        names = sorted((await evaluate_dor(db, int(task["id"]))).missing_required)
    except Exception:  # noqa: BLE001 - справка не должна падать из-за чтения git
        return "поля не определены (проверка готовности не прочитана)"
    return ", ".join(names) or "поля не названы"


async def _decision_brief(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> dict[str, Any]:
    grounds = await inbox_decisions._decide_grounds(db, task)
    return _brief(
        "Работа остановилась и ждёт решения.",
        "Нужно решить, как продолжать.",
        _recommend(HUMAN_CALL, "", [grounds]),
    )


async def _running_brief(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    offered = ctx.get("repair_offered")
    if offered is None and task.get("review_verdict") == VERDICT_CHANGES:
        from hub.services.executor_launch import repair_offered

        offered = await repair_offered(db, task)
    if offered:
        return _brief(
            "Вердикт «на доработку» вынесен, исправление не запущено.",
            "Можно запустить повторный прогон.",
            _recommend(RERUN, "", ["после «на доработку» есть что чинить"]),
        )
    return _brief(
        "Над задачей идёт работа.",
        "От вас ничего не нужно.",
        _recommend(WORKING, "", ["исполнитель работает"]),
    )


async def _completed_brief(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    if automation_not_applicable(task):
        return _brief(
            "Состояние изменено и принято.",
            "От вас ничего не нужно.",
            _recommend(DELIVERED, "", ["задача-состояние: выкат не применим"]),
        )
    delivery = ctx.get("delivery")
    if delivery is None:
        from hub.services.delivery_state import delivery_state

        delivery = await delivery_state(db, int(task["id"]))
    if delivery.get("state") == "in_prod":
        return _brief(
            "Работа влита и запущена в проде.",
            "От вас ничего не нужно.",
            _recommend(DELIVERED, "", [str(delivery.get("reason") or "в проде")]),
        )
    reason = str(delivery.get("reason") or "выкат не подтверждён")
    return _brief(
        "Работа влита, но запуск в проде не подтверждён.",
        "Стоит убедиться, что выкат состоялся.",
        _recommend(MERGED_NOT_SHIPPED, "", [reason]),
        risks=[_risk("medium", "Выкат не подтверждён", reason)],
    )


def recommend_line(brief: dict[str, Any]) -> str:
    """Рекомендация одной строкой: её печатают и карточка, и входящие (#1810)."""
    rec = brief["recommend"]
    why = "; ".join(str(w) for w in rec["why"])
    return f"{rec['action']}: {why}" if why else str(rec["action"])


async def build(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Справка по задаче: ``{happened, need, risks[≤3], recommend, checks}``.

    ``ctx`` — уже посчитанное вызывающим (``route``, ``repair_offered``,
    ``delivery``): чтобы не спрашивать у читателей дважды. Без него всё
    читается здесь. Ничего не пишет.
    """
    ctx = ctx or {}
    status = str(task.get("status") or "")
    if status == "review":
        if automation_not_applicable(task):
            return await _state_brief(db, task)
        return await _review_brief(db, task, ctx)
    if status == "draft":
        return await _draft_brief(db, task)
    if status == "needs_decision":
        return await _decision_brief(db, task)
    if status in ("running", "claimed", "fix_requested"):
        return await _running_brief(db, task, ctx)
    if status == "completed":
        return await _completed_brief(db, task, ctx)
    return _brief(
        f"Статус задачи: {status or 'неизвестен'}.",
        "От вас ничего не нужно.",
        _recommend(NO_ACTION, "", ["для этого статуса справка действия не даёт"]),
    )
