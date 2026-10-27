"""Маршруты хостов исполнителя (#1712).

Вынесены из ``hub/app.py``: у того бюджет размера (#1669) без запаса. Три
группы, и граница между ними — это и есть безопасность задачи:

* ``/api/admin/executor-hosts`` — завести, отключить, перечислить. Только
  человек, а cookie-сессия — только с верным CSRF, какой бы ни был глобальный
  режим (``_human_door``). Агент и ключ хоста сюда не проходят.
* ``/api/executor-hosts/{name}/orders…`` — то, что хост делает своим ключом
  роли slot_runner: забрать заказы, выкупить код, прочитать итог заказа, подтвердить,
  сообщить фазу и итог. Allowlist в ``hub/auth.py`` пускает ключ ровно на шесть
  маршрутов своего хоста (SLOT_RUNNER_ALLOWLIST); здесь
  то же проверяется по базе, потому что middleware знает только путь.
* параметры запуска исполнителя (``ExecutorLaunchRequest``) — на слот хоста.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError

from hub import config
from hub.auth import LoginRateLimiter, current_identity, require_human_or_admin
from hub.services import (
    executor_host_slots,
    executor_orders,
    host_redeem,
    launch_feedback,
    memory_wait,
)

router = APIRouter()

#: Потолок long-poll заказов: как у /api/events.
ORDERS_WAIT_MAX = 60
#: Одновременных long-poll одного хоста: законному хосту нужен один, остальные —
#: запас на перезапуск, когда старое соединение ещё не закрылось.
ORDERS_POLL_MAX = 3
_polling: dict[str, int] = {}
#: Чтений итога заказа одним хостом в окне (#1831); сверх — 429.
ORDER_READ_MAX = 60
ORDER_READ_WINDOW = 60
_read_limiter = LoginRateLimiter(ORDER_READ_MAX, ORDER_READ_WINDOW)
#: Фаз заказа от одного хоста в окне (#1817); сверх — 429.
PHASE_MAX = 30
PHASE_WINDOW = 60
_phase_limiter = LoginRateLimiter(PHASE_MAX, PHASE_WINDOW)


class ExecutorHostCreate(BaseModel):
    name: str
    public_key_pem: str
    expires_days: int = Field(default=executor_orders.HOST_KEY_DAYS, ge=1, le=365)


class ExecutorHostSlots(BaseModel):
    slots: int = Field(strict=True)


class ExecutorOrderAck(BaseModel):
    result: str


class ExecutorOrderRedeem(BaseModel):
    code: str


class ExecutorOrderPhase(BaseModel):
    phase: str
    # #1972: цифры памяти для фазы waiting_memory; тип не приводится, строгая
    # проверка целых - в memory_wait.validate_mib (строка, boolean, дробь - 422).
    need_mib: object = None
    free_mib: object = None


class ExecutorOrderReport(BaseModel):
    outcome: str
    sha: str = ""
    branch: str = ""
    note: str = ""
    reason_code: str = ""
    reason: str = ""


class ExecutorLaunchRequest(BaseModel):
    """Тело запуска исполнителя: цель и (для слота) задача (#1712)."""

    target: str = "cloud"
    task_id: int | None = None


async def launch_request(request: Request) -> ExecutorLaunchRequest:
    """Тело запуска; без тела — облако и голова очереди, как до #1712."""
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" not in ctype:
        return ExecutorLaunchRequest()
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not data:
        return ExecutorLaunchRequest()
    try:
        return ExecutorLaunchRequest.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            422,
            detail={
                "reason": "executor_launch_invalid",
                "message": "; ".join(
                    str(err.get("msg", "")) for err in exc.errors(include_url=False)
                ),
            },
        ) from exc


async def refuse_registered_slot(db: Any, channel: str, slot: str) -> None:
    """Ручная дверь chat-pair/start не выдаёт код слоту зарегистрированного хоста.

    У такого слота одна дверь — заказ хоста: его код привязан к заказу, и
    человеческий код мимо брони открыл бы второго исполнителя на слот.
    """
    if channel == "slot" and await executor_orders.get_host(db, slot):
        raise HTTPException(
            409,
            detail={
                "reason": "slot_host_registered",
                "message": f"используйте executor-launch target=slot:{slot}",
            },
        )


def _db(request: Request) -> Any:
    conn = getattr(request.state, "db", None)
    return conn if conn is not None else request.app.state.db


