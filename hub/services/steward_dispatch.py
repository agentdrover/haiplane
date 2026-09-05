"""Who wakes the steward (#1073, epic #994).

The steward is not a session and does not wait for work. The hub does: the
poller ticks every thirty seconds, sees a submission whose generation carries
no judgement yet, and ORDERS a run. The run is ephemeral by construction —
one order, one judgement, one generation — and dies. What lives permanently
is this loop, not the agent.

That direction is the security property, not an implementation detail. A
judge that can start itself decides WHEN it judges, and the packet it reads
(#1074/#1075) stops being tied to a moment somebody else chose. So ordering
is a hub-only verb: the steward principal has two operations (#1021), and
neither of them is this one.

Five guards stand between a submission and an order, and each of them fails
toward today's human route rather than toward a run:

``STEWARD_MODE``
    off (or any unrecognised value) closes the dispatcher entirely;
at-most-once
    the unique index on (task_id, generation, kind) makes a second order
    impossible rather than unlikely — two ticks racing on one generation is
    the ordinary case, and a duplicate costs a second paid run;
daily cap
    twenty runs per project per UTC day; hitting it is `daily_cap` in the
    feed and the human route, never "checked and clean";
deadline
    ``review:client`` is a human-owned slot with no deadline of its own, so a
    hung run would sit there looking ordered forever. The slot has one;
nothing new (#1150)
    a resubmission that did not touch the places the previous generation's
    findings named buys no run at all. No new information, no new opinion —
    and unlike the other four, this one is about MONEY as much as order: the
    refusal costs nothing where the run costs 1.5-2.7M provider tokens for
    an answer already known.

What this module does NOT do is start the cloud agent. Ordering a run and
executing it are different jobs with different failure modes, and the second
one belongs to F3 (#997) along with the shadow table and the canaries. The
order is the contract between them: this module writes it, F3 picks it up.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services.project_policy import gate_policy_of

log = logging.getLogger(__name__)

KIND_VERDICT = "verdict"
# Второй вид работы того же диспетчера (#1160): суждение о ПОСТАНОВКЕ драфта,
# а не о сдаче. Второй вид, а не второй диспетчер — второй означал бы вторую
# суточную квоту и второй счётчик отказов, которые никто не сверяет с первыми.
#
# Считается ревизиями постановки (``statement_generation``, #1156), а не
# поколениями сдачи: у драфта сдач нет, и ``submission_generation`` у него
# навсегда ноль. Оба вида стоят на одном уникальном индексе
# (task_id, generation, kind) — поэтому каждый читатель этой таблицы обязан
# спрашивать kind: генерация 1 у вердикта и генерация 1 у DoR описывают
# РАЗНЫЕ вещи, и слот одного не смеет закрывать слот другого.
KIND_DOR = "dor"

RUN_OPEN = "open"
RUN_JUDGED = "judged"
RUN_TIMEOUT = "timeout"
RUN_SUPERSEDED = "superseded"
# Заказ, которого не было и не будет на эту генерацию (#1150): хаб решил, что
# пересдача не несёт новой информации. Строка в steward_runs, а не только
# событие в фиде — и это разница между «сказали» и «закрыли»: событие не
# мешает следующему тику решить иначе, а строка с этим ключом делает второй
# заказ на ту же генерацию невозможным по уникальному индексу (отчёт 212).
RUN_REFUSED = "refused"

# Refusal codes. They are the vocabulary of the escalate reasons the contract
# already closes over (#1022), so a refusal here and an escalation there mean
# the same thing by name rather than by resemblance.
REFUSED_MODE_OFF = "steward_off"
REFUSED_DAILY_CAP = "daily_cap"
REFUSED_ALREADY_ORDERED = "already_ordered"
REFUSED_NO_GENERATION = "no_generation"
REFUSED_NO_NEW_INFORMATION = "no_new_information"

EVENT_ORDERED = "steward_run_ordered"
EVENT_REFUSED = "steward_run_refused"
EVENT_CLOSED = "steward_run_closed"

_MODES = {"off", "shadow", "act"}


def configured_mode() -> str:
    """The raw value the environment carries, validated but not capped.

    ONE caller by design: steward_shadow.effective_mode, which is where act
    is granted or refused. Everyone else reads requested_mode and therefore
    cannot act on autonomy nobody measured.
    """
    mode = (config.STEWARD_MODE or "off").strip().lower()
    return mode if mode in _MODES else "off"


def requested_mode() -> str:
    """What the environment ASKS for, capped at shadow (#1107 review).

    An unknown value is ``off``: a mistyped drop-in must never be the thing
    that switches a contour on (#835).

    And ``act`` is never returned here. Autonomy is not a configuration
    value — it is a permission the measurement grants, and granting it needs
    the database (steward_shadow.effective_mode). Capping the synchronous
    reader is what makes forgetting that function harmless: a consumer that
    asks here gets shadow, which is today's behaviour, rather than autonomy
    nobody checked.
    """
    mode = configured_mode()
    return "shadow" if mode == "act" else mode


def steward_mode() -> str:
    """Backwards-compatible alias of :func:`requested_mode`."""
    return requested_mode()


def dispatcher_enabled() -> bool:
    return steward_mode() != "off"


async def _refuse(
    db: aiosqlite.Connection, task_id: int, reason: str, detail: str
) -> None:
    """Say no in the feed. A silent refusal is indistinguishable from a bug."""
    await repo.insert_event(
        db,
        kind=EVENT_REFUSED,
        task_id=task_id,
        actor="hub",
        payload={"reason": reason, "detail": detail},
    )
    await db.commit()


async def runs_today(db: aiosqlite.Connection, project_id: int | None) -> int:
    """Orders placed for this project within the current UTC day.

    A refusal (#1150) is not an order: it bought nothing and must not spend
    the cap that exists to bound what is bought.
    """
    rows = await fetchall(
        db,
        "SELECT COUNT(*) AS n FROM steward_runs "
        "WHERE project_id IS ? AND date(created_at) = date('now') "
        "AND status != ?",
        (project_id, RUN_REFUSED),
    )
    return int(dict(rows[0]).get("n") or 0) if rows else 0


async def open_run(
    db: aiosqlite.Connection, task_id: int, generation: int, kind: str = KIND_VERDICT
) -> dict[str, Any] | None:
    """The open order for this generation, or None."""
    rows = await fetchall(
        db,
        "SELECT * FROM steward_runs "
        "WHERE task_id=? AND generation=? AND kind=? AND status=?",
        (task_id, generation, kind, RUN_OPEN),
    )
    return dict(rows[0]) if rows else None


async def _revision_missing(
    db: aiosqlite.Connection, task_id: int, generation: int, kind: str
) -> str:
    """Почему платить не за что, или "" если ревизия есть.

    «Ревизия, которой нет» у двух видов работы означает разное, и разница
    не косметическая — она про ноль.

    ``verdict`` считает ПОКОЛЕНИЯ СДАЧИ, и они начинаются с единицы: ноль
    значит «сдачи не было», судить нечего.

    ``dor`` считает РЕВИЗИИ ПОСТАНОВКИ, и ноль у них законен — это первая
    редакция драфта, которую никто не правил. Не поколение, а ОТПЕЧАТОК
    отвечает здесь на вопрос «есть ли ревизия»: пустой означает, что базис
    не снят (#1166), и прогон лёг бы на ревизию, которую первая же пустая
    правка объявит другой. Диспетчер снимает базис до заказа, но проверка
    стоит в самой оплачиваемой операции — там, где её нельзя обойти,
    позвав ``order_run`` мимо диспетчера.
    """
    if kind != KIND_DOR:
        return "" if generation > 0 else "у задачи нет закреплённой сдачи"
    if generation < 0:
        return "поколение постановки отрицательно"
    row = await repo.get_task(db, task_id)
    if row is None:
        return "задачи нет"
    if not str(dict(row).get("statement_fingerprint") or ""):
        return (
            "базис постановки не снят: отпечаток пуст, и ревизии, за которую "
            "платят, ещё не существует"
        )
    return ""


async def order_run(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    kind: str = KIND_VERDICT,
) -> dict[str, Any] | None:
    """Place one order, or refuse with a named reason.

    Returns the order on success and None on every refusal — the caller has
    nothing to do either way, because a refusal is not a failure: it is the
    human route continuing to work exactly as it does today.
    """
    if not dispatcher_enabled():
        await _refuse(
            db,
            task_id,
            REFUSED_MODE_OFF,
            f"STEWARD_MODE={config.STEWARD_MODE!r} — контур закрыт",
        )
        return None
    missing = await _revision_missing(db, task_id, generation, kind)
    if missing:
        await _refuse(db, task_id, REFUSED_NO_GENERATION, missing)
        return None

    project = await repo.resolve_project_for_task(db, task_id)
    project_id = dict(project)["id"] if project is not None else None
    used = await runs_today(db, project_id)
    if used >= config.STEWARD_DAILY_CAP:
        await _refuse(
            db,
            task_id,
            REFUSED_DAILY_CAP,
            f"суточный потолок исчерпан: {used}/{config.STEWARD_DAILY_CAP} "
            "прогонов на проект за UTC-сутки — задача идёт человеческим маршрутом",
        )
        return None

    # The order and its uniqueness are one statement: a check-then-insert
    # would be exactly the race the index exists to lose.
    try:
        cursor = await db.execute(
            "INSERT INTO steward_runs "
            "(task_id, generation, kind, status, model, project_id, deadline_at) "
            "VALUES (?, ?, ?, ?, ?, ?, "
            "datetime('now', ?))",
            (
                task_id,
                generation,
                kind,
                RUN_OPEN,
                config.STEWARD_MODEL,
                project_id,
                f"+{config.STEWARD_RUN_DEADLINE_MIN} minutes",
            ),
        )
    except aiosqlite.IntegrityError:
        await _refuse(
            db,
            task_id,
            REFUSED_ALREADY_ORDERED,
            f"прогон на генерацию {generation} ({kind}) уже заказан",
        )
        return None

    run_id = cursor.lastrowid
    await repo.insert_event(
        db,
        kind=EVENT_ORDERED,
        task_id=task_id,
        actor="hub",
        payload={
            "run_id": run_id,
            "generation": generation,
            "kind": kind,
            "model": config.STEWARD_MODEL,
            "mode": steward_mode(),
        },
    )
    await db.commit()
    log.info("steward run ordered: task #%s gen %s kind %s", task_id, generation, kind)
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run_id,))
    return dict(rows[0]) if rows else None


async def close_run(
    db: aiosqlite.Connection, run: dict[str, Any], status: str, reason: str
) -> bool:
    """Close an OPEN slot and say why, in the feed as well as in the row.

    Only open (#1106 review): the poller reads the open slots, then walks
    them one by one, and a judgement can land in that gap. Writing by id
    alone let the deadline overwrite a slot that had already been judged —
    the answer was in the row and the state said the run timed out. A
    deadline may only close what is still unfinished.

    Returns False when the slot was already closed by someone else. That is
    not an error: it means the race resolved the other way, and the caller
    has nothing left to do.
    """
    cursor = await db.execute(
        "UPDATE steward_runs SET status=?, closed_reason=?, "
        "closed_at=datetime('now') WHERE id=? AND status=?",
        (status, reason, run["id"], RUN_OPEN),
    )
    if cursor.rowcount != 1:
        log.info("steward run %s already closed — %s not applied", run["id"], status)
        return False
    await repo.insert_event(
        db,
        kind=EVENT_CLOSED,
        task_id=run["task_id"],
        actor="hub",
        payload={
            "run_id": run["id"],
            "generation": run["generation"],
            "status": status,
            "reason": reason,
        },
    )
    await db.commit()
    return True


def _policy_wants_steward(project_row: Any | None, gate: str = "verdict") -> bool:
    """Does the project's own gate policy hand THIS gate to the steward (#743)?

    Один вопрос на два гейта, а не два похожих читателя: вердикт и DoR
    делегируются одним и тем же словом, и правило «нераспознанное значение
    читается как человек» (#835) у них общее. Второй читатель рядом с первым
    разъехался бы — и разъехался бы тот, который мягче.

    Resolution failures refuse toward the human, like every other read of this
    policy: a project that cannot be resolved has not asked for anything.

    Сравнение со строкой ``steward`` живёт ровно здесь и нигде больше — #1157
    заводит перечень делегирующих значений ключа ``dor`` и один читатель для
    него, и заменить придётся одно место, а не каждое употребление.
    """
    if project_row is None:
        return False
    return gate_policy_of(project_row).get(gate) == "steward"


async def _nothing_new_since(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> str:
    """Почему пересдача не несёт новой информации, или "" если несёт.

    Правило: нет новой информации — нет нового мнения. Агент, запущенный
    на пересдаче, где места находок не тронуты, прочитает тот же пакет,
    придёт к тому же выводу и вернёт то же суждение — полтора-два миллиона
    токенов провайдера за воспроизведение известного ответа. Хуже: два
    одинаковых суждения подряд читаются как подтверждение, хотя это одно
    суждение, посчитанное дважды.

    Отвечает ХАБ и отвечает диффом. Спросить об этом модель значило бы
    поменять проверяемый факт на мнение — и заплатить за мнение.

    Три случая пропускают дальше, и каждый по своей причине:

    * первая сдача — сравнивать не с чем;
    * у прошлой генерации не было подтверждённых находок — отказывать не за
      что: возвращали не по ним;
    * хотя бы про одну находку хаб НЕ СМОГ узнать, тронута ли она. «Не
      удалось посмотреть» — не «ничего не изменилось» (#762): здесь
      неизвестность стоит прогона, а не отказа, потому что цена ошибки
      несимметрична — лишний прогон стоит денег, пропущенная правка стоит
      суждения о коде, которого никто не судил.
    """
    from hub.services.finding_evidence import OUTCOME_UNTOUCHED, evidence_for_report

    previous = generation - 1
    if previous < 1:
        # Первой сдаче сравнивать не с чем. Строго говоря, эту строку можно
        # снять без изменения поведения: поколения начинаются с единицы
        # (order_due_runs отбирает submission_generation > 0), и запрос
        # отчётов нулевого поколения всегда пуст. Оставлена намеренно —
        # она отвечает на вопрос «почему первая сдача проходит» там, где
        # его задают, а не заставляет читателя выводить ответ из фильтра
        # в другой функции.
        return ""
    reports = await repo.machine_reviews_of_generation(db, task_id, previous)
    confirmed: list[dict[str, Any]] = []
    for report in reports:
        raw = dict(report).get("findings_confirmed")
        if isinstance(raw, str):
            try:
                entries = json.loads(raw or "[]")
            except ValueError:
                entries = []
        else:
            entries = raw or []
        confirmed.extend(e for e in entries if isinstance(e, dict))

    # Один страж на один случай. Здесь их было два: отдельная проверка
    # «подтверждённых находок нет» и проверка пустого результата ниже. Они
    # закрывали ровно одно и то же — evidence_for_report на пустом списке
    # возвращает пустой словарь, — и мутация, снимавшая первую, не меняла
    # поведения. Проверка, которую нельзя сломать по отдельности, не
    # проверяется по отдельности, и держать её значит держать код, про
    # который нельзя сказать, работает ли он.
    # Считаем до ЗАКРЕПЛЁННОГО sha этой сдачи, а не до вершины ветки
    # (#1150 ревью, отчёт 209). Имя ветки — движущаяся цель: к моменту тика
    # поллера она может стоять не там, где стояла сдача, и ответ описывал бы
    # код, которого никто не сдавал. Решение о сдаче читает то, что сдача
    # закрепила.
    task_row = await repo.get_task(db, task_id)
    pinned = ((dict(task_row).get("submission_sha") if task_row else "") or "").strip()
    if not pinned:
        # Нечего закреплять — нечего и сравнивать. Прогон покупается:
        # неизвестность стоит денег, а отказ по незнанию стоит суждения.
        return ""

    evidence = await evidence_for_report(
        db, task_id, confirmed, generation=previous, head=pinned
    )
    if not evidence:
        return ""
    outcomes = [str(blob.get("outcome") or "") for blob in evidence.values()]
    if any(outcome != OUTCOME_UNTOUCHED for outcome in outcomes):
        return ""
    return (
        f"пересдача не тронула места находок прошлой сдачи "
        f"(подтверждённых находок: {len(confirmed)}) — прогон вернул бы то же "
        "суждение, посчитанное второй раз"
    )


async def order_due_runs(db: aiosqlite.Connection) -> int:
    """Order a run for every submission that is waiting for one."""
    if not dispatcher_enabled():
        return 0
    ordered = 0
    rows = await fetchall(
        db,
        "SELECT id, submission_generation FROM tasks "
        "WHERE status='review' AND (review_job_id IS NULL OR review_job_id='') "
        "AND submission_generation > 0",
    )
    for row in rows:
        task = dict(row)
        task_id, generation = task["id"], task["submission_generation"] or 0
        project = await repo.resolve_project_for_task(db, task_id)
        if not _policy_wants_steward(project):
            continue
        if await open_run(db, task_id, generation) is not None:
            continue
        if await _settled(db, task_id, generation):
            continue
        # Пятый страж, и единственный, который экономит деньги: отказ ДО
        # заказа стоит ноль, отказ после — полный прогон (#1150).
        stale = await _nothing_new_since(db, task_id, generation)
        if stale:
            await _close_generation_as_refused(db, task_id, generation, stale)
            continue
        if await order_run(db, task_id, generation) is not None:
            ordered += 1
    return ordered


async def _close_generation_as_refused(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    detail: str,
    *,
    kind: str = KIND_VERDICT,
    reason: str = REFUSED_NO_NEW_INFORMATION,
) -> None:
    """Закрыть генерацию для заказа — строкой, а не только словом.

    Отчёт 212: отказ no_new_information писался событием, и событие ничего
    не запирало. Следующий тик считал всё заново, и стоило вычислению
    ответить иначе — например, git не смог прочитать дифф и «неизвестно»
    честно купило прогон, — как тот же заказ размещался на том же
    основании, которое минуту назад отвергли. Заказ на генерацию обязан
    быть решён один раз.

    Решение здесь — строка steward_runs со статусом refused. Она стоит на
    том же уникальном индексе (task_id, generation, kind), что и настоящие
    заказы: второй заказ на ту же генерацию невозможен, а не маловероятен —
    тем же приёмом, каким #1073 закрыл гонку двух тиков. Следующий тик
    видит закрытую строку в _settled и до вычисления не доходит.

    Событие пишется ОДИН раз — потому что путь сюда после строки закрыт, а
    не потому что кто-то помнит, что уже писал.
    """
    project = await repo.resolve_project_for_task(db, task_id)
    project_id = dict(project)["id"] if project is not None else None
    try:
        await db.execute(
            "INSERT INTO steward_runs "
            "(task_id, generation, kind, status, model, project_id, "
            "deadline_at, closed_reason, closed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, datetime('now'))",
            (
                task_id,
                generation,
                kind,
                RUN_REFUSED,
                config.STEWARD_MODEL,
                project_id,
                detail,
            ),
        )
    except aiosqlite.IntegrityError:
        # Генерация уже решена — заказом или отказом. Второй раз ничего не
        # говорим: строка уже стоит, и она и есть ответ.
        return
    await repo.insert_event(
        db,
        kind=EVENT_REFUSED,
        task_id=task_id,
        actor="hub",
        payload={
            "reason": reason,
            "detail": detail,
            "generation": generation,
            "kind": kind,
        },
    )
    await db.commit()


async def _settled(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    kind: str = KIND_VERDICT,
) -> bool:
    """Is this generation already decided — judged, timed out, superseded, refused?

    Anything that is not an OPEN slot counts: the question is not "did a
    judgement land" but "is there anything left to order here", and a
    refusal (#1150) answers it as firmly as a judgement does.

    Спрашивает KIND, потому что слот стоит на тройке (#1160). Без него
    вердиктный прогон на поколении 1 объявлял бы решённой ревизию 1
    ПОСТАНОВКИ — два разных счётчика, случайно совпавших числом, и драфт
    молча остался бы непрочитанным.
    """
    rows = await fetchall(
        db,
        "SELECT 1 FROM steward_runs WHERE task_id=? AND generation=? AND kind=? "
        "AND status != ? LIMIT 1",
        (task_id, generation, kind, RUN_OPEN),
    )
    return bool(rows)


async def close_finished_runs(db: aiosqlite.Connection) -> int:
    """Close slots that are over: overdue, or overtaken by a human.

    Runs even when the dispatcher is switched off. Turning the contour off
    must not leave slots hanging open — the switch stops new orders, it does
    not abandon the ones already placed.
    """
    closed = 0
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE status=?", (RUN_OPEN,))
    for row in rows:
        run = dict(row)
        task_row = await repo.get_task(db, run["task_id"])
        task = dict(task_row) if task_row is not None else {}
        # A human verdict on this very generation ends the run: the judgement
        # it was ordered for is no longer anybody's to make (#1022 gives such
        # a late judgement a 409, and this closes the slot behind it).
        # #1120 review: a resubmission ends the run too. Its subject stopped
        # being the thing under review, and a slot left open would hold the
        # daily cap and the evidence door for code nobody is judging any more.
        #
        # #1160: у DoR-прогона тот же довод, но ДРУГОЙ счётчик — правка
        # постановки, а не пересдача. Спросить у драфта submission_generation
        # значило бы сравнивать его ревизию с вечным нулём: слот не закрылся
        # бы никогда, а после первой же сдачи закрылся бы разом и не по делу.
        kind = str(run.get("kind") or KIND_VERDICT)
        moved_on = (
            "submission_generation" if kind == KIND_VERDICT else "statement_generation"
        )
        current_generation = int(task.get(moved_on) or 0)
        if task and current_generation > int(run["generation"]):
            await close_run(
                db,
                run,
                RUN_SUPERSEDED,
                (
                    f"работа пересдана: генерация {current_generation} вместо "
                    f"{run['generation']} — этот прогон судил другой код"
                    if kind == KIND_VERDICT
                    else f"постановку правили: ревизия {current_generation} "
                    f"вместо {run['generation']} — этот прогон читал другой текст"
                ),
            )
            closed += 1
            continue
        # Человеческий вердикт — про сдачу, и закрывает он слот сдачи.
        # DoR-апрув драфта человеком выглядит иначе (статус, а не вердикт),
        # и закрывать по нему нечего: сюда он не приходит.
        verdict_generation = task.get("review_verdict_generation")
        if task and kind == KIND_VERDICT and verdict_generation == run["generation"]:
            if await close_run(
                db,
                run,
                RUN_SUPERSEDED,
                "человеческий вердикт на эту генерацию — судить больше нечего",
            ):
                closed += 1
            continue
        overdue = await fetchall(
            db,
            "SELECT 1 FROM steward_runs WHERE id=? AND deadline_at <= datetime('now')",
            (run["id"],),
        )
        if overdue:
            if await close_run(
                db,
                run,
                RUN_TIMEOUT,
                "прогон не вернул суждение до дедлайна слота",
            ):
                closed += 1
    return closed


async def order_due_dor_runs(db: aiosqlite.Connection) -> int:
    """Заказать чтение постановки каждому драфту, который его ждёт (#1160).

    Второй вид работы того же диспетчера. Всё, что делает первый — общая
    суточная квота, at-most-once на слоте, отказ ДО старта, — здесь не
    повторено, а ПЕРЕИСПОЛЬЗОВАНО: заказ размещает та же ``order_run``, и
    потолок она считает по всей таблице, а не по своему виду.

    Порядок стражей — это порядок цен, и он единственный правильный:

    1. политика проекта: гейт DoR отдан стюарду, иначе драфт не наш;
    2. АВТОПИЛОТ, и до всего платного. Правило снимает драфты низких классов
       по формальным признакам (#584), и заказывать за деньги чтение того,
       что уже снято бесплатно, значит платить за сделанную работу. Здесь же
       и причина спрашивать его именно вызовом, а не копией условий: копия
       разъедется, и разъедется в сторону лишнего прогона;
    3. базис постановки — до заказа, потому что заказ на ревизию, которой
       нет, покупает вторую ревизию сам себе (см. ``baseline_if_absent``);
    4. слот и решённость — ровно те же, что у вердикта, но спрошенные с
       kind: числа двух счётчиков совпадают случайно;
    5. суждение на этой ревизии уже есть — отказ ДО старта, урок #1150:
       отказ до заказа стоит ноль, отказ после — полный прогон.
    """
    if not dispatcher_enabled():
        return 0
    from hub.services.auto_approve import maybe_auto_approve
    from hub.services.statement_generation import baseline_if_absent

    ordered = 0
    # Сужение — в ЗАПРОСЕ, а не фильтром в памяти: драфтов в базе больше,
    # чем задач в review, и тик поллера ходит по ним каждые тридцать секунд.
    rows = await fetchall(
        db,
        "SELECT id FROM tasks WHERE status='draft' AND dor_passed=1 AND archived=0",
    )
    for row in rows:
        task_id = int(dict(row)["id"])
        project = await repo.resolve_project_for_task(db, task_id)
        if not _policy_wants_steward(project, gate="dor"):
            continue
        if await maybe_auto_approve(db, task_id):
            # Драфт снят правилом и больше не драфт. Коммит — потому что
            # автопилот пишет в транзакции вызывающего (#584), а вызывающий
            # здесь мы.
            await db.commit()
            continue
        generation = await baseline_if_absent(db, task_id)
        await db.commit()
        if await open_run(db, task_id, generation, KIND_DOR) is not None:
            continue
        if await _settled(db, task_id, generation, KIND_DOR):
            continue
        judged = await repo.get_steward_judgement(db, task_id, generation, KIND_DOR)
        if judged is not None:
            await _close_generation_as_refused(
                db,
                task_id,
                generation,
                f"суждение о постановке на ревизии {generation} уже есть — "
                "текст с тех пор не менялся, и прогон вернул бы то же мнение, "
                "посчитанное второй раз",
                kind=KIND_DOR,
            )
            continue
        if await order_run(db, task_id, generation, KIND_DOR) is not None:
            ordered += 1
    return ordered


async def sweep_steward_runs(db: aiosqlite.Connection) -> None:
    """One poller pass: close what is over, order what is due, start what waits.

    Starting comes last on purpose. Closing first frees the daily budget of a
    slot that is already over, and ordering before starting means an order
    placed this tick is executed in the same pass rather than thirty seconds
    later — the judge is cheap to call and expensive to keep waiting.

    Вердикты заказываются раньше драфтов, и это решение про общую квоту
    (#1160): когда потолка на всех не хватает, упирается в него тот, кто
    просит вторым. Сдача, стоящая в review, ждёт суждения о коде, который
    уже написан; драфт подождёт до завтрашних суток дешевле. Если окажется,
    что драфты регулярно вытесняются — это разговор про величину квоты, и
    он записан в условии пересмотра задачи, а не решается здесь молча.

    The corridor check comes last of all (#1145): it reads judgements this
    same pass may have just recorded closing runs above, but touches no run
    and no order — it only ever writes an alert, never a mode.
    """
    from hub.services.steward_shadow import check_escalation_corridor, start_due_runs

    await close_finished_runs(db)
    await order_due_runs(db)
    await order_due_dor_runs(db)
    await start_due_runs(db)
    await check_escalation_corridor(db)
