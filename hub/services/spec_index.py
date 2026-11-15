"""Индексация спецификации проекта с ветки по умолчанию (#2214).

Один вход - ``index_project``. Он читает файлы ``specs/<capability>/spec.md`` у
git_ops по ref ``origin/<default_branch>`` (не из рабочей копии), разбирает ВСЕ
файлы и проверяет уникальность id по проекту, и только потом одной
write-транзакцией заменяет индекс. Git работает вне транзакции.

Три исхода, и два из них индекс не трогают:

* ``indexed`` - индекс заменён целиком;
* ``refused`` - дубль id или файл не разобран; ответ называет файлы, прежний
  индекс цел (никакой частичной записи: пишется либо всё, либо ничего);
* ``unreadable`` - git не ответил. Это не «спецификации нет»: по нему индекс не
  стирается.

Построчная переиндексация с доверием к слитому тексту (команда и ход поллера) -
``hub.services.spec_reindex`` (#2223); она берёт чтение и запись отсюда.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import repository as repo
from hub import repository_spec_index as store
from hub.db import write_transaction
from hub.integrations.registry import plugins
from hub.models import (
    IndexedRequirement,
    SpecIndexIssue,
    SpecIndexResult,
    SpecRequirement,
)
from hub.services.spec_format import (
    SpecParseError,
    capability_of,
    parse_spec_file,
    requirement_revision,
    scenarios_json,
)

log = logging.getLogger(__name__)


async def index_state(
    db: aiosqlite.Connection, project_id: int
) -> dict[str, Any] | None:
    """Коммит и время последней успешной индексации; None - ни разу не строился."""
    return await store.get_state(db, project_id)


async def indexed_files(
    db: aiosqlite.Connection, project_id: int
) -> list[dict[str, Any]]:
    return await store.list_files(db, project_id)


async def indexed_requirements(
    db: aiosqlite.Connection, project_id: int
) -> list[IndexedRequirement]:
    return await store.list_requirements(db, project_id)


def _unreadable(ref: str, reason: str) -> SpecIndexResult:
    return SpecIndexResult(
        state="unreadable",
        ref=ref,
        issues=[SpecIndexIssue(file="", reason=reason)],
        detail=f"spec index not read: {reason}",
    )


def _parse_files(
    files: list[dict[str, Any]],
) -> tuple[dict[str, list[SpecRequirement]], list[SpecIndexIssue]]:
    """Разбор каждого файла; ошибка файла - запись в issues, остальные читаются дальше."""
    parsed: dict[str, list[SpecRequirement]] = {}
    issues: list[SpecIndexIssue] = []
    for f in files:
        path = f["path"]
        if f["error"]:
            issues.append(SpecIndexIssue(file=path, reason=f["error"]))
            continue
        try:
            parsed[path] = parse_spec_file(f["text"])
        except SpecParseError as exc:
            issues.append(SpecIndexIssue(file=path, reason=str(exc)))
    return parsed, issues


def _duplicate_ids(parsed: dict[str, list[SpecRequirement]]) -> list[SpecIndexIssue]:
    """id уникален в проекте: второй и следующие файлы с тем же id названы поимённо."""
    owner: dict[str, str] = {}
    issues: list[SpecIndexIssue] = []
    for path, reqs in parsed.items():
        for req in reqs:
            first = owner.setdefault(req.id, path)
            if first != path:
                issues.append(
                    SpecIndexIssue(
                        file=path, reason=f"{req.id} is already declared in {first}"
                    )
                )
    return issues


def _refused(ref: str, commit: str, issues: list[SpecIndexIssue]) -> SpecIndexResult:
    names = "; ".join(f"{i.file}: {i.reason}" for i in issues)
    return SpecIndexResult(
        state="refused",
        ref=ref,
        commit=commit,
        issues=issues,
        detail=f"spec indexing refused, previous index kept: {names}",
    )


def rows_of(
    files: list[dict[str, Any]], parsed: dict[str, list[SpecRequirement]]
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    file_rows = [
        {
            "path": f["path"],
            "capability": capability_of(f["path"]),
            "revision": f["blob_sha"],
        }
        for f in files
    ]
    req_rows = [
        {
            "id": req.id,
            "path": path,
            "obligation": req.obligation,
            "scenarios": scenarios_json(req),
            "revision": requirement_revision(req),
        }
        for path, reqs in parsed.items()
        for req in reqs
    ]
    return file_rows, req_rows


async def _read_spec(workspace: str, branch: str) -> dict[str, Any]:
    try:
        return await plugins.git_ops.read_spec_files(workspace, branch)
    except Exception as exc:  # noqa: BLE001 - сбой чтения это ответ «неизвестно»
        log.warning("spec files of %s not read: %s", workspace, exc)
        return {
            "state": "unreadable",
            "ref": f"origin/{branch}",
            "reason": f"read failed: {type(exc).__name__}",
        }


@dataclass(frozen=True)
class SpecRead:
    """Прочитанная и проверенная спецификация коммита: файлы разобраны, id уникальны."""

    workspace: str
    branch: str
    ref: str
    commit: str
    files: list[dict[str, Any]]
    parsed: dict[str, list[SpecRequirement]]


async def read_checked(
    db: aiosqlite.Connection, project_id: int, *, fetch: bool = False
) -> SpecRead | SpecIndexResult:
    """Прочитать спецификацию ветки по умолчанию и проверить её; в БД не пишет.

    Отказ (``refused``) и неизвестность (``unreadable``) приходят готовым
    ``SpecIndexResult``: вызывающий возвращает его как есть. ``fetch`` обновляет
    ``origin/<ветка>`` перед чтением: явная команда не вправе читать старый ref.
    """
    project = await repo.get_project(db, project_id)
    if project is None:
        raise LookupError(f"project {project_id} not found")
    branch = (project["default_branch"] or "").strip()
    workspace = (project["workspace_path"] or "").strip()
    if not workspace:
        return _unreadable(f"origin/{branch}", "the project has no workspace_path")
    if fetch:
        fetched, why = await plugins.git_ops.fetch_base(workspace, branch)
        if not fetched:
            return _unreadable(f"origin/{branch}", f"fetch failed: {why[:150]}")
    raw = await _read_spec(workspace, branch)
    if raw["state"] != "read":
        return _unreadable(
            raw.get("ref", ""), raw.get("reason") or "git did not answer"
        )
    ref, commit, files = raw["ref"], raw["commit"], raw["files"]
    parsed, issues = _parse_files(files)
    issues += _duplicate_ids(parsed)
    if issues:
        return _refused(ref, commit, issues)
    return SpecRead(workspace, branch, ref, commit, files, parsed)


async def index_project(db: aiosqlite.Connection, project_id: int) -> SpecIndexResult:
    """Построить индекс проекта по ветке по умолчанию; отказ индекс не меняет."""
    read = await read_checked(db, project_id)
    if isinstance(read, SpecIndexResult):
        return read
    file_rows, req_rows = rows_of(read.files, read.parsed)
    async with write_transaction(db):
        await store.replace_index(
            db,
            project_id,
            ref=read.ref,
            commit_sha=read.commit,
            files=file_rows,
            requirements=req_rows,
        )
    return SpecIndexResult(
        state="indexed",
        ref=read.ref,
        commit=read.commit,
        files=len(file_rows),
        requirements=len(req_rows),
    )
