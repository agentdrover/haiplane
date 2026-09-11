"""Применение суждения: здесь стюард впервые двигает чужую задачу (#1149).

До этого модуля он советует, и цена ошибки — лишняя строка в карточке.
После — он меняет исход. Разница между «есть право применять» и «что
именно произойдёт» проведена намеренно: первое отвечает привратник
(#1147, #1148), второе здесь. Функция, отвечающая на оба вопроса сразу,
не проверяется по половине.

Три правила, и каждое существует против своей ошибки.

КЛИЕНТСКИЙ ПУТЬ. У задачи без ``review_job_id`` нет облачного
исполнителя, которому можно поручить правку. Возврат идёт в ту же ветку:
задача в ``running``, ``review_cycle`` +1, и ничего больше — ни job, ни
параллельной fix-задачи. Серверный маршрут здесь породил бы либо висящий
job, либо вторую задачу на ту же работу.

БЮДЖЕТ ОБЩИЙ. Исчерпан — задача идёт в ``needs_decision``
СУЩЕСТВУЮЩИМ переходом. Арбитра на клиентском пути нет, и отдельной
квоты для стюарда тоже: счётчик, который никто не сверяет с общим,
разъедется, а разъедется тот, который мягче. Вопрос «исчерпан ли»
задаётся ровно одной функции — ``review_budget_exhausted`` (#423), и её
докстринг прямо запрещает сравнивать счётчик с потолком где-либо ещё.

ЧЕЛОВЕК СТАРШЕ. Вердикт, уже стоящий на этой генерации, суждение
стюарда не перезаписывает — 409. Не потому, что человек быстрее, а
потому, что он главнее.

ПРО ИМЯ СОБЫТИЯ, чтобы читатель не обманулся. ``steward_applied``
пишется контрактом #1023 в момент ЗАПИСИ суждения — то есть означает
«вердикт не эскалация», а не «применено». В теневой фазе это было
безобидно, потому что не применялось ничего. Здесь применение настоящее,
и его следом служит ОБЫЧНАЯ запись вердикта: ``review_verdict_recorded``
с актором steward, ровно та же, которой пользуется человек. Она же даёт
at-most-once — вердикт привязан к генерации, — и она же кладёт суждение
стюарда в те же метрики, где считаются человеческие. Переименовывать
событие #1023 не стал: его читают метрики, и правка ради стройности
названия сломала бы счёт.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.models import ReviewVerdict, TaskReviewVerdict
from hub.services.steward_evidence import ReviewBrief

log = logging.getLogger(__name__)

APPLIED = "applied"
RETURNED = "returned_to_running"
ESCALATED_TO_HUMAN = "needs_decision"

_STEWARD_ACTOR = "steward"

# Восемь признаков сошедшейся сдачи, ИМЕНАМИ. Перечень публичный по той же
# причине, по которой публичен PRECONDITION_FACTS у привратника: полноту
# проверяет тест, а не внимательность читателя, и признак, добавленный в
# правило и забытый здесь, тихо перестал бы называть себя в отказе.
#
# Порядок — порядок постановки #1231, чтобы строку в карточке можно было
# сверить с ней глазами.
SELF_APPROVAL_SIGNALS: tuple[str, ...] = (
    "report_is_current",
    "no_confirmed_findings",
    "no_unresolved_findings",
    "report_is_whole",
    "reviewed_by_someone_else",
    "checks_ran_on_the_submitted_commit",
    "acceptance_criteria_are_green",
    "evidence_coverage_is_complete",
)

# Запреты. Их природа другая, и поэтому они лежат ОТДЕЛЬНО от признаков, а
# не девятым, десятым и одиннадцатым в том же списке: признак не сошёлся —
# его можно досдать и вернуться; запрет не снимается никаким набором
# свидетельств вообще. Сложить их в один список значило бы пообещать, что
# достаточно дособрать доказательств.
SELF_APPROVAL_FORBIDS: tuple[str, ...] = (
    "gate_decision_path",
    "live_check_unknown",
    "reviewer_unreachable",
)

# Чем проверяется критерий, который НЕЛЬЗЯ закрыть тестом: такой критерий
# требует, чтобы кто-то посмотрел на живое поведение. Перечисляется, а не
# выводится из «не test», чтобы новое значение словаря не попадало сюда
# молча — оно обязано быть названо здесь осознанно.
_VERIFIED_BY_LOOKING: frozenset[str] = frozenset({"manual", "log_check", "ui_check"})
# Слова, по которым самостоятельное одобрение узнают в карточке. Константа,
# а не литерал в двух местах: строку, по которой читатель и тест находят
# запись, нельзя чинить в одном месте и забыть в другом.
_SELF_APPROVAL_HEADLINE = "Одобрено стюардом без человека"


@dataclass(frozen=True)
class SelfApproval:
    """Можно ли вынести APPROVED без человека — и почему именно.

    Три поля, а не одно «да/нет», потому что читателю нужны разные вещи в
    разных случаях. ``converged`` — на чём стоит решение, когда оно
    принято: одобрение, не назвавшее своих оснований, человек на
    спот-чеке проверить не может. ``missing`` — какой ИМЕННО признак не
    сошёлся: «не одобрено» без имени признака заставляет искать причину
    заново. ``forbidden`` — запреты, и они отдельно от признаков нарочно.
    """

    converged: tuple[str, ...] = ()
    missing: tuple[tuple[str, str], ...] = ()
    forbidden: tuple[tuple[str, str], ...] = ()

    @property
    def allowed(self) -> bool:
        """Сошлись ВСЕ признаки и не сработал ни один запрет.

        Проверяется полнотой ``converged``, а не пустотой ``missing``:
        признак, который правило забыло посчитать вовсе, не попал бы ни в
        один список, и «нет несошедшихся» прочиталось бы как «все сошлись»
        — ровно та подмена, против которой стоит вся задача (#762).
        """
        return (
            not self.missing
            and not self.forbidden
            and tuple(self.converged) == SELF_APPROVAL_SIGNALS
        )

    @property
    def reason(self) -> str:
        """Причина к человеку — запреты первыми, признаки следом."""
        parts = [f"запрет {code}: {detail}" for code, detail in self.forbidden]
        parts += [
            f"признак {code} не сошёлся: {detail}" for code, detail in self.missing
        ]
        return "; ".join(parts)


async def apply_judgement(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[str, str]:
    """Применить суждение стюарда этой генерации. Возвращает (исход, деталь).

    Исход — что стало с задачей, а не что решил судья: ``applied`` для
    approve, ``returned_to_running`` для возврата на клиентском пути,
    ``needs_decision`` когда бюджет исчерпан. Три разных слова, потому что
    человеку, читающему фид, нужно знать, где теперь его задача.

    Ничего не проверяет из того, что проверил привратник: право применять
    — вопрос #1147 и #1148, и дублировать его здесь значило бы завести
    второй ответ на один вопрос.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        raise HTTPException(404, detail=f"задачи #{task_id} нет")
    task = dict(row)

    _refuse_if_the_submission_moved(task, generation)
    _refuse_if_the_verdict_is_taken(task, generation)

    judgement = await repo.get_steward_judgement(db, task_id, generation, "verdict")
    if judgement is None:
        raise HTTPException(
            409,
            detail=(f"суждения на генерацию {generation} нет — применять нечего"),
        )
    verdict = str(dict(judgement).get("verdict") or "")

    if verdict == "approve":
        await _record(db, task_id, ReviewVerdict.approved, generation)
        return APPLIED, f"approve применён к сдаче {generation}"

    if verdict != "changes_requested":
        # escalate сюда не доходит: он и есть отказ судить, и применять в
        # нём нечего. Отдельная ветка на случай нового слова в словаре —
        # незнакомый вердикт обязан остановиться, а не пройти молча.
        raise HTTPException(
            409,
            detail=f"вердикт {verdict!r} не применяется: применяются approve и changes_requested",
        )

    from hub.services.orchestration import review_budget_exhausted

    cycles = int(task.get("review_cycle") or 0)
    if review_budget_exhausted(cycles):
        await _hand_to_the_human(db, task_id, cycles)
        return ESCALATED_TO_HUMAN, (
            f"бюджет циклов исчерпан ({cycles}) — решение за человеком"
        )

    await _record(db, task_id, ReviewVerdict.changes_requested, generation)
    return RETURNED, f"работа возвращена автору, цикл {cycles + 1}"


def _refuse_if_the_submission_moved(task: dict[str, Any], generation: int) -> None:
    """Суждение о ПРОШЛОЙ сдаче не применяется к нынешней.

    Найдено кросс-модельным ревью и воспроизведено: без этой проверки
    суждение генерации 1 записывалось вердиктом на генерацию 2. Причина
    в том, что запись вердикта привязывает его к ТЕКУЩЕЙ сдаче задачи, а
    не к той, о которой судили, — и человеческий approve на живой сдаче
    оказывался затёрт мнением о коде, которого на ветке уже нет.

    Проверка «вердикт на эту генерацию уже стоит» этот случай не ловит и
    не могла: она сравнивает поле с ЗАПРОШЕННОЙ генерацией, поэтому чужая
    генерация проходит мимо неё именно потому, что чужая. Пин из #1120
    здесь тот же: суждение о сдаче, которую уже сменили, описывает не тот
    исход, который решается.
    """
    live = int(task.get("submission_generation") or 0)
    if live == generation:
        return
    raise HTTPException(
        409,
        detail=(
            f"суждение о сдаче {generation}, а живая сдача — {live}: "
            "применять его значило бы записать вердикт о коде, которого "
            "на ветке уже нет"
        ),
    )


def _refuse_if_the_verdict_is_taken(task: dict[str, Any], generation: int) -> None:
    """Вердикт на эту генерацию уже стоит — суждение опоздало.

    Одна проверка на два случая, и это не экономия: человеческий вердикт
    и уже применённое суждение стюарда лежат в ОДНОМ поле, потому что
    применение пишется той же записью, что и человеческое решение. Значит
    «человек успел раньше» и «мы применяем второй раз» — один и тот же
    факт, и разделять его на две проверки значило бы позволить им
    разойтись.
    """
    stored = (task.get("review_verdict") or "").strip()
    verdict_generation = task.get("review_verdict_generation")
    if not stored or verdict_generation != generation:
        return
    raise HTTPException(
        409,
        detail=(
            f"на сдачу {generation} вердикт уже записан ({stored}) — "
            "суждение стюарда его не перезаписывает: человек старше, и "
            "повторное применение тоже"
        ),
    )


async def _record(
    db: aiosqlite.Connection,
    task_id: int,
    verdict: ReviewVerdict,
    generation: int,
) -> None:
    """Записать вердикт ТЕМ ЖЕ путём, которым его пишет человек.

    Клиентский путь (возврат в running в той же ветке, review_cycle +1,
    без review_job_id и без fix-задач) уже реализован там (#307), и
    второй маршрут рядом означал бы второе описание одного перехода.
    Актор — steward, поэтому в фиде и в метриках видно, кто решил.
    """
    from hub.services.lifecycle import record_review_verdict

    await record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(
            agent=_STEWARD_ACTOR,
            verdict=verdict,
            comments=f"Применено стюардом по суждению генерации {generation}.",
        ),
    )


