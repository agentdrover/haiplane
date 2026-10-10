"""Маршрут вердикта: кто вынесет вердикт текущей сдачи и при каком условии (#1440).

Три места решают, чем кончится ревью: автопилот (``auto_verdict``), стюард
(``steward_dispatch`` / ``steward_apply``) и человек. До этой задачи
ответ «кто из них» знали только решатели в момент решения; агент и стюард-сессия
восстанавливали его по памяти и 26.09 дважды ошиблись (#1384, #1385).

Эта функция — не четвёртый решатель и не вторая копия правил. У неё нет
своих условий: каждый вопрос она задаёт тому же читателю, которым пользуется
решатель, — стойке автопилота (``auto_verdict.autopilot_stance``, её проверки
выполняет и ``maybe_auto_verdict``), ``steward_dispatch._policy_wants_steward``,
``steward_shadow.mode_report`` / ``family_refusal`` и
``steward_apply.policy_refusal`` — и только называет ответ. Правило, которое
меняется у решателя, меняется и здесь, потому что здесь его нет.

Неизвестное — это человек с названной причиной, а не автопилот: нечитаемая
политика, нет отчёта, непонятная модель.

Чистые помощники текста (``route_line``, ``actor_hint_of``,
``next_action_of``) живут в ``hub.mcp_envelope``: конверт ответа MCP не может
импортировать сервисы (круг), а им он и пользуется.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import aiosqlite

from hub.mcp_envelope import route_line
from hub.services.result_kind import (
    AUTOMATION_REFUSAL,
    AUTOMATION_REFUSAL_CODE,
    automation_not_applicable,
)

DECIDER_POLICY = "policy"
DECIDER_STEWARD = "steward"
DECIDER_HUMAN = "human"
DECIDER_NONE = "none"

CODE_LOCK_743 = "gate_lock_743"
CODE_STEWARD_OFF = "steward_off"
CODE_REVIEWER_PENDING = "reviewer_pending"
CODE_STEWARD_SHADOW = "steward_shadow"
CODE_STEWARD_ACT = "steward_act"
CODE_NOT_IN_REVIEW = "not_in_review"

# Коды стойки, после которых стюарда спрашивать не о чем: сдачи, отчёта или
# политики нет, и судить нечего и некому.
_UNKNOWN_CODES = frozenset(
    {
        "project_unresolved",
        "policy_unreadable",
        "no_submission",
        "no_report",
        "report_stale",
        "review_running",
    }
)


@dataclass(frozen=True)
class VerdictRoute:
    """Ответ маршрута. ``final`` — кто на деле запишет вердикт."""

    decider: str
    final: str
    mode: str
    code: str
    reason: str
    condition: str = ""
    pending: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pending"] = list(self.pending)
        # Одна строка на все выходы: карточка, бриф, MCP и CLI печатают её,
        # а не собирают из полей каждый по-своему.
        data["line"] = route_line(data)
        return data


#: Условие на каждую проверку, которой при показе не делали. Названы ВСЕ,
#: а не первая найденная: строка «условие: …» с одним из двух прячет второе
#: непроверенное (#1440, находка ревью).
_PENDING_TEXT = {
    "branch": (
        "вершина ветки на месте, дифф в заявленных областях и класс не "
        "вырос (проверка по сети, при показе не делалась)"
    ),
    "provider_usage": (
        "провайдер подтвердит, что пустое ревью — работа (биллинг по сети, "
        "при показе не проверялся)"
    ),
}


def _condition_of(pending: tuple[str, ...]) -> str:
    if not pending:
        return ""
    return "если " + "; и ".join(_PENDING_TEXT.get(name, name) for name in pending)


def _human(code: str, reason: str, mode: str = "") -> VerdictRoute:
    return VerdictRoute(DECIDER_HUMAN, DECIDER_HUMAN, mode, code, reason)


async def _text_origin_refusal(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> tuple[str, str] | None:
    """``(код, причина)``, если текущий отчёт восстановлен из текста прогона (#1587)."""
    from hub import repository as repo
    from hub.services.auto_verdict import text_origin_refusal

    review = await repo.get_latest_machine_review(db, int(task["id"]))
    if review is None:
        return None
    row = dict(review)
    if (row.get("submission_generation") or 0) != int(
        task.get("submission_generation") or 0
    ):
        return None
    return text_origin_refusal(row)


async def _steward_route(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    project: Any,
    stance: Any,
) -> VerdictRoute:
    """Автопилот молчит, а проект просит стюарда: судит ли он и применяется ли суждение."""
    from hub import config
    from hub.services import steward_apply, steward_shadow

    if stance.code in _UNKNOWN_CODES:
        return _human(stance.code, stance.reason)
    mode = (await steward_shadow.mode_report(db))["effective"]
    if mode == "off":
        return _human(
            CODE_STEWARD_OFF,
            f"стюард выключен (STEWARD_MODE={config.STEWARD_MODE!r}); "
            f"автопилот не вынесет: {stance.reason}",
            mode,
        )
    generation = int(task.get("submission_generation") or 0)
    implementer = (task.get("submission_model") or "").strip()
    reviewer = await steward_shadow.reviewer_model(db, int(task["id"]), generation)
    steward = (config.STEWARD_MODEL or "").strip()
    if steward_shadow._only_the_reviewer_is_missing(
        steward, implementer, reviewer
    ) and await steward_shadow._project_expects_a_reviewer(db, int(task["id"])):
        return _human(
            CODE_REVIEWER_PENDING,
            "кросс-модельного ревьюера ещё не позвали: стюард не начнёт, пока "
            "модель ревьюера неизвестна",
            mode,
        )
    refusal = steward_shadow.family_refusal(steward, implementer, reviewer)
    if refusal is not None:
        return _human(refusal[0], refusal[1], mode)
    not_given = await steward_apply.policy_refusal(db, int(task["id"]))
    if not_given is not None:
        return VerdictRoute(
            DECIDER_STEWARD,
            DECIDER_HUMAN,
            mode,
            not_given[0],
            f"стюард судит в тени, вердикт выносит человек: {not_given[1]}",
        )
    if mode != "act":
        return VerdictRoute(
            DECIDER_STEWARD,
            DECIDER_HUMAN,
            mode,
            CODE_STEWARD_SHADOW,
            "стюард в тени: суждение записывается, вердикт выносит человек",
        )
    return VerdictRoute(
        DECIDER_STEWARD,
        DECIDER_STEWARD,
        mode,
        CODE_STEWARD_ACT,
        f"автопилот не вынесет ({stance.reason}); судит стюард",
        "если суждение — approve, советник-критик согласен на том же пакете и "
        "привратник применения не возражает; иначе "
        "вердикт остаётся человеку",
    )


async def verdict_route(
    db: aiosqlite.Connection, task_id: int, *, observe: bool = False
) -> VerdictRoute:
    """Кто вынесет вердикт текущей сдачи — читатель, ничего не пишет.

    ``observe=False`` (по умолчанию) не ходит в сеть: ветку и биллинг
    провайдера не проверяет, и «политика» тогда названа с условием в
    ``condition``. ``observe=True`` задаёт те же вопросы, что решатель, —
    так отвечает сверка маршрута с исходом.
    """
    from hub import repository as repo
    from hub.services import auto_verdict, project_policy, steward_dispatch

    row = await repo.get_task(db, task_id)
    if row is None or dict(row).get("status") != "review":
        return VerdictRoute(
            DECIDER_NONE, DECIDER_NONE, "", CODE_NOT_IN_REVIEW, "задача не на ревью"
        )
    task = dict(row)
    if automation_not_applicable(task):
        # #1648: задача-состояние (#1647) — вердикт ТОЛЬКО человека, и причина
        # названа тем же текстом, что в отказах дверей автоматики. До стойки
        # автопилота не доходим: она ищет машинный отчёт, которого у такой
        # задачи нет, и отвечала бы «нет отчёта» вместо настоящей причины.
        return _human(AUTOMATION_REFUSAL_CODE, AUTOMATION_REFUSAL)
    stance = await auto_verdict.autopilot_stance(db, task_id, observe=observe)
    if stance.outcome == auto_verdict.OUTCOME_APPROVE:
        return VerdictRoute(
            DECIDER_POLICY,
            DECIDER_POLICY,
            "",
            stance.code,
            stance.reason,
            _condition_of(stance.pending),
            stance.pending,
        )
    project = await repo.resolve_project_for_task(db, task_id)
    if project is not None and steward_dispatch._policy_wants_steward(project):
        # #1587: отчёт, переписанный хабом из текста прогона, стюарду не
        # передаётся — автопилот до него не доходит (проект не auto), поэтому
        # проверка здесь, а не в стойке.
        text_origin = await _text_origin_refusal(db, task)
        if text_origin is not None:
            return _human(*text_origin)
        return await _steward_route(db, task, project, stance)
    if stance.code == auto_verdict.CODE_NOT_DELEGATED and (
        project is not None and project_policy.gate_lock_applies(project["slug"])
    ):
        return _human(
            CODE_LOCK_743,
            "замок #743: проект default не принимает делегирование вердикта",
        )
    return _human(stance.code, stance.reason)
