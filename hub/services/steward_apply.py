"""Привратник применения: что стоит между суждением и изменением исхода (#1147).

До F4 стюард только советует, и цена ошибки — лишняя строка в карточке.
После — он меняет исход, и цена ошибки становится дефектом, доставленным в
develop без человека. Этот модуль и есть та разница: он не применяет
ничего, он отвечает на один вопрос — есть ли право применять.

Отвечает ПЕРЕЧИСЛЕНИЕМ, а не примером. Три группы отказов, и у каждой своя
природа:

предусловия
    факты пакета доказательств (#1074): CI на закреплённом sha, вершина
    ветки, дифф против заявленных областей, класс риска, отчёт этой
    генерации. Все под одним кодом ``precondition_failed`` — вопрос у них
    общий: описывает ли пакет тот код, который судят;
громкие основания
    те же пять, при которых отказывается сам автовердикт (#745), каждое со
    своим кодом. Живут в hub/services/gate_grounds.py, чтобы список был
    один: стюард не получает прав, которых нет у автопилота;
ladder
    поверхности, меняющие правила самих гейтов — по ФАКТИЧЕСКОМУ диффу.
    Заявленная область здесь не годится: задача, объявившая «hub/services/»,
    проходит проверку по декларации и меняет hub/auth.py.

ПАКЕТ — ЕДИНСТВЕННЫЙ ВХОД, и это не аккуратность, а правило #1075. Все
факты читаются из пакета, который хаб собрал сам; ни одного пересчёта
рядом. Второй способ узнать тот же факт означает второй ответ на один
вопрос, и расходиться они начнут молча.

Новых кодов эскалации не заводится: словарь #1022 закрыт, и всё, что здесь
нужно, в нём уже есть. Исключение одно и не про суждение судьи: отказ
применения без согласия советника (#1601, ``advisor_not_concurred``) — код
привратника, а не причина эскалации судьи.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import config
from hub.db import fetchall
from hub.services import gate_grounds as grounds
from hub.services.refusal_reason import (
    ACT_REVOKED,
    CEILING_CHECK_FAILED,
    CEILING_FACT_UNBOUND,
    REASON_TEXTS,
    CodedRefusal,
)
from hub.models import RiskClass
from hub.services.auto_approve import ladder_hits
from hub.services.project_policy import STEWARD_CEILING_UNREADABLE
from hub.services.steward_evidence import EvidencePacket, build_evidence_packet

log = logging.getLogger(__name__)

REFUSED_PRECONDITION = "precondition_failed"
REFUSED_LADDER = "ladder_surface"
REFUSED_UNCLOSED = "unclosed_finding"
# Один актор на обоих гейтах: тот, кто одобрил AC, судит их выполнение
# (#1162). Код из того же закрытого словаря #1022, где он и заводился;
# им же названо саморевью в громких основаниях — написание одно.
REFUSED_SELF_AUTHORED = "self_authored"
REFUSED_NOT_DELEGATED = "policy_not_delegated"
# #1855: класс сдачи выше потолка проекта (или не известен при заданном
# потолке). Код из закрытого словаря #1022 (STEWARD_ESCALATE_REASONS).
REFUSED_CLASS_CEILING = "class_above_policy_ceiling"

# Типы закрытия из закрытого словаря #1022, и рядом — ЧЕМ каждый
# подтверждается. Соответствие публичное, потому что его полноту проверяет
# тест: тип, добавленный в словарь и забытый здесь, тихо стал бы закрытием
# без доказательства — то есть словом вместо факта.
# Не тип закрытия, а метка «стюард сказал про эту находку два разных
# слова». Вне словаря намеренно: она обязана провалить проверку типа и
# попасть в отказ вместе с тем, что именно не так.
_CONTRADICTORY = "противоречивое заявление"

CLOSURE_EVIDENCE: dict[str, str] = {
    "fixed": "коммит после отчёта тронул строки находки",
    "human_disposition": "человек вынес диспозицию по этой находке",
    "out_of_scope_linked": "находка вынесена в существующую связанную задачу",
    "author_outcome": "автор назвал судьбу неразрешённой находки на пересдаче",
}

# К каким разделам отчёта тип закрытия применим (#1170). Опись публичная по
# той же причине, что и словарь выше, и нужна отдельно от него: тип, годный
# везде, дал бы стюарду закрывать ПОДТВЕРЖДЁННУЮ находку тем, что автор
# что-то про неё сказал, — а про подтверждённую находку слово автора уже
# разобрано в #1148 и закрытием не является. Поведение confirmed этой
# задачей не меняется, и вот строка, которая это удерживает.
CLOSURE_SECTIONS: dict[str, tuple[str, ...]] = {
    "fixed": grounds.ACCOUNTABLE_SECTIONS,
    "human_disposition": grounds.ACCOUNTABLE_SECTIONS,
    "out_of_scope_linked": grounds.ACCOUNTABLE_SECTIONS,
    "author_outcome": ("unresolved",),
}

# Как называется находка каждого раздела в отказе. Раздел в тексте не
# украшение: «не закрыта ничем» про подтверждённую находку и про ту, которую
# никто не рассудил, — разные новости для того, кто будет разбирать.
_SECTION_NOUN: dict[str, str] = {
    "confirmed": "находка",
    "unresolved": "неразрешённая находка",
}

# Факты пакета, которые обязаны быть и обязаны быть чистыми, чтобы approve
# можно было применить. Перечень публичный: полноту проверяет тест, а не
# внимательность читателя — тот же приём, которым закреплён пин генерации
# (#1120) и недостижимость act мимо порогов (#1107).
#
# ac_locator, red_base и dependency_state в перечень НЕ входят намеренно:
# они описывают постановку и окружение, а не сдаваемый код, и человеческий
# гейт их сегодня тоже не считает препятствием для approve. Отказывать по
# ним значило бы ужесточить правило, не объявив этого.
PRECONDITION_FACTS: tuple[str, ...] = (
    "machine_review_report",
    "ci_pinned_sha",
    "branch_tip",
    "diff_vs_areas",
    "risk_class",
)


def _fact_refusal(source: str, fact: Any) -> str | None:
    """Почему этот факт не пускает approve, или None, если пускает.

    Отсутствие факта — тоже отказ, и это главное правило пакета (#762):
    «хаб не смог посмотреть» и «хаб посмотрел, там чисто» — разные
    состояния, и подставлять второе вместо первого значит одобрять по
    незнанию.
    """
    if fact is None:
        return f"{source}: факта нет в пакете вовсе"
    if fact.state != "present":
        return f"{source}: {fact.detail}"
    value = fact.value or {}
    if source == "machine_review_report":
        # Незавершённый харнесс — не чистый отчёт. Находки как таковые
        # проверяет соседняя задача (#1148): здесь только полнота прогона.
        if value.get("incomplete"):
            return "machine_review_report: отчёт неполон (incomplete)"
        return None
    if source == "ci_pinned_sha":
        return None if value.get("passed") else f"ci_pinned_sha: {fact.detail}"
    if source == "branch_tip":
        return "branch_tip: вершина уехала после сдачи" if value.get("moved") else None
    if source == "diff_vs_areas":
        if value.get("within_declared"):
            return None
        return f"diff_vs_areas: {fact.detail}"
    if source == "risk_class":
        return (
            "risk_class: дифф поднял класс выше заявленного"
            if value.get("raised")
            else None
        )
    # Незнакомый источник — отказ, а не пропуск: перечень выше и этот
    # разбор обязаны совпадать, и расхождение должно стоить отказа, а не
    # тишины.
    return f"{source}: источник не разобран привратником"


def precondition_refusals(packet: EvidencePacket) -> list[tuple[str, str]]:
    """Отказы по фактам пакета — все, а не первый попавшийся.

    Перечисляются полностью, потому что человеку, читающему фид, нужна
    причина, а не первая из причин: остановиться на одной значит заставить
    его чинить по одной и возвращаться.
    """
    out: list[tuple[str, str]] = []
    for source in PRECONDITION_FACTS:
        refusal = _fact_refusal(source, packet.facts.get(source))
        if refusal:
            out.append((REFUSED_PRECONDITION, refusal))
    return out


def read_precondition_refusals(packet: EvidencePacket) -> list[tuple[str, str]]:
    """``(источник, отказ)`` только по ПРОЧИТАННЫМ фактам пакета.

    Те же правила, что у ``precondition_refusals`` (``_fact_refusal``), но факт,
    которого нет или который не прочитан (state != present), отказом не
    считается: читателю, которому нужно знать «отказал бы по увиденному», текст
    отказа разбирать не приходится.
    """
    out: list[tuple[str, str]] = []
    for source in PRECONDITION_FACTS:
        fact = packet.facts.get(source)
        if fact is None or fact.state != "present":
            continue
        refusal = _fact_refusal(source, fact)
        if refusal:
            out.append((source, refusal))
    return out


def _class_of(raw: Any) -> RiskClass | None:
    try:
        return RiskClass(str(raw))
    except ValueError:
        return None


def class_ceiling_refusal(
    ceiling: str | None, fact: Any, key: str = "steward_approve_max_class"
) -> tuple[str, str] | None:
    """Потолок класса риска для approve стюарда против факта ``risk_class`` (#1855).

    Таблица решений при заданном потолке:

    * ``raised`` (дифф поднял класс) — прежний отказ предусловия говорит сам,
      здесь молчим: потолок его не отменяет и не дублирует;
    * stored и recomputed известны — потолок против max(stored, recomputed) по
      порядку ``RiskClass``;
    * факта нет, класс повреждён или recomputed пуст — отказ с названием
      недостающего факта: неизвестный класс не равен R0;
    * потолка нет — прежнее поведение (None).

    Нечитаемый потолок (мимо модели в базе) отказывает на любой класс.
    """
    if ceiling is None:
        return None
    named = f"{key}={ceiling}"
    if ceiling == STEWARD_CEILING_UNREADABLE:
        return (
            REFUSED_CLASS_CEILING,
            f"{named}: значение потолка нечитаемо, approve не применяется",
        )
    if fact is None or fact.state != "present":
        why = "факта нет в пакете" if fact is None else fact.detail
        return (
            REFUSED_CLASS_CEILING,
            f"{named}: недостающий факт risk_class — {why}",
        )
    value = fact.value or {}
    if value.get("raised"):
        return None
    stored = _class_of(value.get("stored"))
    recomputed = _class_of(value.get("recomputed"))
    if stored is None or recomputed is None:
        missing = " и ".join(
            name
            for name, known in (("stored", stored), ("recomputed", recomputed))
            if known is None
        )
        return (
            REFUSED_CLASS_CEILING,
            f"{named}: недостающий факт risk_class.{missing} — "
            "неизвестный класс не равен R0",
        )
    order = list(RiskClass)
    limit = _class_of(ceiling.upper())
    worst = max(stored, recomputed, key=order.index)
    if limit is not None and order.index(worst) > order.index(limit):
        return (
            REFUSED_CLASS_CEILING,
            f"{named}: класс задачи {stored.value}, по диффу {recomputed.value}"
            f" — выше потолка {limit.value}",
        )
    return None


def exceeding_class(ceiling: str | None, fact: Any) -> RiskClass | None:
    """Класс, на котором потолок отказывает ПО СУЩЕСТВУ, или None (#1986).

    Только «класс известен и выше потолка»: нечитаемый потолок, отсутствующий
    или повреждённый класс и ``raised`` - это не превышение, а недостача факта,
    и исключение «двойной чистоты» на них не распространяется.
    """
    if ceiling is None or ceiling == STEWARD_CEILING_UNREADABLE:
        return None
    if fact is None or fact.state != "present" or (fact.value or {}).get("raised"):
        return None
    value = fact.value or {}
    stored = _class_of(value.get("stored"))
    recomputed = _class_of(value.get("recomputed"))
    limit = _class_of(ceiling.upper())
    if stored is None or recomputed is None or limit is None:
        return None
    order = list(RiskClass)
    worst = max(stored, recomputed, key=order.index)
    return worst if order.index(worst) > order.index(limit) else None


# Факт класса ранней проверки потолка. Транзакция записи вердикта
# (lifecycle._automation_ci_gate) сверяет потолок ещё раз по свежей политике и
# свежему сохранённому классу, но пересчитанный по диффу класс в ней не
# получить (сеть), поэтому берётся факт ранней проверки — привязанный к
# (задача, поколение, коммит сдачи) и живущий ОДНО применение: контекст
# открывает ``ceiling_scope``. Нет контекста, записи или совпадения — отказ.
_EARLY_RISK: ContextVar[dict[int, tuple[int, str, Any, str]] | None] = ContextVar(
    "steward_early_risk", default=None
)


# Решение «двойной чистоты» одного применения (#1986), по задачам: ``early`` -
# аудит раннего этапа, ``applied`` - аудит, подтверждённый транзакционным
# этапом и уходящий в запись вердикта. Живёт в том же контексте и умирает с ним.
_CLEAN: ContextVar[dict[int, dict[str, Any]] | None] = ContextVar(
    "steward_double_clean", default=None
)
STEWARD_CEILING_KEY = "steward_approve_max_class"


class CeilingUnavailable(CodedRefusal):
    """Потолок не удалось прочитать или проверить: «не смогли», а не «класс выше».

    Причина без сырого текста исключения (он остаётся в ``__cause__`` и журнале
    хаба); ``reason`` годится для маршрута вердикта и карточки.
    """

    def __init__(self, exc: Exception) -> None:
        name = type(exc).__name__
        super().__init__(
            503, f"{CEILING_CHECK_FAILED}: {name}", code=CEILING_CHECK_FAILED
        )
        self.reason = f"{REASON_TEXTS[CEILING_CHECK_FAILED]} ({name})"


#: Текст отказа, когда факта ранней проверки для этой сдачи нет (``ceiling_gate``).
_UNBOUND_MARK = "ранняя проверка не относится к этой сдаче"


def unbound_fact_refusal(detail: str) -> HTTPException:
    """422 ворот записи вердикта по потолку; нет факта ранней проверки - свой код (#2054).

    Текст отказа прежний (клиенты читают его), код ``ceiling_fact_unbound`` - для
    причины применения: «класс не вычислен для этой сдачи» проходит сам, а «класс
    выше потолка» нет.
    """
    if _UNBOUND_MARK in detail:
        return CodedRefusal(422, detail, code=CEILING_FACT_UNBOUND)
    return HTTPException(422, detail)


@asynccontextmanager
async def ceiling_check_guard() -> AsyncIterator[None]:
    """Исключение проверки потолка - ``CeilingUnavailable``, а не безымянная ошибка.

    Ворота и отказы (``HTTPException``) проходят как есть.
    """
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        raise CeilingUnavailable(exc) from exc


async def read_ceiling(
    db: aiosqlite.Connection, task_id: int, key: str = "steward_approve_max_class"
) -> str | None:
    """Потолок для ВСЕХ читателей: сбой чтения - ``CeilingUnavailable`` у источника."""
    async with ceiling_check_guard():
        return await current_ceiling(db, task_id, key)


@asynccontextmanager
async def ceiling_scope() -> AsyncIterator[None]:
    """Одно применение стюарда: факт ранней проверки не переживает его."""
    from hub.services.steward_empty_report import proof_scope

    token = _EARLY_RISK.set({})
    clean_token = _CLEAN.set({})
    try:
        async with proof_scope():
            yield
    finally:
        _CLEAN.reset(clean_token)
        _EARLY_RISK.reset(token)


def _clean_slot(task_id: int) -> dict[str, Any] | None:
    known = _CLEAN.get()
    return None if known is None else known.setdefault(task_id, {})


def applied_double_clean(task_id: int) -> dict[str, Any] | None:
    """Аудит исключения, подтверждённого транзакцией этой записи вердикта, или None."""
    return ((_CLEAN.get() or {}).get(task_id) or {}).get("applied")


def double_clean_payload(task_id: int) -> dict[str, Any]:
    """Поле аудита для события вердикта: пусто, если исключения не было (#1986)."""
    audit = applied_double_clean(task_id)
    return {"double_clean": audit} if audit else {}


def double_clean_note(task_id: int) -> str:
    """Строка карточки об одобрении по двойной чистоте, или пусто (#1986)."""
    from hub.services.steward_double_clean import line

    audit = applied_double_clean(task_id)
    return f"\n{line(audit)}" if audit else ""


async def remember_risk_fact(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    fact: Any,
    key: str = "steward_approve_max_class",
) -> None:
    from hub import repository as repo

    known = _EARLY_RISK.get()
    if known is None:
        return
    task = dict(await repo.get_task(db, task_id) or {})
    known[task_id] = (generation, str(task.get("submission_sha") or ""), fact, key)


async def current_ceiling(
    db: aiosqlite.Connection, task_id: int, key: str = "steward_approve_max_class"
) -> str | None:
    """Потолок решателя (ключ ``key``) по ПЕРЕЧИТАННОЙ политике проекта (#1855)."""
    from hub import repository as repo
    from hub.services.project_policy import _ceiling_of, gate_policy_of

    project = await repo.resolve_project_for_task(db, task_id)
    return _ceiling_of(gate_policy_of(project) if project is not None else {}, key)


async def ceiling_gate(
    db: aiosqlite.Connection, task_id: int, key: str = "steward_approve_max_class"
) -> tuple[str, str] | None:
    """Потолок внутри транзакции записи вердикта (#1855).

    Политика и СОХРАНЁННЫЙ класс перечитываются тем же соединением; класс для
    потолка — max(текущий stored, recomputed ранней проверки). Факт ранней
    проверки обязан относиться к тому же поколению и коммиту сдачи: иначе или
    без него — отказ, неизвестное не равно R0.
    """
    from hub import repository as repo
    from hub.services.steward_evidence import present

    slot = _clean_slot(task_id)
    if slot is not None:
        slot.pop("applied", None)
    ceiling = await read_ceiling(db, task_id, key)
    if ceiling is None:
        return None
    task = dict(await repo.get_task(db, task_id) or {})
    record = (_EARLY_RISK.get() or {}).get(task_id)
    live = (
        int(task.get("submission_generation") or 0),
        str(task.get("submission_sha") or ""),
    )
    if record is None or (record[0], record[1], record[3]) != (*live, key):
        return (
            REFUSED_CLASS_CEILING,
            f"{key}={ceiling}: недостающий факт risk_class — "
            f"{_UNBOUND_MARK} (поколение или коммит)",
        )
    fact = record[2]
    if fact is not None and fact.state == "present":
        value = fact.value or {}
        fact = present(
            "risk_class",
            fact.detail,
            stored=str(task.get("risk_class") or "").strip() or None,
            recomputed=value.get("recomputed"),
            raised=value.get("raised"),
        )
    refusal = class_ceiling_refusal(ceiling, fact, key)
    if refusal is None or key != STEWARD_CEILING_KEY:
        return refusal
    return await _double_clean(db, task_id, live[0], ceiling, fact, refusal, late=True)


async def _double_clean(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    ceiling: str,
    fact: Any,
    refusal: tuple[str, str],
    *,
    late: bool,
    statement: str | None = None,
) -> tuple[str, str] | None:
    """Исключение «двойной чистоты» (#1986): None - потолок не возражает.

    ОДНО решение на оба этапа (``steward_double_clean.evaluate``). Ранний этап
    запоминает аудит, транзакционный обязан получить такой же: иначе прежний
    отказ дополняется перечнем невыполненных условий.
    """
    from hub.services import steward_double_clean as double_clean

    worst = exceeding_class(ceiling, fact)
    if worst is None:
        return refusal
    slot = _clean_slot(task_id)
    decision = await double_clean.evaluate(
        db,
        task_id,
        generation,
        worst,
        early=(slot or {}).get("early"),
        late=late,
        statement=statement,
    )
    if not decision.enabled:
        return refusal
    if decision.ok:
        if slot is not None:
            slot["applied" if late else "early"] = decision.audit
        return None
    return (
        refusal[0],
        refusal[1] + "; двойная чистота не выполнена — " + "; ".join(decision.unmet),
    )


async def ceiling_refusals(
    db: aiosqlite.Connection,
    task_id: int,
    packet: EvidencePacket,
    statement: str | None = None,
) -> list[tuple[str, str]]:
    """Потолок approve стюарда проекта; политика перечитывается здесь (#1855)."""
    fact = packet.facts.get("risk_class")
    await remember_risk_fact(db, task_id, packet.generation, fact)
    slot = _clean_slot(task_id)
    if slot is not None:
        slot.clear()  # решение прошлого прохода не переживает этот
    ceiling = await read_ceiling(db, task_id)
    refusal = class_ceiling_refusal(ceiling, fact)
    if refusal is not None and ceiling is not None:
        refusal = await _double_clean(
            db,
            task_id,
            packet.generation,
            ceiling,
            fact,
            refusal,
            late=False,
            statement=statement,
        )
    return [refusal] if refusal else []


async def loud_refusals(
    db: aiosqlite.Connection, task: dict[str, Any], packet: EvidencePacket
) -> list[tuple[str, str]]:
    """Те же пять оснований, при которых отказывается автовердикт.

    Читаются из общего источника (#1147), а не переписываются: правило,
    добавленное туда и забытое здесь, тихо расширило бы автономию.
    """
    report = packet.facts.get("machine_review_report")
    if report is None or report.state != "present":
        # Без отчёта громкие основания не вычислимы, и это уже сказано
        # предусловием. Молчать здесь честнее, чем объявлять чистоту.
        return []
    from hub.services.steward_shadow import reviewer_model as read_reviewer_model

    value = report.value or {}
    review_id = value.get("review_id")
    # Модель ревьюера берётся из записи хаба о запуске, а не из декларации
    # отчёта: хаб САМ запускал ревьюера, и это факт, которым он владеет,
    # тогда как модель, названная отчётом, — утверждение отчёта о себе
    # (#1008).
    reviewer = await read_reviewer_model(db, task["id"], packet.generation)

    pairs = [
        (
            "report_security_finding",
            grounds.security_ground(
                value.get("confirmed") or [],
                value.get("rejected") or [],
                value.get("unresolved") or [],
            ),
        ),
        (
            "report_token_budget",
            grounds.token_budget_ground(
                value.get("tokens_spent"), config.REVIEW_TOKEN_BUDGET
            ),
        ),
        (
            "report_sibling_mismatch",
            await grounds.sibling_mismatch_ground(
                db, task["id"], packet.generation, int(review_id or 0)
            ),
        ),
        (
            REFUSED_SELF_AUTHORED,
            grounds.self_review_ground(
                bool(value.get("self_reviewed")),
                config.REVIEW_SELF_APPROVE == "allow",
            ),
        ),
        (
            "same_family_as_reviewer",
            grounds.monoculture_ground(
                (task.get("submission_model") or "").strip(), reviewer
            ),
        ),
    ]
    return [(code, detail) for code, detail in pairs if detail]


@dataclasses.dataclass(frozen=True)
class ClosureFinding:
    """Незакрытая находка: отказ привратника и признак, ПРОЧИТАНО ли её закрытие.

    ``observed`` ложно, когда отказ вызван непрочитанным: заявленные закрытия
    не разобрать (повреждённый JSON) или хаб не смог посмотреть правки.
    """

    uid: str
    refusal: tuple[str, str]
    observed: bool


async def closure_refusals(
    db: aiosqlite.Connection, task_id: int, packet: EvidencePacket
) -> list[tuple[str, str]]:
    """Каждая подтверждённая находка закрыта, и КАЖДОЕ закрытие проверено.

    Правила и подробное обоснование - в ``closure_findings``; здесь только
    отказы, без признака чтения.
    """
    return [f.refusal for f in await closure_findings(db, task_id, packet)]


async def _claims_unreadable(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> bool:
    """Заявленные закрытия записаны, но не разбираются (не JSON или не список)."""
    import json

    from hub import repository as repo

    row = await repo.get_steward_judgement(db, task_id, generation, "verdict")
    if row is None:
        return False
    try:
        return not isinstance(json.loads(str(dict(row).get("closures"))), list)
    except (TypeError, ValueError):
        return True  # в том числе пусто и NULL: пустое не равно пустому списку


async def closure_findings(
    db: aiosqlite.Connection, task_id: int, packet: EvidencePacket
) -> list[ClosureFinding]:
    """Каждая подтверждённая находка закрыта, и КАЖДОЕ закрытие проверено.

    Грязный путь существует ради случая, когда ревью что-то нашло, а работа
    всё равно годна. Но если approve проходит, не отчитавшись по каждой
    находке, находки перестают влиять на исход — и отчёт превращается в
    текст, который никто не обязан читать.

    Факт закрытия считает ХАБ. «Исправлено» нельзя принимать на слово:
    модель, которая судит, и модель, которая пишет, ошибаются одинаково
    охотно, а правку хаб умеет увидеть сам. Три типа — три разных источника
    факта, и ни одного, который сообщал бы сам себя:

    fixed
        коммит после отчёта тронул диапазон строк находки. Считается тем
        же расчётом, что и доказательство в карточке (#1039), и ДО
        закреплённого sha, а не до вершины ветки: решение о сдаче читает
        то, что сдача закрепила (урок #1150).

        ЗНАЕМЫЙ ПРЕДЕЛ, названный кросс-модельным ревью и проверенный:
        отчёт всегда относится к ТЕКУЩЕЙ сдаче, поэтому отправная точка
        расчёта и закреплённый sha — один и тот же коммит, диапазон пуст,
        и подтвердить fixed на применении нельзя НИКОГДА. Это не случайно:
        код под судом и есть код, который читал отчёт, и «уже исправлено»
        про него не бывает — правка означает новую сдачу и новый отчёт.
        Тип остаётся в словаре #1022, потому что он осмыслен на исходах
        находок при пересдаче (#911), а не на применении. Заявленный
        здесь, он честно отказывает и говорит, что именно хаб наблюдал;
    human_disposition
        по находке уже есть запись человека (#1038). Ссылка на решение, а
        не пересказ его;
    out_of_scope_linked
        находка вынесена в СУЩЕСТВУЮЩУЮ задачу: linked_task_id в записи
        исхода, и эта задача проверяется на существование. Обещание завести
        её потом закрытием не является;
    author_outcome
        только для НЕРАЗРЕШЁННЫХ (#1170): автор назвал судьбу находки
        словарём #1085, и хаб записал её сам на своём гейте сдачи. Стюард
        здесь ничего не судит — находку не рассудили адъюдикаторы, и его
        мнение стало бы третьим; он проверяет, что её рассудил АВТОР.

    Разделов два, а не один (#1170). Раньше функция читала только
    ``findings_confirmed`` и при пустом списке выходила сразу — «закрывать
    нечего», — хотя по замеру #163-#167 (0 confirmed, 6 unresolved, все шесть
    настоящие) закрывать было ровно всё. Опись разделов общая с автовердиктом:
    grounds.ACCOUNTABLE_SECTIONS.

    Возвращает по одному отказу на каждую незакрытую находку, а не первый:
    человеку, который будет разбирать, нужен список, а не первая строка из
    него.
    """
    from hub.services.finding_evidence import OUTCOME_UNKNOWN, evidence_for_report

    report = packet.facts.get("machine_review_report")
    if report is None or report.state != "present":
        # Отчёта нет — предусловие уже отказало. Молчать здесь честнее, чем
        # объявлять, что закрывать нечего.
        return []
    value = report.value or {}
    sections = {
        name: [f for f in (value.get(name) or []) if isinstance(f, dict)]
        for name in grounds.ACCOUNTABLE_SECTIONS
    }
    # Повтор находки, отложенной до недоставленной задачи, отчёта не требует
    # (#1448): предикат тот же, что у автовердикта, и посчитан в пакете.
    repeats = value.get("deferred_repeats") or {}
    owed = [
        entry
        for entry in _findings_owing_an_account(sections)
        if not (entry[0] == "confirmed" and entry[1] in repeats)
    ]
    if not owed:
        return []
    confirmed = sections.get("confirmed") or []

    claimed = await _claimed_closures(db, task_id, packet.generation)
    claims_unread = await _claims_unreadable(db, task_id, packet.generation)

    touched: dict[str, tuple[str, str]] = {}
    if any(
        claimed.get(uid) == "fixed"
        for section, uid, _finding in owed
        if section == "confirmed"
    ):
        # Один обход ветки на все находки: вызов на каждую сделал бы
        # привратника непригодным на живом размере отчёта (#1042).
        #
        # Считаем ДО закреплённого sha, а не до вершины ветки. Вершина —
        # движущаяся цель: к моменту вопроса она может стоять не там, где
        # стояла сдача, и «исправлено» подтверждалось бы коммитом, которого
        # в сдаче нет (урок #1150).
        from hub import repository as repo

        row = await repo.get_task(db, task_id)
        pinned = ((dict(row).get("submission_sha") if row else "") or "").strip()
        evidence = await evidence_for_report(
            db, task_id, confirmed, generation=packet.generation, head=pinned
        )
        touched = {
            uid: (
                str(blob.get("outcome") or ""),
                str(blob.get("reason") or ""),
            )
            for uid, blob in evidence.items()
        }

    out: list[ClosureFinding] = []
    for section, uid, finding in owed:
        noun = _SECTION_NOUN[section]
        title = str(finding.get("title") or uid)[:80]
        kind = claimed.get(uid)
        if kind is None:
            out.append(
                ClosureFinding(
                    uid,
                    (REFUSED_UNCLOSED, f"{noun} «{title}» ({uid}) не закрыта ничем"),
                    not claims_unread,
                )
            )
            continue
        if kind not in CLOSURE_EVIDENCE:
            out.append(
                ClosureFinding(
                    uid,
                    (
                        REFUSED_UNCLOSED,
                        f"{noun} «{title}» ({uid}): закрытие типа {kind!r} вне "
                        f"словаря {sorted(CLOSURE_EVIDENCE)}",
                    ),
                    True,
                )
            )
            continue
        if section not in CLOSURE_SECTIONS[kind]:
            out.append(
                ClosureFinding(
                    uid,
                    (
                        REFUSED_UNCLOSED,
                        f"{noun} «{title}» ({uid}): закрытие типа {kind!r} применимо "
                        f"только к разделам {sorted(CLOSURE_SECTIONS[kind])}, а эта "
                        f"находка из раздела {section!r}",
                    ),
                    True,
                )
            )
            continue
        proven = await _closure_is_proven(db, task_id, packet, uid, kind, touched)
        if not proven:
            seen = touched.get(uid)
            out.append(
                ClosureFinding(
                    uid,
                    (
                        REFUSED_UNCLOSED,
                        f"{noun} «{title}» ({uid}): закрытие {kind} объявлено, но "
                        f"хаб его не подтверждает — ожидалось, что "
                        f"{CLOSURE_EVIDENCE[kind]}; "
                        + await _what_the_hub_saw(db, task_id, kind, uid, seen),
                    ),
                    not (
                        kind == "fixed"
                        and seen is not None
                        and seen[0] == OUTCOME_UNKNOWN
                    ),
                )
            )
    return out


def _findings_owing_an_account(
    sections: dict[str, list[dict[str, Any]]],
) -> list[tuple[str, str, dict[str, Any]]]:
    """(раздел, uid, находка) по каждой находке, за которую нужно отчитаться.

    Идентичность выдаёт СВОЙ расчёт на каждый раздел: у подтверждённой
    находки в материал идёт её место в коде, у неразрешённой места нет вовсе
    (#1085), и общая функция на оба списка выдала бы для одного из них
    уверенный, но чужой id.
    """
    from hub.services.finding_identity import finding_uids, unresolved_uids

    per_section = {"confirmed": finding_uids, "unresolved": unresolved_uids}
    out: list[tuple[str, str, dict[str, Any]]] = []
    for name in grounds.ACCOUNTABLE_SECTIONS:
        findings = sections.get(name) or []
        if not findings:
            continue
        uids = per_section[name](findings)
        out.extend(
            (name, uid, finding) for uid, finding in zip(uids, findings, strict=True)
        )
    return out


async def _author_named_the_outcome(
    db: aiosqlite.Connection, task_id: int, uid: str
) -> bool:
    """Рассудил ли эту неразрешённую находку её АВТОР и записал ли это (#1170).

    Факт считает хаб: строку в finding_outcomes он пишет сам, на своём гейте
    сдачи (lifecycle._step_finding_outcomes), а не принимает от стюарда.
    Заявление стюарда здесь не участвует вовсе — только его указание, где
    искать.

    ``not_judged`` закрытием НЕ является, и это не строгость, а смысл слова:
    словарь #1085 завёл его ровно затем, чтобы «посмотрел и это не дефект»
    перестало быть неотличимо от «не смотрел». Второе — не судьба находки, а
    её отсутствие, и approve по нему был бы тем же молчаливым штампом, против
    которого стоит вся проверка.
    """
    from hub import repository as repo
    from hub.models import FindingOutcome
    from hub.services.finding_outcome import KIND_UNRESOLVED

    row = await repo.finding_outcome_for_uid(
        db, task_id, uid, finding_kind=KIND_UNRESOLVED
    )
    if row is None:
        return False
    return str(dict(row).get("outcome") or "") != FindingOutcome.not_judged.value


async def _what_the_hub_saw(
    db: aiosqlite.Connection,
    task_id: int,
    kind: str,
    uid: str,
    seen: tuple[str, str] | None,
) -> str:
    """Наблюдённое состояние, а не только ожидание.

    Найдено кросс-модельным ревью: один шаблон отказа накрывал два разных
    случая. «Коммитов по этим строкам нет» и «в дифф заглянуть не удалось»
    — разные вещи для того, кто будет разбирать: первое означает, что
    правки не было, второе — что мы не смотрели. Незнание, названное
    отсутствием, и есть та ошибка, против которой стоит #762.

    У author_outcome та же развилка своими словами (#1170): «автор про эту
    находку не сказал ничего» и «автор сказал not_judged» — разные новости, и
    чинятся они по-разному.
    """
    from hub.services.finding_evidence import OUTCOME_UNKNOWN, OUTCOME_UNTOUCHED

    if kind == "author_outcome":
        from hub import repository as repo
        from hub.services.finding_outcome import KIND_UNRESOLVED

        row = await repo.finding_outcome_for_uid(
            db, task_id, uid, finding_kind=KIND_UNRESOLVED
        )
        if row is None:
            return (
                "хаб посмотрел исходы находок задачи: про эту автор не говорил "
                "ничего — судьба не названа, а не названа неубедительно"
            )
        return (
            f"хаб посмотрел исходы находок задачи: автор назвал "
            f"{str(dict(row).get('outcome') or '')!r} — это «никто не "
            "разбирался», то есть отсутствие судьбы, а не судьба"
        )
    if kind != "fixed" or seen is None:
        return "хаб опоры не нашёл"
    outcome, reason = seen
    if outcome == OUTCOME_UNTOUCHED:
        return "хаб посмотрел коммиты после отчёта: строк находки они не трогали"
    if outcome == OUTCOME_UNKNOWN:
        return (
            "хаб посмотреть НЕ СМОГ"
            + (f" ({reason})" if reason else "")
            + " — это не «правки не было», это отсутствие наблюдения"
        )
    return "хаб опоры не нашёл"


async def _claimed_closures(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> dict[str, str]:
    """Что стюард ЗАЯВИЛ про каждую находку — по uid, а не по позиции (#1007)."""
    import json

    from hub import repository as repo

    row = await repo.get_steward_judgement(db, task_id, generation, "verdict")
    if row is None:
        return {}
    try:
        entries = json.loads(dict(row).get("closures") or "[]")
    except (TypeError, ValueError):
        return {}
    if not isinstance(entries, list):
        return {}
    claimed: dict[str, str] = {}
    for e in entries:
        if not isinstance(e, dict) or not e.get("finding_uid"):
            continue
        uid = str(e.get("finding_uid"))
        kind = str(e.get("type"))
        if uid in claimed and claimed[uid] != kind:
            # Две разные записи про одну находку. Найдено кросс-модельным
            # ревью: сборка словарём молча оставляла последнюю, то есть
            # порядок записей решал, что считается закрытием. Заявление,
            # противоречащее самому себе, — не закрытие, и выбирать за
            # стюарда, какую его половину читать, хаб не должен.
            claimed[uid] = _CONTRADICTORY
            continue
        claimed.setdefault(uid, kind)
    return claimed


async def _closure_is_proven(
    db: aiosqlite.Connection,
    task_id: int,
    packet: EvidencePacket,
    uid: str,
    kind: str,
    touched: dict[str, tuple[str, str]],
) -> bool:
    """Подтверждает ли ХАБ это закрытие. Незнание — не подтверждение (#762)."""
    from hub import repository as repo
    from hub.services.finding_evidence import OUTCOME_TOUCHED

    if kind == "fixed":
        seen = touched.get(uid)
        return seen is not None and seen[0] == OUTCOME_TOUCHED
    if kind == "author_outcome":
        return await _author_named_the_outcome(db, task_id, uid)
    report = packet.facts.get("machine_review_report")
    review_id = int(((report.value if report else None) or {}).get("review_id") or 0)
    if kind == "human_disposition":
        rows = await repo.list_finding_dispositions(db, review_id)
        return any(
            str(dict(r).get("finding_uid") or "") == uid
            and str(dict(r).get("decided_by") or "").strip()
            for r in rows
        )
    if kind == "out_of_scope_linked":
        for row in await repo.list_finding_outcomes(db, review_id):
            entry = dict(row)
            if str(entry.get("finding_uid") or "") != uid:
                continue
            linked = entry.get("linked_task_id")
            if not linked:
                return False
            if int(linked) == task_id:
                # Ссылка на саму себя. Найдено кросс-модельным ревью:
                # проверка существования её пропускала, потому что задача,
                # разумеется, существует. Но «вынесено в другую задачу»
                # означает, что работа уехала ОТСЮДА; указание на эту же
                # задачу оставляет её здесь и при этом объявляет закрытой.
                return False
            # Задача обязана СУЩЕСТВОВАТЬ: ссылка на несозданное — обещание,
            # а не вынос.
            return await repo.get_task(db, int(linked)) is not None
        return False
    return False


def ladder_refusal(packet: EvidencePacket) -> tuple[str, str] | None:
    """Ladder — по фактическому диффу, а не по заявленным областям.

    Дифф лежит в пакете (``diff_vs_areas.paths``), собранный тем же обходом
    ветки, которым хаб считает выход за области. Если прочитать его не
    удалось, факт отсутствует — и отказ уже дан предусловием: нечитаемый
    дифф не есть безопасный дифф.
    """
    fact = packet.facts.get("diff_vs_areas")
    if fact is None or fact.state != "present":
        return None
    hits = ladder_hits(list((fact.value or {}).get("paths") or []))
    if not hits:
        return None
    return (
        REFUSED_LADDER,
        "дифф трогает поверхности, меняющие правила гейтов: "
        + ", ".join(hits)
        + " — такое решение остаётся человеку независимо от класса",
    )


def mode_refusal() -> tuple[str, str] | None:
    """Выключенный или неузнанный режим — отказ до всякой проверки фактов.

    Нераспознанное значение читается как off (#835): опечатка в drop-in не
    имеет права быть тем, что включает контур. Читается синхронным
    читателем, который никогда не отдаёт act (#1107) — здесь достаточно
    знать, что контур вообще открыт.
    """
    from hub.services.steward_dispatch import requested_mode

    if requested_mode() != "off":
        return None
    return (
        REFUSED_PRECONDITION,
        f"STEWARD_MODE={config.STEWARD_MODE!r} — контур закрыт, применять нечего",
    )


async def self_authored_refusal(
    db: aiosqlite.Connection, task_id: int
) -> tuple[str, str] | None:
    """Постановку одобрил стюард — вердикт по ней остаётся человеку (#1162).

    Гейт ревью держится на том, что сдающий и выносящий вердикт — разные
    акторы (#306, #433). Стюард на двух гейтах разом нарушает это по сути:
    он одобрил AC, а потом судит, выполнены ли ОНИ, — и слабый критерий,
    пропущенный на первом гейте, на втором прикроет сам себя.

    Признак — ЗАПИСЬ суждения kind='dor' с вердиктом approve, а не факт
    прогона: драфт, который стюард прочитал и ВЕРНУЛ, а человек потом
    одобрил руками, самоавторством не является — решение на том гейте
    принял человек. По той же причине читается ``verdict``, а не
    ``submitted_verdict``: одобрение, понижённое политикой до escalate
    (низкая уверенность, #1022), драфт с гейта не сняло.

    Одобрение относится к РЕДАКЦИИ постановки, а не к задаче (#1282). У
    строки ``kind='dor'`` колонка ``generation`` и есть поколение
    постановки (#1156, #1160 — слот dor на уникальном индексе), поэтому
    второе поле или сверка по времени правки были бы вторым источником той
    же правды; время к тому же не отличает переписанную постановку от
    пересохранённой. Если постановку после одобрения переписали, стюард
    судит не ту редакцию, которую одобрял, — отказа нет: человеческий апрув
    строки в steward_judgements не оставляет, и поднятое поколение
    единственный его след.

    Поколение неизвестно, пока базис постановки не снят: при пустом
    отпечатке счётчик означает «не знаю, правили ли», а не «редакция N»
    (db.py, add_tasks_statement_generation). Тогда отказ остаётся — решение
    в сторону человека. Разграничитель именно отпечаток, а не нулевое
    поколение: ``baseline_if_absent`` снимает базис, не двигая счётчик, и
    законное одобрение лежит на поколении 0.
    """
    rows = await fetchall(
        db,
        "SELECT j.generation, t.statement_generation, t.statement_fingerprint "
        "FROM steward_judgements j JOIN tasks t ON t.id = j.task_id "
        "WHERE j.task_id=? AND j.kind='dor' AND j.verdict='approve' "
        "ORDER BY j.id",
        (task_id,),
    )
    if not rows:
        return None
    if not str(rows[0]["statement_fingerprint"] or ""):
        return (
            REFUSED_SELF_AUTHORED,
            "постановку этой задачи снял с гейта DoR сам стюард (суждение "
            "kind=dor, approve), а поколение постановки неизвестно — базис "
            "не снимался, и нельзя сказать, ту ли редакцию он одобрял; "
            "решение остаётся человеку",
        )
    current = int(rows[0]["statement_generation"] or 0)
    if not any(int(r["generation"]) == current for r in rows):
        return None
    return (
        REFUSED_SELF_AUTHORED,
        "постановку этой задачи снял с гейта DoR сам стюард (суждение "
        f"kind=dor, approve, поколение постановки {current}) — судить "
        "выполнение им же одобренных AC остаётся человеку",
    )


async def policy_refusal(
    db: aiosqlite.Connection, task_id: int
) -> tuple[str, str] | None:
    """Вердикт проекта не отдан стюарду — применять нечего и некому (#1268).

    Теневое участие открывает заказ и суждение, но не решение: вердикт там
    выносит человек. Проверка стоит здесь, а не на заказе, потому что
    автономия (``steward_shadow.effective_mode``) глобальна — выданная по
    измерению одного проекта, она иначе дотянулась бы до любого, где стюарда
    только слушают, включая репозиторий самого хаба. Режим и автономия этот
    отказ не снимают: он о том, ЧЬЁ решение, а не о том, насколько судье
    доверяют.

    Вопрос «отдан ли» задаётся единственному читателю политики стюарда;
    непрочитанный проект — не отданный.
    """
    from hub import repository as repo
    from hub.services.project_policy import gate_policy_of
    from hub.services.steward_dispatch import verdict_delegated_to_steward

    project = await repo.resolve_project_for_task(db, task_id)
    if verdict_delegated_to_steward(project):
        return None
    stored = gate_policy_of(project).get("verdict") if project is not None else None
    return (
        REFUSED_NOT_DELEGATED,
        f"политика проекта: verdict={stored or 'human'} — вердикт не отдан "
        "стюарду, суждение записано в тени и не применяется",
    )


async def authority_refusal(
    db: aiosqlite.Connection, task_id: int
) -> tuple[str, str] | None:
    """Есть ли у стюарда право двигать задачу СЕЙЧАС: ``act`` выдан и вердикт ему отдан.

    Чистое чтение, для write-транзакции. Один вопрос и для одобрения
    (``write_refusal``), и для возврата на доработку (#2053): копия условия
    разошлась бы ровно там, где право отозвали.
    """
    from hub.services.steward_shadow import act_granted_now, act_unmet_now

    # #2040: глобальный допуск act - тот же вопрос, что у ранних проверок
    # (``effective_mode``), но чистым чтением: стюарда могли выключить между
    # ними и записью.
    if not await act_granted_now(db):
        return (
            ACT_REVOKED,
            f"STEWARD_MODE={config.STEWARD_MODE!r}: автономия act сейчас не "
            "выдана - применять нечего"
            + "".join(f" [{item}]" for item in await act_unmet_now(db)),
        )
    return await policy_refusal(db, task_id)


async def write_refusal(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[str, str] | None:
    """Право на автономное одобрение, как оно есть СЕЙЧАС (#2037, #2040): отказ или ``None``.

    Зовётся внутри write-транзакции, что записывает approve стюарда
    (``lifecycle._automation_ci_gate``): ранние проверки читали делегирование и
    cap-handoff до неё, а между ними и записью вердикт могли забрать себе
    (``verdict=human``), сдачу передали человеку из-за потолка или стюарда
    выключили. Читатели те же, что у ранних проверок (``authority_refusal`` и
    ``cap_refusal``), без копий условий; причина отказа - та же строка. Только
    чтение базы, сети нет.
    """
    from hub.services.steward_advisor import cap_refusal
    from hub.services.steward_empty_report import write_refusal as empty_refusal

    return (
        await authority_refusal(db, task_id)
        or await cap_refusal(db, task_id, generation)
        or await empty_refusal(db, task_id, generation)  # #2082: без сети
    )


async def text_origin_refusals(
    db: aiosqlite.Connection, task_id: int, generation: int | None
) -> list[tuple[str, str]]:
    """Отчёт, переписанный хабом из текста прогона, самостоятельного approve не даёт (#1587).

    Тот же признак, что у автопилота и маршрута вердикта: если стюард всё же
    начал суждение раньше, чем отчёт стал известен как текстовый, применять
    его approve нельзя.
    """
    from hub import repository as repo
    from hub.services.auto_verdict import text_origin_refusal

    review = await repo.get_latest_machine_review(db, task_id)
    if review is None:
        return []
    row = dict(review)
    if generation is not None and (row.get("submission_generation") or 0) != generation:
        return []
    refusal = text_origin_refusal(row)
    return [(REFUSED_PRECONDITION, refusal[1])] if refusal else []


async def integrity_refusals(
    db: aiosqlite.Connection, task_id: int
) -> list[tuple[str, str]]:
    """Дифф меняет входы CI или не прочитан: approve стюарда не применяется (#1688).

    Дифф читается здесь, ДО записи вердикта (сеть); транзакция вердикта потом
    перечитывает базу тем же ``_automation_ci_gate``.
    """
    from hub import repository as repo
    from hub.services import ci_integrity

    await ci_integrity.ensure_scan(db, task_id)
    task = dict(await repo.get_task(db, task_id) or {"id": task_id})
    standing = await ci_integrity.standing_of(db, task)
    if standing.gap is None:
        return []
    return [(REFUSED_PRECONDITION, standing.reason)]


async def apply_refusals(
    db: aiosqlite.Connection, task_id: int, generation: int | None = None
) -> list[tuple[str, str]]:
    """Все причины, по которым approve этой сдачи применять нельзя.

    Пустой список означает «привратник не возражает» — не «применяй». Что
    делать дальше, решают соседние задачи F4: закрытия находок (#1148) и
    сами переходы (#1149). Разделение намеренное: «можно ли» и «что именно
    произойдёт» ошибаются по-разному, и функция, отвечающая на оба вопроса
    сразу, не проверяется по половине.
    """
    mode = mode_refusal()
    if mode:
        return [mode]
    not_delegated = await policy_refusal(db, task_id)
    # #1986: отпечаток постановки снимается ДО сборки пакета (см. evaluate).
    from hub.services.steward_double_clean import statement_digest

    statement = await statement_digest(db, task_id)
    packet = await build_evidence_packet(db, task_id, generation)
    if packet is None:
        return [(REFUSED_PRECONDITION, f"задачи #{task_id} нет")]
    from hub import repository as repo

    row = await repo.get_task(db, task_id)
    task = dict(row) if row is not None else {"id": task_id}

    out = [not_delegated] if not_delegated else []
    out.extend(await text_origin_refusals(db, task_id, generation))
    out.extend(await integrity_refusals(db, task_id))
    out.extend(precondition_refusals(packet))
    async with ceiling_check_guard():
        out.extend(await ceiling_refusals(db, task_id, packet, statement))
    # #2082: после потолка - он решает, действует ли двойная чистота.
    from hub.services.steward_empty_report import early_refusal

    empty = await early_refusal(db, task_id, packet.generation)
    if empty:
        out.append(empty)
    self_authored = await self_authored_refusal(db, task_id)
    if self_authored:
        out.append(self_authored)
    out.extend(await loud_refusals(db, task, packet))
    out.extend(await closure_refusals(db, task_id, packet))
    ladder = ladder_refusal(packet)
    if ladder:
        out.append(ladder)
    # #1601: approve судьи применяется только при согласии советника на ТОМ
    # ЖЕ пакете. Спрашивается готовым пакетом — второй сборки рядом нет.
    from hub.services.steward_advisor import advisor_refusal

    advisor = await advisor_refusal(db, task_id, packet.generation, packet)
    if advisor:
        out.append(advisor)
    return out