async def _hand_to_the_human(
    db: aiosqlite.Connection, task_id: int, cycles: int
) -> None:
    """Бюджет исчерпан: существующий переход в needs_decision, без арбитра.

    Арбитра на клиентском пути нет — его диспетчеризация живёт в серверном
    маршруте и требует job. Заводить его сюда значило бы построить второй
    арбитраж ради одного случая; человек здесь и есть арбитр.
    """
    from hub.services.orchestration import log_activity

    moved = await repo.transition_status_if(
        db, task_id, expected_from="review", new_status=ESCALATED_TO_HUMAN
    )
    if not moved:
        # Задача уже не в review — эскалация состоялась раньше. Второй
        # алерт про исчерпанный бюджет не добавил бы ничего, кроме шума в
        # карточке, и создал бы впечатление двух разных событий. Этот путь
        # вердикта не пишет, поэтому замок «вердикт уже стоит» его не
        # держит — держит вот этот отказ.
        raise HTTPException(
            409,
            detail=(
                "бюджет уже исчерпан и задача уже передана человеку — "
                "повторное применение ничего не меняет"
            ),
        )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        (
            f"Бюджет циклов ревью исчерпан ({cycles}), и стюард снова просит "
            "правок. Дальше решает человек (hub_decide_task): rework вернёт "
            "задачу в running, accept завершит её как есть. Арбитра на "
            "клиентском пути нет — им и является это решение."
        ),
        author_kind="hub",
    )
    # Тем же событием, которым эскалирует канонический путь
    # (orchestration.py, review_cycle_limit). Своё имя здесь означало бы,
    # что счётчик исчерпанных бюджетов расходится в зависимости от того,
    # кто вернул работу, — а весь эпик стоит на сравнении этих двух
    # маршрутов.
    await repo.insert_event(
        db,
        kind=ESCALATED_TO_HUMAN,
        task_id=task_id,
        actor=_STEWARD_ACTOR,
        payload={"reason": "review_cycle_limit"},
    )
    await log_activity(
        db,
        "task_needs_decision",
        f"Task #{task_id} → needs_decision (steward, cycles={cycles})",
    )
    await db.commit()


