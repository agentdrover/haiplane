"""Detect commits that reached a base branch outside the merge pipeline (#534).

The repository is private on GitHub's free plan, so branch protection and
rulesets are unavailable: nothing on the server can refuse a direct push to
``main`` or ``develop``. The client-side pre-push hook is bypassed by
``--no-verify`` and absent from clones without ``core.hooksPath``.

So this replaces the impossible "prevent" with the achievable "notice". A
violation stays possible; it stops being invisible.

THREE OUTCOMES, NOT TWO. "No drift" and "could not check" are different
answers and are never collapsed. On production the ``default`` project — the
hub's own repository — has an empty ``repo``, a ``workspace_path`` that does
not exist, and a fallback clone with no remote at all; ``gh`` is not
installed either. A guard that reported "clean" there would be reporting on
nothing while the milestone read as delivered.

WHAT COUNTS AS EXPECTED. The hub merges through ``gh pr merge --squash``, so
its own merges land on the base as ordinary non-merge commits — the same
shape a direct push produces. Graph shape alone cannot separate them.

Submission #1 took a "(#N)" in the subject as proof of a pull request, and a
direct push titled ``hotfix: bypass auth (#42)`` passed as legitimate.
Submission #2 required that number to be one the hub had merged — and review
showed that only moved the goalpost: the number is still text the pusher
controls, and a merged one is a git log away.

So the evidence is the commit itself. The hub records the SHA its merge
produced, and a commit on the base is expected only when its SHA is one of
those. A pusher cannot choose the SHA, so there is nothing left to type.

WHY A BASELINE. History written before the hub started recording its own
merges cannot be judged: those merges are real but unrecorded. Reporting them
would bury the operator under alerts about correct work — the exact noise the
recorded risk warns destroys the signal. The first check stores where the
base stood and reports nothing; judgement starts from there.

The bias is deliberate and matches the risk recorded on the task: when in
doubt, miss a drift rather than raise a false alert. An alert that cries wolf
gets muted, and then the real one is missed too.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import aiosqlite

from hub import repository as repo
from hub.db import log_activity
from hub.integrations.registry import plugins
from hub.services.project_policy import base_branch_of

log = logging.getLogger("hub.drift_guard")

# Pull-request number as gh writes it: "(#123)" on a squash, "Merge pull
# request #123" on a merge commit. Extracting the NUMBER matters — the
# pattern alone proves nothing, only a number the hub merged does.
_PR_NUMBER = re.compile(r"\(#(\d+)\)|Merge pull request #(\d+)")


def pr_number_in(subject: str) -> int | None:
    """The PR number a commit subject claims, if any."""
    m = _PR_NUMBER.search(subject or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


# How far back to look. A base branch that drifted long ago is history, not
# news; re-reporting it on every run would be the duplicate noise AC-5 forbids.
DEFAULT_LOOKBACK = 50


@dataclass
class DriftCommit:
    sha: str
    subject: str
    author: str


@dataclass
class DriftReport:
    """Outcome of checking one project's base branch.

    ``status`` is one of:
      drift   — commits reached the base outside the pipeline
      clean   — the base was checked and nothing unexpected is on it
      unknown — the check could not run; ``reason`` says why
    """

    project_slug: str
    base_branch: str
    status: str
    reason: str = ""
    commits: list[DriftCommit] = field(default_factory=list)

    @property
    def checked(self) -> bool:
        return self.status != "unknown"


def parse_log(raw: str) -> list[DriftCommit]:
    """Parse ``git log`` output. Format: ``sha\\x1fsubject\\x1fauthor`` per line.

    A unit separator rather than a space, because subjects contain spaces. A
    line the format did not produce is skipped: it is not evidence of
    anything, and guessing at it would invent drift.
    """
    out: list[DriftCommit] = []
    for line in (raw or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, subject, author = (p.strip() for p in parts)
        if sha:
            out.append(DriftCommit(sha=sha, subject=subject, author=author))
    return out


def classify_commits(
    raw: str,
    known_shas: set[str] | None = None,
    baseline_sha: str | None = None,
) -> list[DriftCommit]:
    """Commits that reached the base outside the pipeline, newest first.

    ``known_shas`` are the commits the hub's own merges produced. Matching on
    the SHA rather than on anything in the subject is the point: the subject
    is written by whoever pushes, the SHA is not.

    ``baseline_sha`` stops the walk: everything at or below it predates the
    guard and cannot be judged.
    """
    known = known_shas or set()
    out: list[DriftCommit] = []
    for commit in parse_log(raw):
        if baseline_sha and commit.sha == baseline_sha:
            break
        if commit.sha in known:
            continue
        out.append(commit)
    return out


async def check_project(
    project: dict,
    *,
    db: aiosqlite.Connection | None = None,
    lookback: int = DEFAULT_LOOKBACK,
) -> DriftReport:
    """Check one project's base branch. Never raises — a failure is ``unknown``."""
    slug = project.get("slug") or "?"
    # Never hardcoded: calc-kids lives on master while the hub lives on develop.
    # #475: resolved by the one reader every gate uses, so a project that
    # declares no branch is watched on the configured default instead of being
    # dropped from the watch entirely — silence here reads as "clean".
    base = base_branch_of(project)
    workspace = (project.get("workspace_path") or "").strip()

    if not workspace:
        return DriftReport(slug, base, "unknown", "project has no workspace_path")

    try:
        ok, detail = await plugins.git_ops.fetch_base(workspace, base)
    except Exception as exc:  # noqa: BLE001 - best effort by contract (AC-4)
        log.warning("drift check for %s: fetch raised %s", slug, exc)
        return DriftReport(slug, base, "unknown", f"fetch failed: {exc}")
    if not ok:
        return DriftReport(slug, base, "unknown", f"fetch failed: {detail}")

    try:
        raw = await plugins.git_ops.first_parent_log(workspace, base, lookback)
    except Exception as exc:  # noqa: BLE001
        log.warning("drift check for %s: log raised %s", slug, exc)
        return DriftReport(slug, base, "unknown", f"log failed: {exc}")
    if raw is None:
        return DriftReport(slug, base, "unknown", "could not read the base branch log")

    parsed = parse_log(raw)
    baseline = (project.get("drift_baseline_sha") or "").strip()

    if not baseline:
        # First look at this project: record where the base stands and judge
        # nothing. Everything below is history the hub never observed.
        head = parsed[0].sha if parsed else ""
        if db is not None and head:
            await repo.set_drift_baseline(db, project["id"], head)
        return DriftReport(
            slug,
            base,
            "clean",
            reason="baseline recorded on first check; history before it is not judged",
        )

    known: set[str] = set()
    if db is not None:
        known = await repo.known_pipeline_shas(db, project["id"])

    commits = classify_commits(raw, known_shas=known, baseline_sha=baseline)

    # Name the range that was actually read. The window is fixed and the
    # baseline never moves, so after enough merges the baseline falls out the
    # bottom and "clean" quietly narrows from "clean" to "clean in the last
    # fifty" — a distinction the operator cannot make from the word alone.
    covered = f"checked the last {lookback} commits on {base}"
    if not any(c.sha == baseline for c in parsed):
        covered += "; the baseline is older than that window, so anything before it was not read"

    if not commits:
        return DriftReport(slug, base, "clean", reason=covered)
    return DriftReport(slug, base, "drift", reason=covered, commits=commits)


