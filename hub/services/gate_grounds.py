"""Громкие основания гейта: одно описание на два потребителя (#1147).

Автовердикт (#745) отказывается одобрять сам при пяти условиях и каждое
называет вслух: security-находка, перерасход токен-бюджета, расхождение с
соседним отчётом той же сдачи, саморевью и монокультура моделей. Стюард,
получая право ПРИМЕНЯТЬ вердикт, обязан упираться в те же пять — иначе он
получит права, которых нет у автопилота, и обход старого гейта будет
стоить ровно одного перевода проекта на нового судью.

Поэтому условия живут здесь, а не в двух местах. Второй список рядом с
первым разъезжается, и разъезжается тот, который мягче: правило, добавленное
в автовердикт и забытое у стюарда, тихо расширяет автономию — то есть
ошибается в сторону, где ошибка дороже.

Что здесь НЕ живёт: тихие отказы автовердикта (нет отчёта, красный CI,
уехавшая вершина, дифф вне областей, поднявшийся класс). Они не «громкие
основания», а предусловия, и у стюарда читаются из пакета доказательств
(#1074) одним кодом precondition_failed. Разница не косметическая: громкое
основание — это положительный факт об отчёте, который был прочитан, а
предусловие — вопрос о том, можно ли вообще что-то применять.

Отдельно от обоих — ПЕРЕЧЕНЬ РАЗДЕЛОВ ОТЧЁТА, за которые кто-то обязан
отчитаться, прежде чем approve пройдёт без человека (#1170). У автовердикта
это была одна строка ``if confirmed or unresolved or incomplete``, и она не
переехала сюда вместе с пятёркой: раздел с находками — не громкое основание
(отчёт с находками сам по себе выводить к человеку не обязан), а требование
отчитаться. Перечень переехал, потому что разъезжается он так же, как
разъехалась бы пятёрка, и разъехался УЖЕ: стюард закрывал confirmed
закрытиями (#1148), incomplete — предусловием, а unresolved не смотрел
вовсе, хотя по замеру #163-#167 именно там лежали все шесть настоящих
дефектов. Список теперь один, и полноту его покрытия у стюарда проверяет
тест перечислением, а не внимательность читателя.

Коды берутся из закрытого словаря STEWARD_ESCALATE_REASONS (#1022) и здесь
не изобретаются. Текст детали — тот же, что автовердикт писал в фид до этой
задачи, дословно: сообщение, которое владелец уже научился узнавать, не
должно меняться из-за переезда условия в другой файл.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import config
from hub.db import fetchall

# Порядок здесь — тот же, в котором их проверял автовердикт, и он значим:
# первым срабатывает то, что дешевле всего проверить и дороже всего
# пропустить. Перечень публичный, потому что полноту проверяет тест, а не
# внимательность читателя (#1107, #1120 — тем же приёмом).
LOUD_GROUND_CODES: tuple[str, ...] = (
    "report_security_finding",
    "report_token_budget",
    "report_sibling_mismatch",
    "self_authored",
    "same_family_as_reviewer",
)


#: Всё, из-за чего approve не проходит сам собой (#1170). Не «громкие
#: основания»: автовердикт отказывается по ним МОЛЧА, потому что отчёт с
#: находками — штатный исход ревью, а не происшествие. Перечень здесь, чтобы
#: у стюарда была одна проверяемая опись того, за что нужно отчитаться.
UNATTENDED_BLOCKERS: tuple[str, ...] = ("confirmed", "unresolved", "incomplete")

#: Из них — те, что суть СПИСКИ находок, и потому закрываются поимённо, а не
#: целиком. ``incomplete`` сюда не входит намеренно: это свойство прогона, у
#: него нет находок, которые можно было бы разобрать по одной, и у стюарда он
#: закрыт предусловием пакета.
ACCOUNTABLE_SECTIONS: tuple[str, ...] = ("confirmed", "unresolved")


def unattended_blockers(
    confirmed: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    incomplete: bool,
) -> tuple[str, ...]:
    """Какие разделы отчёта требуют отчёта — ВСЕ, а не первый попавшийся.

    Возвращает имена, а не булево: вызывающему нужно не только «нельзя», но и
    за что именно. Автовердикт отказывает при любом непустом ответе, стюард
    требует по каждому названной и проверяемой хабом судьбы.
    """
    named = {
        "confirmed": bool(confirmed),
        "unresolved": bool(unresolved),
        "incomplete": bool(incomplete),
    }
    # Порядок — из описи, а не из порядка проверок: две функции, называющие
    # одни и те же разделы в разном порядке, читаются как разные ответы.
    return tuple(name for name in UNATTENDED_BLOCKERS if named[name])


def _finding_blob(finding: dict[str, Any]) -> str:
    return " ".join(str(finding.get(k, "")) for k in ("category", "title", "severity"))


def mentions_security(findings: list[dict[str, Any]]) -> bool:
    """Есть ли среди находок security — в ЛЮБОМ статусе.

    Включая отклонённые и нерассуженные: сам факт подозрения выводит
    вердикт к человеку. Опровергнутая security-находка — это находка, по
    которой кто-то не согласился, а не отсутствие находки.
    """
    return any(
        "security" in _finding_blob(f).lower() or "безопасн" in _finding_blob(f).lower()
        for f in findings
    )


def security_ground(
    confirmed: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
) -> str | None:
    if not mentions_security(confirmed + rejected + unresolved):
        return None
    return (
        "security-находка в machine-review (в любом статусе — сам факт "
        "подозрения выводит вердикт к человеку)"
    )


def token_budget_ground(tokens_spent: int | None, budget: int) -> str | None:
    """Перерасход означает, что раунд не сошёлся штатно.

    Ноль или None в бюджете — проверка выключена, а не пройдена: сравнивать
    не с чем, и молчать здесь честнее, чем объявлять чистоту.
    """
    if not budget:
        return None
    if (tokens_spent or 0) <= budget:
        return None
    return (
        f"перерасход токен-бюджета ревью: {tokens_spent} > {budget} — "
        "раунд не сошёлся штатно"
    )


async def sibling_mismatch_ground(
    db: aiosqlite.Connection, task_id: int, generation: int, review_id: int
) -> str | None:
    """Соседний отчёт той же сдачи нашёл то, чего не нашёл текущий.

    Два ревьюера на одну сдачу расходятся не потому, что один ошибся, а
    потому, что смотрели по-разному. Расхождение — это данные, и решает их
    человек.
    """
    from hub.services.auto_verdict import _finding_dicts

    siblings = await fetchall(
        db,
        "SELECT id, findings_confirmed FROM machine_reviews "
        "WHERE task_id=? AND submission_generation=? AND id != ?",
        (task_id, generation, review_id),
    )
    for sibling in siblings:
        if _finding_dicts(sibling["findings_confirmed"]):
            return (
                f"расхождение ревьюеров: отчёт #{sibling['id']} этой же сдачи "
                "нёс confirmed-находки, текущий — нет"
            )
    return None


def self_review_ground(self_reviewed: bool, solo_allowed: bool) -> str | None:
    """Отчёт подан тем же принципалом, который делал работу (#728).

    Разнородность моделей этого не ловит: объявить другую модель дешевле,
    чем быть другим принципалом. Solo-режим не делает саморевью независимым
    — он делает его разрешённым, и потому снимает основание, а не отменяет
    факт.
    """
    if not self_reviewed or solo_allowed:
        return None
    return (
        "саморевью: machine-review подан тем же принципалом, который "
        "выполнял задачу — разнородность моделей этого не ловит, "
        "независимость проверяется по личности, а не по объявленной модели"
    )


def monoculture_ground(implementer_model: str, reviewer_model: str) -> str | None:
    """Код и ревью одного семейства — коррелированные слепые пятна (#758).

    Возвращает None и при ОТСУТСТВИИ любой из деклараций: отсутствие данных
    не есть разнородность, но и не есть монокультура. Это тихий отказ, а не
    громкое основание, и он остаётся у вызывающего — здесь бы он превратился
    в обвинение там, где просто нечего сравнивать (#762).
    """
    from hub.services.model_family import same_family

    diversity = same_family(implementer_model, reviewer_model)
    if diversity is not True:
        return None
    return (
        f"монокультура ревью: код ({implementer_model}) и ревью "
        f"({reviewer_model}) — одно семейство моделей; коррелированные "
        "слепые пятна проходят оба фильтра синхронно"
    )


# ---------------------------------------------------------------------------
# Детерминированный слой решения — над ПАКЕТОМ, а не над базой (#1167)
# ---------------------------------------------------------------------------
#
# Лестница автовердикта была написана внутри ``maybe_auto_verdict``: она
# ходит в базу за задачей, за проектом, за отчётом, за прогоном CI, за
# вершиной ветки и за диффом — и решает по дороге. Пока единственный
# потребитель живой, это дёшево. Но проверить правку политики можно было
# только выкатив её: чтобы прогнать лестницу по сдаче, случившейся в апреле,
# пришлось бы вернуть базу в состояние апреля.
#
# Поэтому решение отделено от добычи. ``decide`` не знает ни про базу, ни
# про сеть: ему дают собранный пакет доказательств (#1074) и параметры
# политики, он возвращает вердикт. Живой автовердикт продолжает добывать
# сам — эта функция не заменяет его, а описывает ту же лестницу в форме,
# которую можно запустить над историей.
#
# ПОРЯДОК ПРОВЕРОК ЗДЕСЬ — ТОТ ЖЕ, что в maybe_auto_verdict, и это не
# стилистика: лестница, чьи ступени переставлены, отвечает то же самое лишь
# на сдачах, где сработала ровно одна ступень. Реплей, называющий другое
# основание, чем назвал бы живой гейт, меряет не ту политику.
#
# Словарь исходов — стюардовский (``STEWARD_VERDICTS``), а не «одобрил /
# промолчал»: тихий отказ автовердикта и эскалация стюарда — это одно и то
# же событие, «вердикт остаётся человеку», и таблица 2x2 считает именно его.

VERDICT_APPROVE = "approve"
VERDICT_CHANGES = "changes_requested"
VERDICT_ESCALATE = "escalate"


@dataclass(frozen=True)
class GatePolicy:
    """Настраиваемая часть лестницы — ровно то, что реплей и меняет.

    Значения по умолчанию описывают СЕГОДНЯШНЮЮ политику, чтобы прогон без
    аргументов отвечал то, что отвечает прод. Кандидатная политика — это
    другой экземпляр этого же класса, и разница между двумя отчётами
    объясняется разницей между двумя наборами полей, а не правкой кода.
    """

    name: str = "current"
    #: Из конфигурации, а не ноль. Ноль у ``token_budget_ground`` означает
    #: «проверка выключена», и умолчание-ноль сделало бы стенд мягче живого
    #: гейта ровно там, где стенд обязан его повторять: живые
    #: ``maybe_auto_verdict`` и ``steward_apply`` передают
    #: ``config.REVIEW_TOKEN_BUDGET``. Отчёт, где сдача с перерасходом
    #: проходит, а прод её эскалирует, хуже отсутствия отчёта — на него
    #: сошлются.
    #: Фабрика, а не константа: значение читается при СОЗДАНИИ политики,
    #: поэтому правка настройки видна стенду без правки кода.
    token_budget: int = field(default_factory=lambda: config.REVIEW_TOKEN_BUDGET)
    #: Отчёт без единого кандидата — это «нет данных», а не «нет находок»
    #: (harness v7, #745). Выключается только для сравнения политик.
    require_raw_count: bool = True
    #: REVIEW_SELF_APPROVE=allow: саморевью разрешено, а не независимо.
    solo_allowed: bool = False


@dataclass(frozen=True)
class PolicyInputs:
    """Факты сдачи, которых нет в пакете, но которые читает лестница.

    Три штуки, и все три — свойства СДАЧИ, а не кода: две объявленные
    модели и ответ соседнего отчёта. Модели в пакет не попадают потому,
    что пакет описывает предмет суждения, а не тех, кто его готовил;
    расхождение соседей требует запроса к базе, и на историческом корпусе
    оно считается один раз на выгрузке, а не на каждом прогоне политики.
    """

    implementer_model: str = ""
    reviewer_model: str = ""
    sibling_mismatch: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    """Что политика сказала бы и на каком основании."""

    verdict: str
    reason: str = ""
    detail: str = ""
    #: Источники пакета, которые лестница успела прочитать до ответа.
    #: Не «все восемь»: основание, до которого решение не дошло, оно и не
    #: читало, и приписывать его себе значит переоценивать обоснованность.
    grounds: tuple[str, ...] = ()

    @property
    def is_approve(self) -> bool:
        return self.verdict == VERDICT_APPROVE


def _escalate(reason: str, detail: str, grounds: tuple[str, ...]) -> PolicyDecision:
    return PolicyDecision(
        verdict=VERDICT_ESCALATE, reason=reason, detail=detail, grounds=grounds
    )


def _report_stage(
    report: Any, inputs: PolicyInputs, policy: GatePolicy, seen: tuple[str, ...]
) -> PolicyDecision | None:
    """Ступени, читающие ОТЧЁТ: громкая пятёрка и разделы без отчёта."""
    confirmed = list(report.value.get("confirmed") or [])
    rejected = list(report.value.get("rejected") or [])
    unresolved = list(report.value.get("unresolved") or [])

    security = security_ground(confirmed, rejected, unresolved)
    if security:
        return _escalate("report_security_finding", security, seen)
    budget = token_budget_ground(report.value.get("tokens_spent"), policy.token_budget)
    if budget:
        return _escalate("report_token_budget", budget, seen)
    if inputs.sibling_mismatch:
        return _escalate(
            "report_sibling_mismatch",
            "расхождение ревьюеров: соседний отчёт этой же сдачи нёс "
            "confirmed-находки, текущий — нет",
            seen,
        )

    blockers = unattended_blockers(
        confirmed, unresolved, bool(report.value.get("incomplete"))
    )
    if "confirmed" in blockers or "unresolved" in blockers:
        return PolicyDecision(
            verdict=VERDICT_CHANGES,
            reason="unclosed_finding",
            detail=f"разделы без отчёта: {', '.join(blockers)}",
            grounds=seen,
        )
    if blockers:
        return _escalate("report_incomplete", "прогон харнесса не завершён", seen)
    if policy.require_raw_count and (report.value.get("raw_count") or 0) < 1:
        return _escalate(
            "precondition_failed",
            "отчёт не выдвинул ни одного кандидата: это отсутствие данных, "
            "а не отсутствие находок",
            seen,
        )
    return None


def _fact_stage(
    fact: Any, bad: bool, reason: str, detail: str, seen: tuple[str, ...]
) -> PolicyDecision | None:
    """Один факт-предусловие: не прочитан — эскалация, плох — эскалация.

    Одна функция на четыре предусловия, потому что правило у них одно:
    ``absent`` и «прочитан, и ответ плохой» ведут к человеку одинаково, но
    НАЗЫВАЮТСЯ по-разному. Четыре копии этого правила разъехались бы там же,
    где разъезжаются все копии, — на пятом предусловии.
    """
    if fact.is_absent:
        return _escalate("precondition_failed", fact.detail, seen)
    if bad:
        return _escalate(reason, fact.detail or detail, seen)
    return None


def decide(
    packet: Any,
    inputs: PolicyInputs | None = None,
    policy: GatePolicy | None = None,
) -> PolicyDecision:
    """Что детерминированная политика сказала бы об этом пакете.

    Ни одного обращения к базе и ни одного к провайдеру — по построению, а
    не по обещанию: у функции нет ни соединения, ни клиента, и добавить их
    можно только изменив сигнатуру.
    """
    inputs = inputs or PolicyInputs()
    policy = policy or GatePolicy()
    seen: list[str] = []

    def read(source: str) -> Any:
        if source not in seen:
            seen.append(source)
        return packet.fact(source)

    report = read("machine_review_report")
    if report.is_absent:
        return _escalate(
            "no_current_report",
            report.detail or "отчёта машинного ревью этой генерации нет",
            tuple(seen),
        )
    decision = _report_stage(report, inputs, policy, tuple(seen))
    if decision:
        return decision

    # Предусловия — в порядке автовердикта: CI, вершина, дифф, класс.
    ci = read("ci_pinned_sha")
    decision = _fact_stage(
        ci,
        not ci.value.get("passed"),
        "precondition_failed",
        "CI не зелёный",
        tuple(seen),
    )
    if decision:
        return decision
    tip = read("branch_tip")
    decision = _fact_stage(
        tip,
        bool(tip.value.get("moved")),
        "precondition_failed",
        "вершина ветки ушла от закреплённой сдачи",
        tuple(seen),
    )
    if decision:
        return decision
    surface = read("diff_vs_areas")
    decision = _fact_stage(
        surface,
        not surface.value.get("within_declared"),
        "ladder_surface",
        "дифф вышел за заявленные области",
        tuple(seen),
    )
    if decision:
        return decision
    risk = read("risk_class")
    decision = _fact_stage(
        risk,
        bool(risk.value.get("raised")),
        "risk_class_raised",
        "класс сдачи выше заявленного",
        tuple(seen),
    )
    if decision:
        return decision

    # --- Независимость и разнородность -----------------------------------
    self_ground = self_review_ground(
        bool(report.value.get("self_reviewed")), policy.solo_allowed
    )
    if self_ground:
        return _escalate("self_authored", self_ground, tuple(seen))
    mono = monoculture_ground(inputs.implementer_model, inputs.reviewer_model)
    if mono:
        return _escalate("same_family_as_reviewer", mono, tuple(seen))
    if not inputs.implementer_model or not inputs.reviewer_model:
        # Отсутствие декларации — не разнородность (#758/#762). Тихий отказ
        # у автовердикта, эскалация здесь: слово разное, событие одно.
        return _escalate(
            "precondition_failed",
            "одна из деклараций моделей отсутствует — сравнивать нечего",
            tuple(seen),
        )

    return PolicyDecision(verdict=VERDICT_APPROVE, grounds=tuple(seen))
