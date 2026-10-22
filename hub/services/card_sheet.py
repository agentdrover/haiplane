"""Ответы листов действий карточки и защита от устаревшего экрана (#1813).

Лист отправляет форму на уже существующий маршрут (``web-approve``,
``web-reject``, ``web-review-verdict``, ``web-decide``, ``web-return-to-work``).
Новых маршрутов нет: декоратор ``sheet_action`` оборачивает обработчик и делает
две вещи.

1. Сверяет то, что человек видел на экране (поля ``expected_*`` формы листа),
   с живой задачей. Сверка стоит РЯДОМ с записью, а не до неё: reject, decide и
   return-to-work идут целиком внутри одного write-лока (``write_transaction``;
   сервисы уже умеют вложенный вызов и коммитят сами), approve кладёт ожидание
   в ``expected_state`` и сверяет его там, где сам сервис читает строку под
   локом (расчёт DoR с git под локом держать нельзя), вердикт сверяет
   ``expected_generation`` в своём сервисе (``verdict_guard``). Расхождение -
   409 с текстом, записей нет. Формы «Подробностей» полей ``expected_*`` не
   несут и ведут себя как раньше.
2. Для запроса листа (заголовки ``HX-Request`` и ``X-Hub-Sheet``) называет
   исход ФАКТОМ: «Отправлено» лист пишет только когда обработчик ответил
   фрагментом выполненного действия, и называет статус, который сейчас у
   задачи. Отказ (409, 422, DoR, заморозка) идёт текстом в лист с тем же кодом
   ответа; ввод на листе не трогается.

Лист - только для человека: запрос листа (заголовок или поле ``c2_sheet``)
от агентской личности получает 403, в том числе на маршруте вердикта.
Повтор вердикта, который ничего не записал (ревизия не выросла), листом
называется повтором, а не «Отправлено».

Весь текст, пришедший не из этого файла, экранируется.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import html
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from hub import repository as repo
from hub.auth import current_identity
from hub.db import write_transaction
from hub.mcp_envelope import compute_awaiting
from hub.services import expected_state

SHEET_HEADER = "x-hub-sheet"
#: Признак выполненного действия в ответе обработчиков (``_task_done_html``).
DONE_MARK = 'class="inbox-item-done"'
_AWAITING = {
    "none": "ничего не ожидается",
    "human_decision": "решение человека",
    "ci": "результат CI",
    "review": "ревью",
}
Note = Callable[[Any, int, Any], Awaitable[str]]


def is_sheet(request: Request) -> bool:
    return bool(request.headers.get(SHEET_HEADER)) and (
        request.headers.get("HX-Request") == "true"
    )


def _db(request: Request) -> Any:
    conn = getattr(request.state, "db", None)
    return conn if conn is not None else request.app.state.db


def _note(message: str, status_code: int) -> HTMLResponse:
    body = f'<div class="c2-sheet-note" role="alert">{html.escape(message)}</div>'
    return HTMLResponse(body, status_code=status_code)


def stale_reason(action: str, task: dict[str, Any], form: Any) -> str:
    """Почему экран устарел, или пусто. Сверяется только названное формой."""
    status = str(task.get("status") or "")
    if action in ("approve", "reject") and "expected_statement_generation" in form:
        seen = str(form.get("expected_statement_generation"))
        live = str(task.get("statement_generation") or 0)
        if status != "draft":
            return (
                f"Постановка уже не черновик: статус сейчас {status}. "
                "Действие не выполнено, повторно оно не отправлялось."
            )
        if seen != live:
            return (
                f"Постановка изменилась: на экране поколение {seen}, сейчас {live}. "
                "Ничего не записано; обновите страницу и перечитайте постановку."
            )
    if action in ("decide", "return") and "expected_status" in form:
        seen_status = str(form.get("expected_status"))
        if seen_status != status:
            return (
                f"Состояние задачи изменилось: на экране {seen_status}, сейчас "
                f"{status}. Ничего не записано; обновите страницу."
            )
        seen = str(form.get("expected_submission_generation", ""))
        live = str(task.get("submission_generation") or 0)
        if seen and seen != live:
            return (
                f"Пришла новая сдача: на экране поколение сдачи {seen}, сейчас "
                f"{live}. Ничего не записано; обновите страницу и перечитайте её."
            )
    return ""


def _detail_text(detail: Any) -> str:
    if isinstance(detail, dict):
        parts = [
            str(detail.get(k) or "").strip()
            for k in ("message", "hint")
            if detail.get(k)
        ]
        missing = detail.get("missing_required")
        if missing:
            parts.append("Не хватает: " + ", ".join(str(m) for m in missing) + ".")
        return " ".join(p for p in parts if p) or str(detail.get("reason") or "Отказ.")
    return str(detail).strip() or "Отказ."


async def done_response(db: Any, task_id: int, note: str = "") -> HTMLResponse:
    """«Отправлено» и то, что сейчас у задачи по данным хаба."""
    row = await repo.get_task(db, task_id)
    task = dict(row) if row else {}
    status = str(task.get("status") or "")
    awaiting = _AWAITING.get(str(compute_awaiting(status, task)), "")
    verdict = ""
    if task.get("review_verdict"):
        verdict = (
            f" Вердикт на карточке: {task['review_verdict']} (сдача "
            f"{task.get('review_verdict_generation')}, ревизия "
            f"{task.get('review_verdict_revision')})."
        )
    tail = f" {note}" if note else ""
    body = (
        '<div class="c2-sheet-done" role="status" data-c2-done="1">'
        "<strong>Отправлено.</strong> Хаб принял запрос; статус задачи сейчас "
        f"<code>{html.escape(status)}</code>"
        + (f", ожидается: {html.escape(awaiting)}" if awaiting else "")
        + f".{html.escape(verdict)}{html.escape(tail)}</div>"
    )
    return HTMLResponse(body)


def _body_text(response: Response) -> str:
    raw = getattr(response, "body", b"") or b""
    return raw.decode("utf-8", "replace")


def _as_refusal(response: Response) -> Response:
    """Ответ обработчика, не ставший выполненным действием, - отказом листа."""
    status = response.status_code if response.status_code >= 300 else 422
    text = _body_text(response)
    if "c2-sheet-note" in text:
        return HTMLResponse(text, status_code=status)
    wrapped = f'<div class="c2-sheet-note" role="alert">{text}</div>'
    return HTMLResponse(wrapped, status_code=status)


def _has_expectations(form: Any) -> bool:
    return any(str(key).startswith("expected_") for key in form)


def _same_verdict(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = ("review_verdict", "review_verdict_revision", "review_verdict_generation")
    return bool(before.get("review_verdict")) and all(
        before.get(k) == after.get(k) for k in keys
    )


async def _guarded(
    db: Any, action: str, task_id: int, form: Any, call: Callable[[], Awaitable[Any]]
) -> tuple[str, Any]:
    """Сверка ожиданий и запись под одним write-локом; ``(причина, ответ)``."""
    async with write_transaction(db):
        row = await repo.get_task(db, task_id)
        reason = stale_reason(action, dict(row), form) if row else ""
        if reason:
            return reason, None
        return "", await call()


async def _dispatch(
    db: Any, action: str, task_id: int, form: Any, call: Callable[[], Awaitable[Any]]
) -> tuple[str, Any]:
    if not _has_expectations(form):
        return "", await call()
    if action == "approve":
        row = await repo.get_task(db, task_id)
        reason = stale_reason(action, dict(row), form) if row else ""
        if reason:
            return reason, None
        seen = str(form.get("expected_statement_generation", ""))
        expected = int(seen) if seen.isdigit() else None
        with expected_state.expecting_statement_generation(expected):
            return "", await call()
    if action in ("reject", "decide", "return"):
        return await _guarded(db, action, task_id, form, call)
    return "", await call()


def _risk_refusal(form: Any) -> str:
    """Лист «Принять при высоком риске»: подтверждение и причина - на сервере."""
    if form.get("risk_ack") != "required":
        return ""
    if form.get("verdict") != "approved":
        return "Лист высокого риска записывает только «Принять»."
    if not form.get("confirm_risk"):
        return (
            "Нужно подтверждение: отметьте, что вы прочитали высокие риски и "
            "принимаете их. Вердикт не записан."
        )
    if not str(form.get("comments", "")).strip():
        return "Нужна причина, по которой риск приемлем. Вердикт не записан."
    return ""


def _risk_comment(form: Any) -> str:
    reason = str(form.get("comments", "")).strip()
    # Причина идёт первой: защита «текст против вердикта» читает начало текста.
    return f"{reason}\n\nПринято при высоком риске; риск подтверждён человеком."


def _refuse(request: Request, task_id: int, message: str, status: int) -> Response:
    if is_sheet(request):
        return _note(message, status)
    return RedirectResponse(
        f"/tasks/{task_id}?review_error={quote(message)}", status_code=303
    )


#: Замки по задаче: два нажатия одного листа не идут параллельно. Сервисы
#: коммитят до сети (диспетчер, мерж), и без замка второй запрос проходил бы
#: тот же гард, пока первый ещё в сети (rework запустил бы задание дважды), а
#: повтор вердикта не отличался бы от первой записи. Замок процесса: хаб
#: работает одним процессом; записи другого процесса закрывает write-лок базы.
_LOCKS: dict[int, list[Any]] = {}


@contextlib.asynccontextmanager
async def _task_lock(task_id: int):
    entry = _LOCKS.setdefault(task_id, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            _LOCKS.pop(task_id, None)


async def _outcome(
    request: Request,
    action: str,
    note: Note | None,
    before: dict[str, Any],
    response: Response,
) -> Response:
    """Ответ листа по ответу обработчика: выполнено, повтор или отказ."""
    task_id = int(request.path_params["task_id"])
    db = _db(request)
    if response.status_code >= 300 or DONE_MARK not in _body_text(response):
        return _as_refusal(response)
    after = dict(await repo.get_task(db, task_id) or {})
    if action == "verdict" and _same_verdict(before, after):
        return _note(
            "Этот вердикт уже стоит на карточке (ревизия "
            f"{after.get('review_verdict_revision')}): повтор ничего не записал.",
            409,
        )
    extra = await note(db, task_id, await request.form()) if note else ""
    return await done_response(db, task_id, extra)


async def _serve(
    request: Request,
    action: str,
    note: Note | None,
    call: Callable[[], Awaitable[Response]],
) -> Response:
    task_id = int(request.path_params["task_id"])
    db = _db(request)
    sheet = is_sheet(request)
    form = await request.form()
    before = dict(await repo.get_task(db, task_id) or {})
    try:
        reason, response = await _dispatch(db, action, task_id, form, call)
    except HTTPException as exc:
        if sheet:
            return _note(_detail_text(exc.detail), exc.status_code)
        if exc.status_code == 409 and _has_expectations(form):
            return _refuse(request, task_id, _detail_text(exc.detail), 409)
        raise
    if reason:
        return _refuse(request, task_id, reason, 409)
    if not sheet:
        return response
    return await _outcome(request, action, note, before, response)


def sheet_action(action: str, *, note: Note | None = None):
    """Обернуть обработчик маршрута, к которому ведёт лист (см. модуль)."""

    def decorate(fn: Callable[..., Awaitable[Response]]):
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Response:
            request: Request = kwargs["request"]
            task_id = int(kwargs["task_id"])
            form = await request.form()
            marked = is_sheet(request) or "c2_sheet" in form
            if marked and not current_identity(request).is_human:
                return _refuse(
                    request, task_id, "Лист действий доступен только человеку.", 403
                )

            if marked and action == "verdict":
                refusal = _risk_refusal(form)
                if refusal:
                    return _refuse(request, task_id, refusal, 422)
                if form.get("risk_ack") == "required":
                    kwargs["comments"] = _risk_comment(form)

            async def call() -> Response:
                return await fn(*args, **kwargs)

            if not (marked or _has_expectations(form)):
                return await call()
            # Снимок «до» берётся под замком: повтор видит запись первого нажатия.
            async with _task_lock(task_id):
                return await _serve(request, action, note, call)

        return wrapper

    return decorate
