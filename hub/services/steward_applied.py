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
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.models import ReviewVerdict, TaskReviewVerdict

log = logging.getLogger(__name__)

APPLIED = "applied"
RETURNED = "returned_to_running"
ESCALATED_TO_HUMAN = "needs_decision"

_STEWARD_ACTOR = "steward"


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