def _as_http(exc: executor_orders.HostError) -> HTTPException:
    return HTTPException(exc.status, detail=exc.detail)


async def _human(request: Request, what: str) -> config.TokenIdentity:
    # Лениво: hub.app импортирует этот модуль при сборке, а дверь живёт в нём.
    from hub.app import _human_door

    return await _human_door(request, what)


@router.post("/api/admin/executor-hosts")
async def api_executor_host_create(body: ExecutorHostCreate, request: Request) -> dict:
    """Завести хост: принципал slot_runner и ключ API, показанный ОДИН раз."""
    identity = await _human(request, "executor host creation")
    try:
        return await executor_orders.register_host(
            _db(request),
            name=body.name,
            public_key_pem=body.public_key_pem,
            created_by=identity.principal_id,
            expires_days=body.expires_days,
        )
    except executor_orders.HostError as exc:
        raise _as_http(exc) from exc


@router.post("/api/admin/executor-hosts/{name}/disable")
async def api_executor_host_disable(name: str, request: Request) -> dict:
    """Отключить хост: ключ отозван, коды сожжены, заказы и сессии закрыты."""
    identity = await _human(request, "executor host disabling")
    try:
        return await executor_orders.disable_host(
            _db(request), name, by=identity.principal_id
        )
    except executor_orders.HostError as exc:
        raise _as_http(exc) from exc


@router.post("/api/admin/executor-hosts/{name}/slots")
async def api_executor_host_slots(
    name: str, body: ExecutorHostSlots, request: Request
) -> dict:
    """#2107: сколько незакрытых заказов хост держит одновременно (1..4); только человек."""
    identity = await _human(request, "executor host slots")
    try:
        return await executor_host_slots.set_host_slots(
            _db(request), name, body.slots, by=identity.principal_id
        )
    except executor_orders.HostError as exc:
        raise _as_http(exc) from exc


@router.get("/api/admin/executor-hosts")
async def api_executor_hosts(
    request: Request, _identity=Depends(require_human_or_admin)
) -> dict:
    """Реестр хостов без ключей и без шифротекстов."""
    return {"hosts": await executor_orders.list_hosts(_db(request))}


async def _host_caller(request: Request, name: str) -> None:
    """Ключ хоста — и именно ЭТОГО хоста; всё прочее — 403.

    Агент и человек сюда не ходят: у них нет хоста. Ключ другого хоста не
    открывает чужих заказов. Allowlist в middleware уже сверил путь с именем
    принципала; это та же проверка по таблице хостов.
    """
    identity = current_identity(request)
    own = (
        await executor_orders.host_of_principal(_db(request), identity.principal_id)
        if identity.is_slot_runner
        else ""
    )
    if not own or own != name:
        raise HTTPException(
            403,
            detail={
                "reason": "slot_runner_only",
                "message": "маршрут принадлежит ключу хоста исполнителя этого хоста",
            },
        )


async def _has_unredeemed(db: Any, host: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM executor_orders WHERE host = ? "
        f"AND state IN {memory_wait.UNREDEEMED_IN_SQL} LIMIT 1",  # nosec B608 - constant
        (host,),
    )
    return await cursor.fetchone() is not None


@router.get("/api/executor-hosts/{name}/orders")
async def api_executor_host_orders(
    name: str,
    request: Request,
    wait: int = Query(default=0, ge=0, description="Long-poll секунд (до 60)"),
) -> dict:
    """Незакрытые невыкупленные заказы хоста; long-poll по образцу /api/events.

    Пока заказов нет, раз в секунду смотрит дешёвым SELECT и не берёт
    write-блокировку: писатели не ждут хост, который просто ждёт.
    """
    await _host_caller(request, name)
    db = _db(request)
    if _polling.get(name, 0) >= ORDERS_POLL_MAX:
        # Ключ одного хоста не должен копить сколько угодно открытых соединений.
        raise HTTPException(
            429,
            detail={
                "reason": "too_many_polls",
                "message": f"не больше {ORDERS_POLL_MAX} одновременных опросов хоста",
            },
        )
    _polling[name] = _polling.get(name, 0) + 1
    try:
        deadline = time.monotonic() + min(wait, ORDERS_WAIT_MAX)
        orders: list[dict[str, Any]] = []
        while True:
            if await _has_unredeemed(db, name):
                orders = await executor_orders.deliver_orders(db, name)
            if orders or time.monotonic() >= deadline:
                break
            await asyncio.sleep(1.0)
    finally:
        _polling[name] -= 1
        if not _polling[name]:
            del _polling[name]
    return {"host": name, "orders": orders}


