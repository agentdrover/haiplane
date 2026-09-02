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
from hub.services.finding_identity import finding_uids
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


async def evidence_for_report(
    db: aiosqlite.Connection,
    task_id: int,
    findings: list[dict[str, Any]],
    *,
    generation: int,
    head: str = "",
) -> dict[str, dict[str, Any]]:
    """Touch evidence for one report, keyed by ``finding_uid``.

    One ``git log`` covers every placed finding in the report. The card still
    walks findings one by one; the queue cannot — a call per finding is what
    would make /findings unusable at the live size (#1042). Address by uid,
    not by position (#1007). Never writes a disposition (#876).

    ``head`` names the commit to stop at. Empty means the live branch tip —
    the question the card and the queue ask. A caller deciding something
    ABOUT A SUBMISSION passes that submission's pinned sha instead (#1150):
    a branch name is a moving target, and by the time the question is asked
    it may stand somewhere the submission never did.
    """
    rows = [f if isinstance(f, dict) else {} for f in findings]
    uids = finding_uids(rows)
    out: dict[str, dict[str, Any]] = {}
    placed: list[tuple[str, dict[str, Any]]] = []
    for uid, row in zip(uids, rows, strict=True):
        locator = _finding_locator(row)
        if locator == FindingLocator.none.value:
            out[uid] = _unknown(REASON_LOCATOR_NONE).as_dict()
            continue
        if not locator:
            out[uid] = _unknown(REASON_LOCATOR_MISSING).as_dict()
            continue
        placed.append((uid, row))

    if not placed:
        return out

    shared = await _shared_lookup(db, task_id, generation, head)
    if isinstance(shared, TouchEvidence):
        blob = shared.as_dict()
        for uid, _row in placed:
            out[uid] = blob
        return out

    clone, baseline, tip = shared
    paths: list[str] = []
    seen_paths: set[str] = set()
    for uid, row in placed:
        path = (row.get("file") or "").strip()
        locator = _finding_locator(row)
        if locator == FindingLocator.lines.value:
            start = row.get("start_line")
            if start is None:
                start = row.get("line")
            end = row.get("end_line")
            if end is None:
                end = start
            if not path or start is None or end is None:
                out[uid] = _unknown(REASON_LOCATOR_MISSING).as_dict()
                continue
        elif not path:
            out[uid] = _unknown(REASON_LOCATOR_MISSING).as_dict()
            continue
        if path not in seen_paths:
            seen_paths.add(path)
            paths.append(path)

    still = [(uid, row) for uid, row in placed if uid not in out]
    if not still:
        return out
    if not paths:
        for uid, _row in still:
            out[uid] = _unknown(REASON_LOCATOR_MISSING).as_dict()
        return out

    parsed = await _log_hunks(clone, baseline, tip, paths)
    if parsed is None:
        blob = _unknown(REASON_GIT_FAILED).as_dict()
        for uid, _row in still:
            out[uid] = blob
        return out

    for uid, row in still:
        commits = _commits_for_finding(row, parsed)
        if commits is None:
            out[uid] = _unknown(REASON_LINES_UNAVAILABLE).as_dict()
            continue
        if commits:
            out[uid] = TouchEvidence(
                outcome=OUTCOME_TOUCHED, commits=tuple(commits)
            ).as_dict()
        else:
            out[uid] = TouchEvidence(outcome=OUTCOME_UNTOUCHED).as_dict()
    return out


async def _shared_lookup(
    db: aiosqlite.Connection, task_id: int, generation: int, head: str = ""
) -> tuple[str, str, str] | TouchEvidence:
    """Клон, отправная точка и ВЕРШИНА, до которой считать.

    ``head`` пустой — вершина берётся по имени ветки, как было. Это верно
    для карточки и очереди: они спрашивают «трогал ли кто-нибудь находку с
    тех пор», и ответ про живую ветку.

    ``head`` задан — считается ровно до него, и это другой вопрос. Решение,
    принимаемое О СДАЧЕ, обязано читать закреплённый ею sha: ветка —
    движущаяся цель, и к моменту вопроса она может стоять не там, где
    стояла сдача (#572 и весь класс за ним). Ответ по имени ветки описывал
    бы код, которого никто не сдавал.
    """
    ctx = await project_git_context(db, task_id)
    clone = (ctx.get("repo") or "").strip()
    if not clone:
        return _unknown(REASON_NO_CLONE)

    pinned = await repo.get_submission(db, task_id, generation)
    baseline = ((pinned["sha"] if pinned is not None else "") or "").strip()
    if not baseline:
        return _unknown(REASON_SHA_MISSING)

    if head:
        tip = head.strip()
        exists_head = await _commit_exists(clone, tip)
        if exists_head is None:
            return _unknown(REASON_GIT_FAILED)
        if exists_head is False:
            # Закреплённого коммита в клоне нет: «не удалось посмотреть», а
            # не «ничего не менялось». Разница здесь стоит целого прогона.
            return _unknown(REASON_TIP_MISSING)
    else:
        task = await repo.get_task(db, task_id)
        tip = await _resolve_tip(clone, dict(task) if task is not None else {})
    if not tip:
        return _unknown(REASON_TIP_MISSING)

    exists = await _commit_exists(clone, baseline)
    if exists is None:
        return _unknown(REASON_GIT_FAILED)
    if exists is False:
        return _unknown(REASON_SHA_MISSING)
    return clone, baseline, tip


