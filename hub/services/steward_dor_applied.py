"""Что происходит с ДРАФТОМ после суждения стюарда (#1161).

Отделено от привратника по тому же принципу, по которому #1149 отделена
от #1147: «можно ли применять» и «что именно произойдёт» ошибаются
по-разному, и функция, отвечающая на оба вопроса сразу, не проверяется по
половине. Здесь только второе.

ДРАФТ ОСТАЁТСЯ ДРАФТОМ. Ни одна ветка этого модуля не пишет в строку
задачи: ни статус, ни ``dor_passed``. Возврат — это сообщение, а не
переход. Соблазн завести статус вроде ``needs_rework`` велик именно
потому, что он выглядит аккуратнее, но каждый новый статус — это новая
ветка во всех досках, отчётах и переходах, и заводить её ради одного вида
возврата нельзя (прямое ограничение F6). Цена ошибки в другую сторону
названа в рисках задачи: сброшенный ``dor_passed`` выкидывает драфт из
выборки диспетчера (``status='draft' AND dor_passed=1``), и цикл F6
оборвался бы молча.

ЗАМЕЧАНИЯ СПИСКОМ, А НЕ ВПЕЧАТЛЕНИЕМ. Автору уходит перечень находок
суждения, каждая своей строкой и со своим ``finding_uid`` (#1007), —
формат уже есть, и второго не нужно. «Постановку надо доработать» —
это не замечание, а отказ без содержания: ответить на него автор не
может, поэтому возврат без единого слова не отправляется, а отклоняется
громко (#553).

ПОВЕРХНОСТЬ ТА ЖЕ, ЧТО У WATCHDOG. Запись кладётся туда же, куда
``_sweep_unrefined_drafts`` (#751) кладёт напоминание про непроработанный
драфт: ``task_updates`` с ``kind='alert'`` от ``hub``. Вторая поверхность
означала бы, что автор смотрит в два места, и одно из них будет забыто.
Пересечься эти две записи не могут: watchdog выбирает драфты БЕЗ
пройденного DoR, а до стюарда доходят только прошедшие.

ПОТОЛОК ПРОТИВ ХОЖДЕНИЯ ПО КРУГУ. Риск фичи назван прямо: стюард
начинает требовать формальной полноты, и драфт ходит туда-обратно.
Потолок стоит на РЕВИЗИИ постановки (``statement_generation``, #1156), а
не на числе заходов: ревизия не сдвинулась — значит автор постановку не
менял, и третье замечание тем же текстом ничего не добавит. Дальше решает
человек, и звать его никуда не нужно: драфт и так стоит в его очереди по
статусу (``_HUMAN_QUEUE_ACTIONS['draft']``) — ему сообщается, ПОЧЕМУ
стюард больше не пытается.

ОДНО ПРИМЕНЕНИЕ ЗА РАЗ. Проверка «уже возвращали?» и запись возврата
стоят в одной транзакции, взятой сразу на запись: без неё два применения
с разных соединений оба читают «ещё не возвращали» и оба пишут автору
полный перечень — потолок, ради которого задача и заводилась, не
срабатывает. Нечитаемые находки при этом отказ, а не пустой список:
«замечаний не было» про суждение, которое их содержит, — неправда о
собственных данных (#516).

ЧЕГО ЗДЕСЬ НЕТ. Одобрения драфта: снятие гейта DoR — предмет соседних
задач (привратник #1159, композиция с автопилотом #1157), и scope этой
задачи выводит их наружу. ``approve`` сюда доходит и отклоняется, а не
проходит молча: «применил одобрение» и «ничего не сделал» вызывающий
обязан различать.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.db import fetchall, log_activity, write_transaction
from hub.services.finding_identity import finding_uids
from hub.services.steward_dispatch import KIND_DOR

log = logging.getLogger(__name__)

#: Драфт вернулся автору: замечания отправлены, задача не двинулась.
RETURNED = "returned_to_author"
#: Потолок сработал: тот же текст, второй заход — дальше человек.
ESCALATED_TO_HUMAN = "human_decides"

#: События возврата и потолка. Своё имя у каждого, потому что считать их
#: придётся раздельно: индикатор задачи — «возвратов на один драфт», а
#: срабатывания потолка это условие пересмотра, а не та же величина.
#: В ``HUMAN_GATE_EVENT_KINDS`` не добавлены намеренно: там считаются
#: человеческие касания гейтов, а это действия стюарда.
EVENT_RETURNED = "steward_dor_returned"
EVENT_CEILING = "steward_dor_ceiling"

_STEWARD_ACTOR = "steward"

#: Находка без единого слова — ни заголовка, ни объяснения. Строку она всё
#: равно получает: молча выброшенное замечание автор не отличит от
#: замечания, которого не было.
_NO_TEXT = "судья не оставил текста"


async def apply_dor_judgement(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[str, str]:
    """Применить суждение о постановке этой ревизии. Возвращает (исход, деталь).

    Исход — что стало с драфтом, а не что решил судья: ``returned_to_author``
    когда замечания ушли автору, ``human_decides`` когда сработал потолок.
    Оба слова описывают задачу, оставшуюся драфтом: разница между ними в
    том, кто следующий её читает.

    Прав применять не проверяет: это вопрос привратника (#1159), и второй
    ответ на него здесь означал бы два места, где его можно решить
    по-разному.

    Решение целиком идёт под ``BEGIN IMMEDIATE`` (находка ревью 8fcfcf79).
    До этой правки статус, ревизия и события возврата читались вне всякой
    транзакции, а писалось всё несколькими ``await`` позже: два применения
    с разных соединений оба не видели ``steward_dor_returned`` и оба
    отправляли автору полный перечень — потолок не срабатывал, и автор
    получал два «первых» возврата. Под write-локом соперник либо успел до
    нас и виден в прочитанном, либо ждёт очереди и увидит уже записанное
    событие. Тот же приём, каким #1160 закрыл окно «человек одобрил, пока
    мы думали» (#238), и та же схема «проверил — вставил» (#1065).
    """
    async with write_transaction(db):
        outcome, detail, remarks_count = await _decide(db, task_id, generation)

    # Журнал активности коммитит сам, поэтому пишется ПОСЛЕ блока: внутри
    # он зафиксировал бы чужую незавершённую транзакцию — ровно та дыра,
    # ради которой блок и заведён.
    if outcome == RETURNED:
        await log_activity(
            db,
            "steward_dor_returned",
            f"Task #{task_id} draft returned to author "
            f"(revision {generation}, remarks {remarks_count})",
        )
    else:
        await log_activity(
            db,
            "steward_dor_ceiling",
            f"Task #{task_id} draft loop ceiling at revision {generation}",
        )
    return outcome, detail


async def _decide(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[str, str, int]:
    """Прочитать, решить и записать — одним блоком под write-локом.

    Отдельной функцией, а не телом ``async with``: отказы здесь выходят
    исключением, и блок обязан их откатить, а не дописать половину
    возврата — алерт без события автор прочитал бы как возврат, которого
    хаб не помнит.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        raise HTTPException(404, detail=f"задачи #{task_id} нет")
    task = dict(row)

    _refuse_if_the_draft_is_gone(task, task_id)
    _refuse_if_the_statement_moved(task, generation)

    judgement = await repo.get_steward_judgement(db, task_id, generation, KIND_DOR)
    if judgement is None:
        raise HTTPException(
            409,
            detail=(
                f"суждения о постановке на ревизию {generation} нет — применять нечего"
            ),
        )
    verdict = str(dict(judgement).get("verdict") or "")

    if verdict == "approve":
        raise HTTPException(
            409,
            detail=(
                "одобрение драфта здесь не применяется: возврат автору и "
                "снятие гейта DoR — разные задачи, и одобряет драфт "
                "привратник, а не этот модуль"
            ),
        )
    if verdict != "changes_requested":
        # escalate — это отказ судить, и применять в нём нечего. Отдельная
        # ветка на случай нового слова в словаре: незнакомый вердикт обязан
        # остановиться, а не пройти молча.
        raise HTTPException(
            409,
            detail=(
                f"вердикт {verdict!r} к драфту не применяется: "
                "применяется changes_requested"
            ),
        )

    remarks = _remarks(dict(judgement))
    if not remarks:
        raise HTTPException(
            409,
            detail=(
                "возврат без единого замечания — это «доработайте» без "
                "содержания: ответить на него автор не может, и драфт "
                "остался нетронутым"
            ),
        )

    if await _seen(db, task_id, generation, EVENT_CEILING):
        # Третий заход на ту же ревизию: человеку уже сказали. Второй
        # алерт тем же текстом выглядел бы новым событием, которого не
        # было.
        raise HTTPException(
            409,
            detail=(
                f"постановка на ревизии {generation} уже передана человеку — "
                "повторное применение ничего не меняет"
            ),
        )

    if await _seen(db, task_id, generation, EVENT_RETURNED):
        await _hand_to_the_human(db, task_id, generation, remarks)
        return (
            ESCALATED_TO_HUMAN,
            (
                f"постановка на ревизии {generation} не менялась с прошлого "
                "возврата — решает человек"
            ),
            len(remarks),
        )

    await _return_to_author(db, task_id, generation, remarks)
    return RETURNED, f"автору отправлено замечаний: {len(remarks)}", len(remarks)


def _refuse_if_the_draft_is_gone(task: dict[str, Any], task_id: int) -> None:
    """Суждение о постановке применяется к драфту и только к нему.

    Драфт мог быть одобрен или отклонён человеком, пока прогон думал:
    заказ проверяет статус под write-локом (#1160), но между суждением и
    его применением та же щель открывается заново. Замечание по
    постановке задачи, которую уже взяли в работу, автору нечем ответить —
    правится постановка только у драфта.
    """
    status = str(task.get("status") or "")
    if status == "draft":
        return
    raise HTTPException(
        409,
        detail=(
            f"задача #{task_id} больше не драфт (status={status!r}): "
            "замечания по постановке адресованы автору драфта, а этот "
            "текст уже прошёл гейт"
        ),
    )


def _refuse_if_the_statement_moved(task: dict[str, Any], generation: int) -> None:
    """Суждение о ПРОШЛОЙ редакции не применяется к нынешней.

    Тот же пин, что у применения вердикта (#1149) и у двери доказательств
    (#1120), только единица счёта здесь — ревизия постановки, а не
    поколение сдачи. Автор правит текст, пока судья читает предыдущий:
    замечания про AC, которого в постановке уже нет, — это не строгость, а
    ложь про живой текст.

    И это же отделяет «ревизия не менялась» от «суждение опоздало»: без
    проверки оба случая пришли бы в потолок, и драфт уходил бы к человеку
    из-за расторопности автора, а не из-за круга.
    """
    live = int(task.get("statement_generation") or 0)
    if live == generation:
        return
    raise HTTPException(
        409,
        detail=(
            f"суждение о ревизии {generation}, а живая ревизия постановки — "
            f"{live}: замечания описывают текст, которого в задаче уже нет"
        ),
    )


def _remarks(judgement: dict[str, Any]) -> list[str]:
    """Находки суждения — строками, по одной на замечание.

    Идентичность берётся у #1007 (``finding_uids``), а не считается здесь
    заново: автор и судья обязаны звать одну находку одним именем, иначе
    ответ автора не с чем сопоставить. Порядок — как в отчёте: он же
    порядок uid, выданных позиционно.

    Находка без заголовка и без объяснения строку всё равно получает: она
    бессодержательна, но выбросить её молча значило бы соврать про число
    замечаний. Отказ от возврата целиком — выше, и только когда таких
    находок ВСЕ.
    """
    entries = _findings(judgement.get("findings"))
    if not entries or not any(_has_text(entry) for entry in entries):
        return []
    uids = finding_uids(entries)
    return [_one_remark(entry, uid) for entry, uid in zip(entries, uids, strict=True)]


#: Поля, в которых у находки лежит «почему», в порядке предпочтения.
#: Контракт суждения (#1022) — ``list[dict]`` без схемы полей, и судьи
#: пишут объяснение по-разному: опубликованная в репозитории схема находок
#: (``MachineFinding``) зовёт его ``detail``, словарь стюарда — ``why``,
#: отклонённая находка — ``reason``. Разбор находок в ``steward_evidence``
#: читает title/detail/why/reason; читать здесь только ``why`` значило бы,
#: что находка ``{title, detail}`` доедет до автора без причины — ровно то,
#: что AC-1 запрещает, — а находка с одним ``detail`` будет сочтена пустой
#: и возврат отклонится «без единого замечания» (находка ревью e2db5890).
_WHY_FIELDS = ("why", "detail", "reason")


def _why(entry: Mapping[str, Any]) -> str:
    """Объяснение находки — из того поля, в которое его положил судья."""
    for field in _WHY_FIELDS:
        text = str(entry.get(field) or "").strip()
        if text:
            return text
    return ""


def _has_text(entry: Mapping[str, Any]) -> bool:
    """Есть ли в находке хоть одно слово для автора.

    Проверяется по полям, а не по готовой строке: строка содержит ещё и
    uid с местом, и «в ней что-то написано» перестало бы отличать
    замечание от его адреса.
    """
    return bool(str(entry.get("title") or "").strip() or _why(entry))


def _findings(raw: Any) -> list[Mapping[str, Any]]:
    """Находки суждения из хранилища или из памяти, без второго формата.

    В базе колонка — JSON-текст, у вызывающего в памяти это уже список.
    Функция, понимающая только одну из форм, — ровно тот случай, когда
    ``getattr`` над словарём возвращает None про каждое поле, о котором
    его спрашивают (#1007).

    Текст, который не разбирается, — отказ, а не пустой список: см. ниже.
    """
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "[]")
        except ValueError as broken:
            # Пустой список сюда не годится: выше он неотличим от суждения
            # без находок, и автор получил бы отказ «возврат без единого
            # замечания» про суждение, замечания в котором есть — их просто
            # не прочитали (правило честности #516, находка ревью 552e3b70).
            log.warning("steward dor judgement: findings are not JSON")
            raise HTTPException(
                409,
                detail=(
                    "находки суждения не прочитать: колонка findings — не "
                    f"JSON ({broken}). Это не «замечаний не было»: "
                    "применить такое суждение нечем, и формат чинится там, "
                    "где оно записано"
                ),
            ) from broken
    else:
        parsed = raw
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, Mapping)]


