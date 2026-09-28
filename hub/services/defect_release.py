"""Which release a defect showed up in, and who may have broken it (#917).

The membership of a release is a recorded fact, not a walk over git history:
when a release PR merges, every unreleased merge of the project is stamped
with the release's merge commit (``pipeline_merges.released_sha``, #950). The
deploy job then reports what production runs (``releases.deployed_sha``,
#495). Joining the two is the whole of "which tasks went out with it".

The matching rule, written once here:

    a merge belongs to a release when both rows are of the same project and
    ``released_sha`` equals ``deployed_sha`` in full, compared after trimming
    and lower-casing.

Why exact. The deploy job reports ``github.sha`` of the push to ``main``, and
that push IS the release PR's merge commit — the very sha
``release._stamp_released_merges`` stores. A prefix match would let two
commits that share twelve characters vouch for each other; a row that does
not match exactly is a merge whose release was never reported (a manual merge,
a develop←main return, an installation whose CI does not call back). Such rows
are left out of every membership and counted instead, so the gap has a number.

A suggestion is a hypothesis. It is shown next to ``caused_by_task_id`` and
recorded in ``defect_cause_suggestions``, but only an explicit confirmation —
a refine that sets ``caused_by_task_id`` — writes the fact column.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.models import DefectCauseCandidate, DefectCauseSuggestion
from hub.services.change_map import areas_overlap

REASON_NO_RELEASE = "релиз дефекта не указан"
REASON_NO_AREAS = "область дефекта не объявлена"
REASON_NO_MEMBERS = "состав релиза неизвестен"
REASON_NO_OVERLAP = "в составе релиза нет задач с пересекающейся областью"


@dataclass(frozen=True)
class ReleaseMembership:
    """Tasks a release carried by the matching rule, and what did not match."""

    release_id: int
    deployed_sha: str = ""
    task_ids: list[int] = field(default_factory=list)
    # Project merges with a released_sha that matches NO recorded deploy.
    unmatched_rows: int = 0


def _sha(value: Any) -> str:
    return str(value or "").strip().lower()


def _areas(raw: Any) -> list[str]:
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
    return [str(a).strip() for a in items if str(a or "").strip()]


async def release_membership(
    db: aiosqlite.Connection, release_id: int
) -> ReleaseMembership:
    """The tasks this release carried, by the rule in the module docstring."""
    rows = await fetchall(
        db, "SELECT project_id, deployed_sha FROM releases WHERE id = ?", (release_id,)
    )
    if not rows:
        return ReleaseMembership(release_id=release_id)
    release = dict(rows[0])
    project_id, wanted = release["project_id"], _sha(release["deployed_sha"])
    deployed = {
        _sha(dict(r)["deployed_sha"])
        for r in await fetchall(
            db, "SELECT deployed_sha FROM releases WHERE project_id IS ?", (project_id,)
        )
    }
    merges = await fetchall(
        db,
        "SELECT task_id, released_sha FROM pipeline_merges "
        "WHERE project_id IS ? AND COALESCE(released_sha, '') != '' ORDER BY id",
        (project_id,),
    )
    task_ids: list[int] = []
    unmatched = 0
    for merge in (dict(m) for m in merges):
        sha = _sha(merge["released_sha"])
        if sha not in deployed:
            unmatched += 1
        elif sha == wanted and merge["task_id"] and merge["task_id"] not in task_ids:
            task_ids.append(int(merge["task_id"]))
    return ReleaseMembership(
        release_id=release_id,
        deployed_sha=wanted,
        task_ids=task_ids,
        unmatched_rows=unmatched,
    )


async def _candidates(
    db: aiosqlite.Connection,
    member_ids: list[int],
    defect_areas: list[str],
    confirmed_id: int | None,
) -> list[DefectCauseCandidate]:
    marks = ",".join("?" for _ in member_ids)
    rows = await fetchall(
        db,
        f"SELECT id, title, affected_areas FROM tasks WHERE id IN ({marks})",  # nosec B608 - placeholders only
        tuple(member_ids),
    )
    by_id = {int(dict(r)["id"]): dict(r) for r in rows}
    found: list[DefectCauseCandidate] = []
    for task_id in member_ids:
        task = by_id.get(task_id)
        if task is None:
            continue
        overlap = [
            f"{mine} ↔ {theirs}"
            for mine in defect_areas
            for theirs in _areas(task["affected_areas"])
            if areas_overlap(mine, theirs)
        ]
        if overlap:
            found.append(
                DefectCauseCandidate(
                    task_id=task_id,
                    title=task["title"] or "",
                    overlap=overlap,
                    confirmed=confirmed_id == task_id,
                )
            )
    return found


async def suggest_causes(
    db: aiosqlite.Connection, defect_task_id: int
) -> DefectCauseSuggestion:
    """Culprit candidates for a defect; an empty list always names its reason."""
    row = await repo.get_task(db, defect_task_id)
    task = dict(row) if row else {}
    release_id = task.get("release_id")
    if release_id is None:
        return DefectCauseSuggestion(reason=REASON_NO_RELEASE)
    membership = await release_membership(db, int(release_id))
    members = [t for t in membership.task_ids if t != defect_task_id]
    areas = _areas(task.get("affected_areas"))
    missing = [
        reason
        for reason, absent in (
            (REASON_NO_AREAS, not areas),
            (REASON_NO_MEMBERS, not members),
        )
        if absent
    ]
    candidates = (
        []
        if missing
        else await _candidates(db, members, areas, task.get("caused_by_task_id"))
    )
    if not missing and not candidates:
        missing = [REASON_NO_OVERLAP]
    return DefectCauseSuggestion(
        release_id=int(release_id),
        release_sha=membership.deployed_sha,
        candidates=candidates,
        reason="; ".join(missing),
        unmatched_rows=membership.unmatched_rows,
    )


async def record_suggestions(db: aiosqlite.Connection, defect_task_id: int) -> int:
    """Record each proposed candidate once per release; return how many are new.

    Rows are never rewritten or deleted here: the share of suggestions that
    got confirmed is measured over what WAS proposed, so a later area edit
    that drops a candidate must not erase that it had been proposed.
    """
    suggestion = await suggest_causes(db, defect_task_id)
    added = 0
    for candidate in suggestion.candidates:
        cur = await db.execute(
            "INSERT OR IGNORE INTO defect_cause_suggestions "
            "(defect_task_id, candidate_task_id, release_id, overlap) "
            "VALUES (?, ?, ?, ?)",
            (
                defect_task_id,
                candidate.task_id,
                suggestion.release_id,
                json.dumps(candidate.overlap, ensure_ascii=False),
            ),
        )
        added += cur.rowcount or 0
    return added
