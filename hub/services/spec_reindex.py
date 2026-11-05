"""Переиндексация спецификации с базовой ветки (#2223).

Первый индекс строит ``spec_index.index_project`` (#2214). Здесь он догоняет
базовую ветку: по ref ``origin/<ветка>``, не по рабочей копии. Поводов два, и
оба идут через ``reindex_project``: ход поллера (``run_sweep``: SHA базовой ветки
сдвинулся и среди изменений есть ``specs/``) и явная команда
``POST /api/projects/{id}/spec/reindex``, одна для REST, CLI и MCP.

Отказ - файл не разобран или дубль id - целиком возвращает ответ с именами
файлов и ничего не пишет: либо весь индекс заменён, либо прежний цел.

Блок требования из слитого коммита попадает в индекс, только если

* его хеш равен ПОСЛЕДНЕМУ одобренному снимку этого id среди слияний окна
  ``прежний_коммит..новый`` (``pipeline_merges``), либо
* его хеш не менялся относительно прежнего индекса.

Снимки читаются в порядке коммитов git, а не записей хаба: одобренный текст,
который позже снят или заменён одобренным, возвращённый в файл коммитом без
снимка, не подхватывается. Иначе блок пропущен и назван в ``skipped``: прежняя
ревизия остаётся, а у нового id ревизии нет. Исчезнувший из файла блок снимается,
только если последняя одобренная запись окна - ``removed``/``renamed`` с
ревизией, которую индекс или одобренная цепочка окна знали; иначе он остаётся.
Окно ``прежний_коммит..новый`` должно быть прочитано целиком: если git его не
назвал или список усечён, индекс не переписывается, курсор стоит, а ответ -
``unreadable`` с причиной (поллер повторит ход). Первая сборка индекса сверять не с чем и проходит без этого фильтра. Запись долга
по пропущенным id - #2220, ответ контекста при отставшем индексе - #2216.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import repository as repo
from hub import repository_spec_index as store
from hub.db import log_activity, write_transaction
from hub.integrations.registry import plugins
from hub.models import IndexedRequirement, SpecIndexIssue, SpecIndexResult
from hub.services import spec_index
from hub.services.spec_format import requirement_revision, scenarios_json
from hub.services.spec_index import SpecRead

log = logging.getLogger(__name__)

#: Не чаще одного прохода за столько секунд: проход делает ``git fetch`` по каждому проекту.
SWEEP_INTERVAL = 120.0
_ATTEMPTS = 3
_ADDING_OPS = ("added", "modified", "renamed")

#: Коммит базовой ветки, который проход уже разобрал (проект -> SHA): тот же SHA
#: второй раз не читается. В памяти намеренно: рестарт стоит одного лишнего чтения.
_settled: dict[int, str] = {}
_clock: dict[str, float] = {}


#: Последняя одобренная запись id в окне - снятие (хеша нет).
REMOVED = ""


@dataclass
class Approved:
    """Последняя одобренная запись каждого id окна: хеш блока или ``REMOVED``."""

    latest: dict[str, str] = field(default_factory=dict)
    known: dict[str, set[str]] = field(default_factory=dict)


def ordered_snapshots(
    pairs: Iterable[tuple[str, str]], commits: list[str]
) -> list[str]:
    """JSON снимков в порядке коммитов окна (старые первыми), не записей хаба."""
    position = {sha: i for i, sha in enumerate(commits)}
    ranked = sorted(
        (p for p in pairs if p[0] in position), key=lambda p: position[p[0]]
    )
    return [raw for _sha, raw in ranked]


def approved_from(
    snapshots: Iterable[str], prior: Mapping[str, str] | None = None
) -> Approved:
    """Свёртка снимков в порядке коммитов: позднее одобрение id заменяет раннее.

    ``prior`` - ревизии прежнего индекса: снять можно только текст, который
    индекс или одобренная цепочка окна уже знали. Непрочитанный снимок ничего
    не одобряет.
    """
    approved = Approved(known={i: {h} for i, h in (prior or {}).items()})
    for raw in snapshots:
        try:
            records = json.loads(raw).get("records") or []
        except (ValueError, AttributeError):
            continue
        for rec in records:
            _collect(approved, rec)
    return approved


def _retire(approved: Approved, ident: str, base: str) -> None:
    if base and base in approved.known.get(ident, set()):
        approved.latest[ident] = REMOVED
        approved.known[ident] = set()


def _collect(approved: Approved, rec: dict[str, Any]) -> None:
    op, digest = rec.get("op"), str(rec.get("content_hash") or "")
    ident = str(rec.get("requirement_id") or "")
    if op == "removed":
        _retire(approved, ident, digest)
        return
    if op == "renamed":
        _retire(approved, ident, str(rec.get("base_spec_revision") or ""))
        ident = str(rec.get("new_requirement_id") or "")
    if op in _ADDING_OPS and digest and ident:
        approved.latest[ident] = digest
        approved.known.setdefault(ident, set()).add(digest)


@dataclass
class Plan:
    files: list[dict[str, str]]
    requirements: list[dict[str, Any]]
    skipped: list[SpecIndexIssue]


def _block_row(path: str, req: Any, revision: str) -> dict[str, Any]:
    return {
        "id": req.id,
        "path": path,
        "obligation": req.obligation,
        "scenarios": scenarios_json(req),
        "revision": revision,
    }


def _kept_row(prior: IndexedRequirement) -> dict[str, Any]:
    return _block_row(prior.path, prior, prior.revision)


def _skip_issue(
    path: str, ident: str, revision: str, commit: str, kept: bool
) -> SpecIndexIssue:
    outcome = "previous revision kept" if kept else "not indexed"
    return SpecIndexIssue(
        file=path,
        reason=(
            f"{ident}: block {revision[:12]} is not an approved snapshot of "
            f"{commit[:12]}, {outcome}"
        ),
    )


def _file_rows(
    read: SpecRead,
    rows: list[dict[str, Any]],
    prior_files: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Файлы дерева; файл, на который ссылается оставленный блок, а в дереве его нет, переносится."""
    files, _ = spec_index.rows_of(read.files, read.parsed)
    have = {f["path"] for f in files}
    for old in prior_files:
        if old["path"] not in have and any(r["path"] == old["path"] for r in rows):
            files.append(dict(old))
    return files


