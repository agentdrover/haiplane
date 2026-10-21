"""Что читать у git для блока «тесты AC первыми» (#1933): закреплённый sha, один источник."""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from hub.integrations.registry import plugins
from hub.services.review_oversized import read_sources

log = logging.getLogger("hub")


async def pinned_diff(ctx: tuple[str, str] | None, sha: str) -> str | None:
    """Дифф коммита сдачи как ОБЪЕКТА (#1933); ``None`` - не прочитан.

    ``integrity_read`` разрешает sha через ``rev-parse <sha>^{commit}``, поэтому
    ветка, названная как sha, не подменит его (``branch_diff`` сначала смотрит
    ``origin/<имя>``).
    """
    if ctx is None:
        return None
    try:
        got = await plugins.git_ops.integrity_read(ctx[0], ctx[1], sha)
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("could not read the pinned diff %s: %s", sha[:12], exc)
        return None
    return got.get("diff") if got else None


async def ac_inputs(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: tuple[str, str] | None,
    refs: list[str],
    plan_diff: str | None,
    inline_diff: bool,
) -> tuple[str | None, dict[str, str | None] | None, bool]:
    """``(дифф на sha, файлы тестов AC на sha, блок обязателен)`` для блока #1933.

    Один источник - закреплённый sha. Облако: дифф объекта-коммита; локальный
    заказ: ``plan_diff`` уже на sha. Закреплённый дифф не прочитан - блок
    обязателен и всё в нём «неизвестно»: ветка в расчёт не берётся. Файлы
    читаются, только если в диффе есть файл выше потолка.
    """
    from hub.services.review_dispatch import (  # цикл: dispatch импортирует нас
        REVIEW_FILE_LINE_CAP,
        file_line_counts,
        split_generated,
    )

    sha = (task.get("submission_sha") or "").strip()
    if not refs or not sha:
        return None, None, False
    if inline_diff:
        basis = plan_diff
    else:
        basis = await pinned_diff(ctx, sha)
        if basis is None:
            return None, None, True
    pinned = None if inline_diff else basis
    if basis is None or not any(
        count > REVIEW_FILE_LINE_CAP
        for _, count in file_line_counts(split_generated(basis)[0])
    ):
        return pinned, None, False
    sources = await read_sources(plugins.git_ops, ctx[0] if ctx else None, sha, refs)
    return pinned, sources, False
