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

Лента чистится только для записей ХАБА (agent='hub', kind='alert') с известным
началом: авторские записи людей и агентов — содержание, их путь не утечка.
"""

from __future__ import annotations

import re
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

#: Другие алерты хаба, в которые попадает причина отказа: переспрос и его
#: исчерпание (``cause``), отвергнутые параметры заказа.
_RELATED_ALERT = re.compile(
    r"\[переспрос ревью(?: исчерпан)?: сдача \d+\]|Заказ ревьюера "
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


def is_refusal_alert(content: str) -> bool:
    """Алерт хаба об отказе облачного ревьюера (по началу текста)."""
    return content.startswith(REFUSAL_ALERT_PREFIXES) or bool(
        _RELATED_ALERT.match(content)
    )


def published_update(row: Any) -> dict[str, Any]:
    """Строка ленты, готовая к показу: алерт отказа очищен, остальное цело."""
    data = dict(row)
    content = str(data.get("content") or "")
    if (
        data.get("kind") == "alert"
        and data.get("agent") == "hub"
        and is_refusal_alert(content)
    ):
        data["content"] = public_reason(content)
    return data


def update_views(rows: Any) -> list[TaskUpdateView]:
    """Лента задачи для показа: каждая строка через :func:`published_update`."""
    return [TaskUpdateView(**published_update(r)) for r in rows]
