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
        if str(risk.get("text")) in printed:
            continue
        printed.add(str(risk.get("text")))
        risks.append(risk)
    # Риск, ушедший в «почему», уже назван: у проверки на него ссылка, не повтор.
    named = printed | {str(r.get("text")) for r in brief.get("risks") or []}
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
) -> dict[str, Any]:
    """Всё, что нужно шаблону C2. Чистая функция: ничего не читает и не пишет."""
    tone, label = tone_for(task, brief, delivery, in_flight)
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
        "from_you": (brief or {}).get("need", ""),
        "recommend": (brief or {}).get("recommend"),
        "risks": [_risk_view(r) for r in risks],
        "checks": lights,
    }


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
    return card_view(
        row,
        brief,
        delivery=delivery,
        in_flight=bool(in_flight),
        project_name=str(project["name"]) if project is not None else "",
        plain_summary=str(getattr(task_view, "plain_summary_shown", "") or ""),
    )
