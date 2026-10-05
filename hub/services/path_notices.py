"""Предупреждения по путям диффа сдачи (#1589).

Правка некоторых путей требует ручного шага вне CI: закреплённая копия
deploy-скрипта на сервере, установленная служба ревьюера, переменные окружения,
бэкап перед миграцией. Проект записывает такие правила ключом политики
``path_notices`` — список {pattern, text}, — и хаб называет совпавшие тексты при
сдаче, в брифе ревью и в карточке вердикта.

Предупреждение ничего не доказывает и ничего не исполняет: оно не говорит, что
шаг выполнен, и не блокирует сдачу. Результат фиксируется ОДИН раз на поколение
сдачи (таблица ``path_notice_results``) и не пересчитывается правкой политики:
иначе бриф и карточка показали бы не то, что видел сдавший.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.integrations.registry import plugins
from hub.models import PathNoticeItem, PathNoticesView
from hub.services import project_policy

log = logging.getLogger(__name__)

STATE_MATCHED = "matched"
STATE_NONE = "none"
STATE_UNKNOWN = "unknown"

#: Заголовок строки ленты: по нему её ищут и тесты, и читатель ленты.
PATH_NOTICES_MARK = "Предупреждение по путям диффа (path_notices)"
#: Сколько путей одного правила хранится и показывается; остальное — числом.
STORED_PATHS_PER_NOTICE = 20


@lru_cache(maxsize=256)
def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Шаблон пути в регулярное выражение с фиксированной семантикой.

    ``*`` не пересекает ``/``; ``?`` — один символ кроме ``/``; сегмент ``**``
    целиком — любое число каталогов, включая ноль (``deploy/**/*.service``
    покрывает ``deploy/x.service``), а в конце шаблона — любой хвост из одного
    и более имён (``deploy/review-runner/**``). ``**`` внутри сегмента читается
    как ``*``. Всё остальное — буквально, с учётом регистра. fnmatch из
    risk_class сюда не годится: там ``*`` пересекает ``/``.
    """
    segments = pattern.strip().split("/")
    out: list[str] = []
    last = len(segments) - 1
    for i, seg in enumerate(segments):
        if seg == "**":
            out.append(".+" if i == last else "(?:[^/]+/)*")
            continue
        body = "".join(
            "[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch)
            for ch in re.sub(r"\*{2,}", "*", seg)
        )
        out.append(body if i == last else body + "/")
    return re.compile("".join(out))


def path_matches(pattern: str, path: str) -> bool:
    """Задевает ли путь шаблон: полное совпадение, без неявных префиксов."""
    return compile_pattern(pattern).fullmatch(path) is not None


def match_rules(rules: list[dict[str, str]], paths: list[str]) -> list[dict[str, Any]]:
    """Совпавшие правила; одинаковый текст — одно предупреждение без дублей.

    Пересекающиеся правила показываются все; правила с одним и тем же текстом
    сливаются: паттерны и пути объединяются, текст печатается один раз.
    """
    merged: dict[str, dict[str, Any]] = {}
    for rule in rules:
        hit = [p for p in paths if path_matches(rule["pattern"], p)]
        if not hit:
            continue
        entry = merged.setdefault(
            rule["text"], {"text": rule["text"], "patterns": [], "paths": []}
        )
        if rule["pattern"] not in entry["patterns"]:
            entry["patterns"].append(rule["pattern"])
        entry["paths"].extend(p for p in hit if p not in entry["paths"])
    return [
        {
            **entry,
            "paths": entry["paths"][:STORED_PATHS_PER_NOTICE],
            "more_paths": max(0, len(entry["paths"]) - STORED_PATHS_PER_NOTICE),
        }
        for entry in merged.values()
    ]


@dataclass
class PathNoticeOutcome:
    """Что шаг сдачи узнал о путях; пишется в базу вместе с поколением."""

    state: str
    sha: str = ""
    reason: str = ""
    notices: list[dict[str, Any]] = field(default_factory=list)


def render_text(
    generation: int, sha: str, state: str, reason: str, notices: list[dict[str, Any]]
) -> str:
    """Один текст для ленты, MCP и брифа; в web он выводится экранированным."""
    head = f"{PATH_NOTICES_MARK}, поколение {generation}"
    if sha:
        head += f", коммит {sha[:12]}"
    if state == STATE_UNKNOWN:
        return (
            f"{head}: проверка путей не выполнена: {reason or 'причина не названа'}. "
            "Это не значит, что правила не задеты."
        )
    if state == STATE_NONE:
        return f"{head}: путей из правил в диффе нет."
    lines = [f"{head}. Правка требует ручных шагов вне CI:"]
    for notice in notices:
        shown = ", ".join(notice["paths"][:10])
        extra = int(notice.get("more_paths") or 0) + max(0, len(notice["paths"]) - 10)
        more = f" и ещё {extra}" if extra else ""
        lines.append(f"— {shown}{more}: {notice['text']}")
    return "\n".join(lines)


