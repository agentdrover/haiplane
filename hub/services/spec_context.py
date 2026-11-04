"""Требования индекса, которые контекст задачи показывает по записям дельты.

Id берутся из ``behavior_delta`` и заново не пишутся. Контекст возвращает
обязательство для такого id и только если индекс его уже знает. Путь из
affected_areas требование не выбирает.

Текст берётся из индекса, пока файл на базовой ветке стоит на той ревизии,
которую запомнил индекс. Если файл ушёл вперёд (#2216), контекст читает текст
обязательства из файла на базовой ветке, называет обе ревизии и помечает текст
как прочитанный из файла; текст индекса действующим не называется. Индекс при
этом не трогается: переиндексация - отдельная задача (#2223).
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.integrations.registry import plugins
from hub.models import ContextRequirement, IndexedRequirement
from hub.services.behavior_snapshot import requirement_ids_of
from hub.services.spec_format import (
    SpecParseError,
    parse_spec_file,
    requirement_revision,
)
from hub.services.spec_index import indexed_requirements

log = logging.getLogger(__name__)


def _line(req: ContextRequirement) -> str:
    """Строка дайджеста: источник текста назван, когда это не индекс."""
    if req.source == "index":
        return f"- {req.id}: {req.obligation}"
    behind = f"the index is behind at revision {req.index_file_revision}"
    if req.source == "file":
        return (
            f"- {req.id} (text read from file {req.path} at revision "
            f"{req.file_revision}; {behind}, its text is not the current "
            f"record): {req.obligation}"
        )
    base = req.file_revision or "no such file"
    return (
        f"- {req.id} (no current text: {req.note}; {behind}, file "
        f"{req.path} on the base branch is at revision {base})"
    )


def requirement_lines(reqs: list[ContextRequirement]) -> list[str]:
    """Строки дайджеста. Пустой список не добавляет раздел: молчание — не «нет REQ»."""
    if not reqs:
        return []
    return ["Requirements:", *(_line(req) for req in reqs)]


def _from_index(req: IndexedRequirement) -> ContextRequirement:
    return ContextRequirement(
        **req.model_dump(), source="index", index_file_revision=req.file_revision
    )


def _file_unavailable(
    req: IndexedRequirement, base_revision: str, note: str
) -> ContextRequirement:
    """Файл ушёл вперёд, а текста этого требования в нём нет: текст индекса не отдаётся."""
    data = req.model_dump() | {
        "obligation": "",
        "scenarios": [],
        "revision": "",
        "file_revision": base_revision,
        "source": "file_unavailable",
        "index_file_revision": req.file_revision,
        "note": note,
    }
    return ContextRequirement(**data)


def _from_moved_file(
    req: IndexedRequirement, entry: dict[str, Any] | None
) -> ContextRequirement:
    """Требование по файлу базовой ветки, чья ревизия не равна ревизии индекса."""
    if entry is None:
        return _file_unavailable(req, "", "the file is gone from the base branch")
    base = entry["blob_sha"]
    if entry["error"]:
        return _file_unavailable(
            req, base, f"the file is not readable: {entry['error']}"
        )
    try:
        blocks = parse_spec_file(entry["text"])
    except SpecParseError as exc:
        return _file_unavailable(req, base, f"the file does not parse: {exc}")
    block = next((b for b in blocks if b.id == req.id), None)
    if block is None:
        return _file_unavailable(req, base, f"{req.id} is not in the file any more")
    return ContextRequirement(
        id=req.id,
        obligation=block.obligation,
        scenarios=block.scenarios,
        path=req.path,
        capability=req.capability,
        file_revision=base,
        revision=requirement_revision(block),
        source="file",
        index_file_revision=req.file_revision,
    )


async def _base_files(project: aiosqlite.Row) -> dict[str, dict[str, Any]] | None:
    """Файлы спецификации базовой ветки по пути; None - git не ответил.

    «Не смог посмотреть» не обвинение: без ответа git текст остаётся из
    индекса, как до #2216 (линия #725).
    """
    workspace = (project["workspace_path"] or "").strip()
    if not workspace:
        return None
    branch = (project["default_branch"] or "").strip()
    try:
        raw = await plugins.git_ops.read_spec_files(workspace, branch)
    except Exception as exc:  # noqa: BLE001 - сбой чтения это ответ «неизвестно»
        log.warning("spec files of %s not read for context: %s", workspace, exc)
        return None
    if raw.get("state") != "read":
        return None
    return {f["path"]: f for f in raw["files"]}


async def linked_requirements(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> list[ContextRequirement]:
    """Требования в порядке id дельты. Неизвестного индексу id в ответе нет."""
    ids = requirement_ids_of(task.get("behavior_delta"))
    if not ids:
        return []
    # Проект живёт на эпике, ребёнок наследует: колонка tasks.project_id у
    # ребёнка NULL, ключ индекса берём тем же путём, что и остальной /context.
    project = await repo.resolve_project_for_task(db, int(task["id"]))
    if project is None:
        return []
    by_id = {req.id: req for req in await indexed_requirements(db, int(project["id"]))}
    found = [by_id[req_id] for req_id in ids if req_id in by_id]
    files = await _base_files(project) if found else None
    if files is None:
        return [_from_index(req) for req in found]
    return [
        _from_index(req)
        if (entry := files.get(req.path)) is not None
        and entry["blob_sha"] == req.file_revision
        else _from_moved_file(req, files.get(req.path))
        for req in found
    ]
