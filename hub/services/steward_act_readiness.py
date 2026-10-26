"""Отчёт готовности act стюарда: чистое чтение (#1855).

Владелец включает act (применение approve стюарда без человека) по цифрам, а
не на глаз. Отчёт отвечает на три вопроса, ничего не записывая — ни в ленту,
ни в базу, ни в метки применения:

* какие критерии выхода (``act_refusals_v2``) не выполнены. В режиме ``act``
  это фактические отказы, в ``shadow``/``off`` — гипотетические «как если бы
  act был запрошен», посчитанные тем же расчётом;
* на каких проектах act применит вердикт: поимённо, с потолком класса;
* какие сдачи применимы «на момент снимка»: готовы, заблокированы (с
  причиной) или неизвестно.

Кандидаты читаются ТОЛЬКО из сохранённого: суждения, ответ советника, политика
и сохранённый класс задачи. Пакет доказательств здесь не собирается: его сборка
обновляет подтверждение CI и скан диффа, то есть пишет. Поэтому «готов» значит
«читаемые без записи проверки пройдены»; остальные факты (CI, вершина ветки,
дифф и пересчитанный класс, отчёт ревью, закрытие находок) читает привратник
``steward_apply.apply_refusals`` в момент применения.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.models import RiskClass
from hub.services import project_policy, steward_dispatch, steward_shadow
from hub.services.result_kind import task_automation_not_applicable

READY = "ready"
BLOCKED = "blocked"
UNKNOWN = "unknown"

#: Предел списка кандидатов: отчёт читает человек, а не поллер.
MAX_CANDIDATES = 50

UNCHECKED_NOTE = (
    "применимы на момент снимка по сохранённым данным (суждение, ответ советника, "
    "политика проекта, сохранённый класс); факты пакета — CI, вершину ветки, дифф "
    "и пересчитанный класс, отчёт ревью, закрытие находок — привратник читает "
    "в момент применения"
)

# approve судьи нового контура без исхода применения — те же строки, что берёт
# поллер (steward_advisor.apply_advisor_outcomes), но без метки.
_CANDIDATE_SQL = (
    "SELECT j.task_id, j.generation, t.risk_class "
    "FROM steward_judgements j JOIN tasks t ON t.id = j.task_id "
    "WHERE j.kind='verdict' AND j.verdict='approve' AND j.contour=2 "
    "AND j.advisor_outcome='' AND t.status='review' "
    "AND t.submission_generation = j.generation "
    "AND (t.review_verdict_generation IS NULL "
    "     OR t.review_verdict_generation != j.generation) "
    "ORDER BY j.task_id LIMIT ?"
)


def _reason(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def ceiling_reason(
    ceiling: str | None,
    stored_raw: Any,
    recomputed_raw: Any = None,
    key: str = "steward_approve_max_class",
) -> tuple[str, dict[str, str]] | None:
    """(BLOCKED|UNKNOWN, причина) по потолку, или None, если потолка нет.

    Пересчитанный по диффу класс чистым чтением не получить (он собирается
    вместе с записывающим пакетом), поэтому при заданном потолке кандидат без
    него не бывает ``ready``: «не проверено» не равно «в пределах». Если
    пересчитанный класс известен вызывающему, сравнивается max(stored, recomputed).
    """
    from hub.services.steward_apply import REFUSED_CLASS_CEILING

    if ceiling is None:
        return None
    named = f"{key}={ceiling}"
    if ceiling == project_policy.STEWARD_CEILING_UNREADABLE:
        return BLOCKED, _reason(
            REFUSED_CLASS_CEILING, f"{named}: значение потолка нечитаемо"
        )
    try:
        stored = RiskClass(str(stored_raw or "").strip())
    except ValueError:
        return BLOCKED, _reason(
            REFUSED_CLASS_CEILING,
            f"{named}: недостающий факт risk_class.stored — класс задачи не вычислен",
        )
    order = list(RiskClass)
    limit = order.index(RiskClass(ceiling.upper()))
    if order.index(stored) > limit:
        return BLOCKED, _reason(
            REFUSED_CLASS_CEILING, f"{named}: класс задачи {stored.value} выше потолка"
        )
    try:
        recomputed = RiskClass(str(recomputed_raw or "").strip())
    except ValueError:
        return UNKNOWN, _reason(
            REFUSED_CLASS_CEILING,
            f"{named}: пересчитанный класс не проверен (risk_class.recomputed)",
        )
    if order.index(recomputed) > limit:
        return BLOCKED, _reason(
            REFUSED_CLASS_CEILING,
            f"{named}: по диффу {recomputed.value} выше потолка",
        )
    return None


def _advisor_reason(state: Any) -> tuple[str, dict[str, str]] | None:
    """(BLOCKED|UNKNOWN, причина) по состоянию советника, или None, если согласен."""
    from hub.services import steward_advisor as adv

    detail = f"советник: {state.state} — {state.reason}".rstrip(" —")
    if state.state == adv.STATE_RECEIVED:
        if state.answer == adv.ANSWER_CONCUR:
            return None
        return BLOCKED, _reason(adv.REFUSED_ADVISOR, "советник возражает")
    if state.state in (adv.STATE_REFUSED, adv.STATE_TIMEOUT) or state.skipped:
        return BLOCKED, _reason(adv.REFUSED_ADVISOR, detail)
    return UNKNOWN, _reason(adv.REFUSED_ADVISOR, detail)


async def _empty_report_reason(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[str, dict[str, str]] | None:
    """(BLOCKED|UNKNOWN, причина) по пустому отчёту (#2082): чтение, без сети.

    Известный запрет - класс выше потолка пустого ревью; биллинг не проверен -
    ``unknown``, потому что ``ready`` обещало бы то, чего чтение не видело.
    """
    from hub.services.steward_empty_report import (
        CODE_NO_DATA,
        refusal_of,
        stored_reading,
    )

    reading = await stored_reading(db, task_id, generation)
    if reading is None:
        return None
    decision, task = reading
    if decision.code:
        return BLOCKED, _reason(*(refusal_of(decision, task) or ("", "")))
    if decision.pending:
        return UNKNOWN, _reason(
            CODE_NO_DATA,
            "пустой отчёт — нет данных: биллинг провайдера не проверен "
            "(чтение без сети), работа ревьюера не доказана",
        )
    return None


async def _candidate(db: aiosqlite.Connection, row: Any) -> dict[str, Any] | None:
    from hub.services.steward_advisor import advisor_state, cap_refusal
    from hub.services.steward_apply import policy_refusal
    from hub.services.steward_double_clean import may_apply_to_class

    task_id, generation = int(row["task_id"]), int(row["generation"])
    if await task_automation_not_applicable(db, task_id):
        return None  # задача-state контуру не отдаётся — кандидатом не является
    blocked: list[dict[str, str]] = []
    unknown: list[dict[str, str]] = []
    not_delegated = await policy_refusal(db, task_id)
    if not_delegated:
        blocked.append(_reason(*not_delegated))
    project = await repo.resolve_project_for_task(db, task_id)
    policy = project_policy.gate_policy_of(project) if project is not None else {}
    ceiling = ceiling_reason(
        project_policy.steward_approve_max_class_of(policy), row["risk_class"]
    )
    double_clean = bool(
        ceiling
        and ceiling[0] == BLOCKED
        and await may_apply_to_class(db, task_id, row["risk_class"])
    )
    if double_clean:
        # #1986: выше обычного потолка, но не выше двойной чистоты. Пара отчётов,
        # CI и согласие советника чтением не проверяются: непроверенное - unknown.
        unknown.append(
            _reason(
                ceiling[1]["code"],
                ceiling[1]["detail"]
                + "; двойная чистота: условия (пара отчётов, CI, советник) "
                "чтением не проверены",
            )
        )
    elif ceiling:
        (blocked if ceiling[0] == BLOCKED else unknown).append(ceiling[1])
    empty = (
        None if double_clean else await _empty_report_reason(db, task_id, generation)
    )
    if empty:
        (blocked if empty[0] == BLOCKED else unknown).append(empty[1])
    # Передача по суточному потолку - известный запрет и при действительном
    # согласии; спрашивается тем же читателем, что и применение (чистое чтение).
    handoff = await cap_refusal(db, task_id, generation)
    if handoff:
        blocked.append(_reason(*handoff))
        advised = None
    else:
        advised = _advisor_reason(await advisor_state(db, task_id, generation))
    if advised:
        (blocked if advised[0] == BLOCKED else unknown).append(advised[1])
    state = BLOCKED if blocked else UNKNOWN if unknown else READY
    return {
        "task_id": task_id,
        "generation": generation,
        "project": str(project["slug"]) if project is not None else None,
        "state": state,
        "reasons": [*blocked, *unknown],
    }


async def candidates(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    rows = await fetchall(db, _CANDIDATE_SQL, (MAX_CANDIDATES,))
    out = [await _candidate(db, row) for row in rows]
    return [c for c in out if c is not None]


async def delegated_projects(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Проекты, где вердикт отдан стюарду: act применит approve на каждом."""
    out: list[dict[str, Any]] = []
    for project in await repo.list_projects(db):
        if not steward_dispatch.verdict_delegated_to_steward(project):
            continue
        policy = project_policy.gate_policy_of(project)
        out.append(
            {
                "slug": str(project["slug"]),
                "steward_shadow": steward_dispatch.steward_shadow_of(policy),
                "steward_approve_max_class": (
                    project_policy.steward_approve_max_class_of(policy)
                ),
                # #1986: исключение из потолка при двойной чистоте.
                "steward_approve_max_class_double_clean": (
                    project_policy.steward_double_clean_ceiling_of(policy)
                ),
                "double_clean_reviewers": (
                    project_policy.double_clean_reviewers_of(policy)
                ),
            }
        )
    return sorted(out, key=lambda p: p["slug"])


async def autopilot_projects(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Проекты с verdict=auto: потолок автопилота (``auto_approve_max_class``)."""
    out = []
    for project in await repo.list_projects(db):
        policy = project_policy.gate_policy_of(project)
        if project_policy.gate_value_of(policy, "verdict") != "auto":
            continue
        out.append(
            {
                "slug": str(project["slug"]),
                "auto_approve_max_class": project_policy.auto_approve_max_class_of(
                    policy
                ),
            }
        )
    return sorted(out, key=lambda p: p["slug"])


async def readiness_report(
    db: aiosqlite.Connection, mode: dict[str, Any]
) -> dict[str, Any]:
    """Готовность act: критерии, охват, кандидаты. Ничего не пишет."""
    hypothetical = mode["requested"] != "act"
    if hypothetical:
        refusals = [
            {"code": c, "detail": d} for c, d in await steward_shadow.act_refusals(db)
        ]
    else:
        refusals = list(mode["act_refusals"])
    found = await candidates(db)
    return {
        "status": "ok",
        "basis": "hypothetical" if hypothetical else "actual",
        "act_refusals_v2": refusals,
        "act_would_be_granted": not refusals,
        "projects": await delegated_projects(db),
        "autopilot_projects": await autopilot_projects(db),
        "candidates": found,
        "candidate_counts": {
            state: sum(1 for c in found if c["state"] == state)
            for state in (READY, BLOCKED, UNKNOWN)
        },
        "note": UNCHECKED_NOTE,
    }


def unknown_report(reason: str) -> dict[str, Any]:
    """Отчёт не посчитан: «неизвестно», а не «пусто» (#762)."""
    return {"status": "unknown", "reason": reason}
