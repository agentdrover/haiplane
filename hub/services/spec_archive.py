"""Архив дельты в файл спецификации на ветке задачи (#2220).

Автор правит ``specs/<capability>/spec.md`` на ветке задачи до сдачи. На сдаче
хаб читает файлы на закреплённом head_sha и на его merge-base с базовой веткой и
сверяет блоки со снимком #2218. Своего коммита в файл хаб не добавляет: файл на
ветке - дело автора, а diff ревьюят вместе с кодом.

Правила сверки по записям снимка:

* added и modified: блок id есть на head_sha, его sha256 равен ``content_hash``;
* renamed: старого id на head_sha нет, блок ``new_requirement_id`` хешируется как
  в снимке (id в каноническом JSON - новый);
* removed: id на head_sha нет; хеш снимка равен блоку на ``base_spec_revision``
  (это уже проверила сдача при фиксации снимка), а не блоку файла на head_sha;
* «дельты нет»: diff ``specs/`` пуст.

Исход пишется в сам снимок как ``spec_check`` и действителен только для его
head_sha: новый коммит не наследует сверку, слияние ждёт новой сдачи (см.
``behavior_snapshot.refusal_for_moved_head``).

Если commit прочитать нечем, сдача не отказывается и снимок честно помечен
``unchecked``: сеть и клон - не повод остановить конвейер, но «не сверено» не
выдаётся за «сверено».

PR, слитый вне хаба на другом SHA или с блоками не из снимка, - не чистый архив:
``note_outside_merge`` пишет долг по id снимка (без id в снимке - на весь проект),
индекс блоки долга не берёт (``spec_debt``). Долг пишут все пути доставки:
``_refused_merge_outcome``, обрыв пары на сдвинутой вершине (``_approved_code_check``)
и отказ мержа у headless-поллера (``orchestration.note_refused_merge``).
Закрытие долга - #2219, переиндексация - #2223.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.db import committed_if_own
from hub.integrations.registry import plugins
from hub.services import behavior_snapshot, spec_debt
from hub.services.spec_format import (
    SpecParseError,
    parse_spec_file,
    requirement_revision,
)

log = logging.getLogger(__name__)

_OPS_PRESENT_AT_BASE = ("removed", "renamed")


@dataclass(frozen=True)
class SpecFacts:
    """Что git ответил про ``specs/`` на head и на базе сверки."""

    readable: bool
    head_sha: str
    base_sha: str = ""
    reason: str = ""
    head_blocks: dict[str, str] = field(default_factory=dict)
    base_blocks: dict[str, str] = field(default_factory=dict)
    changed_paths: tuple[str, ...] = ()
    #: ``(путь, причина)`` файлов head, которые не разобраны.
    issues: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Mismatch:
    reason: str
    message: str
    ids: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


def _blocks(
    files: list[dict[str, Any]],
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """``{id: sha256 блока}`` по файлам и ``(путь, причина)`` негодных файлов."""
    blocks: dict[str, str] = {}
    owner: dict[str, str] = {}
    issues: list[tuple[str, str]] = []
    for item in files:
        path = str(item["path"])
        if item.get("error"):
            issues.append((path, str(item["error"])))
            continue
        try:
            reqs = parse_spec_file(item["text"])
        except SpecParseError as exc:
            issues.append((path, str(exc)))
            continue
        for req in reqs:
            if req.id in owner:
                issues.append(
                    (path, f"{req.id} is already declared in {owner[req.id]}")
                )
                continue
            owner[req.id] = path
            blocks[req.id] = requirement_revision(req)
    return blocks, issues


def facts_from(raw: dict[str, Any]) -> SpecFacts:
    """Факты по ответу ``git_ops.read_spec_delta``; чужую базу не угадывает."""
    head = str(raw.get("head") or "")
    if raw.get("state") != "read":
        return SpecFacts(
            False, head, reason=str(raw.get("reason") or "git did not answer")
        )
    head_blocks, issues = _blocks(raw["head_files"])
    base_blocks, base_issues = _blocks(raw["base_files"])
    if base_issues:
        names = "; ".join(f"{path}: {why}" for path, why in base_issues)
        return SpecFacts(False, head, reason=f"spec files at the merge base: {names}")
    return SpecFacts(
        True,
        head,
        str(raw["base"]),
        "",
        head_blocks,
        base_blocks,
        tuple(raw["changed"]),
        tuple(issues),
    )


async def read_at(db: aiosqlite.Connection, task_id: int, sha: str) -> SpecFacts:
    """Прочитать ``specs/`` на ``sha`` и на базе сверки; сбой чтения - ``readable=False``."""
    project = await repo.resolve_project_for_task(db, task_id)
    workspace = str(project["workspace_path"] or "").strip() if project else ""
    if project is None or not workspace:
        return SpecFacts(False, sha, reason="the project has no workspace_path")
    branch = str(project["default_branch"] or "").strip()
    try:
        raw = await plugins.git_ops.read_spec_delta(workspace, sha, branch)
    except Exception as exc:  # noqa: BLE001 - сбой чтения это «не сверено», не отказ
        log.warning("spec delta of #%s at %s not read: %s", task_id, sha[:12], exc)
        raw = {"state": "unreadable", "head": sha, "reason": type(exc).__name__}
    return facts_from(raw)


async def read_facts(
    db: aiosqlite.Connection, task: dict[str, Any], head_sha: str, payload: str
) -> SpecFacts | None:
    """Факты для сверки сдачи; ``None`` - к этой сдаче сверка не относится.

    Относится, когда снимок создан, head закреплён и сдача внутри границы
    #2219 (task или subtask проекта с индексом). Читает git, поэтому вызывается
    до транзакции записи.
    """
    if not payload or not head_sha:
        return None
    if not await behavior_snapshot.inside_gate(db, task):
        return None
    return await read_at(db, int(task["id"]), head_sha)


def _expected_ops(records: list[dict[str, Any]]) -> dict[str, str]:
    """Что снимок обещает по каждому id: renamed - это removed старого и added нового."""
    ops: dict[str, str] = {}
    for rec in records:
        if rec["op"] == "renamed":
            ops[rec["requirement_id"]] = "removed"
            ops[rec["new_requirement_id"]] = "added"
        else:
            ops[rec["requirement_id"]] = rec["op"]
    return ops


def _actual_ops(facts: SpecFacts) -> dict[str, str]:
    """Что diff блоков делает с каждым id между базой и head."""
    ops: dict[str, str] = {}
    for ident in {*facts.head_blocks, *facts.base_blocks}:
        head, base = facts.head_blocks.get(ident), facts.base_blocks.get(ident)
        if head == base:
            continue
        ops[ident] = (
            "added" if base is None else "removed" if head is None else "modified"
        )
    return ops


def _edit_without_delta(facts: SpecFacts) -> Mismatch | None:
    if not facts.changed_paths:
        return None
    paths = ", ".join(facts.changed_paths)
    return Mismatch(
        "spec_edit_without_delta",
        f"spec_edit_without_delta: снимок говорит «дельты нет», а diff меняет {paths}",
        paths=facts.changed_paths,
    )


def still_present(facts: SpecFacts, records: list[dict[str, Any]]) -> Mismatch | None:
    """removed и старый id у renamed: на head_sha такого блока быть не должно."""
    ids = sorted(
        {
            rec["requirement_id"]
            for rec in records
            if rec["op"] in _OPS_PRESENT_AT_BASE
            and rec["requirement_id"] in facts.head_blocks
        }
    )
    if not ids:
        return None
    return Mismatch(
        "spec_removed_id_present",
        f"spec_removed_id_present: {', '.join(ids)} ещё есть на head_sha, "
        "а снимок его убирает",
        ids=tuple(ids),
    )


def _op_mismatch(facts: SpecFacts, records: list[dict[str, Any]]) -> Mismatch | None:
    expected, actual = _expected_ops(records), _actual_ops(facts)
    bad = sorted(i for i in {*expected, *actual} if expected.get(i) != actual.get(i))
    if not bad:
        return None
    parts = [
        f"{i}: снимок {expected.get(i) or 'не называет'}, diff {actual.get(i) or 'не меняет'}"
        for i in bad
    ]
    return Mismatch(
        "spec_diff_mismatch",
        f"spec_diff_mismatch: diff specs/ расходится со снимком по id — {'; '.join(parts)}",
        ids=tuple(bad),
    )


def block_hash_mismatch(
    facts: SpecFacts, records: list[dict[str, Any]]
) -> Mismatch | None:
    """sha256 блока на head_sha против хеша записи; id renamed - новый, removed не хешируется."""
    bad = sorted(
        {
            ident
            for rec in records
            if rec["op"] != "removed"
            for ident in [rec["new_requirement_id"] or rec["requirement_id"]]
            if facts.head_blocks.get(ident) != rec["content_hash"]
        }
    )
    if not bad:
        return None
    return Mismatch(
        "spec_block_hash_mismatch",
        "spec_block_hash_mismatch: хеш блока в файле не равен хешу снимка у "
        + ", ".join(bad),
        ids=tuple(bad),
    )


def mismatch_of(facts: SpecFacts, data: dict[str, Any]) -> Mismatch | None:
    """Первое расхождение diff со снимком; ``None`` - совпало."""
    if facts.issues:
        names = "; ".join(f"{path}: {why}" for path, why in facts.issues)
        return Mismatch(
            "spec_file_unparsed",
            f"spec_file_unparsed: файл спецификации на head_sha не разобран — {names}",
            paths=tuple(path for path, _why in facts.issues),
        )
    records = data.get("records") or []
    if not records:
        return _edit_without_delta(facts)
    return (
        still_present(facts, records)
        or _op_mismatch(facts, records)
        or block_hash_mismatch(facts, records)
    )


def _refusal(found: Mismatch) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "reason": found.reason,
            "message": found.message,
            "ids": list(found.ids),
            "paths": list(found.paths),
            "hint": (
                "Приведите файл specs/ на ветке и behavior_delta к одному виду "
                "и сдайте снова на новом коммите"
            ),
            "actor_hint": "agent",
        },
    )


def verify(facts: SpecFacts, data: dict[str, Any]) -> dict[str, str]:
    """Исход сверки для снимка; расхождение - HTTP 422 с названными id."""
    if not facts.readable:
        return {
            "state": "unchecked",
            "head_sha": facts.head_sha,
            "reason": facts.reason,
        }
    found = mismatch_of(facts, data)
    if found is not None:
        raise _refusal(found)
    return {"state": "matched", "head_sha": facts.head_sha, "base_sha": facts.base_sha}


def stamp(facts: SpecFacts | None, payload: str) -> str:
    """Сверить снимок с diff и вписать ``spec_check``; без фактов снимок не меняется."""
    if facts is None or not payload:
        return payload
    data = json.loads(payload)
    data["spec_check"] = verify(facts, data)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _snapshot_ids(data: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for rec in data.get("records") or []:
        for ident in (rec.get("requirement_id"), rec.get("new_requirement_id")):
            if ident and ident not in ids:
                ids.append(ident)
    return ids


async def _outside_reason(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    data: dict[str, Any],
    merged: str,
) -> str:
    """Почему слитый PR - не чистый архив; пусто - чистый или сверить нечем."""
    if merged != str(data.get("head_sha") or ""):
        return "sha_differs"
    if (data.get("spec_check") or {}).get("state") == "matched":
        return ""
    facts = await read_at(db, int(task["id"]), merged)
    records = data.get("records") or []
    if not facts.readable or not records:
        return ""
    found = still_present(facts, records) or block_hash_mismatch(facts, records)
    return "blocks_differ" if found else ""


def _debt_note(pr_number: int, data: dict[str, Any], merged: str, reason: str) -> str:
    why = (
        "слитый SHA не равен одобренному"
        if reason == "sha_differs"
        else "блоки specs/ слитого SHA не равны снимку"
    )
    ids = ", ".join(_snapshot_ids(data)) or "id в снимке нет, долг на весь проект"
    return (
        f"Не чистый архив: долг по спецификации. PR #{pr_number} слит вне хаба на "
        f"{merged[:12]}, одобрен {str(data.get('head_sha'))[:12]} ({why}). "
        f"Id снимка: {ids}. Индекс эти блоки не принимает."
    )


async def _record_outside_merge(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    pr_number: int,
) -> str:
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    raw = (
        await behavior_snapshot.load_raw(db, task_id, generation) if generation else ""
    )
    if not raw:
        return ""
    data = json.loads(raw)
    merged = await plugins.git_ops.pr_head_sha(
        pr_number,
        repo=ctx.get("repo"),
        gh_repo=ctx.get("gh_repo"),
        forge=ctx.get("forge", ""),
    )
    project = await repo.resolve_project_for_task(db, task_id)
    if not merged or project is None:
        return ""
    reason = await _outside_reason(db, task, data, merged)
    if not reason:
        return ""
    note = _debt_note(pr_number, data, merged, reason)
    async with committed_if_own(db):
        fresh = await spec_debt.record(
            db,
            project_id=int(project["id"]),
            task_id=task_id,
            ids=_snapshot_ids(data),
            pr_number=pr_number,
            approved_sha=str(data.get("head_sha") or ""),
            merged_sha=merged,
            reason=reason,
        )
        if fresh:
            await repo.add_task_update(db, task_id, "hub", "alert", note)
    return note


async def note_outside_merge(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    pr_number: int,
) -> str:
    """Хаб увидел PR, слитый не им: долг по id снимка, если это не чистый архив.

    Возвращает строку для детали доставки либо пустую, когда долга нет или
    сверить нечем (форж не назвал head PR). Сбой здесь доставку не роняет:
    ``merged_outside_gate`` от этого не зависит.
    """
    try:
        return await _record_outside_merge(db, task, ctx, pr_number)
    except Exception as exc:  # noqa: BLE001 - долг не должен ронять разбор доставки
        log.warning(
            "spec debt of #%s (PR #%s) not recorded: %s", task["id"], pr_number, exc
        )
        return ""
