"""Отказ web-кнопки читается в браузере (#1995).

Обработчик web-маршрута, получивший от сервиса отказ (HTTPException 400/409/422)
или не принявший форму (RequestValidationError), отвечал FastAPI-JSON без
charset: браузер показывал ``{"detail":...}`` кракозябрами, а человек не видел
ни причины, ни своего ввода. Здесь один писатель вместо правки каждого
маршрута: ``WebRefusalRoute`` ловит такой отказ у POST-маршрутов роутера
``hub.web`` и отвечает

* браузеру — страницей карточки задачи (переиспользуется ``implementer_error``
  карточки, введённое сохраняется в ``form_values``) либо, без задачи в пути,
  малой страницей ``web_refusal.html``;
* клиенту без ``Accept: text/html`` (curl, скрипт) — прежним JSON, но с
  ``charset=utf-8``: без него браузер и терминал читают UTF-8 как Latin-1;
* HTMX — фрагментом с ``HX-Retarget``/``HX-Reswap``: htmx не вставляет 4xx
  сам, правило ``htmx:beforeSwap`` в base.html разрешает вставку (как у CSRF 403
  в ``hub/csrf.py``).

Маршруты ``/api`` и ``/mcp`` сюда не попадают: их JSON-контракт прежний.
Обработчики, которые ловят свои отказы сами (implementer-start, executor-*,
review-verdict, archive), не меняются: их исключение до маршрута не доходит.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute

log = logging.getLogger("hub.web")

REFUSAL_STATUSES = frozenset({400, 409, 422})
_API_PREFIXES = ("/api", "/mcp")
_LEADS = {
    400: "Хаб отклонил запрос (400).",
    409: "Действие не выполнено: состояние задачи не позволяет его (409).",
    422: "Форма заполнена неверно (422).",
}
# Поля, которые не показывают обратно: служебные и секретные.
_NOT_ECHOED = frozenset({"csrf_token", "password", "token"})


def refusal_text(status: int, detail: Any) -> str:
    """Одна строка для человека: русская шапка по статусу и причина сервиса."""
    lead = _LEADS.get(status, f"Хаб отклонил действие ({status}).")
    if isinstance(detail, dict):
        parts = [
            str(detail.get("message") or "").strip(),
            str(detail.get("hint") or "").strip(),
        ]
        body = " ".join(p for p in parts if p)
        code = str(detail.get("reason") or "").strip()
        if code:
            body = f"{body} (код: {code})".strip()
    else:
        body = str(detail).strip()
    return f"{lead} {body}".strip()


def validation_text(exc: RequestValidationError) -> str:
    """Поля формы, которые не прошли разбор, по именам и в порядке запроса."""
    seen: list[str] = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query")]
        name = loc[-1] if loc else "?"
        if name not in seen:
            seen.append(name)
    return refusal_text(422, "не прошло проверку поле: " + ", ".join(seen))


def field_kind(error_type: str, *, present: bool) -> str:
    """missing / blank / too_long; прочее — имя типа ошибки pydantic."""
    if not present or error_type == "missing":
        return "missing"
    if "too_long" in error_type:
        return "too_long"
    if error_type in ("value_error", "string_too_short"):
        return "blank"
    return error_type or "invalid"


def log_field_refusals(
    request: Request, errors: Iterable[Any], form_keys: Iterable[str]
) -> None:
    """Тип отказа поля формы в лог: путь и поле, значения нет (#1995)."""
    keys = set(form_keys)
    logged: set[str] = set()
    for err in errors:
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query")]
        field = loc[0] if loc else "?"
        if field in logged:
            continue
        logged.add(field)
        log.warning(
            "web form refused: path=%s field=%s kind=%s",
            request.url.path,
            field,
            field_kind(str(err.get("type", "")), present=field in keys),
        )


def manual_recovery_validation_detail(errors: Iterable[Any]) -> str:
    """Explain the missing facts on the recovery form without logging values."""
    messages = []
    labels = {
        "reason": "Укажите причину возврата",
        "confirmed_process_stopped": "Подтвердите, что проверили остановку внешнего процесса",
        "verification": "Опишите, как и на каком хосте проверена остановка процесса",
        "expected_holder": "Обновите карточку: ожидаемый держатель отсутствует или некорректен",
        "expected_session": "Обновите карточку: ожидаемая сессия отсутствует или некорректна",
        "expected_claimed_at": "Обновите карточку: время захвата отсутствует или некорректно",
        "expected_last_update_id": "Обновите карточку: версия истории отсутствует или некорректна",
    }
    for error in errors:
        loc = error.get("loc", ())
        field = str(loc[0]) if loc else "?"
        if field in ("verification", "reason") and "too_long" in str(
            error.get("type", "")
        ):
            subject = (
                "описание проверки" if field == "verification" else "причину возврата"
            )
            message = f"Сократите {subject}: максимум 5000 символов"
        else:
            message = labels.get(field, "Проверьте поле формы")
        line = f"{message} ({field})"
        if line not in messages:
            messages.append(line)
    return "; ".join(messages)


async def _form_keys(request: Request) -> list[str]:
    try:
        return list((await request.form()).keys())
    except Exception:  # noqa: BLE001 - тело не разобрано: полей нет
        return []


async def _form_values(request: Request) -> dict[str, str]:
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - тело не разобрано: эхо необязательно
        return {}
    return {
        k: v for k, v in form.items() if isinstance(v, str) and k not in _NOT_ECHOED
    }


def _back_path(request: Request) -> str:
    parts = urlsplit(request.headers.get("referer", ""))
    return parts.path if parts.path.startswith("/") else "/"


def _htmx_fragment(status: int, text: str) -> Response:
    return HTMLResponse(
        '<div class="task-action-note task-action-note--warning" role="alert" '
        f'data-reason="web_refusal">{html.escape(text)}</div>',
        status_code=status,
        headers={"HX-Retarget": "#main", "HX-Reswap": "afterbegin"},
    )


def _json_refusal(status: int, detail: Any) -> Response:
    return JSONResponse(
        {"detail": jsonable_encoder(detail)},
        status_code=status,
        headers={"content-type": "application/json; charset=utf-8"},
    )


def _accept_qualities(header: str) -> dict[str, float]:
    """Медиатип -> q из Accept; битый q считается единицей, q=0 — «нельзя»."""
    found: dict[str, float] = {}
    for item in header.lower().split(","):
        media, *params = (part.strip() for part in item.split(";"))
        if not media:
            continue
        q = 1.0
        for param in params:
            name, _, value = param.partition("=")
            if name.strip() == "q":
                try:
                    q = max(0.0, min(1.0, float(value)))
                except ValueError:
                    q = 1.0
        found[media] = max(q, found.get(media, 0.0))
    return found


def _wants_html(request: Request) -> bool:
    """HTML — если text/html названа явно, не запрещена (q=0) и не хуже JSON.

    ``*/*`` HTML не выбирает: curl и скрипты без явного text/html получают
    прежний JSON. JSON берёт q из явной записи, иначе из ``*/*``.
    """
    q = _accept_qualities(request.headers.get("accept", ""))
    html_q = q.get("text/html", q.get("text/*", 0.0))
    json_q = q.get("application/json", q.get("application/*", q.get("*/*", 0.0)))
    return html_q > 0 and html_q >= json_q


async def refusal_response(
    request: Request, status: int, text: str, detail: Any
) -> Response:
    if request.headers.get("hx-request", "").lower() == "true":
        return _htmx_fragment(status, text)
    if not _wants_html(request):
        return _json_refusal(status, detail)
    from hub import web

    raw_id = str(request.path_params.get("task_id", ""))
    if raw_id.isdigit():
        try:
            return await web._web_task_detail_page(
                request,
                int(raw_id),
                implementer_error=text,
                form_values=await _form_values(request),
                status_code=status,
            )
        except HTTPException:
            pass  # задачи нет: покажем отказ на общей странице
    return web.TEMPLATES.TemplateResponse(
        request,
        "web_refusal.html",
        {"message": text, "back": _back_path(request)},
        status,
    )


class WebRefusalRoute(APIRoute):
    """Маршрут web-роутера: отказ POST-кнопки отвечает читаемо, не сырым JSON."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            if request.method != "POST" or request.url.path.startswith(_API_PREFIXES):
                return await original(request)
            try:
                return await original(request)
            except RequestValidationError as exc:
                log_field_refusals(request, exc.errors(), await _form_keys(request))
                return await refusal_response(
                    request, 422, validation_text(exc), exc.errors()
                )
            except HTTPException as exc:
                if exc.status_code not in REFUSAL_STATUSES:
                    raise
                return await refusal_response(
                    request,
                    exc.status_code,
                    refusal_text(exc.status_code, exc.detail),
                    exc.detail,
                )

        return handler
