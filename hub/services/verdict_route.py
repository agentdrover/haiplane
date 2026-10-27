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
from hub.services import verdict_owner
from hub.services.steward_apply import CeilingUnavailable
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
CODE_ADVISOR_SKIPPED = "steward_advisor_skipped"
CODE_APPLY_FAILED = "steward_apply_failed"
CODE_NOT_IN_REVIEW = "not_in_review"
CODE_OWNER_WRITTEN = "verdict_owner_human"

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
    "class_ceiling": (
        "класс по диффу не выше потолка проекта (auto_approve_max_class; "
        "дифф при показе не читался)"
    ),
    "ci_integrity": (
        "дифф не меняет входы CI (проверка по клону, при показе не делалась)"
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


async def _ci_integrity_refusal(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> tuple[str, str] | None:
    """``(код, причина)``, если сохранённая проверка целостности CI не чиста (#1688).

    Только база: непросмотренный дифф здесь не отказ (стюард просмотрит его
    сам, прежде чем применять). Причина несёт справку владельцу.
    """
    from hub.services import auto_verdict, ci_integrity

    standing = await ci_integrity.standing_of(db, task)
    if standing.gap is None or standing.scan.state == ci_integrity.UNSCANNED:
        return None
    return auto_verdict.CODE_CI_INTEGRITY, standing.reason


async def _failed_application(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> str:
    """Причина, если применение approve этой сдачи кончилось ``failed``, иначе пусто."""
    from hub import repository as repo
    from hub.services.steward_advisor import OUTCOME_FAILED

    row = await repo.get_steward_judgement(db, task_id, generation, "verdict")
    judgement = dict(row) if row is not None else {}
    if judgement.get("advisor_outcome") != OUTCOME_FAILED:
        return ""
    return str(judgement.get("advisor_outcome_detail") or "причина не записана")


async def _empty_report_route(
    db: aiosqlite.Connection, task: dict[str, Any], mode: str
) -> tuple[VerdictRoute | None, str]:
    """(запрет, условие) по отчёту без кандидатов: тот же предикат, без сети (#2082)."""
    from hub.services import steward_empty_report

    reading = await steward_empty_report.stored_reading(
        db, int(task["id"]), int(task.get("submission_generation") or 0)
    )
    if reading is None:
        return None, ""
    decision, row = reading
    if decision.code:
        refusal = steward_empty_report.refusal_of(decision, row)
        return _human(decision.code, refusal[1] if refusal else "", mode), ""
    if decision.pending:
        return None, (
            "; отчёт без кандидатов применится, только если биллинг "
            "провайдера докажет работу ревьюера (сеть, при показе не проверялся)"
        )
    return None, ""


async def _steward_route(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    project: Any,
    stance: Any,
) -> VerdictRoute:
    """Автопилот молчит, а проект просит стюарда: судит ли он и применяется ли суждение."""
    from hub import config
    from hub.services import steward_apply, steward_cap, steward_shadow

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
    failed = await _failed_application(db, int(task["id"]), generation)
    if failed:
        # Применение approve этой сдачи не удалось: поллер его не повторит,
        # вердикт остаётся человеку, и причина названа тут, а не молчанием.
        return _human(
            CODE_APPLY_FAILED, f"одобрение стюарда не применено: {failed}", mode
        )
    from hub.services.steward_act_readiness import BLOCKED, ceiling_reason

    key = "steward_approve_max_class"
    found = ceiling_reason(
        await steward_apply.read_ceiling(db, int(task["id"]), key),
        task.get("risk_class"),
        key=key,
    )
    ceiling_condition = ""
    if found is not None and found[0] == BLOCKED:
        from hub.services.steward_double_clean import may_apply_to_class

        if not await may_apply_to_class(db, int(task["id"]), task.get("risk_class")):
            return _human(found[1]["code"], found[1]["detail"], mode)
        # #1986: выше потолка, но в пределах класса двойной чистоты: судит стюард,
        # а применится ли, решат пара отчётов, CI и советник (чтением не проверить).
        ceiling_condition = (
            "; класс выше потолка проекта допустим только при двойной чистоте "
            "(steward_approve_max_class_double_clean: два чистых отчёта с пином, "
            "CI без разрыва, согласие советника; пара фиксируется при заказе)"
        )
    elif found is not None:
        ceiling_condition = (
            "; класс по диффу не выше потолка проекта (steward_approve_max_class; "
            "дифф при показе не читался)"
        )
    # #2082: пустой отчёт - запрет или условие, в каждом режиме.
    empty_route, empty_condition = await _empty_report_route(db, task, mode)
    if empty_route is not None:
        return empty_route
    handoff = await steward_cap.cap_handoff(db, int(task["id"]), generation)
    if handoff is not None:
        # #1915: одно условие с диспетчером, заказом советника и применением.
        return _human(steward_cap.CODE_DAILY_CAP, handoff.reason, mode)
    from hub.services.steward_advisor import advisor_state

    advisor = await advisor_state(db, int(task["id"]), generation)
    if advisor.skipped:
        # #2024: заказ советника пропущен по причине вне советника (например,
        # у double-clean нет замороженной пары): стюард не применит approve,
        # вердикт за человеком. Заказ вернётся, если причина снимется.
        return _human(CODE_ADVISOR_SKIPPED, advisor.reason, mode)
    return VerdictRoute(
        DECIDER_STEWARD,
        DECIDER_STEWARD,
        mode,
        CODE_STEWARD_ACT,
        f"автопилот не вынесет ({stance.reason}); судит стюард",
        "если суждение — approve, советник-критик согласен на том же пакете и "
        "привратник применения не возражает"
        + ceiling_condition
        + empty_condition
        + "; иначе "
        "вердикт остаётся человеку",
    )


async def verdict_route(
    db: aiosqlite.Connection, task_id: int, *, observe: bool = False
) -> VerdictRoute:
    """Маршрут вердикта, а к нему справка по целостности CI, какой бы ни был маршрут (#1688).

    Справка нужна владельцу и там, где вердикт и так за ним (политика human,
    автопилот выключен): ручной вердикт читает её, прежде чем одобрить.
    """
    import dataclasses

    from hub import repository as repo
    from hub.services import ci_integrity

    try:
        route = await _decide_route(db, task_id, observe=observe)
    except CeilingUnavailable as exc:
        # Потолок не прочитать (автопилот, стюард, советник читают его одним
        # читателем): маршрут не падает, вердикт за человеком с названной причиной.
        route = _human(exc.code, exc.reason)
    if route.decider == DECIDER_NONE:
        return route
    task = dict(await repo.get_task(db, task_id) or {})
    standing = await ci_integrity.standing_of(db, task)
    if (
        standing.gap is None
        or standing.scan.state == ci_integrity.UNSCANNED
        or standing.note in route.reason
    ):
        return route
    return dataclasses.replace(
        route, reason=f"{route.reason}. Справка по CI: {standing.reason}"
    )


async def _cap_handoff_ended(
    db: aiosqlite.Connection, task: dict[str, Any], owner_reason: str
) -> bool:
    """Запись о суточном потолке кончилась вместе с сутками: потолок временный."""
    from hub.services import steward_cap

    if not owner_reason.startswith(steward_cap.CODE_DAILY_CAP):
        return False
    handoff = await steward_cap.cap_handoff(
        db, int(task["id"]), int(task.get("submission_generation") or 0)
    )
    return handoff is None


async def _decide_route(
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
    # #2052: записанный владелец раньше любого прогноза. Актор, сложивший
    # решение, сказал это вслух; стойка автопилота «одобрил бы» не отменяет
    # того, что стюард уже передал сдачу человеку.
    owner, owner_reason = verdict_owner.written_owner(task)
    if owner == verdict_owner.OWNER_HUMAN and not await _cap_handoff_ended(
        db, task, owner_reason
    ):
        code, _, detail = owner_reason.partition(": ")
        if not code.replace("_", "").isalnum() or not code.islower():
            code, detail = CODE_OWNER_WRITTEN, owner_reason
        return _human(code, detail or owner_reason)
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
        integrity = await _ci_integrity_refusal(db, task)
        if integrity is not None:
            return _human(*integrity)
        return await _steward_route(db, task, project, stance)
    if stance.code == auto_verdict.CODE_NOT_DELEGATED and (
        project is not None and project_policy.gate_lock_applies(project["slug"])
    ):
        return _human(
            CODE_LOCK_743,
            "замок #743: проект default не принимает делегирование вердикта",
        )
    return _human(stance.code, stance.reason)