async def check_all_projects(
    db: aiosqlite.Connection, *, lookback: int = DEFAULT_LOOKBACK
) -> list[DriftReport]:
    """Check every active project and record new drift exactly once."""
    rows = await repo.list_projects(db)
    reports: list[DriftReport] = []
    for row in rows:
        project = dict(row)
        if project.get("status") != "active" or project.get("archived"):
            continue
        report = await check_project(project, db=db, lookback=lookback)
        reports.append(report)
        if report.status == "drift":
            await _record_new_drift(db, project, report)
    return reports


async def _record_new_drift(
    db: aiosqlite.Connection, project: dict, report: DriftReport
) -> int:
    """Persist unseen drift commits. Returns how many were new.

    Idempotent by (project_id, sha): a second run over the same drift records
    nothing and therefore alerts about nothing. A commit a human has already
    accepted stays accepted — rows are never reopened, only inserted.
    """
    new = 0
    for commit in report.commits:
        inserted = await repo.record_drift_commit(
            db,
            project_id=project["id"],
            sha=commit.sha,
            branch=report.base_branch,
            subject=commit.subject[:500],
            author=commit.author[:200],
        )
        if not inserted:
            continue
        new += 1
        log.warning(
            "drift: %s reached %s of project %s outside the merge pipeline (%s by %s)",
            commit.sha[:12],
            report.base_branch,
            report.project_slug,
            commit.subject[:80],
            commit.author,
        )
        # A row in a table nobody opens is not an alert. The operator reads
        # the activity feed, so the drift has to appear there — the review of
        # submission #1 was right that recording it privately changes nothing
        # (#534).
        await repo.insert_event(
            db,
            kind="base_branch_drift",
            project_id=project["id"],
            actor=commit.author[:100],
            payload={
                "sha": commit.sha,
                "branch": report.base_branch,
                "subject": commit.subject[:300],
                "project": report.project_slug,
            },
        )
        await log_activity(
            db,
            "base_branch_drift",
            f"{commit.sha[:12]} reached {report.base_branch} of "
            f"{report.project_slug} outside the merge pipeline: "
            f"{commit.subject[:120]}",
        )
    await db.commit()
    return new


__all__ = [
    "DEFAULT_LOOKBACK",
    "DriftCommit",
    "DriftReport",
    "check_all_projects",
    "check_project",
    "classify_commits",
    "parse_log",
    "pr_number_in",
]
