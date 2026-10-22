"""Отказ облачного ревьюера на публикационной границе (#2006).

Продолжение #2002. Транспортный отказ Cursor нёс в ``Refusal.detail`` сырой
текст исключения (``PermissionError: ... '/srv/.../key.pem'``), а дальше он шёл
в alert ленты, долг второй двери, headline, бриф, MCP, инбокс и карточку.

Правило здесь одно, в две стороны:

* ПИСАТЕЛЬ называет отказ ВИДОМ (:func:`safe_detail`): имя класса исключения для
  транспорта, ``HTTP <код>[, <error.code>]`` для ответа провайдера. Сырой текст
  остаётся в журнале хаба (``log.warning`` в ``cursor_cloud``).
* ЧИТАТЕЛЬ уже сохранённого отказа пропускает текст через
  :func:`brief_failure.public_reason` (:func:`public_text`, :func:`published_update`):
  записи, сделанные до исправления, путь уже несут.

Лента чистится только для записей ХАБА (agent='hub', author_kind='hub' без
принципала, как в ``repo.hub_authored_updates``), любого вида и с любым началом.
Запись, поставленная запросом принципала под именем «hub», и запись без
author_kind — авторские: путь в них содержание.
"""

from __future__ import annotations

from typing import Any

from hub.models import TaskUpdateView
from hub.services.brief_failure import public_reason

#: Начала алертов ``maybe_dispatch_review``, означающих «провайдер агента не
#: создал»: прямой отказ и ответ, который не дошёл при подтверждённой пустоте.
#: Отказ по конфигурации хаба («не хватает конфигурации») сюда не входит —
#: это не провайдер. Значения не менять: по ним отказ узнают сторож, очередь
#: и SQL-запросы ``review_availability``.
REFUSAL_ALERT_PREFIXES: tuple[str, ...] = (
    "Кросс-модельное ревью НЕ вызвано: провайдер отказал",
    "Кросс-модельное ревью НЕ вызвано: ответ провайдера не дошёл",
)

_UNNAMED = "причина не названа"


def safe_detail(refusal: Any) -> str:
    """Вид отказа для публикации: без сырого текста исключения и тела ответа."""
    if refusal is None:
        return _UNNAMED
    kind = str(getattr(refusal, "kind", "") or "").strip()
    if kind:
        return kind
    status = int(getattr(refusal, "status", 0) or 0)
    if status:
        code = str(getattr(refusal, "code", "") or "").strip()
        return f"HTTP {status}" + (f", {code}" if code else "")
    return public_reason(str(getattr(refusal, "detail", "") or "")) or _UNNAMED


def public_text(text: str) -> str:
    """Текст сохранённого отказа без путей и адресов (читатель)."""
    return public_reason(text)


def scrub_text(text: str | None) -> str | None:
    """``public_reason`` для необязательного текста: ``None`` и пустое проходят."""
    return public_reason(text) if text else text


def is_hub_authored(row: Any) -> bool:
    """Запись написана самим хабом: author_kind=hub, имя hub, без принципала."""
    return (
        row.get("agent") == "hub"
        and row.get("author_kind") == "hub"
        and not row.get("principal_id")
    )


def published_update(row: Any) -> dict[str, Any]:
    """Строка ленты, готовая к показу.

    Белый список по АВТОРУ (#2006, раунд 4): текст хаба - машинный, авторского
    содержания в нём нет, и любой его вид (alert, status, report) с любым
    началом идёт через ``public_reason``. Списка начал алертов больше нет: он
    рос с каждым раундом. Записи агентов и людей не меняются.
    """
    data = dict(row)
    if is_hub_authored(data):
        data["content"] = scrub_text(str(data.get("content") or ""))
    return data


def update_views(rows: Any) -> list[TaskUpdateView]:
    """Лента задачи для показа: каждая строка через :func:`published_update`."""
    return [TaskUpdateView(**published_update(r)) for r in rows]
