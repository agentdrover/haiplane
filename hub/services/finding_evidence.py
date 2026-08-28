"""Whether a finding's place was touched after the report (#1039).

This is a fact, not a disposition. A commit that edited the named lines is
not the same as «the defect is fixed», and the hub must not pretend it is
(#876): no radio is pre-checked, nothing is written to finding_dispositions.

Three answers, never two (#762). «Could not look» is not «untouched».
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.integrations.git_ops import _git
from hub.models import FindingLocator
from hub.services.orchestration import project_git_context

OUTCOME_TOUCHED = "touched"
OUTCOME_UNTOUCHED = "untouched"
OUTCOME_UNKNOWN = "unknown"

REASON_NO_CLONE = "no_clone"
REASON_SHA_MISSING = "sha_missing"
REASON_LOCATOR_NONE = "locator_none"
REASON_LOCATOR_MISSING = "locator_missing"
REASON_TIP_MISSING = "tip_missing"
REASON_GIT_FAILED = "git_failed"
REASON_LINES_UNAVAILABLE = "lines_unavailable"

_REASON_LABELS = {
    REASON_NO_CLONE: "нет рабочего клона",
    REASON_SHA_MISSING: "sha генерации отчёта не найден",
    REASON_LOCATOR_NONE: "локатор none — места нет",
    REASON_LOCATOR_MISSING: "локатор не назван",
    REASON_TIP_MISSING: "кончик ветки не найден",
    REASON_GIT_FAILED: "git не смог посчитать",
    REASON_LINES_UNAVAILABLE: "строки в этой истории недоступны",
}


@dataclass(frozen=True)
class TouchCommit:
    sha: str
    subject: str


@dataclass(frozen=True)
class TouchEvidence:
    outcome: str
    reason: str = ""
    commits: tuple[TouchCommit, ...] = ()

    @property
    def reason_label(self) -> str:
        return _REASON_LABELS.get(self.reason, self.reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "reason_label": self.reason_label,
            "commits": [
                {"sha": c.sha, "subject": c.subject, "short": c.sha[:12]}
                for c in self.commits
            ],
        }


def _unknown(reason: str) -> TouchEvidence:
    return TouchEvidence(outcome=OUTCOME_UNKNOWN, reason=reason)


def _finding_locator(finding: dict[str, Any]) -> str:
    raw = finding.get("locator")
    if raw is None:
        return ""
    if isinstance(raw, FindingLocator):
        return raw.value
    return str(raw).strip()


async def finding_touch_evidence(
    db: aiosqlite.Connection,
    task_id: int,
    finding: dict[str, Any],
    *,
    generation: int,
) -> TouchEvidence:
    """What happened to this finding's place after the report of ``generation``."""
    locator = _finding_locator(finding)
    if locator == FindingLocator.none.value:
        return _unknown(REASON_LOCATOR_NONE)
    if not locator:
        return _unknown(REASON_LOCATOR_MISSING)

    ctx = await project_git_context(db, task_id)
    clone = (ctx.get("repo") or "").strip()
    if not clone:
        return _unknown(REASON_NO_CLONE)

    pinned = await repo.get_submission(db, task_id, generation)
    baseline = ((pinned["sha"] if pinned is not None else "") or "").strip()
    if not baseline:
        return _unknown(REASON_SHA_MISSING)

    task = await repo.get_task(db, task_id)
    tip = await _resolve_tip(clone, dict(task) if task is not None else {})
    if not tip:
        return _unknown(REASON_TIP_MISSING)

    exists = await _commit_exists(clone, baseline)
    if exists is None:
        return _unknown(REASON_GIT_FAILED)
    if exists is False:
        return _unknown(REASON_SHA_MISSING)

    path = (finding.get("file") or "").strip()
    if locator == FindingLocator.lines.value:
        start = finding.get("start_line")
        if start is None:
            start = finding.get("line")
        end = finding.get("end_line")
        if end is None:
            end = start
        if not path or start is None or end is None:
            return _unknown(REASON_LOCATOR_MISSING)
        commits = await _commits_on_lines(
            clone, baseline, tip, path, int(start), int(end)
        )
        if commits is None:
            return _unknown(REASON_LINES_UNAVAILABLE)
    else:
        if not path:
            return _unknown(REASON_LOCATOR_MISSING)
        commits = await _commits_on_file(clone, baseline, tip, path)
        if commits is None:
            return _unknown(REASON_GIT_FAILED)

    if commits:
        return TouchEvidence(outcome=OUTCOME_TOUCHED, commits=tuple(commits))
    return TouchEvidence(outcome=OUTCOME_UNTOUCHED)


async def evidence_for_findings(
    db: aiosqlite.Connection,
    task_id: int,
    findings: list[dict[str, Any]],
    *,
    generation: int,
) -> list[dict[str, Any]]:
    """Per-finding dicts for the card. Never writes a disposition."""
    out: list[dict[str, Any]] = []
    for finding in findings:
        row = finding if isinstance(finding, dict) else {}
        evidence = await finding_touch_evidence(db, task_id, row, generation=generation)
        out.append(evidence.as_dict())
    return out


async def _resolve_tip(clone: str, task: dict[str, Any]) -> str:
    branch = (task.get("branch") or "").strip()
    candidates: list[str] = []
    if branch:
        candidates.extend((branch, f"origin/{branch}"))
    candidates.append("HEAD")
    for ref in candidates:
        rc, out, _ = await _git("rev-parse", "--verify", ref, repo=clone, check=False)
        if rc == 0 and (out or "").strip():
            return out.strip()
    return ""


async def _commit_exists(clone: str, sha: str) -> bool | None:
    rc, _, _ = await _git("rev-parse", "--git-dir", repo=clone, check=False)
    if rc != 0:
        return None
    rc, _, _ = await _git(
        "cat-file", "-e", f"{sha}^{{commit}}", repo=clone, check=False
    )
    if rc != 0:
        return False
    return True


async def _commits_on_file(
    clone: str, since: str, until: str, path: str
) -> list[TouchCommit] | None:
    rc, out, _ = await _git(
        "log",
        "--format=%H%x09%s",
        "-s",
        f"{since}..{until}",
        "--",
        path,
        repo=clone,
        check=False,
    )
    if rc != 0:
        return None
    return _parse_log(out)


async def _commits_on_lines(
    clone: str,
    since: str,
    until: str,
    path: str,
    start: int,
    end: int,
) -> list[TouchCommit] | None:
    spec = f"{start},{end}:{path}"
    rc, out, _ = await _git(
        "log",
        f"-L{spec}",
        "-s",
        "--format=%H%x09%s",
        f"{since}..{until}",
        repo=clone,
        check=False,
    )
    if rc != 0:
        return None
    return _parse_log(out)


def _parse_log(out: str) -> list[TouchCommit]:
    commits: list[TouchCommit] = []
    seen: set[str] = set()
    for raw in (out or "").splitlines():
        line = raw.strip()
        if not line or "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        if sha in seen:
            continue
        seen.add(sha)
        commits.append(TouchCommit(sha=sha, subject=subject))
    return commits
