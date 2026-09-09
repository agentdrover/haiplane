"""Отчёт разбора находок: сток, поток и precision с размером выборки (#1171).

Механика разбора построена целиком и не использована ни разу. Считать заново
здесь нечего — факт касания даёт ``finding_evidence`` (#1039), очередь и её
счётчик живут в ``repository`` одним фрагментом (#1038), а precision и
``by_profile`` заполняются сами, как только ``judged`` перестаёт быть нулём
(#876). Этот модуль отвечает на вопросы, которых ни одна из тех частей не
задаёт:

* **Сток и поток — два разных числа, и они не складываются.** «В очереди за всё
  время N» — это запас: находка, не отвеченная в апреле, не отвечена и
  сегодня, и окно тут скрыло бы ровно самое старое. «Покрытие отчётов за окно:
  X из Y» — поток. Свести их в одну дробь значит поделить одно на другое и
  получить число, не отвечающее ни на один из двух вопросов.

* **Число без размера выборки не печатается (#1153).** Две разобранные находки
  дают precision 1.0, и голая единица зовёт к решению, которого выборка не
  выдерживает. Ниже порога модуль называет НЕДОБОР, а не подставляет цифру:
  «мало данных» — это ответ, а «0.5 по двум находкам» — нет.

* **Неразобранное остаётся неразобранным (#549).** Находка, по которой факта
  нет — локатор не назван, sha не найден, клона нет, — считается отдельной
  строкой с ПРИЧИНОЙ. Дефолтная диспозиция ей не назначается ни при каком
  объёме очереди.

* **Слепая перепроверка — свойство разбора, а не украшение отчёта.** Сто
  находок подряд в одном заходе, и к концу очереди суждение становится
  штампом; поймать это можно только вторым суждением по случайной доле, у
  которого перед глазами нет первого. Выборка детерминированная — по тому же
  принципу, что и спот-чек дайджеста (#739): вторая перепроверка тех же
  находок обязана назвать те же uid, иначе о ней нельзя рассуждать.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.services.finding_evidence import OUTCOME_UNKNOWN, evidence_for_report
from hub.services.finding_identity import finding_uids
from hub.services.orchestration import practice_metrics

#: Сколько разобранных находок нужно, чтобы отвечать по профилю. Порог из
#: постановки #1171: ниже него ответ «deep против lite» не даётся, а называется
#: недобор — это законный исход, а не провал разбора.
MIN_JUDGED_PER_SLICE = 20

#: Доля слепой перепроверки, в десятых. 1 из 10 — те же ~10%, что и у
#: спот-чека дайджеста (#739), и по той же причине: меньше нечего сравнивать,
#: больше — это уже второй проход всей очереди, то есть удвоение цены.
_RECHECK_TENTHS = 1

#: Доля расхождений, выше которой разбор не засчитывается: он был штампом, а не
#: суждением. Число из постановки #1171 (AC-5).
RECHECK_DIVERGENCE_LIMIT = 0.2

#: Окно, которое означает «без окна». ``practice_metrics`` считает ставки
#: только за период, и другого способа спросить у НЕГО ЖЕ про всё время нет.
#: Сто лет — не магия, а отказ от второго расчёта: свой запрос по
#: ``finding_dispositions`` разошёлся бы с оконным в первый же месяц (#518).
ALL_TIME_DAYS = 36500


async def queue_snapshot(
    db: aiosqlite.Connection, *, project_id: int | None = None
) -> dict[str, Any]:
    """Снимок очереди: те же строки, что видит страница, плюс их uid.

    Знаменатель приёмки фиксируется ЗДЕСЬ и один раз. Очередь пополняется
    каждым новым отчётом, и приёмка «разобрано 95% очереди» без снимка
    измеряла бы гонку между разбором и конвейером, а не разбор.

    uid считается по ВСЕМУ списку подтверждённых находок отчёта, а не по
    строке очереди: у близнецов — одинаковые категория, файл, заголовок и
    строка — личность различает порядковый номер внутри отчёта (#1007), и по
    одной вынутой находке его не восстановить.
    """
    rows = [
        dict(r) for r in await repo.list_unjudged_findings(db, project_id=project_id)
    ]
    uid_by_slot = await _uids_by_slot(db, {int(r["review_id"]) for r in rows})
    items = []
    for row in rows:
        finding = _finding_of(row)
        items.append(
            {
                "uid": uid_by_slot.get(
                    (int(row["review_id"]), int(row["finding_index"])), ""
                ),
                "task_id": int(row["task_id"]),
                "review_id": int(row["review_id"]),
                "finding_index": int(row["finding_index"]),
                "category": str(row["category"]),
                "reported_at": str(row["reported_at"] or ""),
                "title": str(finding.get("title") or ""),
            }
        )
    return {"total": len(items), "items": items}


async def _uids_by_slot(
    db: aiosqlite.Connection, review_ids: set[int]
) -> dict[tuple[int, int], str]:
    """uid каждой подтверждённой находки, по (отчёт, позиция)."""
    out: dict[tuple[int, int], str] = {}
    for review_id in sorted(review_ids):
        row = await repo.get_machine_review(db, review_id)
        if row is None:
            continue
        confirmed = _confirmed_of(dict(row))
        for index, uid in enumerate(finding_uids(confirmed)):
            out[(review_id, index)] = uid
    return out


def _confirmed_of(review: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(review.get("findings_confirmed") or "[]")
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [f if isinstance(f, dict) else {} for f in parsed]


def _finding_of(row: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(str(row.get("finding") or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _slice_answer(counts: dict[str, Any], *, minimum: int) -> dict[str, Any]:
    """Строка отчёта по срезу: либо числа с выборкой, либо слово «недобор».

    Ключи ``precision``/``resolution_rate`` не исчезают при недоборе — они
    становятся ``None``, а рядом появляется ``shortfall``. Пропавший ключ
    читается как «ноль» ровно тем кодом, который и должен был остановиться.
    """
    judged = int(counts.get("judged") or 0)
    enough = judged >= minimum
    return {
        "judged": judged,
        "fixed": int(counts.get("fixed") or 0),
        "false_positive": int(counts.get("false_positive") or 0),
        "wont_fix": int(counts.get("wont_fix") or 0),
        "precision": counts.get("precision") if enough else None,
        "resolution_rate": counts.get("resolution_rate") if enough else None,
        "shortfall": (
            "" if enough else f"недобор: разобрано {judged}, нужно {minimum}"
        ),
    }


async def disposition_report(
    db: aiosqlite.Connection,
    *,
    since_days: int = 60,
    minimum: int = MIN_JUDGED_PER_SLICE,
    with_evidence: bool = True,
) -> dict[str, Any]:
    """Всё, что известно о разборе, названное своими именами.

    Числа берутся из ``practice_metrics`` — того же расчёта, что читает
    страница метрик. Второй расчёт «специально для отчёта» разошёлся бы с
    первым в первый же месяц (#518), и разошёлся бы молча.

    Разбор считается ДВАЖДЫ, и это не дублирование, а тот же разлад стока и
    потока, что уже разведён выше. ``practice_metrics`` берёт диспозиции по
    дате ОТЧЁТА: суждение, вынесенное сегодня о находке из отчёта
    трёхмесячной давности, в оконный срез не попадает вовсе. Ровно такой и
    была вся работа #1171 — очередь из 105 находок это запас, накопленный за
    год, — и отчёт, знающий только окно, показал бы «разобрано 0» на
    следующее утро после того, как разобрали весь сток. Поэтому оконный срез
    остаётся (по нему считает страница метрик, и AC-4 читает именно его), а
    рядом встаёт срез за всё время, названный своим именем.
    """
    windowed, disp = await _judged_slices(db, since_days=since_days, minimum=minimum)
    all_time, _ = await _judged_slices(db, since_days=ALL_TIME_DAYS, minimum=minimum)
    stock = await repo.count_unjudged_findings(db)
    return {
        "since_days": since_days,
        "minimum_per_slice": minimum,
        # Сток. Не оконный — и подписан так, чтобы окно из заголовка не
        # прочиталось как его окно.
        "stock": {
            "findings": int(stock["findings"]),
            "reports": int(stock["reports"]),
            "windowed": False,
        },
        # Поток. Оконный, и отчёт считается разобранным, только когда отвечены
        # ВСЕ его подтверждённые находки.
        "flow": {
            "reports_judged": int(disp["reports_judged"]),
            "reports_counted": int(disp["reports_counted"]),
            "since_days": since_days,
            "windowed": True,
        },
        # Разбор ЗА ОКНО. Ключи ``overall``/``by_profile``/``by_model`` лежат
        # на верхнем уровне с самого начала и остаются оконными: их читает
        # AC-4 и страница метрик.
        "overall": windowed["overall"],
        "by_profile": windowed["by_profile"],
        "by_model": windowed["by_model"],
        "judged_window": {**windowed, "since_days": since_days, "windowed": True},
        # Разбор ЗА ВСЁ ВРЕМЯ. Здесь виден разбор старого стока, который в
        # оконный срез не попадает и без этой строки читался бы как ноль.
        "judged_all_time": {**all_time, "windowed": False},
        "unknown": (await unknown_breakdown(db)) if with_evidence else None,
    }


async def _judged_slices(
    db: aiosqlite.Connection, *, since_days: int, minimum: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Срезы разбора за один период — и сырые числа, из которых они собраны.

    Сырые числа возвращаются ОТДЕЛЬНО, а не ключом внутри среза: вложенные,
    они попали бы в JSON отчёта третьей копией тех же цифр рядом с двумя
    посчитанными, и читателю пришлось бы догадываться, какая из трёх —
    ответ. Вызывающему они нужны за одним — ``reports_counted`` потока, — и
    брать их из уже сделанного расчёта дешевле, чем звать его второй раз.
    """
    metrics = await practice_metrics(db, since_days=since_days)
    disp = metrics["machine_reviews"]["dispositions"]
    slices = {
        "overall": _slice_answer(disp, minimum=minimum),
        "by_profile": [
            dict(_slice_answer(row, minimum=minimum), profile=row["profile"])
            for row in disp["by_profile"]
        ],
        "by_model": [
            dict(_slice_answer(row, minimum=minimum), model=row["model"])
            for row in disp["by_model"]
        ],
    }
    return slices, disp


async def first_judgements(db: aiosqlite.Connection) -> dict[str, str]:
    """ПЕРВОЕ суждение по каждому uid — материал слепой перепроверки (AC-5).

    Первое, а не любое. Один и тот же uid встречается больше одного раза
    законно: лестница ревью #879 повторяет находку на той же сдаче, а две
    задачи с одинаковыми категорией, заголовком и местом дают один uid — он
    считается из содержания, без ``review_id`` (#1007). ``list_judged_findings``
    отдаёт такие строки в порядке записи, и словарное включение по ним
    оставило бы ПОСЛЕДНЮЮ: перепроверка сверяла бы второе суждение с третьим,
    а штамп на первом проходе — то самое, ради чего она и заводится, —
    остался бы невидим. Заодно схлопывание занижало бы размер разобранного.

    Строки без uid — из времени до #1007 — вне перепроверки: сравнивать их не
    с чем, и молча складывать их в знаменатель значило бы разбавлять долю
    расхождений историей.
    """
    out: dict[str, str] = {}
    for row in await repo.list_judged_findings(db):
        uid = str(row["finding_uid"] or "")
        if uid and uid not in out:
            out[uid] = str(row["disposition"])
    return out


async def unknown_breakdown(db: aiosqlite.Connection) -> dict[str, Any]:
    """Сколько находок очереди судить НЕ ПО ЧЕМУ, и по какой причине.

    Это не диспозиция и не её замена: находка без факта остаётся в очереди
    неразобранной. Доля важна сама по себе — постановка #1171 называет 40%
    условием пересмотра: выше него история перестаёт быть основанием, и разбор
    переезжает в момент фикса.
    """
    rows = [dict(r) for r in await repo.list_unjudged_findings(db)]
    by_review: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_review.setdefault(int(row["review_id"]), []).append(row)
    reasons: dict[str, int] = {}
    unknown = 0
    for review_id, group in by_review.items():
        review = await repo.get_machine_review(db, review_id)
        if review is None:
            continue
        stored = dict(review)
        confirmed = _confirmed_of(stored)
        by_uid = await evidence_for_report(
            db,
            int(stored["task_id"]),
            confirmed,
            generation=int(stored["submission_generation"] or 0),
        )
        uids = finding_uids(confirmed)
        for row in group:
            index = int(row["finding_index"])
            if index >= len(uids):
                continue
            fact = by_uid.get(uids[index]) or {}
            if fact.get("outcome") != OUTCOME_UNKNOWN:
                continue
            unknown += 1
            reason = str(fact.get("reason") or "")
            reasons[reason] = reasons.get(reason, 0) + 1
    total = len(rows)
    return {
        "queued": total,
        "unknown": unknown,
        "share": round(unknown / total, 3) if total else None,
        "reasons": [
            {"reason": reason, "findings": count}
            for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])
        ],
    }