def self_approval(
    brief: ReviewBrief,
    *,
    diff_paths: Sequence[str] | None,
    reviewer_reachable: bool,
) -> SelfApproval:
    """Сошлись ли ВСЕ свидетельства настолько, что человек здесь ничего не решает.

    ПРАВИЛО СНЯТО С ЗАМЕРА, а не выведено. 09.09.2026 одиннадцать задач
    стояли в review одновременно; ровно у двух (#1164 и #1216) сошёлся
    весь набор, человек одобрил обе без единой правки, обе доставлены. У
    остальных девяти причина отказа каждый раз лежала в этих же полях.

    ГЛАВНАЯ ОШИБКА, ПРОТИВ КОТОРОЙ НАПИСАНА КАЖДАЯ СТРОКА НИЖЕ: «ноль
    находок» и «смотреть было некому» выглядят в отчёте одинаково. В тот
    же день #1198 пришёл с потерянным измерением, а #1202 — отчётом от
    автора кода, и оба показывали ноль подтверждённых. Поэтому ни один
    признак здесь не считается сошедшимся по УМОЛЧАНИЮ: отсутствие отчёта
    роняет КАЖДЫЙ признак, который из отчёта читается, своей строкой, а не
    одну общую; ``incomplete=None`` («никогда не заявляли») — не то же
    самое, что ``incomplete=False``; а критерий без записанного прогона не
    зелёный, а непроверенный.

    Ничего не считает заново: все восемь фактов уже собрал бриф ревью
    (#1074, #725), и второй расчёт того же факта означал бы второй ответ
    на один вопрос — расходиться они начали бы молча.

    ``diff_paths`` — ФАКТИЧЕСКИЕ пути диффа; ``None`` означает «прочитать
    не удалось», и это запрет, а не разрешение: задача, объявившая
    «hub/services/», умеет менять hub/auth.py (#1147).
    """
    missing = _signals_that_did_not_converge(brief)
    forbidden = _forbids(brief, diff_paths, reviewer_reachable)
    named = {code for code, _ in missing}
    return SelfApproval(
        converged=tuple(s for s in SELF_APPROVAL_SIGNALS if s not in named),
        missing=tuple(missing),
        forbidden=tuple(forbidden),
    )