def _view(row: Any) -> PathNoticesView:
    try:
        notices = json.loads(row["notices"] or "[]")
    except (ValueError, TypeError):
        notices = []
    items = [PathNoticeItem(**n) for n in notices if isinstance(n, dict)]
    generation = int(row["generation"])
    return PathNoticesView(
        generation=generation,
        sha=row["sha"] or "",
        state=row["state"],
        reason=row["reason"] or "",
        notices=items,
        text=render_text(
            generation,
            row["sha"] or "",
            row["state"],
            row["reason"] or "",
            [n.model_dump() for n in items],
        ),
    )


async def view_for_generation(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> PathNoticesView | None:
    """Сохранённый результат ЭТОГО поколения; None — правил на сдаче не было."""
    row = await repo.get_path_notice_result(db, task_id, int(generation or 0))
    return _view(row) if row is not None else None


def shown_text(view: PathNoticesView | None) -> str:
    """Текст для показа: только когда есть что сказать (matched или unknown)."""
    if view is None or view.state == STATE_NONE:
        return ""
    return view.text


async def compute(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    *,
    submission_sha: str,
    diff_paths: list[str] | None,
    diff_reason: str,
) -> PathNoticeOutcome | None:
    """Прочитать пути сдачи и сверить с правилами проекта.

    ``None`` — правил нет (нет ключа или список пуст): ничего не читается и
    ничего не меняется. Иначе исход один из трёх, и «не прочитано» — не
    молчание, а состояние ``unknown`` с причиной.
    """
    rules = project_policy.path_notices_of(
        await project_policy.gate_policy_for_task(db, int(task["id"]))
    )
    if not rules:
        return None
    if diff_paths is None:
        return PathNoticeOutcome(
            STATE_UNKNOWN, reason=diff_reason or "дифф ветки не прочитан"
        )
    branch = (task.get("branch") or "").strip()
    try:
        from hub.services.orchestration import project_git_context

        ctx = await project_git_context(db, int(task["id"]))
        read = await plugins.git_ops.branch_touched_paths(
            branch,
            base_branch=ctx.get("base_branch"),
            repo=ctx.get("repo"),
            head_sha=submission_sha,
        )
    except Exception as exc:  # noqa: BLE001 - unknown is a state, never a refusal
        log.warning("path_notices: reading paths failed for #%s: %s", task["id"], exc)
        return PathNoticeOutcome(
            STATE_UNKNOWN,
            sha=submission_sha,
            reason=f"не удалось прочитать пути с переименованиями ({type(exc).__name__})",
        )
    if read is None:
        return PathNoticeOutcome(
            STATE_UNKNOWN,
            sha=submission_sha,
            reason=(
                f"не удалось прочитать пути коммита {submission_sha[:12]}"
                if submission_sha
                else f"не удалось прочитать пути ветки {branch!r}"
            ),
        )
    paths, sha_read = read
    notices = match_rules(rules, paths)
    return PathNoticeOutcome(
        STATE_MATCHED if notices else STATE_NONE, sha=sha_read, notices=notices
    )


async def record(
    db: aiosqlite.Connection, task_id: int, outcome: PathNoticeOutcome
) -> PathNoticesView | None:
    """Записать результат поколения и ОДНУ строку ленты. Без commit.

    Поколение читается из базы: на pair-пути оно уже вырос в транзакции
    сдачи, на headless — бампнут до гейтов. Повторная запись того же
    поколения ничего не пишет и строки ленты не добавляет.
    """
    row = await repo.get_task(db, task_id)
    generation = int(dict(row).get("submission_generation") or 0) if row else 0
    inserted = await repo.insert_path_notice_result(
        db,
        task_id=task_id,
        generation=generation,
        sha=outcome.sha,
        state=outcome.state,
        reason=outcome.reason,
        notices=json.dumps(outcome.notices, ensure_ascii=False),
    )
    view = await view_for_generation(db, task_id, generation)
    if inserted and view is not None and shown_text(view):
        await repo.add_task_update(db, task_id, "hub", "alert", view.text)
    return view