def _one_remark(entry: Mapping[str, Any], uid: str) -> str:
    """Одно замечание: имя находки, её место и объяснение.

    Место называется, когда оно известно, и молчит, когда нет:
    ``locator='none'`` — это ответ, а не незаполненное поле (#1007), и
    строка «поле: —» сообщила бы автору, что где-то потерялось значение.
    """
    title = str(entry.get("title") or "").strip()
    why = _why(entry)
    where = str(entry.get("file") or "").strip()
    line = entry.get("start_line")
    if where and line is not None:
        where = f"{where}:{line}"

    head = f"- [{uid}]"
    if where:
        head = f"{head} {where}"
    if not title and not why:
        return f"{head} — {_NO_TEXT}"
    if title and why:
        return f"{head} {title} — {why}"
    return f"{head} {title or why}"


async def _seen(
    db: aiosqlite.Connection, task_id: int, generation: int, kind: str
) -> bool:
    """Было ли уже событие ``kind`` про ЭТУ ревизию постановки.

    Читается событие, а не текст алерта: ревизия лежит в payload числом, и
    вычитывать её обратно из предложения значило бы разбирать прозу.
    Считать возвраты по строке задачи тоже нельзя — задача не двигается,
    в этом весь смысл возврата, и следа в ней не остаётся по замыслу.
    """
    rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE task_id=? AND kind=?",
        (task_id, kind),
    )
    for row in rows:
        payload = dict(row).get("payload") or "{}"
        try:
            parsed = json.loads(payload)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("generation") == generation:
            return True
    return False