def plan_reindex(
    read: SpecRead,
    prior: list[IndexedRequirement],
    prior_files: list[dict[str, Any]],
    approved: Approved,
) -> Plan:
    """Строки нового индекса: принятые блоки дерева плюс прежние там, где доверия нет."""
    before = {r.id: r for r in prior}
    rows: list[dict[str, Any]] = []
    skipped: list[SpecIndexIssue] = []
    seen: set[str] = set()
    for path, reqs in read.parsed.items():
        for req in reqs:
            seen.add(req.id)
            digest = requirement_revision(req)
            old = before.get(req.id)
            if approved.latest.get(req.id) == digest or (
                old and old.revision == digest
            ):
                rows.append(_block_row(path, req, digest))
                continue
            skipped.append(_skip_issue(path, req.id, digest, read.commit, bool(old)))
            if old is not None:
                rows.append(_kept_row(old))
    rows += [
        _kept_row(r)
        for r in prior
        if r.id not in seen and approved.latest.get(r.id) != REMOVED
    ]
    return Plan(_file_rows(read, rows, prior_files), rows, skipped)


async def _indexed_commit(db: aiosqlite.Connection, project_id: int) -> str:
    state = await store.get_state(db, project_id)
    return str(state["commit_sha"]) if state else ""


async def _commits_since(read: SpecRead, old: str) -> tuple[list[str], str]:
    """``(коммиты old..новый старыми первыми, причина)``; причина пуста, только если окно прочитано целиком.

    Непрочитанное или усечённое окно - не «одобрений нет»: по нему нельзя ни
    принять новый текст, ни снять блок, и индекс с курсором оставляют как есть.
    """
    if not old:
        return [], ""
    try:
        span = await plugins.git_ops.spec_range(read.workspace, old, read.commit)
    except Exception as exc:  # noqa: BLE001 - сбой чтения не роняет проход, а отказывает его
        log.warning("spec range of %s not read: %s", read.workspace, exc)
        return [], "the commit window was not read"
    if span.get("state") != "read":
        return (
            [],
            f"the commit window was not read: {span.get('reason') or 'git gave no answer'}",
        )
    if span.get("truncated"):
        return (
            [],
            "the commit window is longer than git_spec.MAX_RANGE_COMMITS, older approvals are cut off",
        )
    return list(span.get("commits") or []), ""


def _window_unread(read: SpecRead, why: str) -> SpecIndexResult:
    return SpecIndexResult(
        state="unreadable",
        ref=read.ref,
        commit=read.commit,
        issues=[SpecIndexIssue(file="", reason=why)],
        detail=f"spec index not rewritten, the cursor stays: {why}",
    )


async def _write(
    db: aiosqlite.Connection,
    project_id: int,
    read: SpecRead,
    old: str,
    commits: list[str],
) -> SpecIndexResult:
    skipped: list[SpecIndexIssue] = []
    if old:
        prior = await store.list_requirements(db, project_id)
        pairs = await store.approved_snapshots(db, project_id, commits)
        approved = approved_from(
            ordered_snapshots(pairs, commits), {r.id: r.revision for r in prior}
        )
        plan = plan_reindex(
            read, prior, await store.list_files(db, project_id), approved
        )
        files, rows, skipped = plan.files, plan.requirements, plan.skipped
    else:
        files, rows = spec_index.rows_of(read.files, read.parsed)
    await store.replace_index(
        db,
        project_id,
        ref=read.ref,
        commit_sha=read.commit,
        files=files,
        requirements=rows,
    )
    return SpecIndexResult(
        state="indexed",
        ref=read.ref,
        commit=read.commit,
        files=len(files),
        requirements=len(rows),
        skipped=skipped,
    )