@dataclass(frozen=True)
class _Hunk:
    path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass
class _PatchLog:
    commits: list[TouchCommit]
    hunks: dict[str, list[_Hunk]]
    files: dict[str, set[str]]


async def _log_hunks(
    clone: str, since: str, until: str, paths: list[str]
) -> _PatchLog | None:
    rc, out, _ = await _git(
        "log",
        "--format=%x1e%H%x09%s",
        "-p",
        "-U0",
        f"{since}..{until}",
        "--",
        *paths,
        repo=clone,
        check=False,
    )
    if rc != 0:
        return None
    return _parse_patch_log(out)


def _parse_patch_log(out: str) -> _PatchLog:
    commits: list[TouchCommit] = []
    hunks: dict[str, list[_Hunk]] = {}
    files: dict[str, set[str]] = {}
    current_sha = ""
    current_path = ""
    for raw_record in (out or "").split("\x1e"):
        record = raw_record.strip("\n")
        if not record:
            continue
        header, _, rest = record.partition("\n")
        if "\t" not in header:
            continue
        sha, subject = header.split("\t", 1)
        sha = sha.strip()
        if not sha:
            continue
        commits.append(TouchCommit(sha=sha, subject=subject.strip()))
        hunks[sha] = []
        files[sha] = set()
        current_sha = sha
        current_path = ""
        for line in rest.splitlines():
            if line.startswith("--- ") or line.startswith("+++ "):
                parsed_path = _path_from_diff_header(line[4:])
                if parsed_path:
                    current_path = parsed_path
                    files[current_sha].add(parsed_path)
                continue
            if line.startswith("diff --git "):
                current_path = ""
                continue
            if not line.startswith("@@ ") or not current_sha:
                continue
            parsed = _parse_hunk_header(line)
            if parsed is None or not current_path:
                continue
            old_start, old_count, new_start, new_count = parsed
            hunks[current_sha].append(
                _Hunk(current_path, old_start, old_count, new_start, new_count)
            )
    return _PatchLog(commits=commits, hunks=hunks, files=files)


def _path_from_diff_header(raw: str) -> str:
    token = raw.strip().split("\t", 1)[0]
    if token in {"/dev/null", "nul"}:
        return ""
    if token.startswith("b/") or token.startswith("a/"):
        return token[2:]
    return token


def _parse_hunk_header(line: str) -> tuple[int, int, int, int] | None:
    body = line.strip()
    if not body.startswith("@@"):
        return None
    parts = body.split("@@")
    if len(parts) < 2:
        return None
    span = parts[1].strip()
    tokens = span.split()
    if len(tokens) < 2:
        return None
    old = _parse_span(tokens[0], prefix="-")
    new = _parse_span(tokens[1], prefix="+")
    if old is None or new is None:
        return None
    return old[0], old[1], new[0], new[1]


def _parse_span(token: str, *, prefix: str) -> tuple[int, int] | None:
    if not token.startswith(prefix):
        return None
    body = token[len(prefix) :]
    if "," in body:
        start_s, count_s = body.split(",", 1)
        try:
            return int(start_s), int(count_s)
        except ValueError:
            return None
    try:
        return int(body), 1
    except ValueError:
        return None


def _hunk_overlaps(hunk: _Hunk, start: int, end: int) -> bool:
    lo, hi = (start, end) if start <= end else (end, start)

    def _span(at: int, count: int) -> tuple[int, int]:
        if count <= 0:
            return at, at
        return at, at + count - 1

    old_lo, old_hi = _span(hunk.old_start, hunk.old_count)
    new_lo, new_hi = _span(hunk.new_start, hunk.new_count)
    return (old_lo <= hi and lo <= old_hi) or (new_lo <= hi and lo <= new_hi)


def _commits_for_finding(
    row: dict[str, Any], parsed: _PatchLog
) -> list[TouchCommit] | None:
    path = (row.get("file") or "").strip()
    locator = _finding_locator(row)
    matched: list[TouchCommit] = []
    if locator == FindingLocator.lines.value:
        start = row.get("start_line")
        if start is None:
            start = row.get("line")
        end = row.get("end_line")
        if end is None:
            end = start
        if start is None or end is None:
            return None
        start_i, end_i = int(start), int(end)
        for commit in parsed.commits:
            if any(
                hunk.path == path and _hunk_overlaps(hunk, start_i, end_i)
                for hunk in parsed.hunks.get(commit.sha, ())
            ):
                matched.append(commit)
        return matched
    for commit in parsed.commits:
        if path in parsed.files.get(commit.sha, ()):
            matched.append(commit)
    return matched


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
