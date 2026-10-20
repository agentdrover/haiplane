"""Справка для решения без LLM: что произошло, что нужно, риски, рекомендация, проверки (#1810).

Данные для решения лежат в разных читателях: итог машинного ревью, маршрут
вердикта, доверие к CI, целостность CI, доставка, суждение стюарда. Эта сборка
ничего не вычисляет заново: каждый вопрос она задаёт тому же читателю, что
решатель, и только называет ответ простыми словами. Новых таблиц нет, LLM нет:
справка собирается на запрос, поэтому у карточки и у входящих один текст.

Правило безопасности одно: ложное «Принять» хуже ложного «Проверить».
«Принять» выдаётся только из ``accept_proof.prove_accept`` — доказательства по
графу происхождения отчётов (#1912; в #1810 оно было выключено после семи
кругов частных правок). Нет доказательства — «Проверить» или «На доработку».
Отсутствие данных — не данные: непрочитанное называется «неизвестно», а не
«чисто».
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub import repository as repo
from hub.models import MachineReviewView
from hub.services import inbox_decisions, verdict_evidence
from hub.services.accept_proof import (
    CODE_CI,
    CODE_INTEGRITY,
    CODE_REPORT,
    CODE_SHA,
    CODE_VERDICT,
    AcceptProof,
    Blocker,
    prove_accept,
    prove_state_accept,
)
from hub.services.readiness import parse_risks_from_row
from hub.services.result_kind import automation_not_applicable, state_accepted
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
VERDICT_CHANGES = "changes_requested"
VERDICT_APPROVED = "approved"

MAX_RISKS = 3
_WEIGHT = {"high": 3, "medium": 2, "low": 1}
#: Класс риска, с которого справка называет его риском (R3 и выше).
_HIGH_CLASSES = frozenset({"R3", "R4", "R5"})
_STEWARD_ACTIONS = {
    "approve": (CHECK, ""),
    "changes_requested": (REWORK, VERDICT_CHANGES),
    "escalate": (HUMAN_CALL, ""),
}


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
    accept_proven: bool = False,
) -> dict[str, Any]:
    return {
        "accept_proven": accept_proven,
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


STATE_NOT_ACCEPTED = "для задачи-состояния «Принять» не выдаётся: решает человек"


def _state_proven_but_capped(why: list[str]) -> dict[str, Any]:
    """Задача-состояние: комплект доказан, но совет остаётся «Проверить»."""
    return _recommend(CHECK, "", [STATE_NOT_ACCEPTED, *why])


def _from_steward(judged: Any, route: Any) -> dict[str, Any] | None:
    # Низкая уверенность стюарда по контракту (#1022) — эскалация, а не вердикт.
    word = "escalate" if str(judged["confidence"]) == "low" else str(judged["verdict"])
    mapped = _STEWARD_ACTIONS.get(word)
    if mapped is None:
        return None
    action, verdict = mapped
    why = _ground_sources(judged["grounds"])
    if action == HUMAN_CALL:
        why.insert(0, "стюард передал решение вам: причина в суждении на карточке")
    if tail := _route_why(route):
        why.append(tail)
    why = why or ["стюард без пояснений"]
    if action == CHECK and word == "approve":
        # Суждение стюарда «approve» — не доказательство: «Принять» даёт
        # только prove_accept, правила сильнее суждения.
        return None
    return _recommend(action, verdict, why)


def _from_rules(
    proof: AcceptProof | None, blockers: list[Blocker], route: Any
) -> dict[str, Any]:
    if proof is not None:
        why = [
            "CI доверенно зелёный на коммите сдачи",
            "отчёты ревью читаются, полные, независимые, находок нет",
            "ветка стоит на сданном коммите, вердикта «на доработку» нет",
            f"проверено отчётов по графу происхождения: {len(proof.report_ids)}",
        ]
        if tail := _route_why(route):
            why.append(tail)
        return _recommend(ACCEPT, VERDICT_APPROVED, why)
    rework = any(b.level == REWORK for b in blockers)
    action, verdict = (REWORK, VERDICT_CHANGES) if rework else (CHECK, "")
    alt = "" if rework else "На доработку, если проверка подтвердит сомнение"
    return _recommend(action, verdict, [b.risk for b in blockers], alt)


async def _review_brief(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    proof, blockers, views = await prove_accept(db, task, ctx)
    route = ctx.get("route") or await verdict_route(db, int(task["id"]))
    judged = await _steward_row(db, task)
    recommend = _from_steward(judged, route) if judged is not None else None
    if recommend is None:
        recommend = _from_rules(proof, blockers, route)
        if judged is not None and proof is None:
            recommend["why"].append("суждение стюарда не принято: условия не доказаны")
    risks = [_risk(b.severity, b.risk, b.why) for b in blockers] + _context_risks(task)
    sha = str(task.get("submission_sha") or "")[:12]
    return _brief(
        f"Работа сдана на ревью (коммит {sha or 'не закреплён'}).",
        "Нужен ваш вердикт.",
        recommend,
        risks=risks,
        checks=_review_checks(blockers, views, _tip_age_text(task)),
        accept_proven=proof is not None,
    )


def _tip_age_text(task: dict[str, Any]) -> str:
    """Возраст наблюдения вершины: «совпало» держится на нём, читатель его видит."""
    from hub.services.lifecycle import observed_tip_age_minutes

    age = observed_tip_age_minutes(int(task["id"]), str(task.get("branch") or ""))
    if age is None:
        return "вершина ветки равна сданному коммиту"
    return f"вершина ветки равна сданному коммиту (наблюдение {age} мин назад)"


def _review_checks(
    blockers: list[Blocker], views: list[MachineReviewView], tip_ok_text: str
) -> list[dict[str, str]]:
    def row(name: str, code: str, ok_text: str) -> dict[str, str]:
        found = [b for b in blockers if b.code == code]
        if not found:
            return _check(name, "ok", ok_text)
        return _check(name, "doubt", "; ".join(b.risk for b in found))

    report = row("Машинное ревью", CODE_REPORT, "отчёты текущей сдачи годны")
    if not views and report["state"] == "ok":
        report = _check("Машинное ревью", "doubt", "отчётов нет")
    return [
        row("CI", CODE_CI, "зелёный"),
        row("Целостность CI", CODE_INTEGRITY, "входы CI не менялись"),
        report,
        row("Ветка = сдача", CODE_SHA, tip_ok_text),
        row("Вердикт", CODE_VERDICT, "действующего «на доработку» нет"),
    ]


# ---- задача-состояние --------------------------------------------------------


async def _state_brief(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> dict[str, Any]:
    proof, blockers, view = await prove_state_accept(db, task)
    have = f"доказательства поколения {view.generation} по {len(view.evidence)} AC"
    checks = [
        _check("Доказательства", "ok" if view.evidence_complete else "doubt", have),
        _check(
            "Откат (rollback)",
            "ok" if view.rollback else "doubt",
            view.rollback or "откат не назван",
        ),
    ]
    risks = [_risk("high", b.risk, b.why) for b in blockers]
    if proof is None:
        rework = any(b.level == REWORK for b in blockers)
        action, verdict = (REWORK, VERDICT_CHANGES) if rework else (CHECK, "")
        recommend = _recommend(
            action, verdict, [view.headline] + [b.risk for b in blockers]
        )
    else:
        recommend = _state_proven_but_capped([have, "откат назван, комплект полон"])
    return _brief(
        "Состояние изменено руками, доказательства приложены.",
        "Нужен ваш вердикт.",
        recommend,
        risks=risks + _context_risks(task),
        checks=checks,
        accept_proven=proof is not None,
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
    # Повторный прогон предлагает вызывающий (карточка знает repair_offered);
    # без него справка не опрашивает исполнителей и говорит «идёт работа».
    offered = bool(ctx.get("repair_offered"))
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
        # «Принято» — по единому признаку, а не по статусу: completed закрывает и
        # force-complete (result_kind.state_accepted).
        if state_accepted(task):
            return _brief(
                "Состояние изменено и принято.",
                "От вас ничего не нужно.",
                _recommend(DELIVERED, "", ["задача-состояние: выкат не применим"]),
            )
        return _brief(
            "Задача-состояние закрыта без действующего одобрения.",
            "Стоит проверить, почему закрыта без вердикта.",
            _recommend(CHECK, "", ["статус completed принятия не доказывает"]),
            risks=[_risk("medium", "Закрыта без одобрения текущей сдачи")],
        )
    # Доставку считает вызывающий (карточка уже её знает): справка в git не ходит.
    # Нет данных о доставке — это «неизвестно», а не «влито».
    delivery = ctx.get("delivery")
    if not delivery:
        return _brief(
            "Данных о доставке нет: неизвестно, доехала ли работа до прода.",
            "Нужно сверить доставку.",
            _recommend(CHECK, "", ["доставка не сверена: данные не получены"]),
            risks=[_risk("medium", "Доставка не сверена")],
        )
    if delivery.get("state") == "in_prod":
        return _brief(
            "Работа влита и запущена в проде.",
            "От вас ничего не нужно.",
            _recommend(DELIVERED, "", [str(delivery.get("reason") or "в проде")]),
        )
    reason = str(delivery.get("reason") or "выкат не подтверждён")
    # «Неизвестно» не выдаётся за факт: про мерж говорим только при not_in_prod.
    happened = (
        "Работа влита, но запуск в проде не подтверждён."
        if delivery.get("state") == "not_in_prod"
        else "Запуск в проде не подтверждён; факт мержа не установлен."
    )
    action = MERGED_NOT_SHIPPED if delivery.get("state") == "not_in_prod" else CHECK
    return _brief(
        happened,
        "Стоит убедиться, что выкат состоялся.",
        _recommend(action, "", [reason]),
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