def _signals_that_did_not_converge(brief: ReviewBrief) -> list[tuple[str, str]]:
    """Каждый из восьми признаков — СВОЕЙ проверкой и со своим именем.

    Перечисляются ВСЕ несошедшиеся, а не первый: человеку, к которому
    задача уедет, нужна причина, а не первая из причин — иначе он чинит по
    одной и возвращается.
    """
    out: list[tuple[str, str]] = []
    mr = brief.machine_review

    # 1. Отчёт есть и он про ЭТУ сдачу. Отчёт о прошлой генерации — не
    #    более слабое свидетельство, а свидетельство о другом коде.
    if mr is None:
        out.append(("report_is_current", "машинного отчёта на эту сдачу нет вовсе"))
    elif not mr.is_current:
        out.append(
            (
                "report_is_current",
                f"отчёт о сдаче {mr.submission_generation}, а живая — "
                f"{brief.submission_generation}",
            )
        )

    # 2. Подтверждённых находок нет.
    if mr is None:
        out.append(
            (
                "no_confirmed_findings",
                "отчёта нет — подтверждённых находок не считал никто, и это не ноль",
            )
        )
    elif mr.findings_confirmed:
        out.append(
            (
                "no_confirmed_findings",
                f"подтверждённых находок {len(mr.findings_confirmed)}",
            )
        )

    # 3. Неразрешённых находок нет. Отдельно от пункта 2, потому что это
    #    РАЗНЫЕ разделы: 09.09 из семи неразрешённых настоящими оказались
    #    шесть при нуле подтверждённых.
    if mr is None:
        out.append(
            (
                "no_unresolved_findings",
                "отчёта нет — неразрешённых находок не считал никто",
            )
        )
    elif mr.unresolved:
        out.append(
            (
                "no_unresolved_findings",
                f"неразрешённых находок {len(mr.unresolved)} — "
                "их никто не рассудил, а не их нет",
            )
        )

    # 4. Отчёт целый: харнесс не оборвался и не потерял измерений.
    if mr is None:
        out.append(("report_is_whole", "отчёта нет — о его полноте сказать нечего"))
    elif mr.incomplete is None:
        out.append(
            (
                "report_is_whole",
                "отчёт про свою полноту не говорит ничего (incomplete не "
                "заявлен) — это не «полон»",
            )
        )
    elif mr.incomplete:
        out.append(("report_is_whole", "отчёт помечен неполным (incomplete)"))
    elif mr.lost_dimensions:
        out.append(
            (
                "report_is_whole",
                "потеряны измерения: " + ", ".join(sorted(mr.lost_dimensions)),
            )
        )

    # 5. Отчёт сдан не автором кода.
    if mr is None:
        out.append(("reviewed_by_someone_else", "отчёта нет — смотреть было некому"))
    elif mr.self_reviewed:
        out.append(
            (
                "reviewed_by_someone_else",
                "отчёт сдан автором кода (self_reviewed) — это не второй взгляд",
            )
        )

    out.extend(_commit_signal(brief))
    out.extend(_acceptance_signal(brief))

    # 8. Покрытие свидетельств полное: ни один блок брифа не остался без
    #    сигнала.
    coverage = brief.evidence_coverage
    if coverage.state != "complete":
        out.append(
            (
                "evidence_coverage_is_complete",
                f"покрытие свидетельств {coverage.state!r}: "
                + (coverage.headline or "часть блоков брифа не дала сигнала"),
            )
        )
    return out


