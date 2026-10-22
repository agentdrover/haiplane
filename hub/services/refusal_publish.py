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

Лента чистится только для записей ХАБА с известным началом: kind='alert',
author_kind='hub' без принципала (так их помечает ``repo.hub_authored_updates``,
имя agent — лишь второй ключ). Запись, поставленная запросом принципала под
именем «hub», и запись без author_kind — авторские: путь в них содержание.
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

#: Начала других алертов ХАБА, в текст которых попадает ``Refusal.detail`` или
#: ``str(exc)`` из форджа, git и ОС (#2006, раунд 3). Список найден поиском по
#: коду f-строк алертов с ``{exc}``/``{detail}``/``{reason}``; новый такой алерт
#: добавляется сюда, иначе его ЗАПИСАННЫЙ текст остаётся с путём.
#: * executor_launch.py — «Исполнитель НЕ запущен: {причина}» (в старых записях
#:   причина несёт ``Refusal.detail``);
#: * orchestration.py — «Доставка отложена», «Доставка не состоялась» ({detail}
#:   отказа merge-гейта: ``merge_gate_error: {exc}``);
#: * delivery_writes.py — «Ревью одобрено, но PR … не доставлен»;
#: * lifecycle.py, accepted_delivery_retry.py — «Доставка по решению человека …»,
#:   «Автоповтор остановлен», «Уборка worktree не удалась: {exc}»;
#: * poller.py — «PR #N влит, но рабочий каталог не возвращён …: {exc}»;
#: * orchestration.py — «Done report NOT completed», «Developer-agent dispatch
#:   unavailable: {error}».
_FEED_ALERT_STARTS: tuple[str, ...] = (
    "Исполнитель НЕ запущен",
    "Доставка отложена",
    "Доставка не состоялась",
    "Доставка по решению человека",
    "Ревью одобрено, но PR",
    "Автоповтор остановлен",
    "Уборка worktree не удалась",
    "Developer-agent dispatch unavailable",
    "Done report NOT completed",
)
_PR_MERGED_ALERT = re.compile(r"PR #\d+ влит, но рабочий каталог")

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
    return (
        content.startswith(REFUSAL_ALERT_PREFIXES)
        or content.startswith(_FEED_ALERT_STARTS)
        or bool(_RELATED_ALERT.match(content) or _PR_MERGED_ALERT.match(content))
    )


def published_update(row: Any) -> dict[str, Any]:
    """Строка ленты, готовая к показу: алерт отказа очищен, остальное цело."""
    data = dict(row)
    content = str(data.get("content") or "")
    if (
        data.get("kind") == "alert"
        and data.get("agent") == "hub"
        and data.get("author_kind") == "hub"
        and not data.get("principal_id")
        and is_refusal_alert(content)
    ):
        data["content"] = public_reason(content)
    return data


def update_views(rows: Any) -> list[TaskUpdateView]:
    """Лента задачи для показа: каждая строка через :func:`published_update`."""
    return [TaskUpdateView(**published_update(r)) for r in rows]
