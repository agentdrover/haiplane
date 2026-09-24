"""Restore merge-ledger rows lost before #1343, from the hub's own records (#1367).

Before #1343 ``pipeline_merges`` was keyed by PR number, and when the project
moved repositories and numbering started over, the gate's new merges were
dropped silently. The drift guard (#534) then saw them on the base branch
and wrote them into ``base_branch_drift`` as commits "мимо гейта" — so the
signal that should mean "merged by hand" meant mostly "merged by the gate".

This walks the project's drift commits that the ledger does not know and
writes a ledger row ONLY where the hub itself recorded the merge:

* a hub-authored feed record on a task, "PR #N влит" — the forms written by
  the poller's delivery, the human-decision delivery and the registry
  delivery. The task is found through the provider: the merged PR whose
  merge commit IS this sha, whose head branch is the task's branch. Row with
  ``task_id``.
* a release activity "… возвращён в … после релиза" whose detail names this
  sha. Row with ``task_id`` NULL, as the release path writes it.

What is NOT evidence: the "(#N)" in a commit subject, and a PR number alone.
Both are text the person pushing controls (#534), and PR numbers repeat
across repositories (#1343). A real manual merge has no hub record and stays
drift, with the reason named.

Dry run by default. The provider is only read; a provider that does not
answer about one commit leaves that commit in drift with the cause, and the
run goes on. Idempotent: the ledger key is ``merge_sha`` (#1343), and the
drift list read here already leaves out commits the ledger knows.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services.project_policy import forge_of

log = logging.getLogger("hub.merge_ledger_backfill")

EVIDENCE_TASK_FEED = "task_feed"
EVIDENCE_RELEASE_RETURN = "release_return"

# "merge <sha12>; …" — the detail release._return_the_release writes.
_RETURN_DETAIL = re.compile(r"^merge ([0-9a-f]{7,40})\b")


@dataclass
class BackfillLine:
    sha: str
    subject: str
    reason: str
    evidence: str = ""
    task_id: int | None = None
    pr_number: int | None = None
    merged_at: str | None = None


def _merged_record(pr_number: int) -> re.Pattern[str]:
    """«PR #N влит» exactly — not «PR #N не влит», not «PR #N0 влит»."""
    return re.compile(rf"PR #{int(pr_number)} влит(?!\w)")


def _provider_ctx(project: dict[str, Any]) -> dict[str, Any]:
    gh_repo = (project.get("repo") or "").strip() or None
    return {
        "repo": (project.get("workspace_path") or "").strip() or None,
        "gh_repo": gh_repo,
        "forge": forge_of(project) if gh_repo else "",
    }


async def _release_returns(db: aiosqlite.Connection, slug: str) -> dict[str, str]:
    """sha prefix -> timestamp, for this project's recorded release returns."""
    out: dict[str, str] = {}
    for row in await repo.release_return_activities(db):
        r = dict(row)
        if not (r.get("summary") or "").startswith(f"{slug}: "):
            continue
        m = _RETURN_DETAIL.match((r.get("detail") or "").strip())
        if m:
            out[m.group(1)] = r.get("timestamp") or ""
    return out


def _returned_at(sha: str, returns: dict[str, str]) -> str | None:
    for prefix, stamp in returns.items():
        if sha.startswith(prefix):
            return stamp
    return None


async def _hub_merge_record(
    db: aiosqlite.Connection, task_id: int, pr_number: int
) -> str | None:
    """When the hub wrote that it merged this PR on this task, or None."""
    said = _merged_record(pr_number)
    for row in await repo.hub_authored_updates(db, task_id):
        r = dict(row)
        if said.search(r.get("content") or ""):
            return str(r.get("created_at") or "")
    return None


