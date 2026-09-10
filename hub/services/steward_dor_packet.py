"""Пакет доказательств ДРАФТА — единственный вход стюарда на DoR (#1158, F6).

Правило #1075 не меняется: стюард судит по пакету и больше ни по чему.
Меняется то, из чего пакет собран, потому что пакет вердикта (#1074) драфту
не подходит ни одним фактом. Тот собран из отчёта машинного ревью,
закреплённого sha, вершины ветки, диффа против областей и базы — у драфта
нет ни ветки, ни коммита, ни CI, ни отчёта. Отдать ему ту же сборку значило
бы пять фактов в состоянии ``absent`` и ни одного полезного, а судья,
которому нечего читать, читает текст постановки — ровно то, чего #1076
велит не делать.

Драфтовых фактов при этом хватает, и все они ПЕРЕПРОВЕРЯЕМЫ хабом:

* существуют ли критерии приёмки и во что разрешаются их локаторы
  (``ac_locator``);
* какие области заявлены (``diff_vs_areas`` — на драфте у этого источника
  есть только заявленная половина, и это сказано вслух);
* какой класс риска вычислен и по каким признакам (``risk_class``);
* чего задача ждёт и доставлено ли оно (``dependency_state``).

Ни одного нового кода источника здесь не заведено. Словарь #1022 закрыт, и
два его источника — ``ac_locator`` и ``dependency_state`` — вердикт не
использует ни разу: они драфтовые по смыслу, и их наличие в закрытом
множестве было указанием на эту сборку задолго до неё.

ФОРМА ФАКТА ТА ЖЕ, что в #1074, и по той же причине: факт несёт ``present``
(хаб посмотрел и знает ответ, в том числе неприятный) либо ``absent`` (хаб
не смог посмотреть или смотреть было не на что). Пустое значение никогда не
выдаётся за значение — на #762 непрочитанный клон прочитался как чистое
дерево, а на харнессе ``raw_count=0`` — как «находок нет» вместо «данных
нет». На драфте у этой ошибки есть своя форма, и не одна: «локатор не
разрешился» и «локатора нет» — разные факты, и судья, читающий их как один,
ошибётся в пользу одобрения. А «хаб посмотрел и теста нет» против «хаб не
смотрел» — то же различие ещё на зарубку дальше, и вот оно на драфте не
угловое: ветки у драфта нет, поэтому расчёт локаторов отвечает ``unknown``
про КАЖДЫЙ названный локатор, включая тот, что указывает на существующий
тест. Сведи их — и пакет обвинит годную постановку в несуществующем грехе.

ТЕКСТ ПОСТАНОВКИ ИДЁТ КАК ДАННЫЕ. Он написан автором задачи и уезжает в
модель, решающую судьбу этой же задачи, — то есть это последний канал, по
которому посторонний ещё может обратиться к судье. Он едет цитатами
(#1076), с автором и признаком ``injection_suspected``, и никогда не
смешивается с полями, которые читаются как факты хаба.

Чего здесь НЕТ намеренно: текста самих критериев. Пакет сообщает СОСТОЯНИЕ
критериев — существуют, локатор такой-то, — а оценка формулировок это
суждение стюарда, а не факт пакета. И решений по фактам здесь тоже нет:
чего не одобрять ни при каком классе — привратник #1159.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import deserialize_str_list
from hub.models import STEWARD_GROUND_SOURCES
from hub.services.steward_evidence import (
    ABSENT,
    NO_STORED_CLASS,
    PRESENT,
    QUOTE_TASK_STATEMENT,
    EvidenceFact,
    QuotedText,
    absent,
    dependency_fact,
    present,
    quote,
)
from hub.services.test_existence import (
    MISSING,
    NO_VALID_LOCATOR,
    RESOLVABLE,
    UNKNOWN,
    UNPARSEABLE,
)

log = logging.getLogger("hub")

# Источники, которые на драфте вообще имеют смысл. Подмножество #1022, и это
# проверяется при импорте, а не на ревью: источник, которого закрытый словарь
# назвать не может, — это основание, которое хаб перепроверить не сможет.
DRAFT_GROUND_SOURCES: tuple[str, ...] = (
    "ac_locator",
    "diff_vs_areas",
    "risk_class",
    "dependency_state",
)
_outside = [s for s in DRAFT_GROUND_SOURCES if s not in STEWARD_GROUND_SOURCES]
if _outside:  # pragma: no cover — падает при импорте, до любого теста
    raise ValueError(
        f"draft sources outside the closed set of steward grounds: {_outside}; "
        f"allowed: {', '.join(STEWARD_GROUND_SOURCES)}"
    )

# Причины отсутствия — кодами, чтобы привратник ветвился на них, а не разбирал
# прозу; человеческая формулировка едет рядом в ``detail``.
NO_ACCEPTANCE_CRITERIA = "no_acceptance_criteria"
NO_DECLARED_AREAS = "no_declared_areas"
# Бриф ревью не собрался — значит разрешимость локаторов НЕ ВЫЧИСЛЕНА. Это
# отдельный код, а не ``no_acceptance_criteria``: у второго есть утвердительный
# смысл («хаб посмотрел, критериев нет»), и подставить его вместо незнания
# значило бы обвинить постановку с критериями в их отсутствии — тот же промах
# #762, от которого пакет и защищает.
BRIEF_UNAVAILABLE = "brief_unavailable"

# Состояния локатора. Их пять, и ни одно не сводится к другому.
#
# ``resolvable``   локатор назван и разрешается в существующий тест;
# ``unresolved``   локатор назван, хаб ПОСМОТРЕЛ и теста не нашёл;
# ``unknown``      локатор назван, но посмотреть не удалось — ветки нет, файл
#                  не прочитался, раннер незнакомый; хаб не знает ответа и
#                  этого не скрывает;
# ``no_locator``   критерий обещает проверку тестом и НЕ НАЗЫВАЕТ теста,
#                  который можно разрешить: поле пусто ЛИБО в поле не локатор,
#                  а проза («см. юнит-тесты»). Это не неудача разрешения, а
#                  отсутствие того, что разрешать;
# ``not_test_bound`` критерий не обещает теста вовсе — локатора и не ждали.
#
# Разделение ``unresolved`` и ``no_locator`` и есть смысл AC-1: расчёт #506
# сводит их в один статус ``missing``, и различает их ТОЛЬКО причина —
# ``NO_VALID_LOCATOR`` против ``NOT_COLLECTED``. На драфте это разные
# основания вернуть постановку, и разбирать их по пустоте поля нельзя:
# непустой мусор в ``test_ref`` даёт ту же самую причину, что и пустое поле,
# потому что разбор не состоялся в обоих случаях. Пакет, глядевший на пустоту,
# писал такому критерию ``unresolved`` — то есть «хаб посмотрел и теста нет»
# про тест, которого автор и не называл.
#
# Разделение ``unresolved`` и ``unknown`` — тот же промах #762 на одну зарубку
# дальше, и на драфте это НЕ угловой случай, а норма: у драфта нет ветки, сбор
# не стартует, файл читать не из чего, и расчёт отвечает ``unknown`` про КАЖДЫЙ
# названный локатор — в том числе про тот, что указывает на существующий тест.
# Свалив ``unknown`` в ``unresolved``, пакет говорил бы «хаб посмотрел и теста
# нет» ровно там, где хаб не смотрел вовсе, — и стюард вернул бы годную
# постановку за несуществующий грех.
LOCATOR_RESOLVABLE = "resolvable"
LOCATOR_UNRESOLVED = "unresolved"
LOCATOR_UNKNOWN = "unknown"
LOCATOR_NO_LOCATOR = "no_locator"
LOCATOR_NOT_TEST_BOUND = "not_test_bound"

# Разбор ответа #506 — ТАБЛИЦЕЙ, покрывающей весь словарь статусов расчёта.
# ``resolvable`` — «названный тест существует». ``missing`` — «посмотрел, не
# нашёл» (уточняется причиной, см. ``_locator_state``). ``unknown`` и
# ``unparseable`` — «смотреть не удалось»: первый про ветку и раннер, второй
# про нечитаемый файл; оба говорят о ХАБЕ, а не о тесте, и здесь не
# уравниваются с обвинением.
#
# Таблица, а не цепочка ``if`` с хвостовым ``return``: у цепочки хвост ловил
# ВСЁ неназванное, и любой статус, который #506 заведёт завтра, молча приезжал
# бы в ``unresolved`` — то есть в обвинение. Полнота таблицы по словарю #506
# проверяется тестом (``test_every_506_status_is_decomposed_by_name``), а не
# добросовестностью того, кто заведёт следующий статус.
_STATUS_STATES: dict[str, str] = {
    RESOLVABLE: LOCATOR_RESOLVABLE,
    MISSING: LOCATOR_UNRESOLVED,
    UNKNOWN: LOCATOR_UNKNOWN,
    UNPARSEABLE: LOCATOR_UNKNOWN,
}


@dataclass(frozen=True)
class DraftEvidencePacket:
    """Всё, из чего стюард может судить ОДНУ ревизию постановки.

    ``facts`` содержит каждый источник из ``DRAFT_GROUND_SOURCES`` — источник
    не пропадает из пакета, он только бывает ``absent``. «Пакет не упоминает
    зависимости» и «пакет говорит, что зависимостей нет» — разные
    утверждения, и знает хаб только второе.

    ``readiness`` — счёт готовности и флаг DoR, как их вычислил хаб. Это
    контекст, а не основание: у счёта готовности нет кода в закрытом словаре
    #1022, а заводить новый запрещено. Поэтому он лежит рядом с фактами, а не
    среди них, — сослаться на него в вердикте нельзя, прочитать можно.
    """

    task_id: int
    statement_generation: int
    facts: dict[str, EvidenceFact]
    readiness: dict[str, Any] = field(default_factory=dict)
    # Чужие слова, отдельно от собственных фактов хаба (#1076).
    quotes: tuple[QuotedText, ...] = ()

    def fact(self, source: str) -> EvidenceFact:
        if source not in DRAFT_GROUND_SOURCES:
            raise ValueError(
                f"source {source!r} is not part of the draft packet; "
                f"draft sources: {', '.join(DRAFT_GROUND_SOURCES)}"
            )
        return self.facts[source]

    def absent_sources(self) -> list[str]:
        return [s for s in DRAFT_GROUND_SOURCES if self.facts[s].is_absent]

    @property
    def injection_suspected(self) -> bool:
        """Пыталась ли какая-нибудь цитата отдать судье приказ (#1076)?"""
        return any(q.suspected for q in self.quotes)

    @property
    def injection_signals(self) -> list[str]:
        out: list[str] = []
        for q in self.quotes:
            out.extend(sig for sig in q.signals if sig not in out)
        return out


def _locator_state(resolution: dict[str, Any]) -> str:
    """Во что разрешился локатор — по записи УЖЕ выполненного расчёта.

    Второго расчёта разрешимости здесь не заводится: статус приходит из
    ``locator_resolution`` брифа (#505/#506), и эта функция его только
    раскладывает. Единственное, что она смотрит сама, — назван ли локатор
    вообще; это чтение заявленного поля, а не проверка того, существует ли
    названный тест.

    Каждый статус расчёта разложен ПОИМЁННО, а не «всё кроме resolvable».
    Отрицанием одного имени неизвестность попадала бы в ту же корзину, что и
    ненайденный тест, а любой новый статус #506 молча приезжал бы в
    ``unresolved`` — то есть обвинением там, где ответа нет. Поэтому
    неизвестный статус уходит в ``unknown``: незнание хаба про свой же
    словарь — это незнание, а не улика против автора.

    ``missing`` уточняется ПРИЧИНОЙ, а не пустотой поля. Расчёт отвечает
    ``missing`` и на «названного теста нет среди собранных», и на «в
    ``test_ref`` нет локатора, который вообще можно разобрать», а второе
    приходит одинаково и от пустого поля, и от прозы вроде «см. юнит-тесты».
    Разбирая по пустоте, пакет писал прозе ``unresolved`` — обвинение в
    несуществующем тесте вместо просьбы назвать тест. Причина #506 читается
    здесь как есть; второго разбора ``test_ref`` не заводится.
    """
    status = (resolution.get("status") or "").strip()
    reason = (resolution.get("reason") or "").strip()
    if status == MISSING and reason == NO_VALID_LOCATOR:
        return LOCATOR_NO_LOCATOR
    return _STATUS_STATES.get(status, LOCATOR_UNKNOWN)


async def _ac_locator_from_brief(
    db: aiosqlite.Connection, task_id: int
) -> EvidenceFact:
    """Разрешимость локаторов — из брифа ревью, и ни разу не вместо него.

    Сборка брифа поднимает вид задачи из того, что лежит в колонках, и на
    нечитаемом содержимом колонки падает проверкой типов. Пакету падать
    нельзя: он собирается ровно для того, чтобы судья не остался без входа, и
    исключение отсюда ослепило бы его целиком — включая факты, которые
    собрались бы прекрасно и без брифа (области, класс риска, зависимости).

    Поэтому неудача сборки вырождается в ``absent`` с собственным кодом, а не
    в отсутствие критериев и не в пустое значение: «хаб не смог вычислить» и
    «критериев нет» — разные ответы, и второй судья читает как повод вернуть
    постановку.
    """
    from hub.services.review_brief import build_review_brief

    try:
        brief = await build_review_brief(db, task_id)
    except Exception as exc:  # noqa: BLE001 — вход судьи важнее причины отказа
        log.warning(
            "draft packet %s: review brief did not assemble (%s: %s)",
            task_id,
            type(exc).__name__,
            exc,
        )
        return absent(
            "ac_locator",
            BRIEF_UNAVAILABLE,
            f"бриф ревью не собрался ({type(exc).__name__}) — разрешимость "
            "локаторов не вычислена; это незнание хаба, а не отсутствие критериев",
        )
    if brief is None:
        return absent(
            "ac_locator",
            BRIEF_UNAVAILABLE,
            "бриф ревью не собран — разрешимость локаторов не вычислена",
        )
    return _ac_locator_fact(brief)


def _ac_locator_fact(brief: Any) -> EvidenceFact:
    """Существуют ли критерии и во что разрешаются их локаторы (#505/#506)."""
    source = "ac_locator"
    criteria = list(getattr(brief, "acceptance_criteria", []) or [])
    if not criteria:
        return absent(
            source,
            NO_ACCEPTANCE_CRITERIA,
            "у задачи нет критериев приёмки — разрешать нечего",
        )
    resolutions = {
        r.ac_id: r.model_dump()
        for r in (list(getattr(brief, "locator_resolution", []) or []))
    }
    items: list[dict[str, Any]] = []
    for ac in criteria:
        ac_id = getattr(ac, "id", "?")
        verifiable_by = getattr(getattr(ac, "verifiable_by", None), "value", "") or str(
            getattr(ac, "verifiable_by", "") or ""
        )
        resolution = resolutions.get(ac_id)
        if resolution is None:
            # Расчёт #506 пропускает критерии не с verifiable_by=test: они
            # теста не обещали, и «локатора нет» про них было бы упрёком.
            items.append(
                {
                    "ac_id": ac_id,
                    "verifiable_by": verifiable_by,
                    "locator": "",
                    "locator_state": LOCATOR_NOT_TEST_BOUND,
                    "status": "",
                    "reason": "",
                }
            )
            continue
        items.append(
            {
                "ac_id": ac_id,
                "verifiable_by": verifiable_by,
                "locator": resolution.get("locator") or "",
                "locator_state": _locator_state(resolution),
                # Статус и причина исходного расчёта едут как есть: пакет
                # раскладывает их, а не заменяет собой.
                "status": resolution.get("status") or "",
                "reason": resolution.get("reason") or "",
            }
        )
    counts = {
        state: sum(1 for i in items if i["locator_state"] == state)
        for state in (
            LOCATOR_RESOLVABLE,
            LOCATOR_UNRESOLVED,
            LOCATOR_UNKNOWN,
            LOCATOR_NO_LOCATOR,
            LOCATOR_NOT_TEST_BOUND,
        )
    }
    return present(
        source,
        f"критериев {len(items)}: разрешается {counts[LOCATOR_RESOLVABLE]}, "
        f"не разрешилось {counts[LOCATOR_UNRESOLVED]}, "
        f"посмотреть не удалось {counts[LOCATOR_UNKNOWN]}, "
        f"без локатора {counts[LOCATOR_NO_LOCATOR]}, "
        f"без обещания теста {counts[LOCATOR_NOT_TEST_BOUND]}",
        criteria=items,
        counts=counts,
    )


def _declared_areas_fact(task: dict[str, Any]) -> EvidenceFact:
    """Заявленные области — и вслух о том, что сверять их пока не с чем.

    У источника ``diff_vs_areas`` на драфте существует только заявленная
    половина: ветки нет, диффа нет, сверка невозможна. Поэтому значение
    несёт ``compared_against_diff=False`` явным полем, а не умолчанием:
    «области заявлены» не должно прочитаться как «дифф в них уложился».
    """
    source = "diff_vs_areas"
    areas = [a for a in deserialize_str_list(task.get("affected_areas")) if a.strip()]
    if not areas:
        return absent(
            source,
            NO_DECLARED_AREAS,
            "области не заявлены — предсказания радиуса изменений нет",
        )
    return present(
        source,
        f"заявлено областей: {len(areas)}; диффа на драфте нет, сверять не с чем",
        declared=list(areas),
        compared_against_diff=False,
    )


def _draft_risk_fact(task: dict[str, Any]) -> EvidenceFact:
    """Класс риска, вычисленный на постановке, и его признаки (#550/#583).

    Только сохранённая половина. У вердикта фактом является ПАРА «заявленный
    класс против пересчитанного по диффу», и половина пары там объявляется
    отсутствием (#838); на драфте второй половины не существует в принципе,
    и это сказано полем, а не умолчанием.
    """
    source = "risk_class"
    # Значение отдаётся как записано. Второй разбор в перечисление был бы
    # защитой от состояния, которое до сюда не доходит: колонку пишет сам хаб
    # из того же перечисления, а вид задачи, который собирается по дороге,
    # отвергает чужое значение раньше пакета. Ветка, в которую нельзя попасть,
    # не проверяется мутацией и поэтому гниёт молча.
    stored = (task.get("risk_class") or "").strip()
    if not stored:
        return absent(source, NO_STORED_CLASS, "класс риска постановки не вычислен")
    reasons = [
        r for r in deserialize_str_list(task.get("risk_class_reasons")) if r.strip()
    ]
    return present(
        source,
        f"класс постановки {stored}, признаков {len(reasons)}; "
        "пересчитывать по диффу нечего — диффа нет",
        stored=stored,
        reasons=list(reasons),
        recomputed_from_diff=False,
    )


def _statement_quotes(task: dict[str, Any]) -> tuple[QuotedText, ...]:
    """Текст постановки — как текст, с автором.

    Список исчерпывающий по построению, а не по добросовестности: свободного
    текста пакет больше не несёт. Текст критериев сюда не входит намеренно —
    пакет сообщает состояние критериев, а не их формулировки.
    """
    author = (task.get("assigned_agent") or task.get("source") or "").strip()
    texts = [
        (task.get("description") or "").strip(),
        (task.get("user_story") or "").strip(),
        (task.get("problem_statement") or "").strip(),
    ]
    return tuple(quote(QUOTE_TASK_STATEMENT, author, t) for t in texts if t)


def _readiness(task: dict[str, Any]) -> dict[str, Any]:
    """Счёт готовности, как его посчитал хаб. Контекст, не основание.

    Непосчитанное отличается от посчитанного и плохого — то же правило #762,
    что и у фактов, и здесь оно нужно даже сильнее: свежесозданная задача
    держит в обеих колонках NULL, ``bool(None)`` давал ``False``, и «DoR не
    считали» уезжало стюарду как «DoR посчитан и не пройден». Первое просит
    посчитать, второе — вернуть постановку автору.

    ``computed`` отвечает на это одним полем, чтобы читателю не пришлось
    выводить смысл из того, что оба значения оказались ``None``.
    """
    score = task.get("readiness_score")
    passed = task.get("dor_passed")
    return {
        "score": int(score) if score is not None else None,
        "dor_passed": bool(passed) if passed is not None else None,
        "computed": score is not None or passed is not None,
    }


async def build_draft_packet(
    db: aiosqlite.Connection, task_id: int
) -> DraftEvidencePacket | None:
    """Собрать пакет драфта для одной задачи.

    ``None`` — только когда задачи не существует; вызывающий решает, 404 это
    или отказ запускать прогон. Всё остальное вырождается в ``absent``:
    сборка доказательств не имеет права падать оттого, что чего-то нет, иначе
    судья слепнет ровно на нестандартных случаях.

    Разрешимость локаторов берётся из брифа ревью (#308/#506) — того самого
    расчёта, а не второго такого же: два расчёта разойдутся, и разойдутся
    молча. Но берётся так, чтобы неудача ЕГО сборки стоила одного факта, а не
    всего пакета: см. ``_ac_locator_from_brief``.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        return None
    task = dict(row)

    facts = {
        f.source: f
        for f in [
            await _ac_locator_from_brief(db, task_id),
            _declared_areas_fact(task),
            _draft_risk_fact(task),
            await dependency_fact(db, task_id),
        ]
    }
    return DraftEvidencePacket(
        task_id=task_id,
        statement_generation=int(task.get("statement_generation") or 0),
        facts=facts,
        readiness=_readiness(task),
        quotes=_statement_quotes(task),
    )


def draft_packet_payload(packet: DraftEvidencePacket) -> dict[str, Any]:
    """Пакет как JSON для двери. Форма повторяет пакет вердикта (#1074)."""
    return {
        "task_id": packet.task_id,
        "statement_generation": packet.statement_generation,
        "facts": {
            source: {
                "source": fact.source,
                "state": fact.state,
                "detail": fact.detail,
                "reason": fact.reason,
                "value": fact.value,
            }
            for source, fact in packet.facts.items()
        },
        "absent_sources": packet.absent_sources(),
        "readiness": dict(packet.readiness),
        "quotes": [
            {
                "source": q.source,
                "author": q.author,
                "text": q.text,
                "signals": list(q.signals),
            }
            for q in packet.quotes
        ],
        "injection_suspected": packet.injection_suspected,
        "injection_signals": packet.injection_signals,
    }


__all__ = [
    "ABSENT",
    "BRIEF_UNAVAILABLE",
    "DRAFT_GROUND_SOURCES",
    "LOCATOR_NOT_TEST_BOUND",
    "LOCATOR_NO_LOCATOR",
    "LOCATOR_RESOLVABLE",
    "LOCATOR_UNKNOWN",
    "LOCATOR_UNRESOLVED",
    "NO_ACCEPTANCE_CRITERIA",
    "NO_DECLARED_AREAS",
    "NO_STORED_CLASS",
    "PRESENT",
    "DraftEvidencePacket",
    "build_draft_packet",
    "draft_packet_payload",
]
