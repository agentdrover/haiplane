"""Долг по спецификации и его действие на индекс (#2220).

Долг пишется, когда PR слит мимо проверенного снимка: на SHA, который не равен
одобренному, либо с блоками, не равными снимку. Здесь только хранение и ответ
индексу: что считать долгом, решает ``hub.services.spec_archive``. Закрытие долга
человеком - #2219; переиндексация после слияния - #2223.

Индекс не подхватывает блок id из долга: прежняя строка индекса остаётся как
была, а блока, которого в индексе не было, там не появляется. Долг без id
(снимок «дельты нет») - на весь проект: прежний индекс остаётся целиком, пока
долг не закрыт.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub import repository_spec_debt as store
from hub import repository_spec_index as index_store
from hub.models import SpecRequirement
from hub.services.spec_format import scenarios_json


async def open_debts(db: aiosqlite.Connection, project_id: int) -> list[dict[str, Any]]:
    return await store.list_open(db, project_id)


async def record(
    db: aiosqlite.Connection,
    *,
    project_id: int,
    task_id: int,
    ids: list[str],
    pr_number: int,
    approved_sha: str,
    merged_sha: str,
    reason: str,
) -> list[str]:
    """Долг по каждому id снимка; без id - одна строка на весь проект (``""``).

    Возвращает id, записанные впервые: повторное наблюдение того же слияния
    ничего не добавляет.
    """
    fresh: list[str] = []
    for ident in ids or [""]:
        written = await store.insert_debt(
            db,
            project_id=project_id,
            task_id=task_id,
            requirement_id=ident,
            pr_number=pr_number,
            approved_sha=approved_sha,
            merged_sha=merged_sha,
            reason=reason,
        )
        if written:
            fresh.append(ident)
    return fresh


def _held_row(old: Any) -> dict[str, Any]:
    """Строка индекса в прежнем виде: текст, сценарии и ревизия те же."""
    block = SpecRequirement(
        id=old.id, obligation=old.obligation, scenarios=old.scenarios
    )
    return {
        "id": old.id,
        "path": old.path,
        "obligation": old.obligation,
        "scenarios": scenarios_json(block),
        "revision": old.revision,
    }


async def hold_index(
    db: aiosqlite.Connection,
    project_id: int,
    files: list[dict[str, str]],
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Файлы и строки требований для индекса с прежними блоками на месте id из долга.

    Id из долга берёт прежняя строка индекса, если её файл ещё есть среди
    прочитанных; иначе блок в индекс не попадает. Долг на весь проект (``""``:
    в снимке не было id) держит весь прежний индекс - и файлы, и требования,
    включая те, чьих файлов на слитом SHA уже нет; новых строк не берётся.
    Остальные строки не тронуты.
    """
    held = {d["requirement_id"] for d in await store.list_open(db, project_id)}
    if not held:
        return files, rows
    previous = await index_store.list_requirements(db, project_id)
    if "" in held:
        old_files = await index_store.list_files(db, project_id)
        return old_files, [_held_row(old) for old in previous]
    by_id = {r.id: r for r in previous}
    paths = {f["path"] for f in files}
    kept = [row for row in rows if row["id"] not in held]
    kept += [
        _held_row(by_id[ident])
        for ident in sorted(held)
        if ident in by_id and by_id[ident].path in paths
    ]
    return files, kept