def _commit_signal(brief: ReviewBrief) -> list[tuple[str, str]]:
    """6. Проверки прогонялись на ТОМ ЖЕ коммите, который сдан.

    Один признак, а не три, потому что вопрос один: относится ли зелёное к
    сдаваемому коду. Но названная причина всегда говорит, ЧТО именно
    разошлось, — иначе чинить придётся вслепую.
    """
    pinned = (brief.submission_sha or "").strip()
    if not pinned:
        return [
            (
                "checks_ran_on_the_submitted_commit",
                "сдача не закрепила коммит — сверять прогоны не с чем",
            )
        ]
    prepass = brief.prepass
    if prepass.state != "covered":
        return [
            (
                "checks_ran_on_the_submitted_commit",
                f"предпас {prepass.state!r}: "
                + (prepass.reason or "детерминированные проверки не прогонялись"),
            )
        ]
    if (prepass.head_sha or "").strip() != pinned:
        return [
            (
                "checks_ran_on_the_submitted_commit",
                f"предпас снят на {(prepass.head_sha or '—')[:12]}, "
                f"а сдан {pinned[:12]}",
            )
        ]
    ci = brief.ci_run_report
    if ci.state != "current":
        return [
            (
                "checks_ran_on_the_submitted_commit",
                f"отчёт CI {ci.state!r}: "
                + (ci.reason or "прогона по этому коммиту никто не сообщал"),
            )
        ]
    if (ci.head_sha or "").strip() != pinned:
        return [
            (
                "checks_ran_on_the_submitted_commit",
                f"CI отчитался о {(ci.head_sha or '—')[:12]}, а сдан {pinned[:12]}",
            )
        ]
    if brief.sha_check != "match":
        return [
            (
                "checks_ran_on_the_submitted_commit",
                f"sha_check={brief.sha_check!r}: "
                + (brief.sha_check_reason or "вершина ветки и сдача не сверены"),
            )
        ]
    return []


def _acceptance_signal(brief: ReviewBrief) -> list[tuple[str, str]]:
    """7. Все критерии, закрываемые тестом, зелёные И текущие.

    «Текущие» здесь не украшение: результат прошлой генерации описывает
    код, которого на ветке уже нет, и зачесть его значило бы одобрить по
    прогону чужой сдачи.

    Критерий без записанного результата — НЕ зелёный. Это тот же случай
    «смотреть было некому», ради которого написано всё правило: пустой
    список результатов при живых критериях означает, что тесты никто не
    прогонял, а не что они прошли.
    """
    testable = [
        ac
        for ac in brief.acceptance_criteria
        if str(getattr(ac.verifiable_by, "value", ac.verifiable_by)) == "test"
    ]
    if not testable:
        return [
            (
                "acceptance_criteria_are_green",
                "ни один критерий не закрывается тестом — зелёного прогона, "
                "на котором могло бы стоять одобрение, не существует",
            )
        ]
    results = {r.ac_id: r for r in brief.ac_test_results}
    for ac in testable:
        result = results.get(ac.id)
        if result is None:
            return [
                (
                    "acceptance_criteria_are_green",
                    f"{ac.id}: результата теста за эту сдачу нет — критерий "
                    "не проверен, а не пройден",
                )
            ]
        if not result.is_current:
            return [
                (
                    "acceptance_criteria_are_green",
                    f"{ac.id}: результат от прошлой сдачи — он про другой код",
                )
            ]
        if result.status != "pass":
            return [
                (
                    "acceptance_criteria_are_green",
                    f"{ac.id}: тест в состоянии {result.status!r}",
                )
            ]
    return []


