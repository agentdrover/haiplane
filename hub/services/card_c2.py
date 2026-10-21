"""Карточка решения, компоновка C2 (#1812): светофор и блоки из справки.

Здесь нет ни одного правила решения. Справка (``decision_brief.build``) уже
спросила у решателей про CI, ревью, доставку и маршрут вердикта; эта сборка
только переводит её ответ в тон, форму значка и порядок блоков. Правило
безопасности то же, что у справки: зелёный значит «доказано» (доставка
подтверждена либо состояние принято); отсутствие данных, сомнение в проверках
и недоверенное зелёным не бывают. Текст из справки и постановки не
разбирается и не склеивается в HTML: шаблон экранирует его сам.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub import repository as repo
from hub.mcp_envelope import compute_awaiting, compute_next_action
from hub.services import inbox_decisions
from hub.services.decision_brief import (  # noqa: F401 - словарь действий один
    ACCEPT,
    APPROVE_DRAFT,
    CHECK,
    DELIVERED,
    FIX_STATEMENT,
    HUMAN_CALL,
    MERGED_NOT_SHIPPED,
    NO_ACTION,
    REWORK,
    RERUN,
    WORKING,
)

RED, AMBER, BLUE, GREEN, GRAY = "red", "amber", "blue", "green", "gray"
#: Форма значка: тон не держится на одном цвете (текстовый символ + слово).
SHAPES = {RED: "✕", AMBER: "▲", BLUE: "●", GREEN: "✓", GRAY: "–"}
ABOUT_MISSING = "Простым языком не описано"
BRIEF_MISSING = "Справка не собрана"
SEE_ABOVE = "причина названа выше"
DELIVERY_REF = "подробности доставки - в блоке «Доставка» в «Подробностях»"

# Действие справки -> (тон, слово). Зелёного здесь нет намеренно: его даёт
# только ``_delivered_tone`` и только при доказанной доставке.
_BY_ACTION: dict[str, tuple[str, str]] = {
    REWORK: (RED, "Нужна доработка"),
    CHECK: (AMBER, "Нужна проверка"),
    HUMAN_CALL: (AMBER, "Решение за вами"),
    FIX_STATEMENT: (AMBER, "Постановка не готова"),
    MERGED_NOT_SHIPPED: (AMBER, "Влито, выкат не подтверждён"),
    ACCEPT: (BLUE, "Можно принять"),
    APPROVE_DRAFT: (BLUE, "Можно одобрить"),
    RERUN: (BLUE, "Нужен повторный прогон"),
    WORKING: (BLUE, "Идёт работа"),
}


# Статус без справки -> (тон, слово, «От вас», форма). Справка для них не строится
# (decision_brief отвечает «действий нет»), но молчать карточка не вправе.
_NO_BRIEF: dict[str, tuple[str, str, str, str]] = {
    "needs_info": (
        AMBER,
        "Нужен ваш ответ",
        "Ответьте на вопрос агента.",
        "web-answer-form",
    ),
    "open": (
        GRAY,
        "Не начато",
        "От вас ничего не нужно: задача ждёт исполнителя.",
        "",
    ),
    "ci_check": (
        BLUE,
        "Идёт проверка CI",
        "От вас ничего не нужно: ждём конвейер CI.",
        "",
    ),
    "pending_report": (
        AMBER,
        "Ждёт отчёта и решения",
        "Нужен отчёт или ваше решение.",
        "",
    ),
    "failed": (
        RED,
        "Задача не выполнена",
        "Решите, что делать дальше: действия - в «Подробностях».",
        "",
    ),
    "rejected": (
        RED,
        "Отклонено",
        "От вас ничего не нужно: задача отклонена.",
        "",
    ),
}


def no_brief_notice(task: dict[str, Any], question: str = "") -> dict[str, str]:
    """Явное состояние и действие, когда справки нет (#1812, возврат gen1).

    Статус, ``awaiting`` и ``next_action`` берутся у общих читателей
    (``mcp_envelope``), вопрос агента - из ленты тем же читателем, что у
    входящих. Статус, у которого справка должна быть, но не собралась, - серый
    «Справка не собрана»: сбой не выдаётся за известное состояние.
    """
    status = str(task.get("status") or "")
    known = _NO_BRIEF.get(status)
    if known is None:
        return {
            "tone": GRAY,
            "label": BRIEF_MISSING,
            "need": "Справка не собралась: решайте по подробностям ниже.",
            "target": "",
            "next_action": "",
        }
    tone, label, need, target = known
    if status == "needs_info" and question:
        need = f"{need} Вопрос: {question}"
    awaiting = compute_awaiting(status, task)
    return {
        "tone": tone,
        "label": label,
        "need": need,
        "target": target,
        "next_action": compute_next_action(status, awaiting),
    }


def _doubts(brief: dict[str, Any]) -> bool:
    return any(c.get("state") != "ok" for c in brief.get("checks") or [])


def _delivered_tone(task: dict[str, Any], delivery: Any) -> tuple[str, str]:
    if str(task.get("result_kind") or "") == "state":
        return GREEN, "Принято"
    if isinstance(delivery, dict) and delivery.get("state") == "in_prod":
        return GREEN, "Доставлено"
    # Справка сказала «доставлено», а у карточки доказательства нет.
    return AMBER, "Доставка не доказана"


def tone_for(
    task: dict[str, Any], brief: dict[str, Any] | None, delivery: Any, in_flight: bool
) -> tuple[str, str]:
    """``(тон, слово)``: порядок проверок - от самого осторожного к спокойному."""
    if in_flight:
        return BLUE, "Идёт проверка"
    if brief is None:
        return GRAY, BRIEF_MISSING
    action = str(brief["recommend"]["action"])
    if action == DELIVERED:
        return _delivered_tone(task, delivery)
    tone, label = _BY_ACTION.get(action, (GRAY, "Действий нет"))
    if action == ACCEPT and (_doubts(brief) or not brief.get("accept_proven")):
        # «Принять» без доказательства или при сомнении - не спокойный тон.
        return AMBER, "Нужна проверка"
    return tone, label


def _by_reference(brief: dict[str, Any] | None, delivery: Any) -> dict[str, Any] | None:
    """Справка со ссылкой вместо текста, который печатает блок «Доставка» ниже."""
    if brief is None:
        return None
    reason = str(delivery.get("reason") or "") if isinstance(delivery, dict) else ""
    if not reason:
        return brief
    rec = brief["recommend"]
    why = [DELIVERY_REF if str(w) == reason else w for w in rec.get("why") or []]
    risks = [
        {**r, "detail": ""} if str(r.get("detail")) == reason else r
        for r in brief.get("risks") or []
    ]
    return {**brief, "recommend": {**rec, "why": why}, "risks": risks}


def _dedupe(
    brief: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Риски и проверки без того, что рекомендация уже сказала (#1812, AC-1).

    Справка называет одну причину трижды: в «почему», в рисках и в деталях
    проверки. Экран печатает факт один раз по старшинству: рекомендация, риски,
    проверки. Данных не теряется: проверка остаётся строкой со своим состоянием,
    у неё лишь пропадает повтор текста.
    """
    if brief is None:
        return [], []
    printed = {str(w) for w in brief["recommend"].get("why") or []}
    risks = []
    for risk in brief.get("risks") or []:
        text, detail = str(risk.get("text")), str(risk.get("detail") or "")
        if text in printed:
            # Текст риска назван в рекомендации; уникальное пояснение и тяжесть
            # остаются (потеряться может только повтор, не факт).
            if detail and detail not in printed:
                printed.add(detail)
                risks.append({**risk, "text": detail, "detail": ""})
            continue
        printed.add(text)
        risks.append(risk)
    # Риск, ушедший в «почему», уже назван: у проверки на него ссылка, не повтор.
    named = printed | {
        str(x)
        for r in brief.get("risks") or []
        for x in (r.get("text"), r.get("detail"))
        if x
    }
    checks = []
    for check in brief.get("checks") or []:
        parts = [p for p in str(check.get("detail") or "").split("; ") if p]
        fresh = [p for p in parts if p not in named]
        detail = "; ".join(fresh) if fresh or not parts else SEE_ABOVE
        checks.append({**check, "detail": detail})
    return risks, checks


def _light(check: dict[str, Any]) -> dict[str, str]:
    state = str(check.get("state") or "")
    tone = GREEN if state == "ok" else AMBER if state == "doubt" else GRAY
    return {
        "name": str(check.get("name") or ""),
        "state": state,
        "tone": tone,
        "shape": SHAPES[tone],
        "detail": str(check.get("detail") or ""),
    }


def _risk_view(risk: dict[str, Any]) -> dict[str, str]:
    severity = str(risk.get("severity") or "")
    tone = {"high": RED, "medium": AMBER}.get(severity, GRAY)
    return {**{k: str(v) for k, v in risk.items()}, "tone": tone, "shape": SHAPES[tone]}


def card_view(
    task: dict[str, Any],
    brief: dict[str, Any] | None,
    *,
    delivery: Any = None,
    in_flight: bool = False,
    project_name: str = "",
    plain_summary: str = "",
    question: str = "",
) -> dict[str, Any]:
    """Всё, что нужно шаблону C2. Чистая функция: ничего не читает и не пишет."""
    tone, label = tone_for(task, brief, delivery, in_flight)
    notice = no_brief_notice(task, question) if brief is None else None
    if notice is not None and not in_flight:
        tone, label = notice["tone"], notice["label"]
    brief = _by_reference(brief, delivery)
    risks, deduped = _dedupe(brief)
    lights = [_light(c) for c in deduped]
    ok = sum(1 for light in lights if light["state"] == "ok")
    about = (plain_summary or "").strip()
    return {
        "number": task["id"],
        "project": project_name,
        "tone": tone,
        "tone_label": label,
        "shape": SHAPES[tone],
        "lights": lights,
        "lights_summary": f"{ok} из {len(lights)}",
        "about": about or ABOUT_MISSING,
        "about_missing": not about,
        "original_title": str(task.get("title") or ""),
        "happened": (brief or {}).get("happened", ""),
        "from_you": (brief or {}).get("need") or (notice or {}).get("need", ""),
        "next_action": (notice or {}).get("next_action", ""),
        "action_target": _target(task, notice),
        "status": str(task.get("status") or ""),
        "recommend": (brief or {}).get("recommend"),
        "risks": [_risk_view(r) for r in risks],
        "checks": lights,
    }


def _target(task: dict[str, Any], notice: dict[str, str] | None) -> str:
    """Форма для «К действию» без справки; со справкой цель выбирает шаблон."""
    return notice["target"] if notice else ""


async def card_context(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    task_view: Any,
    brief: dict[str, Any] | None,
    delivery: Any,
    in_flight: Any,
) -> dict[str, Any]:
    """Вид C2 для страницы (#1812): проект читается здесь, остальное уже посчитано."""
    project = await repo.resolve_project_for_task(db, int(row["id"]))
    question = ""
    if brief is None and str(row.get("status")) == "needs_info":
        question = (await inbox_decisions._question_grounds(db, row)).strip()
    return card_view(
        row,
        brief,
        delivery=delivery,
        in_flight=bool(in_flight),
        project_name=str(project["name"]) if project is not None else "",
        plain_summary=str(getattr(task_view, "plain_summary_shown", "") or ""),
        question=question,
    )
