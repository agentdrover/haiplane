"""Ответы листов действий карточки и защита от устаревшего экрана (#1813).

Лист отправляет форму на уже существующий маршрут (``web-approve``,
``web-reject``, ``web-review-verdict``, ``web-decide``, ``web-return-to-work``).
Новых маршрутов нет: декоратор ``sheet_action`` оборачивает обработчик и делает
две вещи.

1. Перед вызовом сверяет то, что человек видел на экране (поля ``expected_*``
   формы листа), с живой задачей. Расхождение - 409 с текстом, записей нет.
   Формы «Подробностей» этих полей не несут и ведут себя как раньше. Окно между
   сверкой и записью узкое; записывающие сервисы сами проверяют статус внутри
   своей транзакции, а сервис вердикта - поколение сдачи.
2. Для запроса листа (заголовки ``HX-Request`` и ``X-Hub-Sheet``) называет
   исход ФАКТОМ: «Отправлено» лист пишет только когда обработчик ответил
   фрагментом выполненного действия, и называет статус, который сейчас у
   задачи. Отказ (409, 422, DoR, заморозка) идёт текстом в лист с тем же кодом
   ответа; ввод на листе не трогается.

Весь текст, пришедший не из этого файла, экранируется.
"""

from __future__ import annotations

import functools
import html
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from hub import repository as repo
from hub.mcp_envelope import compute_awaiting

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


def sheet_action(action: str, *, note: Note | None = None):
    """Обернуть обработчик маршрута, к которому ведёт лист (см. модуль)."""

    def decorate(fn: Callable[..., Awaitable[Response]]):
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Response:
            request: Request = kwargs["request"]
            task_id = int(kwargs["task_id"])
            form = await request.form()
            row = await repo.get_task(_db(request), task_id)
            reason = stale_reason(action, dict(row), form) if row else ""
            if reason:
                if is_sheet(request):
                    return _note(reason, 409)
                return RedirectResponse(
                    f"/tasks/{task_id}?review_error={quote(reason)}", status_code=303
                )
            if not is_sheet(request):
                return await fn(*args, **kwargs)
            try:
                response = await fn(*args, **kwargs)
            except HTTPException as exc:
                return _note(_detail_text(exc.detail), exc.status_code)
            if response.status_code < 300 and DONE_MARK in _body_text(response):
                extra = await note(_db(request), task_id, form) if note else ""
                return await done_response(_db(request), task_id, extra)
            return _as_refusal(response)

        return wrapper

    return decorate