def _forbids(
    brief: ReviewBrief,
    diff_paths: Sequence[str] | None,
    reviewer_reachable: bool,
) -> list[tuple[str, str]]:
    """Запреты: то, что не снимается никаким набором свидетельств.

    Отдельно от признаков и в отдельном поле результата, потому что читать
    их надо по-разному. Несошедшийся признак — это «доберите свидетельство
    и возвращайтесь»; запрет — «этой задаче самостоятельного одобрения не
    будет, сколько свидетельств ни собери».
    """
    from hub.services.auto_approve import ladder_hits

    # Слово, которым «доставлено» называет сам хаб (#837). Своя константа
    # рядом разъехалась бы с ним молча — и разъехалась бы та, что мягче.
    from hub.services.delivery_state import IN_PROD

    out: list[tuple[str, str]] = []

    # Судья не подписывает изменение собственных правил. Считается по
    # ФАКТИЧЕСКОМУ диффу: заявленная область — предсказание, и задача,
    # объявившая «hub/services/», меняет hub/auth.py, не солгав ни разу.
    if diff_paths is None:
        out.append(
            (
                "gate_decision_path",
                "дифф прочитать не удалось — чего он трогает, хаб не знает; "
                "незнание не есть безопасность",
            )
        )
    else:
        hits = ladder_hits([str(p) for p in diff_paths])
        if hits:
            out.append(
                (
                    "gate_decision_path",
                    "дифф меняет сам путь решения гейта: "
                    + ", ".join(hits)
                    + " — такое решение остаётся человеку при любом наборе признаков",
                )
            )

    # Постановка требует, чтобы кто-то посмотрел, а никто не смотрел.
    if any(
        str(getattr(ac.verifiable_by, "value", ac.verifiable_by))
        in _VERIFIED_BY_LOOKING
        for ac in brief.acceptance_criteria
    ):
        live = brief.live_check
        # Сверяется с ЗАКРЕПЛЁННЫМ коммитом сдачи, а не с готовым флагом
        # ``sha_mismatch``. Флаг отвечает на другой вопрос: бриф считает его
        # против ДОСТАВЛЕННОГО merge-коммита (review_brief, #814), которого на
        # ревью ещё нет — задача как раз и стоит в review, не в develop.
        # Поэтому до доставки флаг ложен ВСЕГДА, каким бы ни был коммит
        # наблюдения, и запрет, опёртый на него, пропускал наблюдение чужого
        # или неопознанного кода. Найдено кросс-модельным ревью и
        # воспроизведено на настоящем ``live_check_state``.
        pinned = (brief.submission_sha or "").strip()
        observed = (live.sha or "").strip()
        if live.state != "done":
            out.append(
                (
                    "live_check_unknown",
                    f"постановка требует живой проверки, а она {live.state!r}: "
                    + (live.reason or "поведение никто не наблюдал"),
                )
            )
        elif not observed:
            out.append(
                (
                    "live_check_unknown",
                    "живая проверка не назвала коммита, а сдан "
                    f"{pinned[:12] or '—'}: наблюдение неопознанного кода "
                    "свидетельством о сдаваемом не является",
                )
            )
        elif observed != pinned:
            out.append(
                (
                    "live_check_unknown",
                    f"живая проверка снята на {observed[:12]}, а сдан "
                    f"{pinned[:12] or '—'} — она не говорит о сдаваемом коде",
                )
            )
        # Совпавший sha отвечает на вопрос «какой коммит НАЗВАЛИ», и называет
        # его тот же, кто принёс наблюдение. Вопрос «доехал ли он до прода»
        # — другой, и на него отвечает хаб: ``record_live_check`` сознательно
        # принимает запись с deploy_state=unknown (#837), потому что
        # установка без фактов о доставке ничего не знает про прод и отказ
        # там превратил бы незнание в гейт. Принятая запись — не
        # подтверждённая, и читать её как подтверждение значило бы вернуть
        # ровно тот дефект, против которого #837 и написан: свидетельство о
        # нераскатанном коде выглядит сильнее всех прочих блоков и при этом
        # ложно.
        elif live.deploy_state != IN_PROD:
            out.append(
                (
                    "live_check_unknown",
                    "хаб не подтвердил выкат наблюдавшегося коммита "
                    f"(deploy_state={live.deploy_state or '—'!r}): совпадение "
                    "sha говорит, какой коммит назвали, а не то, что он "
                    "доехал до прода",
                )
            )

    # Проект, где хаб не умеет позвать ревьюера: любой отчёт там мог быть
    # сдан только автором, и «второй взгляд» неотличим от первого.
    if not reviewer_reachable:
        out.append(
            (
                "reviewer_unreachable",
                "в проекте хаб не вызывает ревьюера — отчёт здесь мог "
                "появиться только от автора кода",
            )
        )
    return out