async def _judge_by_task_feed(
    db: aiosqlite.Connection, project: dict[str, Any], line: BackfillLine
) -> BackfillLine:
    try:
        pr = await plugins.git_ops.pr_for_merge_commit(
            line.sha, **_provider_ctx(project)
        )
    except Exception as exc:  # noqa: BLE001 - one commit, one named cause
        line.reason = f"провайдер не ответил: {exc}"
        return line
    if not pr or (pr.get("merge_sha") or "") != line.sha:
        line.reason = "у провайдера нет влитого PR с этим мерж-коммитом"
        return line
    number, head = int(pr["number"]), str(pr.get("head") or "")
    line.pr_number = number
    for task in await repo.tasks_on_branch(db, head) if head else []:
        task_id = int(dict(task)["id"])
        owner = await repo.resolve_project_for_task(db, task_id)
        if owner is None or int(dict(owner)["id"]) != int(project["id"]):
            continue
        said_at = await _hub_merge_record(db, task_id, number)
        if said_at is not None:
            line.evidence, line.task_id, line.merged_at = (
                EVIDENCE_TASK_FEED,
                task_id,
                said_at or None,
            )
            line.reason = f"запись хаба в ленте #{task_id}: PR #{number} влит"
            return line
    line.reason = f"нет записи хаба о мерже PR #{number} (ветка {head or '—'})"
    return line


async def _judge(
    db: aiosqlite.Connection,
    project: dict[str, Any],
    drift: dict[str, Any],
    returns: dict[str, str],
) -> BackfillLine:
    line = BackfillLine(
        sha=str(drift.get("sha") or ""),
        subject=str(drift.get("subject") or ""),
        reason="",
    )
    returned_at = _returned_at(line.sha, returns)
    if returned_at is not None:
        line.evidence, line.pr_number, line.merged_at = (
            EVIDENCE_RELEASE_RETURN,
            0,
            returned_at or None,
        )
        line.reason = "активность release: возврат после релиза с этим sha"
        return line
    return await _judge_by_task_feed(db, project, line)


async def _write(
    db: aiosqlite.Connection, project_id: int, lines: list[BackfillLine]
) -> int:
    written = 0
    for line in lines:
        before = await repo.known_pipeline_shas(db, project_id)
        await repo.record_pipeline_merge(
            db,
            pr_number=int(line.pr_number or 0),
            merge_sha=line.sha,
            project_id=project_id,
            task_id=line.task_id,
            merged_at=line.merged_at,
        )
        written += int(line.sha not in before)
    return written


async def backfill_merge_ledger(
    db: aiosqlite.Connection, *, project: str = "default", apply: bool = False
) -> dict[str, Any]:
    """Split the project's unreconciled drift into "restore" and "stays drift".

    Writes only with ``apply=True``. Raises ``LookupError`` for an unknown
    project slug.
    """
    row = await repo.get_project_by_slug(db, project)
    if row is None:
        raise LookupError(f"проект «{project}» не найден")
    proj = dict(row)
    returns = await _release_returns(db, str(proj.get("slug") or project))
    restore: list[BackfillLine] = []
    drift: list[BackfillLine] = []
    for d in await repo.list_drift_commits(db, int(proj["id"])):
        line = await _judge(db, proj, dict(d), returns)
        (restore if line.evidence else drift).append(line)
    written = await _write(db, int(proj["id"]), restore) if apply else 0
    return {
        "project": proj.get("slug") or project,
        "apply": apply,
        "written": written,
        "restore": [asdict(x) for x in restore],
        "drift": [asdict(x) for x in drift],
    }


def render_backfill(report: dict[str, Any]) -> str:
    """One line per commit: what is (or would be) written, what stays drift."""
    verb = "дописано" if report.get("apply") else "допишется"
    out = [
        f"Проект {report.get('project')}: "
        f"{'--apply' if report.get('apply') else 'сухой прогон, ничего не записано'}",
    ]
    for x in report.get("restore") or []:
        task = f"#{x['task_id']}" if x.get("task_id") else "task_id NULL"
        out.append(
            f"  {verb}: {x['sha'][:12]} PR #{x.get('pr_number')} {task} — "
            f"{x['reason']} | {x['subject'][:70]}"
        )
    for x in report.get("drift") or []:
        out.append(
            f"  остаётся дрейфом: {x['sha'][:12]} — {x['reason']} | {x['subject'][:70]}"
        )
    out.append(
        f"Итого: {verb} {len(report.get('restore') or [])}, "
        f"остаётся дрейфом {len(report.get('drift') or [])}, "
        f"записано строк {report.get('written', 0)}"
    )
    return "\n".join(out)
