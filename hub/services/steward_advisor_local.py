"""Локальный советник стюарда на GLM через службу ревью (#1649, вариант Б).

Советник (#1601) — вторая модель другого семейства, которая читает пакет
доказательств судьи и отвечает concur или object. До этой задачи его запускал
только облачный агент Cursor и только на моделях Cursor; GLM (подписка z.ai)
запускается иначе — профилем ``advisor`` службы ревью (deploy/review-runner/),
в контейнере второй обёртки ``haiplane-advisor-run``. Прямого вызова API из
хаба нет: ответ приходит из СЕССИИ ``steward_advisor`` (HTTP на тот же вход,
что у облачного советника), а вывод прогона — только диагностика.

Что держит этот модуль, по порядку жизни прогона:

готовность
    модель запускаема локально, только если служба в heartbeat объявила
    профиль advisor с ТОЙ ЖЕ моделью, heartbeat свеж, а
    ``STEWARD_HUB_TOKEN`` разрешается в принципала. Подмены модели нет;
    слова советника о своей модели доказательством не считаются.
захват и supervisor
    старт ЗАХВАТЫВАЕТ строку заказа (``pending:``) условным UPDATE и
    возвращается: ждать очередь и CLI в проходе поллера нельзя. Остальное
    делает фоновая задача на СВОЁМ соединении. Код доступа выпускает только
    победитель захвата, и только после слота и последней проверки.
после очереди
    заново: строка открыта и захват наш, поколение и approve судьи те же,
    остаток окна не меньше срока контейнера, путь готов, выкладка не идёт.
    ``started_at``, ``agent_id='local:<id задания>'`` и модель пишутся ПОСЛЕ
    этого и ДО выпуска кода; запись проверяет rowcount.
код
    ``CHAT_PAIR_CODE_SECONDS`` (300 с) — на обмен, сессия — ``CHAT_PAIR_TTL_SECONDS``.
    Код выписывается в момент слота; не удалось выписать — запуск остановлен
    ДО публикации, запасного промта со старым кодом и запасного пути через
    stdout нет. Просроченный код не продлевается и платного повтора не
    порождает: заказ на поколение один (уникальный индекс), строка закрыта.
отмена
    любое закрытие строки (таймаут окна, вытеснение пересдачей, принятое
    суждение) просит службу снять прогон; остановка хаба отзывает задание.
    «Отменено» не доказывает остановку контейнера — последняя защита это
    ``--timeout`` обёртки; хаб фиксирует запрос и доступное подтверждение.
авария хаба
    при подъёме и на каждом проходе: открытая строка ``local:`` без живого
    supervisor — задание отзывается, строка закрывается с причиной; повторный
    запуск не покупается.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.integrations import local_reviewer
from hub.services.steward_dispatch import (
    KIND_ADVISOR,
    PENDING_PREFIX,
    RUN_JUDGED,
    RUN_NEVER_STARTED,
    RUN_OPEN,
    RUN_REFUSED,
    RUN_TIMEOUT,
    close_run,
)

log = logging.getLogger(__name__)

LOCAL_PREFIX = "local:"

EVENT_LOCAL_OUTCOME = "steward_local_advisor_outcome"
EVENT_LOCAL_CANCEL = "steward_local_advisor_cancel"

#: Названные исходы supervisor (код — начало closed_reason строки заказа).
OUTCOME_NO_JUDGEMENT = "local_cli_no_judgement"
OUTCOME_PUBLISH_REFUSED = "local_publish_refused"
OUTCOME_SERVICE_ERROR = "local_service_error"
OUTCOME_SUPERVISOR_ERROR = "local_supervisor_error"
OUTCOME_WINDOW_SHORT = "local_window_short"
OUTCOME_CONTAINER_TIMEOUT = "local_container_timeout"
OUTCOME_HUB_STOP = "local_hub_stop"
OUTCOME_HUB_RESTART = "local_hub_restart"
OUTCOME_NOTHING_TO_ANSWER = "advisor_nothing_to_answer"
OUTCOME_MINT_FAILED = "no_identity_channel"
REFUSED_LOCAL_MODEL_MISMATCH = "local_advisor_model_mismatch"

#: Запас к сроку контейнера при проверке остатка окна, секунд.
WINDOW_MARGIN_SEC = 0


def is_local_model(name: str) -> bool:
    """Это имя запускается ЛОКАЛЬНО: оно названо в настройке и не является облачным."""
    local = (config.STEWARD_ADVISOR_LOCAL_MODEL or "").strip()
    return (
        bool(local)
        and (name or "").strip() == local
        and local not in config.SUBSCRIPTION_LAUNCHABLE_MODELS
    )


def candidates() -> tuple[str, ...]:
    """Кандидаты в советники: список владельца, затем локальная модель, если её там нет."""
    listed = tuple(config.STEWARD_ADVISOR_MODELS)
    local = (config.STEWARD_ADVISOR_LOCAL_MODEL or "").strip()
    if (
        local
        and local not in listed
        and local not in config.SUBSCRIPTION_LAUNCHABLE_MODELS
    ):
        return listed + (local,)
    return listed


async def path_problem(db: aiosqlite.Connection) -> tuple[str, str] | None:
    """Почему локальный советник сейчас запустить нельзя: ``(код, текст)`` или ``None``.

    Три разных слова, не одно: незапускаемый транспорт (служба, профиль,
    возможности, heartbeat), несовпадение модели, нет принципала. Ни одно из
    них не ``undeclared_model``: объявлено всё, не готов путь.
    """
    from hub.services import steward_shadow as shadow
    from hub.services.steward_advisor import REFUSED_ADVISOR_NOT_LAUNCHABLE

    model = (config.STEWARD_ADVISOR_LOCAL_MODEL or "").strip()
    if not model:
        return (
            REFUSED_ADVISOR_NOT_LAUNCHABLE,
            "локальная модель советника не задана (STEWARD_ADVISOR_LOCAL_MODEL)",
        )
    problem = local_reviewer.advisor_path_problem(model)
    if problem is not None:
        kind, text = problem
        code = (
            REFUSED_LOCAL_MODEL_MISMATCH
            if kind == local_reviewer.PATH_MODEL_MISMATCH
            else REFUSED_ADVISOR_NOT_LAUNCHABLE
        )
        return (code, text)
    if await shadow.steward_principal_id(db) is None:
        return (
            OUTCOME_MINT_FAILED,
            "советнику нечем аутентифицироваться у хаба: нет принципала за "
            "STEWARD_HUB_TOKEN (или открытый режим)",
        )
    return None


# ---------------------------------------------------------------------------
# Реестр живых supervisor
# ---------------------------------------------------------------------------


@dataclass
class _Handle:
    run_id: int
    task_id: int
    generation: int
    model: str
    db_path: str
    live_db: aiosqlite.Connection | None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    #: queued (ждёт слот) → starting (слот взят, код/публикация) → running.
    phase: str = "queued"
    #: Кто и почему просит снять прогон — пишется в событие.
    reason: str = ""
    job: str = ""


# Ссылка на задачу держится намеренно: задача без ссылки может быть собрана
# сборщиком посреди работы (как _LOCAL_RUNS у ревью). Ключ — id строки заказа.
_HANDLES: dict[int, _Handle] = {}


def _forget_handle(handle: _Handle) -> None:
    """Убрать из реестра СВОЙ handle, а не любой с тем же id заказа.

    После возврата заказа в очередь (дренаж) следующий тик заводит нового
    supervisor под тем же ``run_id``, пока старый ещё завершается: безусловный
    ``pop`` стёр бы регистрацию живого нового, и восстановление сочло бы его
    сиротой (#1649, находка Codex).
    """
    if _HANDLES.get(handle.run_id) is handle:
        del _HANDLES[handle.run_id]


async def wait_for_local_advisors() -> None:
    """Дождаться supervisor этого процесса — их естественного конца. Для тестов."""
    while _HANDLES:
        tasks = [h.task for h in _HANDLES.values() if h.task is not None]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


def request_cancel(run_id: int, reason: str) -> bool:
    """Попросить снять прогон. ``True`` — supervisor был и просьба принята."""
    handle = _HANDLES.get(run_id)
    if handle is None or handle.task is None or handle.task.done():
        return False
    if handle.task is asyncio.current_task():
        return False
    handle.reason = handle.reason or reason
    # Событие ставится ВСЕГДА: публикация в потоке спрашивает его под замком
    # выкладки перед записью job.json (заказ, закрытый за время ожидания замка,
    # не публикуется).
    handle.cancel.set()
    if handle.phase != "running":
        # Задание ещё не опубликовано (очередь, проверка, выпуск кода):
        # снимать нечего, прерывается сама задача.
        handle.task.cancel()
    return True


async def on_run_closed(
    db: aiosqlite.Connection, run: dict[str, Any], status: str, reason: str
) -> None:
    """Строка заказа советника закрыта: снять локальную работу за ней (#1649).

    Единственная точка: все закрытия идут через ``close_run``. Живой supervisor
    получает просьбу; строка ``local:`` без supervisor (авария хаба) —
    задание отзывается по имени. Best effort: закрытие уже состоялось.
    """
    try:
        if request_cancel(int(run["id"]), f"{status}: {reason}"[:300]):
            return
        if int(run["id"]) in _HANDLES:
            return
        rows = await fetchall(
            db, "SELECT agent_id FROM steward_runs WHERE id=?", (run["id"],)
        )
        agent_id = str(dict(rows[0]).get("agent_id") or "") if rows else ""
        if agent_id.startswith(LOCAL_PREFIX):
            local_reviewer.withdraw_job_by_name("job-" + agent_id[len(LOCAL_PREFIX) :])
    except Exception:  # noqa: BLE001 - закрытие уже записано
        log.exception(
            "could not withdraw the local advisor job of run %s", run.get("id")
        )


# ---------------------------------------------------------------------------
# Старт: захват и фоновый supervisor
# ---------------------------------------------------------------------------


async def start_local_advisor(db: aiosqlite.Connection, order: dict[str, Any]) -> bool:
    """Захватить заказ и запустить supervisor. ``True`` — захват наш.

    Возвращается СРАЗУ: очередь на слот и CLI живут минуты, а зовёт поллер.
    Всё платное и необратимое (код доступа, задание) — в supervisor, после
    слота и последней проверки.
    """
    from hub.services import steward_shadow as shadow

    problem = await path_problem(db)
    if problem is not None:
        code, text = problem
        await shadow._refuse_transiently(db, order, code, text)
        return False
    if drain := local_reviewer.drain_refusal():
        await shadow._refuse_transiently(db, order, "drain", drain)
        return False

    claim = f"{PENDING_PREFIX}{order['id']}"
    cursor = await db.execute(
        "UPDATE steward_runs SET agent_id=? WHERE id=? AND agent_id='' AND status=?",
        (claim, order["id"], RUN_OPEN),
    )
    won = cursor.rowcount == 1
    # Коммит, а не откат, и там и там: на общем соединении откат проигравшего
    # снял бы захват победителя, ещё не закоммиченный.
    await db.commit()
    if not won:
        return False

    from hub.services.review_dispatch import _main_db_path

    path = await _main_db_path(db)
    handle = _Handle(
        run_id=int(order["id"]),
        task_id=int(order["task_id"]),
        generation=int(order["generation"]),
        model=(order.get("model") or "").strip(),
        db_path=path,
        live_db=None if path else db,
    )
    task = asyncio.create_task(_supervise(handle))
    handle.task = task
    _HANDLES[handle.run_id] = handle

    def _done(_task: asyncio.Task[None], own: _Handle = handle) -> None:
        _forget_handle(own)

    task.add_done_callback(_done)
    return True


async def _supervise(handle: _Handle) -> None:
    """Фон: слот → перепроверки → код → задание → итог. Исключение не роняет хаб."""
    conn = None
    try:
        if handle.db_path:
            from hub import db as db_module

            conn = await db_module.connect(handle.db_path)
        db = conn if conn is not None else handle.live_db
        if db is None:
            log.error("local advisor %s has nowhere to record", handle.run_id)
            return
        try:
            await _run(db, handle)
        except asyncio.CancelledError:
            await _after_cancel(db, handle)
            raise
        except Exception as exc:  # noqa: BLE001 - исход назван, хаб не падает
            log.exception("local advisor supervisor failed: run %s", handle.run_id)
            await _finish(
                db,
                handle,
                RUN_TIMEOUT,
                OUTCOME_SUPERVISOR_ERROR,
                f"исключение supervisor: {type(exc).__name__}: {exc}"[:300],
            )
    finally:
        if conn is not None:
            await conn.close()


async def _after_cancel(db: aiosqlite.Connection, handle: _Handle) -> None:
    """Задача прервана до публикации задания: назвать причину в ленте."""
    if handle.phase == "running" or handle.reason.startswith("hub_stop"):
        return
    try:
        await repo.insert_event(
            db,
            kind=EVENT_LOCAL_CANCEL,
            task_id=handle.task_id,
            actor="hub",
            payload={
                "run_id": handle.run_id,
                "reason": handle.reason or "cancelled",
                "published": False,
                "cancel_confirmed": None,
            },
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        log.warning("cancel of queued local advisor %s not recorded", handle.run_id)


async def _row(db: aiosqlite.Connection, run_id: int) -> dict[str, Any] | None:
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run_id,))
    return dict(rows[0]) if rows else None


async def _finish(
    db: aiosqlite.Connection,
    handle: _Handle,
    status: str,
    outcome: str,
    detail: str,
    **payload: Any,
) -> bool:
    """Закрыть строку с названным исходом и записать его в ленту. ``False`` — уже закрыта."""
    row = await _row(db, handle.run_id)
    if row is None or row.get("status") != RUN_OPEN:
        return False
    closed = await close_run(db, row, status, f"{outcome}: {detail}")
    if closed:
        await repo.insert_event(
            db,
            kind=EVENT_LOCAL_OUTCOME,
            task_id=handle.task_id,
            actor="hub",
            payload={
                "run_id": handle.run_id,
                "generation": handle.generation,
                "outcome": outcome,
                "status": status,
                "detail": detail,
                **payload,
            },
        )
        await db.commit()
    return closed


async def _release_claim(
    db: aiosqlite.Connection, handle: _Handle, code: str, detail: str, claim: str
) -> None:
    """Вернуть заказ в очередь на следующий тик: ничего не опубликовано и не оплачено."""
    from hub.services import steward_shadow as shadow

    row = await _row(db, handle.run_id)
    if row is None or row.get("status") != RUN_OPEN:
        return
    await db.execute(
        "UPDATE steward_runs SET agent_id='', started_at=NULL "
        "WHERE id=? AND status=? AND agent_id IN (?, ?)",
        (handle.run_id, RUN_OPEN, claim, LOCAL_PREFIX + handle.job),
    )
    await shadow._refuse_transiently(db, row, code, detail)


async def _remaining_sec(db: aiosqlite.Connection, run_id: int) -> int:
    rows = await fetchall(
        db,
        "SELECT CAST(ROUND((julianday(deadline_at) - julianday('now')) * 86400) "
        "AS INTEGER) AS left FROM steward_runs WHERE id=?",
        (run_id,),
    )
    return int(dict(rows[0]).get("left") or 0) if rows else 0


async def _nothing_to_answer(db: aiosqlite.Connection, handle: _Handle) -> str:
    """Почему отвечать больше не на что (пусто — есть на что), по свежим строкам."""
    task_row = await repo.get_task(db, handle.task_id)
    judge = await repo.get_steward_judgement(
        db, handle.task_id, handle.generation, "verdict"
    )
    if (
        task_row is None
        or dict(task_row).get("status") != "review"
        or int(dict(task_row).get("submission_generation") or 0) != handle.generation
        or judge is None
        or dict(judge).get("verdict") != "approve"
    ):
        return (
            "отвечать советнику не на что: сдача ушла из review, поколение "
            "сменилось или approve судьи нет"
        )
    return ""


async def _mint_code(db: aiosqlite.Connection, handle: _Handle) -> tuple[str, str]:
    """Одноразовый код под вид «советник»: ``(код, ошибка)``. Запасного пути нет."""
    from hub.services import chat_pair
    from hub.services import steward_shadow as shadow

    try:
        principal_id = await shadow.steward_principal_id(db)
        if principal_id is None:
            return "", "принципал за STEWARD_HUB_TOKEN не разрешился"
        code, _ttl = await chat_pair.issue_code(
            db,
            principal_id,
            kind="steward_advisor",
            bound_task_id=handle.task_id,
            bound_generation=handle.generation,
        )
    except Exception as exc:  # noqa: BLE001 - любой сбой выписки — остановка
        return "", f"выпуск кода доступа не удался: {type(exc).__name__}"
    return (code, "") if code else ("", "код доступа не выписан")


async def _recheck_at_slot(
    db: aiosqlite.Connection, handle: _Handle, claim: str
) -> tuple[int, int] | None:
    """Последняя проверка ПОСЛЕ очереди: ``(срок контейнера, остаток окна)`` или ``None``.

    Всё, что было правдой до очереди, могло перестать быть ею, пока чужое
    ревью занимало слот. ``None`` — запускать нельзя, и причина уже записана.
    """
    row = await _row(db, handle.run_id)
    if row is None or row.get("status") != RUN_OPEN or row.get("agent_id") != claim:
        return None  # заказ закрыт или захват снят — выпускать нечего
    stale = await _nothing_to_answer(db, handle)
    if stale:
        await _finish(db, handle, RUN_REFUSED, OUTCOME_NOTHING_TO_ANSWER, stale)
        return None
    problem = await path_problem(db)
    if problem is not None:
        await _release_claim(db, handle, problem[0], problem[1], claim)
        return None
    if drain := local_reviewer.drain_refusal():
        await _release_claim(db, handle, "drain", drain, claim)
        return None
    caps = local_reviewer.runner_capabilities()
    limit = caps.advisor_timeout_sec if caps is not None else 0
    left = await _remaining_sec(db, handle.run_id)
    if limit <= 0 or left < limit + WINDOW_MARGIN_SEC:
        await _finish(
            db,
            handle,
            RUN_NEVER_STARTED,
            OUTCOME_WINDOW_SHORT,
            f"остаток окна советника {max(left, 0)} с меньше срока контейнера "
            f"{limit} с: задание не опубликовано, код не выписан",
            remaining_sec=left,
            container_timeout_sec=limit,
        )
        return None
    return limit, left


async def _mark_started(
    db: aiosqlite.Connection, handle: _Handle, claim: str, limit: int, left: int
) -> bool:
    """``started_at``, ``agent_id='local:<id>'`` и модель — после слота, до кода.

    Переход к запуску — ОДИН условный UPDATE: строка открыта и захват наш,
    сдача всё ещё в review на ТОМ ЖЕ поколении, approve судьи на месте, а
    дедлайн не раньше «сейчас + срок контейнера». Проверка поколения раньше
    UPDATE оставляла окно: пересдача между ними, и код выпускался бы под
    старую сдачу (#1649, находка Codex). ``rowcount`` решает.
    """
    from hub.services import steward_shadow as shadow

    handle.job = secrets.token_hex(8)
    agent_id = LOCAL_PREFIX + handle.job
    cursor = await db.execute(
        "UPDATE steward_runs SET agent_id=?, model=?, "
        "started_at=strftime('%Y-%m-%d %H:%M:%f', 'now') "
        "WHERE id=? AND agent_id=? AND status=? "
        "AND deadline_at >= datetime('now', ?) "
        "AND EXISTS (SELECT 1 FROM tasks t WHERE t.id = steward_runs.task_id "
        "AND t.status = 'review' "
        "AND t.submission_generation = steward_runs.generation) "
        "AND EXISTS (SELECT 1 FROM steward_judgements j "
        "WHERE j.task_id = steward_runs.task_id "
        "AND j.generation = steward_runs.generation "
        "AND j.kind = 'verdict' AND j.verdict = 'approve')",
        (
            agent_id,
            handle.model,
            handle.run_id,
            claim,
            RUN_OPEN,
            f"+{int(limit) + WINDOW_MARGIN_SEC} seconds",
        ),
    )
    if cursor.rowcount != 1:
        await db.commit()
        await _start_lost(db, handle, claim, limit)
        return False  # проиграли: ни кода, ни задания
    await repo.insert_event(
        db,
        kind=shadow.EVENT_RUN_STARTED,
        task_id=handle.task_id,
        actor="hub",
        payload={
            "run_id": handle.run_id,
            "generation": handle.generation,
            "kind": KIND_ADVISOR,
            "agent_id": agent_id,
            "model": handle.model,
            "channel": "local",
            "container_timeout_sec": limit,
            "remaining_sec": left,
        },
    )
    await db.commit()
    return True


async def _start_lost(
    db: aiosqlite.Connection, handle: _Handle, claim: str, limit: int
) -> None:
    """Условный старт не прошёл: назвать, почему, если строка всё ещё наша и открыта."""
    row = await _row(db, handle.run_id)
    if row is None or row.get("status") != RUN_OPEN or row.get("agent_id") != claim:
        return  # закрыта или захват снят — причина уже записана тем, кто закрыл
    stale = await _nothing_to_answer(db, handle)
    if stale:
        await _finish(db, handle, RUN_REFUSED, OUTCOME_NOTHING_TO_ANSWER, stale)
        return
    left = await _remaining_sec(db, handle.run_id)
    await _finish(
        db,
        handle,
        RUN_NEVER_STARTED,
        OUTCOME_WINDOW_SHORT,
        f"остаток окна советника {max(left, 0)} с меньше срока контейнера "
        f"{limit} с к моменту старта: задание не опубликовано, код не выписан",
        remaining_sec=left,
        container_timeout_sec=limit,
    )


async def _recheck_before_publish(
    db: aiosqlite.Connection, handle: _Handle, limit: int
) -> bool:
    """Последняя проверка перед публикацией, уже ПОСЛЕ выпуска кода.

    Между стартом и публикацией лежит выпуск кода (await): сдача могла уйти,
    окно — сжаться. Дедлайн проверяется с запасом на срок контейнера.
    """
    row = await _row(db, handle.run_id)
    if (
        row is None
        or row.get("status") != RUN_OPEN
        or row.get("agent_id") != LOCAL_PREFIX + handle.job
    ):
        return False  # закрыта другим путём — просьба о снятии уже отправлена
    stale = await _nothing_to_answer(db, handle)
    if stale:
        await _finish(db, handle, RUN_REFUSED, OUTCOME_NOTHING_TO_ANSWER, stale)
        return False
    left = await _remaining_sec(db, handle.run_id)
    if left < limit + WINDOW_MARGIN_SEC:
        await _finish(
            db,
            handle,
            RUN_NEVER_STARTED,
            OUTCOME_WINDOW_SHORT,
            f"остаток окна советника {max(left, 0)} с меньше срока контейнера "
            f"{limit} с перед публикацией: задание не опубликовано",
            remaining_sec=left,
            container_timeout_sec=limit,
        )
        return False
    return True


async def _run(db: aiosqlite.Connection, handle: _Handle) -> None:
    from hub.services import steward_shadow as shadow
    from hub.services.review_dispatch import instance_base_url
    from hub.services.steward_advisor import _prompt

    claim = f"{PENDING_PREFIX}{handle.run_id}"
    async with local_reviewer._HOST_BUDGET:
        handle.phase = "starting"
        checked = await _recheck_at_slot(db, handle, claim)
        if checked is None:
            return
        limit, left = checked
        if not await _mark_started(db, handle, claim, limit, left):
            return
        # Код выписывается ЗДЕСЬ, в момент слота. Не вышло — остановка до
        # публикации; прежнего кода и запасного пути через stdout нет.
        code, mint_error = await _mint_code(db, handle)
        if mint_error:
            await _finish(
                db,
                handle,
                RUN_REFUSED,
                OUTCOME_MINT_FAILED,
                f"{mint_error} — запуск остановлен до публикации задания",
            )
            return
        hub_base = instance_base_url().rstrip("/")
        prompt = _prompt(
            handle.task_id,
            handle.generation,
            hub_base,
            shadow.delivery_block(handle.task_id, code, hub_base),
        )
        if not await _recheck_before_publish(db, handle, limit):
            return
        handle.phase = "running"
        result = await local_reviewer.run_advisor_job(
            prompt,
            name="job-" + handle.job,
            limit=limit,
            cancel=handle.cancel,
            model=handle.model,
        )
        await _settle(db, handle, result, code, LOCAL_PREFIX + handle.job)


async def _code_state(db: aiosqlite.Connection, code: str) -> str:
    """Что с кодом доступа: погашен ли. Диагностика исхода «без суждения»."""
    from hub.services import chat_pair

    rows = await fetchall(
        db,
        "SELECT redeemed_at FROM chat_pair_codes WHERE code_hash = ?",
        (chat_pair.hash_pair_code(chat_pair.normalize_pair_code(code)),),
    )
    if not rows:
        return "строки кода нет (убрана сборщиком или не выписана)"
    redeemed = dict(rows[0]).get("redeemed_at")
    return "погашен" if redeemed else "НЕ погашен (истёк или не использован)"


async def _settle(
    db: aiosqlite.Connection,
    handle: _Handle,
    result: local_reviewer.AdvisorJobResult,
    code: str,
    agent_id: str,
) -> None:
    """Итог задания → строка заказа. Принятое суждение не перезаписывается никогда."""
    judgement = await repo.get_steward_judgement(
        db, handle.task_id, handle.generation, "advisor"
    )
    run = result.run
    if result.aborted:
        await repo.insert_event(
            db,
            kind=EVENT_LOCAL_CANCEL,
            task_id=handle.task_id,
            actor="hub",
            payload={
                "run_id": handle.run_id,
                "reason": handle.reason or "cancelled",
                "published": False,
                "cancel_confirmed": None,
            },
        )
        await db.commit()
        return
    if result.drained:
        # Выкладка поставила маркер между проверкой и публикацией: ничего не
        # опубликовано. Это не ошибка готовности — заказ ждёт следующего тика.
        await _release_claim(
            db, handle, "drain", result.reason, f"{PENDING_PREFIX}{handle.run_id}"
        )
        return
    if run is not None and run.cancelled:
        await repo.insert_event(
            db,
            kind=EVENT_LOCAL_CANCEL,
            task_id=handle.task_id,
            actor="hub",
            payload={
                "run_id": handle.run_id,
                "reason": handle.reason or "cancelled",
                "published": True,
                "cancel_confirmed": bool(run.cancel_confirmed),
                "judgement_present": judgement is not None,
            },
        )
        await db.commit()
        # Строку закрыл тот, кто просил снять; остановку хаба закрывает
        # cancel_local_advisors.
        return
    if judgement is not None:
        # Суждение принято по HTTP до конца CLI: rc≠0 после него ничего не
        # перезаписывает. Если строка ещё открыта — закрыть как принятую.
        row = await _row(db, handle.run_id)
        if row is not None and row.get("status") == RUN_OPEN:
            await close_run(db, row, RUN_JUDGED, "суждение записано: advisor")
        await repo.insert_event(
            db,
            kind=EVENT_LOCAL_OUTCOME,
            task_id=handle.task_id,
            actor="hub",
            payload={
                "run_id": handle.run_id,
                "outcome": "judged",
                "cli_rc": run.rc if run is not None else None,
            },
        )
        await db.commit()
        return
    if not result.published:
        await _finish(
            db,
            handle,
            RUN_REFUSED,
            OUTCOME_PUBLISH_REFUSED,
            result.reason or "отказ публикации",
        )
        return
    if run is None:
        await _finish(
            db,
            handle,
            RUN_REFUSED,
            OUTCOME_SERVICE_ERROR,
            result.reason or "служба-исполнитель не вернула результат",
        )
        return
    if run.timed_out:
        await _finish(
            db,
            handle,
            RUN_TIMEOUT,
            OUTCOME_CONTAINER_TIMEOUT,
            f"прогон снят по сроку ({run.duration_ms // 1000} с); подтверждение "
            f"снятия: {run.cancel_confirmed}",
            cancel_confirmed=run.cancel_confirmed,
        )
        return
    await _finish(
        db,
        handle,
        RUN_TIMEOUT,
        OUTCOME_NO_JUDGEMENT,
        f"CLI завершился (rc={run.rc}, {run.duration_ms // 1000} с) без суждения из "
        f"сессии советника; код доступа: {await _code_state(db, code)}. Вывод "
        "прогона — диагностика, ответом не считается",
        cli_rc=run.rc,
        agent_id=agent_id,
    )


# ---------------------------------------------------------------------------
# Остановка и авария хаба
# ---------------------------------------------------------------------------


async def cancel_local_advisors() -> None:
    """Остановка хаба: отозвать задания и закрыть строки. Ждать подтверждений нельзя."""
    handles = [h for h in _HANDLES.values() if h.task is not None]
    for handle in handles:
        handle.reason = handle.reason or "hub_stop"
        if handle.task is not None:
            handle.task.cancel()
    if not handles:
        return
    await asyncio.gather(*[h.task for h in handles if h.task], return_exceptions=True)
    for handle in handles:
        await _close_stopped(handle)


async def _close_stopped(handle: _Handle) -> None:
    conn = None
    try:
        if handle.db_path:
            from hub import db as db_module

            conn = await db_module.connect(handle.db_path)
        db = conn if conn is not None else handle.live_db
        if db is None:
            return
        row = await _row(db, handle.run_id)
        if row is None or row.get("status") != RUN_OPEN:
            return
        started = bool(row.get("started_at"))
        await close_run(
            db,
            row,
            RUN_TIMEOUT if started else RUN_NEVER_STARTED,
            f"{OUTCOME_HUB_STOP}: остановка хаба — задание отозвано, службе "
            "отправлено снятие; подтверждения хаб не ждал (последняя защита — "
            "--timeout обёртки)",
        )
    except Exception:  # noqa: BLE001
        log.exception("local advisor %s not closed on stop", handle.run_id)
    finally:
        if conn is not None:
            await conn.close()


async def recover_local_advisor_runs(db: aiosqlite.Connection) -> int:
    """Открытые строки ``local:`` без живого supervisor: отозвать задание и закрыть.

    Хаб единственный процесс (проверено на проде), поэтому пустой реестр при
    открытой строке — это след умершего процесса, а не соседа. Повторный запуск
    не покупается: строка закрывается, а уникальный индекс не даёт заказать
    второй раз. Существующее суждение сохраняется — строка закрывается как
    принятая.
    """
    rows = await fetchall(
        db,
        "SELECT * FROM steward_runs WHERE kind=? AND status=? AND agent_id LIKE ?",
        (KIND_ADVISOR, RUN_OPEN, f"{LOCAL_PREFIX}%"),
    )
    recovered = 0
    for raw in rows:
        run = dict(raw)
        handle = _HANDLES.get(int(run["id"]))
        if handle is not None and handle.task is not None and not handle.task.done():
            continue
        local_reviewer.withdraw_job_by_name(
            "job-" + str(run["agent_id"])[len(LOCAL_PREFIX) :]
        )
        judgement = await repo.get_steward_judgement(
            db, int(run["task_id"]), int(run["generation"]), "advisor"
        )
        if judgement is not None:
            closed = await close_run(db, run, RUN_JUDGED, "суждение записано: advisor")
        else:
            closed = await close_run(
                db,
                run,
                RUN_TIMEOUT,
                f"{OUTCOME_HUB_RESTART}: хаб упал или перезапущен во время локального "
                "прогона советника — задание отозвано, повторный запуск не "
                "покупается",
            )
        if closed:
            await repo.insert_event(
                db,
                kind=EVENT_LOCAL_OUTCOME,
                task_id=int(run["task_id"]),
                actor="hub",
                payload={
                    "run_id": run["id"],
                    "generation": run["generation"],
                    "outcome": OUTCOME_HUB_RESTART,
                    "agent_id": run["agent_id"],
                    "judgement_kept": judgement is not None,
                },
            )
            await db.commit()
            recovered += 1
    return recovered + await _recover_cancel_debt(db)


#: Закрытые строки не старше этого читаются на «долг отмены» (сутки с запасом).
CANCEL_DEBT_WINDOW = "-2 days"


async def _recover_cancel_debt(db: aiosqlite.Connection) -> int:
    """Закрытая строка ``local:``, за которой осталось неотозванное задание.

    Закрытие строки коммитится РАНЬШЕ просьбы о снятии (``on_run_closed``):
    авария между ними оставляла задание живым, а восстановление смотрело
    только на открытые строки. Признак долга — сама очередь: задание строки
    всё ещё лежит с ``job.json`` или промтом. Его отзыв идемпотентен, а
    отозванное долгом не считается, поэтому проход повторов не плодит.
    """
    rows = await fetchall(
        db,
        "SELECT * FROM steward_runs WHERE kind=? AND status != ? "
        "AND agent_id LIKE ? AND closed_at >= datetime('now', ?)",
        (KIND_ADVISOR, RUN_OPEN, f"{LOCAL_PREFIX}%", CANCEL_DEBT_WINDOW),
    )
    withdrawn = 0
    for raw in rows:
        run = dict(raw)
        handle = _HANDLES.get(int(run["id"]))
        if handle is not None and handle.task is not None and not handle.task.done():
            continue  # живой supervisor сам снимет и подтвердит
        name = "job-" + str(run["agent_id"])[len(LOCAL_PREFIX) :]
        if not local_reviewer.job_needs_withdrawal(name):
            continue
        local_reviewer.withdraw_job_by_name(name)
        await repo.insert_event(
            db,
            kind=EVENT_LOCAL_CANCEL,
            task_id=int(run["task_id"]),
            actor="hub",
            payload={
                "run_id": run["id"],
                "reason": "cancel_debt_recovered: строка закрыта, а задание "
                "осталось (авария между закрытием и просьбой о снятии)",
                "published": True,
                "cancel_confirmed": None,
            },
        )
        await db.commit()
        withdrawn += 1
    return withdrawn