async def self_approval_for(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> SelfApproval:
    """Собрать входы правила из того, что хаб уже посчитал, и применить его.

    Бриф — единственный источник восьми признаков (#1074); дифф берётся из
    пакета доказательств тем же обходом ветки, которым хаб считает выход за
    заявленные области. Своего расчёта здесь нет ни одного: второй способ
    узнать тот же факт разошёлся бы с первым молча.
    """
    from hub.services.project_policy import gate_policy_of, review_dispatch_enabled
    from hub.services.review_brief import build_review_brief
    from hub.services.steward_evidence import build_evidence_packet

    brief = await build_review_brief(db, task_id)
    if brief is None:
        raise HTTPException(404, detail=f"задачи #{task_id} нет")

    packet = await build_evidence_packet(db, task_id, generation)
    diff_paths: Sequence[str] | None = None
    fact = packet.facts.get("diff_vs_areas") if packet is not None else None
    if fact is not None and fact.state == "present":
        diff_paths = [str(p) for p in (fact.value or {}).get("paths") or []]

    # Проект не разрешился — значит, вызывает ли хаб здесь ревьюера, мы не
    # знаем. Незнание читается как «не вызывает»: это тот же выбор, что у
    # нераспознанного режима (#835) — неизвестное не имеет права быть тем,
    # что открывает контур.
    project = await repo.resolve_project_for_task(db, task_id)
    reachable = (
        review_dispatch_enabled(gate_policy_of(dict(project)))
        if project is not None
        else False
    )

    return self_approval(brief, diff_paths=diff_paths, reviewer_reachable=reachable)


async def approve_without_a_human(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    decision: SelfApproval,
) -> tuple[str, str]:
    """Вынести APPROVED без человека — или увезти задачу к нему с причиной.

    ``decision`` приходит параметром, а не считается здесь, ровно по той же
    границе, которой разведены привратник и применение (#1147/#1149):
    «можно ли» и «что именно произойдёт» ошибаются по-разному, и функция,
    отвечающая на оба вопроса сразу, не проверяется по половине. Но
    доверия к переданному решению нет — неразрешающее применяется как
    отказ, а не как разрешение.

    Применение идёт ТЕМ ЖЕ ``apply_judgement``, которым применяется любое
    суждение: второй маршрут записи вердикта означал бы, что применённые
    стюардом решения считаются в двух разных местах.
    """
    if not decision.allowed:
        await _hand_to_the_human_because(db, task_id, decision)
        return ESCALATED_TO_HUMAN, (
            "самостоятельного одобрения нет — " + decision.reason
        )

    outcome, detail = await apply_judgement(db, task_id, generation)
    if outcome != APPLIED:
        # Свидетельства сошлись, а суждение стюарда просило правок — и тогда
        # ``apply_judgement`` вернул работу автору. Дописать сюда «одобрено
        # без человека» значило бы записать в аудит исход, которого не было:
        # ложная строка ровно в том месте, ради видимости которого всё это и
        # заведено. Решение по свидетельствам и суждение — разные вопросы, и
        # совпадать они не обязаны.
        return outcome, detail
    await repo.add_task_update(
        db,
        task_id,
        _STEWARD_ACTOR,
        "review",
        _SELF_APPROVAL_HEADLINE
        + f" (сдача {generation}). Решение стоит на восьми признаках, и вот они: "
        + ", ".join(decision.converged)
        + ". Одобрение самостоятельное: человек его не видел, поэтому оно "
        "попадает в выборку на спот-чек чаще среднего (#1144) и стоит "
        "отдельной строкой в дайджесте.",
        author_kind="hub",
    )
    await db.commit()
    return outcome, detail


async def _hand_to_the_human_because(
    db: aiosqlite.Connection, task_id: int, decision: SelfApproval
) -> None:
    """Набор не сошёлся: к человеку, и с ИМЕНЕМ того, что не сошлось.

    Молчаливый возврат к человеку неотличим от зависшей задачи, а возврат
    без имени признака заставляет искать причину заново — при том что хаб
    её уже знает.

    Статус не двигается: несошедшийся набор — это ровно то, как контур
    работает сегодня. Задача остаётся в ``review`` и ждёт человеческого
    вердикта, как ждала до этой задачи; новым здесь является только то,
    что причина названа.
    """
    await repo.add_task_update(
        db,
        task_id,
        _STEWARD_ACTOR,
        "status",
        "Самостоятельного одобрения не будет — решает человек. "
        + decision.reason
        + ".",
        author_kind="hub",
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Боевой вход: где правило действительно вызывается (#1231, вторая сдача)
# ---------------------------------------------------------------------------
#
# ПОЧЕМУ ЭТО ЗДЕСЬ, А НЕ «ПОТОМ». Первая сдача #1231 оставила правило без
# единого вызывающего в ``hub/``: собственный анализатор хаба (#601) на её
# диффе сказал ``only_tests`` про ``self_approval_for`` и
# ``approve_without_a_human``. Механизм, написанный верно и никуда не
# подключённый, — ровно тот отказ, ради которого анализатор и заведён, и он
# зелёный в CI: путь, который не исполняется, не ломает ни одного теста. Без
# этого входа «доля сдач, уехавших без человека» осталась бы нулём при любом
# наборе свидетельств.
#
# ТОЧКА ВЫЗОВА — ЗАПИСЬ СУЖДЕНИЯ, а не проход поллера. Запись суждения
# происходит ровно один раз на тройку (задача, поколение, kind): повтор
# отбивается 409 контракта #1022. Значит at-most-once достаётся даром, и
# карточка получает ровно одну строку об исходе — тогда как проход поллера
# писал бы её каждые тридцать секунд, пока задача стоит в review.
#
# ТРИ ЗАМКА ДО ЛЮБОГО ДЕЙСТВИЯ, и каждый умеет сказать «нет» в одиночку:
# 1. ``effective_mode`` — ЕДИНСТВЕННЫЙ читатель слова ``act`` (#1107).
#    ``STEWARD_MODE`` по умолчанию ``off``, а ``act`` не выдаёт даже
#    окружение: его выдаёт замер. Пока режим не ``act``, эта функция не
#    трогает ни базу, ни карточку и возвращает ``None`` — поведение хаба
#    остаётся тем же, каким было до неё.
# 2. Политика проекта: гейт ``verdict`` делегирован стюарду (#743, #1151).
#    Проект, который никому ничего не делегировал, не получает автономии
#    оттого, что она появилась у соседнего.
# 3. Привратник применения (#1147, #1148): предусловия пакета, громкие
#    основания автовердикта, закрытие находок и ladder. Он спрашивается
#    ОТДЕЛЬНО от восьми признаков и раньше них, потому что отвечает на
#    другой вопрос — «есть ли вообще право применять», — и восемь признаков
#    его не заменяют: сошедшиеся свидетельства ничего не говорят про класс
#    риска и громкие основания.
#
# И только после всех трёх спрашивается само правило.


async def apply_self_approval(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[str, str] | None:
    """Применить правило к записанному approve-суждению. ``None`` — не применялось.

    Три разных ответа, и путать их нельзя. ``None`` означает «контур сюда не
    дотягивается»: режим не ``act`` либо проект не делегировал вердикт — хаб
    ведёт себя ровно как до этой задачи и не пишет ничего. Кортеж с
    ``APPLIED`` — одобрение вынесено без человека. Кортеж с
    ``needs_decision`` — решает человек, и причина названа поимённо.
    """
    from hub.services.steward_dispatch import _policy_wants_steward
    from hub.services.steward_shadow import effective_mode

    if await effective_mode(db) != "act":
        return None
    project = await repo.resolve_project_for_task(db, task_id)
    if not _policy_wants_steward(project, gate="verdict"):
        return None

    from hub.services.steward_apply import apply_refusals

    refusals = await apply_refusals(db, task_id, generation)
    if refusals:
        await _hand_to_the_human_named(
            db,
            task_id,
            "; ".join(f"{code}: {detail}" for code, detail in refusals),
        )
        return ESCALATED_TO_HUMAN, (
            "привратник применения возражает — " + refusals[0][0]
        )

    decision = await self_approval_for(db, task_id, generation)
    return await approve_without_a_human(db, task_id, generation, decision)


async def _hand_to_the_human_named(
    db: aiosqlite.Connection, task_id: int, reason: str
) -> None:
    """Отказ привратника — в карточку своими словами, а не словами правила.

    Отдельно от ``_hand_to_the_human_because`` нарочно: там не сошлись
    СВИДЕТЕЛЬСТВА и их можно досдать, здесь возражает право применять — и
    досдавать нечего. Один текст на два разных случая сказал бы автору, что
    он видит одно и то же.
    """
    await repo.add_task_update(
        db,
        task_id,
        _STEWARD_ACTOR,
        "status",
        "Самостоятельного одобрения не будет — решает человек. "
        "Привратник применения возражает: " + reason + ".",
        author_kind="hub",
    )
    await db.commit()