@router.get("/api/executor-hosts/{name}/orders/{order_id}")
async def api_executor_order_read(name: str, order_id: int, request: Request) -> dict:
    """Итог заказа своего хоста: состояние, причина закрытия, задача (#1831).

    После сдачи сессии прогона отозваны (#1783), и сдачу хост проверяет этим
    чтением. Без кода, токенов и постановки. Чужой и несуществующий заказ — один 404.
    """
    await _host_caller(request, name)
    _read_limiter._max_attempts = ORDER_READ_MAX
    if _read_limiter.is_blocked(name):
        raise HTTPException(
            429,
            detail={
                "reason": "too_many_reads",
                "message": f"не больше {ORDER_READ_MAX} чтений итога в {ORDER_READ_WINDOW} с",
            },
        )
    _read_limiter.record(name)
    outcome = await executor_orders.read_order_outcome(_db(request), name, order_id)
    if outcome is None:
        raise HTTPException(404, detail={"reason": "order_not_found"})
    return outcome


@router.post("/api/executor-hosts/{name}/orders/{order_id}/redeem")
async def api_executor_order_redeem(
    name: str, order_id: int, body: ExecutorOrderRedeem, request: Request
) -> dict:
    """Выкуп кода заказа ключом хоста; повтор после потерянного ответа (#1825).

    Первый вызов — токен сессии; повтор в окне, пока токен не предъявлен, —
    новый токен той же сессии с большей ``token_version``. Тело ответа — секрет.
    """
    await _host_caller(request, name)
    try:
        return await host_redeem.redeem_for_host(
            _db(request), name, order_id, body.code
        )
    except executor_orders.HostError as exc:
        raise _as_http(exc) from exc


@router.post("/api/executor-hosts/{name}/orders/{order_id}/ack")
async def api_executor_order_ack(
    name: str, order_id: int, body: ExecutorOrderAck, request: Request
) -> dict:
    await _host_caller(request, name)
    try:
        return await executor_orders.ack_order(
            _db(request), name, order_id, body.result
        )
    except executor_orders.HostError as exc:
        raise _as_http(exc) from exc


@router.post("/api/executor-hosts/{name}/orders/{order_id}/phase")
async def api_executor_order_phase(
    name: str, order_id: int, body: ExecutorOrderPhase, request: Request
) -> dict:
    """Фаза прогона на хосте (#1817): только вперёд, у выкупленного заказа.

    Повтор той же — 200 без изменений; назад, до redeemed, после report или
    закрытия — 409; сверх квоты хоста — 429. В ленту задачи фаза не пишется.
    Исключение (#1972): ``waiting_memory`` с ``need_mib``/``free_mib`` - ДО выкупа,
    повтор - heartbeat, продлевающий заказ и код (предел 6 ч).
    """
    await _host_caller(request, name)
    _phase_limiter._max_attempts = PHASE_MAX
    if _phase_limiter.is_blocked(name):
        raise HTTPException(
            429,
            detail={
                "reason": "too_many_phases",
                "message": f"не больше {PHASE_MAX} фаз в {PHASE_WINDOW} с",
            },
        )
    _phase_limiter.record(name)
    try:
        return await launch_feedback.record_phase(
            _db(request),
            name,
            order_id,
            body.phase,
            need_mib=body.need_mib,
            free_mib=body.free_mib,
        )
    except executor_orders.HostError as exc:
        raise _as_http(exc) from exc


@router.post("/api/executor-hosts/{name}/orders/{order_id}/report")
async def api_executor_order_report(
    name: str, order_id: int, body: ExecutorOrderReport, request: Request
) -> dict:
    """Итог прогона хоста: сохраняется в заказе и показывается на карточке.

    В ленту задачи report не пишет (итоговую запись пишет закрытие заказа; для
    failed/rescued report закрывает заказ, и его текст становится этой записью).
    Статус задачи маршрут не меняет.
    """
    await _host_caller(request, name)
    try:
        return await executor_orders.report_outcome(
            _db(request),
            name,
            order_id,
            outcome=body.outcome,
            sha=body.sha,
            branch=body.branch,
            note=body.note,
            reason_code=body.reason_code,
            reason=body.reason,
            principal_id=current_identity(request).principal_id,
        )
    except executor_orders.HostError as exc:
        raise _as_http(exc) from exc
