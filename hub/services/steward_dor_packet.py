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

import hashlib
import json
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
    QUOTE_AC_TEST_REF,
    QUOTE_DECLARED_AREA,
    QUOTE_RISK_CLASS_REASON,
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

# Поля постановки, которые едут стюарду ЦИТАТОЙ (#1076). Названы константой, а
# не литералом в цикле, чтобы отпечаток ревизии и проверка его полноты читали
# тот же список, что и сборщик цитат, а не похожий на него.
QUOTED_STATEMENT_COLUMNS: tuple[str, ...] = (
    "description",
    "user_story",
    "problem_statement",
)

# Колонки задачи, которые пакет ОТДАЁТ стюарду и которых нет в отпечатке
# постановки #1156. ``statement_fingerprint`` считает ревизии ПОСТАНОВКИ и
# собран из ``STATEMENT_FIELDS``; класс риска, его признаки, счёт готовности и
# автор в этот набор не входят — их пишет хаб или путь готовности, а не правка
# постановки. Пакет их тем не менее НЕСЁТ, и отпечаток, который их не покрывает,
# объявляет два разных состояния одним.
PACKET_TASK_COLUMNS: tuple[str, ...] = (
    "assigned_agent",
    "dor_passed",
    "readiness_score",
    "risk_class",
    "risk_class_reasons",
    "source",
    "statement_generation",
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
    # Отпечаток СОСТОЯНИЯ, о котором говорит пакет (``_revision_stamp``).
    revision: dict[str, Any] = field(default_factory=dict)

    @property
    def stable(self) -> bool:
        """Описывает ли пакет ОДНО состояние.

        ``False`` означает, что состояние двигалось всё время сборки и пакет
        собран из кусков разных ревизий. Это не факт об постановке, а факт о
        самой сборке, поэтому лежит рядом с фактами, а не среди них.
        """
        return bool(self.revision.get("stable", True))

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


def _authored_texts(
    task: dict[str, Any], ac_fact: EvidenceFact
) -> list[tuple[str, str]]:
    """Каждая строка пакета, которую набрал АВТОР, а не вычислил хаб.

    Список исчерпывающий по построению, и построение здесь — не «свободный
    текст постановки», а ПРОИСХОЖДЕНИЕ. Прошлая редакция считала цитатами
    только три поля постановки, и этого хватало ровно до тех пор, пока факты
    не начали ПЕРЕНОСИТЬ авторские строки: ``test_ref`` уезжал в
    ``ac_locator.criteria[].locator`` дословно, ``affected_areas`` — в
    ``diff_vs_areas.declared``, а признаки класса риска составляет хаб, но
    заявленные области вставляет в них как есть. Перенос делает строку
    похожей на вычисление хаба, и мимо ``injection_signals`` она проходила
    молча: пакет писал ``injection_suspected=False``, то есть УТВЕРЖДАЛ, что
    подозрений нет, про текст, который никто не смотрел. Это хуже молчания —
    судья читает утверждение хаба и получает чужой приказ под видом факта.

    Строки не вырезаются из фактов: стюарду нужно видеть, что именно написал
    автор. Они едут ДВАЖДЫ — значением факта и цитатой, — и второе снимает
    ложное утверждение о безопасности первого.

    Что сюда НЕ входит и почему: ``ac_id`` проверен схемой (AC-<число>),
    ``verifiable_by`` и ``risk_class`` — перечисления, ``status``/``reason``
    локатора — константы расчёта #506, ``reason`` зависимости складывает
    репозиторий из номера PR (#485), счёты и признаки — числа и флаги хаба.
    Ни в одной из них автор не может оставить текст. Полнота этого списка
    проверяется тестом, а не добросовестностью того, кто заведёт следующее
    поле.
    """
    out: list[tuple[str, str]] = []
    for column in QUOTED_STATEMENT_COLUMNS:
        text = (task.get(column) or "").strip()
        if text:
            out.append((QUOTE_TASK_STATEMENT, text))
    # Ниже цитируется РОВНО то значение, которое лежит в факте, — не
    # обрезанное и не нормализованное. Цитата, отличающаяся от факта хотя бы
    # пробелом, перестаёт быть цитатой ИМЕННО этой строки, и «текст проверен»
    # снова становится утверждением про что-то другое.
    if ac_fact.is_present:
        for item in ac_fact.value.get("criteria") or []:
            locator = item.get("locator") or ""
            if locator.strip():
                out.append((QUOTE_AC_TEST_REF, locator))
    for area in deserialize_str_list(task.get("affected_areas")):
        if area.strip():
            out.append((QUOTE_DECLARED_AREA, area))
    for reason in deserialize_str_list(task.get("risk_class_reasons")):
        if reason.strip():
            out.append((QUOTE_RISK_CLASS_REASON, reason))
    return out


def _statement_quotes(
    task: dict[str, Any], ac_fact: EvidenceFact
) -> tuple[QuotedText, ...]:
    """Чужие слова — как слова, с автором и с признаками (#1076).

    Текст критериев (given/when/then) сюда не входит намеренно: пакет
    сообщает СОСТОЯНИЕ критериев, а не их формулировки, и в фактах их нет —
    значит и цитировать нечего.
    """
    author = (task.get("assigned_agent") or task.get("source") or "").strip()
    return tuple(
        quote(source, author, text) for source, text in _authored_texts(task, ac_fact)
    )


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


def _digest(payload: Any) -> str:
    """Устойчивый отпечаток куска состояния.

    ``default=str`` — чтобы значение неожиданного типа НЕ роняло сборку
    доказательств: пакет не имеет права падать оттого, что в колонке лежит
    что-то непривычное. sha256 здесь не про безопасность, а про длину и
    отсутствие коллизий на человеческих объёмах.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode(
            "utf-8"
        )
    ).hexdigest()


async def _revision_stamp(db: aiosqlite.Connection, task_id: int) -> dict[str, Any]:
    """Отпечаток состояния, о котором пакет собирается говорить.

    ЗАЧЕМ ОТПЕЧАТОК, А НЕ ОДНО ПОКОЛЕНИЕ. ``statement_generation`` считает
    ревизии ПОСТАНОВКИ, и считает их по отпечатку из ``STATEMENT_FIELDS``
    плюс критерии (#1156). Рёбер зависимостей в этом отпечатке нет, и это
    правильно: добавление блокера постановку не переписывает. Но пакет
    сообщает о зависимостях ФАКТ, и факт этот меняется ровно тогда, когда
    поколение стоит на месте. Один такт: стюард читает «блокирующих нет»,
    ему добавляют недоставленный блокер, стюард записывает одобрение под тем
    же поколением — и одобрение выглядит выданным по состоянию, которого уже
    нет. Поэтому отпечаток берётся по ВСЕМУ, о чём пакет говорит, а не по
    одной его половине.

    ЗАЧЕМ СЧИТАТЬ ОТПЕЧАТОК ПОСТАНОВКИ ЗАНОВО, А НЕ ЧИТАТЬ КОЛОНКУ. Колонку
    ``statement_fingerprint`` обновляет только путь записи готовности
    (#1156); правка, прошедшая мимо него, оставит колонку прежней. Отпечаток,
    который не двигается при изменившемся содержимом, хуже отсутствующего:
    он утверждает неизменность.

    ЗАЧЕМ ЦЕЛИКОМ, А НЕ ПО ДВУМ ПОЛЯМ. Прошлая редакция хешировала у блокера
    ровно номер задачи и признак доставленности, а у задачи — ничего, кроме
    постановки. Пакет при этом отдаёт стюарду СТАТУС блокера и причину
    недоставки, класс риска с признаками и счёт готовности: ни одно из этих
    полей в отпечаток не входило, и два состояния, различающиеся любым из них,
    давали ОДИН отпечаток. Замерено на этом коде: блокер ``open`` →
    ``in_progress`` при неизменном ребре, ``R2`` → ``R3`` и готовность 94/пройдена
    → 40/не пройдена — факты разные, отпечатки посимвольно равные. И потому же
    гонка внутри сборки не ловилась вовсе: пакет уносил класс ДО правки рядом со
    статусом блокера ПОСЛЕ неё и объявлял себя ``stable=True``, то есть выдавал
    смесь двух состояний за одну ревизию.

    Поэтому отпечаток берётся по ВСЕМУ, о чём пакет говорит: рёбра целиком, как
    их отдаёт репозиторий (#485), и каждая колонка, которую пакет читает мимо
    ``STATEMENT_FIELDS`` (``PACKET_TASK_COLUMNS``). Полнота этого списка
    проверяется тестом, который вычитывает обращения к колонкам из самого
    модуля, — а не добросовестностью того, кто заведёт следующее поле.

    Лишняя чувствительность здесь дешевле недостаточной: отпечаток, дрогнувший
    зря, стоит одной пересборки, а в пределе — честного ``stable=False``.
    Отпечаток, не дрогнувший вовремя, стоит одобрения, выданного по состоянию,
    которого уже нет.
    """
    from hub.services.statement_generation import statement_fingerprint

    row = await repo.get_task(db, task_id)
    task = dict(row) if row is not None else {}
    edges = await repo.list_task_dependencies(db, task_id)
    return {
        "statement_generation": int(task.get("statement_generation") or 0),
        "statement": await statement_fingerprint(db, task_id),
        "task": _digest({c: task.get(c) for c in PACKET_TASK_COLUMNS}),
        # Рёбра целиком: статус и причина едут стюарду внутри факта, значит и в
        # отпечаток входят. Порядок задаёт запрос (ORDER BY t.id), но сортировка
        # повторена здесь: порядок строк — свойство запроса, а не отпечатка.
        "dependencies": _digest(
            sorted(
                (dict(e) for e in edges.get("blocked_by", [])),
                key=lambda e: int(e.get("task_id") or 0),
            )
        ),
    }


# Сколько раз собирать пакет, пока состояние не устоится. Первая попытка —
# обычный путь; две пересборки покрывают одиночную правку, попавшую в окно
# сборки. Правка на КАЖДОЙ попытке — это уже не гонка, а автор, который прямо
# сейчас переписывает постановку, и врать про неё «собрано по одной ревизии»
# нельзя ни при какой глубине повтора.
_ASSEMBLY_ATTEMPTS = 3


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

    ПАКЕТ ОПИСЫВАЕТ ОДНО СОСТОЯНИЕ, И ЭТО ПРОВЕРЯЕТСЯ, А НЕ ПРЕДПОЛАГАЕТСЯ.
    Сборка не мгновенна: бриф ревью ходит в базу и на диск, и правка автора,
    закоммиченная в эту секунду, доставала ранний снимок задачи с одной
    стороны и свежие критерии с другой. Пакет тогда ЗАЯВЛЯЛ старое поколение,
    не описывая целиком ни одну ревизию, — а заявленное поколение и есть ключ,
    под которым ляжет суждение. Поэтому состояние снимается ДО и ПОСЛЕ сборки
    (``_revision_stamp``), при расхождении пакет пересобирается, и если оно
    не сошлось и тогда — пакет говорит об этом полем ``stable``, а не молчит.
    """
    stamp = await _revision_stamp(db, task_id)
    packet: DraftEvidencePacket | None = None
    for _ in range(_ASSEMBLY_ATTEMPTS):
        packet = await _assemble(db, task_id, stamp)
        if packet is None:
            return None
        after = await _revision_stamp(db, task_id)
        if after == stamp:
            return packet
        log.info(
            "draft packet %s: state moved during assembly (%s -> %s), rebuilding",
            task_id,
            stamp["statement_generation"],
            after["statement_generation"],
        )
        stamp = after
    # Врать про одну ревизию нельзя, а отдать судье ПУСТОТУ — тоже: решает
    # привратник (#1159), и решать ему есть по чему только когда
    # нестабильность НАЗВАНА. Рядом едет то, во что состояние ушло, — иначе
    # разбирающему случай остаётся один флаг без диагноза.
    if packet is None:  # pragma: no cover — цикл выполняется хотя бы раз
        return None
    packet.revision["stable"] = False
    packet.revision["observed_after"] = stamp
    log.warning(
        "draft packet %s: state kept moving; packet is not one revision",
        task_id,
    )
    return packet


async def _assemble(
    db: aiosqlite.Connection, task_id: int, stamp: dict[str, Any]
) -> DraftEvidencePacket | None:
    """Одна попытка сборки под УЖЕ снятый отпечаток состояния."""
    row = await repo.get_task(db, task_id)
    if row is None:
        return None
    task = dict(row)

    ac_fact = await _ac_locator_from_brief(db, task_id)
    facts = {
        f.source: f
        for f in [
            ac_fact,
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
        quotes=_statement_quotes(task, ac_fact),
        revision={**stamp, "stable": True},
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
        "revision": dict(packet.revision),
        "stable": packet.stable,
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
    "PACKET_TASK_COLUMNS",
    "PRESENT",
    "QUOTED_STATEMENT_COLUMNS",
    "DraftEvidencePacket",
    "build_draft_packet",
    "draft_packet_payload",
]