def recheck_sample(uids: list[str], salt: str) -> list[str]:
    """~10% разобранных находок для слепого второго суждения (#1171 AC-5).

    Детерминированно по (uid, salt), как выборка спот-чека (#739): вторая
    перепроверка тех же находок обязана назвать те же uid, иначе сравнивать
    два разбора не с чем. Минимум один — выборка из нуля перепроверкой не
    является.
    """
    universe = sorted(set(u for u in uids if u))
    if not universe:
        return []
    picked = [
        uid
        for uid in universe
        if int(hashlib.sha256(f"{uid}:{salt}".encode()).hexdigest(), 16) % 10
        < _RECHECK_TENTHS
    ]
    if not picked:
        picked = [
            min(
                universe,
                key=lambda uid: hashlib.sha256(f"{uid}:{salt}".encode()).hexdigest(),
            )
        ]
    return picked


def compare_recheck(
    first: dict[str, str], second: dict[str, str], sample: list[str]
) -> dict[str, Any]:
    """Насколько второе суждение разошлось с первым.

    Считаются только находки, ПО КОТОРЫМ ЕСТЬ ОБА ответа. uid из выборки, на
    который второго суждения не дали, — это не совпадение и не расхождение, а
    незаконченная перепроверка; он назван отдельной строкой ``unanswered``.
    Молча засчитывать такой uid как согласие значило бы улучшать метрику
    пропуском работы — ровно та подмена, из-за которой разбор и приходится
    перепроверять.
    """
    wanted = sorted(set(sample)) or sorted(set(second))
    paired = [uid for uid in wanted if uid in first and uid in second]
    diverged = [uid for uid in paired if first[uid] != second[uid]]
    share = round(len(diverged) / len(paired), 3) if paired else None
    return {
        "sample": len(wanted),
        "compared": len(paired),
        "diverged": len(diverged),
        "diverged_uids": sorted(diverged),
        "unanswered": [uid for uid in wanted if uid not in paired],
        "share": share,
        "limit": RECHECK_DIVERGENCE_LIMIT,
        "stamped": bool(share is not None and share > RECHECK_DIVERGENCE_LIMIT),
    }
