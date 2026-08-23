"""Notice when the base branch goes red, and say what met there (#929).

Measured in #921 over 12.3 hours: the base was red for 574 of 738 minutes —
78% of the window — across two breakages. Both were caught by push-CI at the
moment of the merge, to the second and to the commit. Nothing followed. The
first stood for 69 minutes, the second for 8 hours 25 minutes and was found
by a human busy with another task, while merges kept landing into it.

So this adds no detection. It adds the part that was missing: the fact
reaching someone who can answer it, with the pair that met already named.

WHAT MET IS COMPUTED, NOT GUESSED. The commits between the last GREEN run of
the base and the first red one are exactly the work that arrived while the
base was believed good. In both measured cases that interval held the answer:
1ea5e120 (#877) against e60fef5c (#847), fe8759dd (#892) against 0c69ba10
(#878). The whole interval is reported, never a guess at the culprit inside
it — narrowing is the reader's job, and a wrong name is worse than a list.

THREE STATES, NOT TWO. Unreadable CI history is "unknown" with a reason, not
"green". A guard that reports health when it cannot see is the failure mode
this codebase keeps re-learning (#725, #839, #885).

ONE SIGNAL PER BREAKAGE. While the base stays red the event is not repeated;
a new one waits until it has gone green and broken again. The drift guard
(#534) learned this the expensive way: a line per poll cycle is how a real
signal gets muted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import log_activity
from hub.integrations.registry import plugins
from hub.services.project_policy import base_branch_of

log = logging.getLogger("hub.red_base")

RED = "red"
GREEN = "green"
UNKNOWN = "unknown"

EVENT_KIND = "base_branch_red"

# Conclusions GitHub gives a run that did not pass. Mirrors the PR probe's
# mapping (#419/#605): an unrecognised conclusion is never read as success.
_FAILED = ("failure", "cancelled", "timed_out", "startup_failure", "action_required")
_PASSED = ("success", "neutral", "skipped")


@dataclass
class BaseCiState:
    """What the base branch's CI says right now."""

    status: str
    branch: str
    reason: str
    red_sha: str = ""
    last_green_sha: str = ""
    met: list[str] = field(default_factory=list)


def read_state(branch: str, runs: list[dict[str, Any]] | None) -> BaseCiState:
    """Turn the branch's run history into one of three answers.

    Pure: the caller does the fetching, so the interesting part stays testable
    without a network. ``runs`` is newest first, as GitHub returns it.
    """
    if runs is None:
        return BaseCiState(UNKNOWN, branch, "историю прогонов CI прочитать не удалось")
    finished = [r for r in runs if r.get("status") == "completed"]
    if not finished:
        # Runs exist but none has finished — the base is mid-flight, and
        # calling that green would be a guess about a result nobody has yet.
        return BaseCiState(
            UNKNOWN,
            branch,
            "завершённых прогонов нет: ветка в процессе проверки"
            if runs
            else "прогонов CI по этой ветке нет",
        )
    newest = finished[0]
    if newest.get("conclusion") in _PASSED:
        return BaseCiState(
            GREEN, branch, "последний прогон зелёный", last_green_sha=newest["sha"]
        )
    if newest.get("conclusion") not in _FAILED:
        return BaseCiState(
            UNKNOWN,
            branch,
            f"исход прогона не распознан: {newest.get('conclusion') or 'пусто'}",
        )
    # Red. Walk back to the last green to bound what arrived in between; the
    # first red run is the one that matters, not the latest, because every
    # run after it is red for the same reason.
    first_red = newest
    last_green = ""
    for run in finished:
        if run.get("conclusion") in _PASSED:
            last_green = run["sha"]
            break
        if run.get("conclusion") in _FAILED:
            first_red = run
    return BaseCiState(
        RED,
        branch,
        "последний прогон красный",
        red_sha=first_red["sha"],
        last_green_sha=last_green,
    )


async def _commits_between(
    project: dict[str, Any], last_green: str, red_sha: str
) -> list[str]:
    """Subjects that arrived between the last green base and the red one."""
    if not last_green or not red_sha:
        return []
    try:
        return await plugins.git_ops.release_range(
            last_green,
            red_sha,
            repo=(project.get("workspace_path") or "").strip() or None,
            gh_repo=(project.get("repo") or "").strip() or None,
        )
    except Exception as exc:  # noqa: BLE001 - the event must still be emitted
        log.warning("commits between %s..%s unreadable: %s", last_green, red_sha, exc)
        return []


async def already_announced(
    db: aiosqlite.Connection, project_id: int, sha: str
) -> bool:
    """Has this breakage already been announced? (AC-3)

    Keyed by the SHA of the first red run: while the base stays broken the
    same SHA comes back every cycle, and a second event about it would add
    nothing except noise. A NEW breakage carries a new SHA and is announced.
    """
    events = await repo.list_events(db, kinds=[EVENT_KIND], limit=50)
    for row in events:
        payload = dict(row).get("payload") or {}
        if isinstance(payload, str):
            import json

            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if payload.get("sha") == sha and dict(row).get("project_id") == project_id:
            return True
    return False


async def check_project(db: aiosqlite.Connection, project_row: Any) -> BaseCiState:
    """Look at one project's base branch and announce a fresh breakage."""
    project = dict(project_row)
    branch = base_branch_of(project_row)
    runs = await plugins.git_ops.branch_ci_runs(
        branch,
        repo=(project.get("workspace_path") or "").strip() or None,
        gh_repo=(project.get("repo") or "").strip() or None,
    )
    state = read_state(branch, runs)
    if state.status != RED:
        return state
    if await already_announced(db, project["id"], state.red_sha):
        return state
    state.met = await _commits_between(project, state.last_green_sha, state.red_sha)
    await repo.insert_event(
        db,
        kind=EVENT_KIND,
        project_id=project["id"],
        actor="hub",
        payload={
            "sha": state.red_sha,
            "branch": branch,
            "project": project.get("slug") or "",
            "last_green_sha": state.last_green_sha,
            "met": state.met[:20],
        },
    )
    met_note = (
        "; встретились: " + ", ".join(s[:80] for s in state.met[:5])
        if state.met
        else "; что встретилось — определить не удалось"
    )
    await log_activity(
        db,
        EVENT_KIND,
        f"{branch} проекта {project.get('slug') or project['id']} красный "
        f"на {state.red_sha[:12]}"
        + (
            f" (последний зелёный {state.last_green_sha[:12]})"
            if state.last_green_sha
            else ""
        )
        + met_note,
    )
    await db.commit()
    log.warning(
        "base branch %s of %s is red at %s",
        branch,
        project.get("slug"),
        state.red_sha[:12],
    )
    return state


async def check_all_projects(db: aiosqlite.Connection) -> list[BaseCiState]:
    """Every active project's base branch, one pass."""
    states = []
    for row in await repo.list_projects(db):
        project = dict(row)
        if project.get("archived"):
            continue
        try:
            states.append(await check_project(db, row))
        except Exception as exc:  # noqa: BLE001 - one project must not stop the rest
            log.warning("red-base check failed for %s: %s", project.get("slug"), exc)
            states.append(
                BaseCiState(
                    UNKNOWN, base_branch_of(row), f"проверка не выполнена: {exc}"
                )
            )
    return states


__all__ = [
    "EVENT_KIND",
    "GREEN",
    "RED",
    "UNKNOWN",
    "BaseCiState",
    "check_all_projects",
    "check_project",
    "read_state",
]