async def _return_to_author(
    db: aiosqlite.Connection, task_id: int, generation: int, remarks: list[str]
) -> None:
    """Замечания автору — той же записью, какой пишет watchdog драфтов (#751).

    Не коммитит: алерт и событие ложатся в транзакцию вызывающего, и
    откат уносит оба. Возврат, о котором есть запись автору и нет события,
    сломал бы потолок — следующее применение сочло бы этот возврат первым.
    """
    listed = "\n".join(remarks)
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        (
            f"Стюард прочитал постановку (ревизия {generation}) и просит "
            f"правок. Замечания ({len(remarks)}):\n{listed}\n"
            "Драфт остался драфтом: статус и DoR не тронуты, править "
            "постановку автору — hub_refine_task. Одна и та же ревизия "
            "второй раз не читается: следующее чтение купит настоящая "
            "правка текста."
        ),
        author_kind="hub",
    )
    await repo.insert_event(
        db,
        kind=EVENT_RETURNED,
        task_id=task_id,
        actor=_STEWARD_ACTOR,
        payload={"generation": generation, "remarks": len(remarks)},
    )


async def _hand_to_the_human(
    db: aiosqlite.Connection, task_id: int, generation: int, remarks: list[str]
) -> None:
    """Потолок: тот же текст во второй раз — дальше решает человек.

    Статус по-прежнему не трогается, и звать человека отдельным переходом
    некуда: драфт стоит в его очереди по одному только статусу. Меняется
    адресат сообщения — оно объясняет, почему стюард больше не пытается, и
    называет решения, которые есть у человека.

    Как и возврат, не коммитит: транзакцию держит вызывающий.
    """
    listed = "\n".join(remarks)
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        (
            f"Стюард просит правок по постановке второй раз, а ревизия "
            f"осталась прежней ({generation}): текст с прошлого возврата не "
            "менялся, и то же замечание третий раз ничего не добавит. "
            f"Дальше решает человек — hub_approve_task одобрит драфт как "
            "есть, hub_reject_task отклонит. Замечания "
            f"({len(remarks)}):\n{listed}"
        ),
        author_kind="hub",
    )
    await repo.insert_event(
        db,
        kind=EVENT_CEILING,
        task_id=task_id,
        actor=_STEWARD_ACTOR,
        payload={"generation": generation, "reason": "statement_unchanged"},
    )


__all__ = [
    "ESCALATED_TO_HUMAN",
    "EVENT_CEILING",
    "EVENT_RETURNED",
    "RETURNED",
    "apply_dor_judgement",
]