async def reindex_project(
    db: aiosqlite.Connection, project_id: int, *, fetch: bool = True
) -> SpecIndexResult:
    """Команда переиндексации: ветка по умолчанию -> индекс; отказ индекс не меняет.

    Окно коммитов ``прежний..новый`` нужно прочитать целиком: не прочитанное или
    усечённое окно возвращает ``unreadable`` без записи, курсор остаётся.

    Git читается до транзакции; под write-замком проверяется, что индекс не
    ушёл вперёд за время чтения, иначе сверка по ушедшему коммиту дала бы чужой
    набор одобренных хешей.
    """
    read = await spec_index.read_checked(db, project_id, fetch=fetch)
    if isinstance(read, SpecIndexResult):
        return read
    for _ in range(_ATTEMPTS):
        old = await _indexed_commit(db, project_id)
        commits, why = await _commits_since(read, old)
        if why:
            return _window_unread(read, why)
        async with write_transaction(db):
            if await _indexed_commit(db, project_id) == old:
                return await _write(db, project_id, read, old, commits)
    return SpecIndexResult(
        state="unreadable",
        ref=read.ref,
        commit=read.commit,
        issues=[SpecIndexIssue(file="", reason="the index moved while reading")],
        detail="spec index not rewritten: it moved while reading, run it again",
    )


async def _announce(
    db: aiosqlite.Connection, slug: str, result: SpecIndexResult
) -> None:
    if result.state == "refused":
        log.warning("spec reindex of %s refused: %s", slug, result.detail)
        await log_activity(db, "spec_reindex_refused", f"{slug}: {result.detail}")
    elif result.state == "unreadable":
        log.warning("spec reindex of %s not done: %s", slug, result.detail)
    elif result.skipped:
        names = "; ".join(f"{i.file}: {i.reason}" for i in result.skipped)
        log.warning("spec reindex of %s skipped blocks: %s", slug, names)
        await log_activity(db, "spec_reindex_skipped", f"{slug}: {names}")
    else:
        log.info("spec reindex of %s: %s at %s", slug, result.state, result.commit)


async def _base_moved(project: dict[str, Any], indexed: str) -> str:
    """Новый SHA базовой ветки, если после него есть что читать; иначе пустая строка."""
    workspace, branch = project["workspace_path"], project["default_branch"]
    fetched, why = await plugins.git_ops.fetch_base(workspace, branch)
    if not fetched:
        log.warning("spec reindex: fetch of %s failed: %s", project["slug"], why[:150])
        return ""
    head = await plugins.git_ops.head_sha(workspace, branch)
    if not head or head == indexed or _settled.get(int(project["id"])) == head:
        return ""
    span = await plugins.git_ops.spec_range(workspace, indexed, head)
    if span.get("state") == "read" and not span.get("specs_changed"):
        _settled[int(project["id"])] = head
        return ""
    return head


async def _poll_project(
    db: aiosqlite.Connection, project: dict[str, Any]
) -> SpecIndexResult | None:
    state = await spec_index.index_state(db, int(project["id"]))
    if state is None or not project["workspace_path"] or not project["default_branch"]:
        return None  # первый индекс - явная команда, не событие поллера
    head = await _base_moved(project, str(state["commit_sha"]))
    if not head:
        return None
    result = await reindex_project(db, int(project["id"]), fetch=False)
    if result.state != "unreadable":
        _settled[int(project["id"])] = head
    await _announce(db, str(project["slug"]), result)
    return result


async def reindex_moved_projects(db: aiosqlite.Connection) -> list[SpecIndexResult]:
    """Один ход поллера: каждый проект с индексом, чья база ушла вперёд со ``specs/``."""
    results: list[SpecIndexResult] = []
    for row in await repo.list_projects(db):
        project = dict(row)
        try:
            result = await _poll_project(db, project)
        except Exception:  # noqa: BLE001 - один проект не отменяет остальные
            log.exception("spec reindex of %s failed", project.get("slug"))
            continue
        if result is not None:
            results.append(result)
    return results


async def run_sweep(db: aiosqlite.Connection) -> None:
    """Вход для ``SWEEPS`` поллера: не чаще ``SWEEP_INTERVAL``, ход не роняет тик."""
    now = time.monotonic()
    last = _clock.get("last")
    if last is not None and now - last < SWEEP_INTERVAL:
        return
    _clock["last"] = now
    try:
        await reindex_moved_projects(db)
    except Exception:  # noqa: BLE001 - the sweep must not kill the loop
        log.exception("spec reindex sweep failed")
