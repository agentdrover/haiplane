"""Hub-dispatched cross-model reviews (#757, feature #738).

Until now the machine review was launched by the implementer (or its
orchestrator) — the reviewed party chose its reviewer. This module moves
the call to the hub: a submission in a ``verdict=auto`` project queues a
Cursor cloud agent on the task branch, with a model whose FAMILY differs
from the implementer's declaration (#758), the hub's own MCP inline under
the dedicated reviewer principal, and explicit prohibitions on writing.

Everything is best-effort by contract: a failed dispatch alerts once and
changes nothing — the verdict simply stays with the human, exactly as it
would have without this module. The poller walks active dispatches; a run
that finished without a report for its generation fails LOUDLY (a stamp
factory must never look like coverage — the cursor_cloud lesson), and a
report whose token count disagrees with the provider's own usage numbers
is flagged: cross-checking against the provider closes the #750 class of
stamps with data instead of discipline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, NamedTuple

import aiosqlite

from hub.db import fetchall
from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.integrations import local_reviewer
from hub.integrations import forge as forge_urls
from hub.integrations.registry import plugins
from hub.models import RiskClass
from hub.services import project_policy
from hub.services.model_family import family
from hub.services.project_policy import gate_policy_of, review_dispatch_enabled

log = logging.getLogger(__name__)

# Real ids from GET /v1/models (checked live 2026-08-20). Order = preference;
# the first whose family differs from the implementer's wins.
#
# Narrowed to the subscription on 2026-08-28 (#1036). Everything outside it
# refuses at creation with HTTP 400 usage_limit_exceeded ("Background Agent
# requires at least $2 remaining until your hard limit"), so gpt-5.3-codex,
# gemini-3.1-pro and claude-sonnet-5 were not fallbacks at all — they were a
# guaranteed failure the moment an implementer outside the claude family made
# the diversity rule reach for the second entry. Checked live against
# GET /v1/models and by launching each.
_REVIEW_MODEL_PREFERENCES: tuple[str, ...] = (
    "grok-4.6",
    "grok-4.5",
    "composer-2.5",
)

_TERMINAL_RUN_STATUSES = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}

# Usage-vs-report tolerance: the harness counts tokens its own way, the
# provider counts billing tokens — a mismatch beyond this share (or a report
# claiming NO tokens while the provider billed some) is flagged to the audit.
_USAGE_MISMATCH_SHARE = 0.25


def pick_review_model(implementer_model: str) -> str:
    """The reviewer model: config override first, else the first preference
    from another family. Falls back to the first preference when the
    implementer is undeclared — the diversity rule (#758) will keep the
    verdict with the human in that case anyway."""
    override = (config.CURSOR_REVIEW_MODEL or "").strip()
    impl_family = family(implementer_model)
    if override:
        return override
    for candidate in _REVIEW_MODEL_PREFERENCES:
        if not impl_family or family(candidate) != impl_family:
            return candidate
    return _REVIEW_MODEL_PREFERENCES[0]


LITE = "lite"
DEEP = "deep"

# Risk classes at or above this one never get the cheap profile.
_DEEP_FROM_CLASS = RiskClass.r3


# Process surfaces (#820). Measured, not guessed: the lite-vs-deep comparison
# of 21.08.2026 found that the cheap profile caught 2 of 7 confirmed findings,
# and BOTH misses were of one kind — a pytest collector that outlived its
# timeout, and a collection run against whatever branch the shared workspace
# happened to be on. #509 had already produced the same shape twice: an
# orphaned validation command and its unbounded output buffer.
#
# These defects are invisible to reading a diff. You have to know how the
# process behaves after the await is cancelled, and who else writes to that
# workspace. So the surfaces where they live buy the expensive profile
# regardless of risk class — the class says how bad a mistake would be, this
# says how likely one is to hide from a single reader.
#
# The list is a filter against a KNOWN class, never a guarantee: a process
# defect written in words nobody listed here walks straight through. It gets
# extended every time one reaches production.
_PROCESS_SURFACES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "запуск подпроцессов",
        (
            "create_subprocess_exec",
            "create_subprocess_shell",
            "subprocess.run",
            "subprocess.Popen",
            "Popen(",
            ".communicate(",
        ),
    ),
    (
        "таймауты и отмена",
        ("wait_for(", "asyncio.timeout", "CancelledError", "TimeoutError"),
    ),
    (
        "воркспейс и ветки",
        (
            "workspace_path",
            "worktree",
            "checkout",
            "branch_diff",
            "fetch_base",
            "head_sha",
        ),
    ),
    (
        "конкурентный доступ",
        ("asyncio.Lock", "asyncio.Semaphore", "threading.", "asyncio.gather"),
    ),
)

_COMMENT_PREFIXES = ("#", '"""', "'''", "*", "//")


def _is_comment(code: str) -> bool:
    """A line that only comments or documents. Markers there are talk, not code."""
    stripped = code.strip()
    return not stripped or stripped.startswith(_COMMENT_PREFIXES)


# Generated files (#874). They spend the cheap profile's ceiling exactly like
# code does, and there is nothing in them for a reviewer to find: nobody wrote
# those lines and nobody will fix them.
#
# Two uses, and only one of them is a real filter. The hub does NOT hand the
# reviewer a diff — it reads one for itself and tells the reviewer to run
# `git diff`. So for the reviewer this list becomes an exclusion pathspec in
# the command it is given, plus the names of what was left out; inside the hub
# it is a genuine filter over the diff that decides the profile, where a marker
# in a lock file used to be able to buy the expensive harness on its own.
#
# Deliberately short, explicit and suffix-based. A broad mask like ``*.json``
# would hide real code, and the failure would be silent — the worst kind here.
_GENERATED_SUFFIXES: tuple[str, ...] = (
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "yarn.lock",
    "Cargo.lock",
    ".snap",
    ".min.js",
    ".min.css",
)
_GENERATED_DIRS: tuple[str, ...] = (
    "__snapshots__/",
    "node_modules/",
)

# Per-file ceiling, in diff lines. One 4000-line file must not eat a budget
# that five 200-line files needed: the reviewer is told which file it is and
# how big, and that the unread remainder belongs in lost_dimensions. Not a
# truncation — the hub has no diff to truncate — but the same guarantee that
# "did not fit" stays a stated fact instead of passing for "nothing found".
REVIEW_FILE_LINE_CAP = 800


def is_generated(path: str) -> bool:
    """Is this path a generated artefact rather than written code?"""
    if any(marker in path for marker in _GENERATED_DIRS):
        return True
    return any(path.endswith(suffix) for suffix in _GENERATED_SUFFIXES)


def split_generated(diff: str) -> tuple[str, list[str]]:
    """``(diff without generated files, names of what was dropped)``.

    Splits on the ``+++`` headers the same way :func:`changed_paths` reads
    them, so both functions agree on where a file's hunk begins. A diff that
    starts with lines before any header keeps them: dropping text we could not
    attribute would be the silent cut this whole task exists to prevent.
    """
    kept: list[str] = []
    dropped: list[str] = []
    skipping = False
    for line in diff.splitlines(keepends=True):
        if line.startswith("+++ "):
            raw = line[4:].strip()
            path = raw[2:] if raw.startswith("b/") else raw
            skipping = path != "/dev/null" and is_generated(path)
            if skipping:
                if path not in dropped:
                    dropped.append(path)
                # The header of the dropped file goes with it; the "--- a/..."
                # line above it was already kept, which is why the reader sees
                # the pair broken rather than the file silently absent.
                continue
        elif skipping and line.startswith("diff --git "):
            skipping = False
        if not skipping:
            kept.append(line)
    return "".join(kept), dropped


def file_line_counts(diff: str) -> list[tuple[str, int]]:
    """How many diff lines each changed file carries, biggest first."""
    counts: dict[str, int] = {}
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            raw = line[4:].strip()
            path = raw[2:] if raw.startswith("b/") else raw
            current = "" if path == "/dev/null" else path
            counts.setdefault(current, 0)
            continue
        if current and (line.startswith("+") or line.startswith("-")):
            counts[current] += 1
    counts.pop("", None)
    return sorted(counts.items(), key=lambda item: -item[1])


async def previous_findings(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> list[str]:
    """What the previous submission's reviewers confirmed (#880).

    Travels with the delta so the new run can check whether the fixes landed,
    instead of rediscovering the same defects from scratch — or, worse, not
    looking at them because their files are the ones it was told to skip.
    """
    previous = await repo.previous_submission(db, task_id, generation)
    if previous is None:
        return []
    titles: list[str] = []
    for row in await repo.machine_reviews_of_generation(
        db, task_id, int(dict(previous).get("generation") or 0)
    ):
        try:
            findings = json.loads(dict(row).get("findings_confirmed") or "[]")
        except ValueError:
            continue
        for finding in findings if isinstance(findings, list) else []:
            if not isinstance(finding, dict):
                continue
            title = str(finding.get("title") or "").strip()
            where = str(finding.get("file") or "").strip()
            if title:
                titles.append(f"{where}: {title}" if where else title)
    return titles


async def generation_delta(
    db: aiosqlite.Connection, task: dict, base: str
) -> tuple[list[str], str]:
    """Files touched since the previous submission, and why, or (empty, reason).

    Returns ``(paths, note)``. A non-empty ``paths`` narrows the review to the
    files this round of fixes touched; an empty one means the whole diff is the
    subject, and ``note`` always says which of those it is and on what grounds.

    Three facts have to hold, and each is checked rather than assumed:

    1. the previous submission was recorded — before #880 nothing kept it;
    2. its commit is an ANCESTOR of the current one. That is the rebase and
       force-push test: after either, "what changed since last time" compares
       commits that no longer share a history;
    3. the base branch has not moved. A project that repointed its default
       branch is asking a different question about the same two commits.

    Anything unproven means the full diff. Reviewing a delta we cannot justify
    would be the one failure this feature must not have — silently reading less
    than the report claims.
    """
    task_id = int(task.get("id") or 0)
    generation = int(task.get("submission_generation") or 0)
    current = (task.get("submission_sha") or "").strip()
    if generation <= 1 or not current:
        return [], "первая сдача — предмет ревью весь дифф"

    previous = await repo.previous_submission(db, task_id, generation)
    if previous is None:
        return [], "предыдущая сдача не записана — читается весь дифф"
    prev = dict(previous)
    prev_sha = (prev.get("sha") or "").strip()
    if not prev_sha:
        return [], "у предыдущей сдачи не закреплён коммит — читается весь дифф"
    if (prev.get("base_branch") or "") != base:
        return [], (
            f"базовая ветка сменилась ({prev.get('base_branch') or '—'} → {base}) "
            "— дельта невалидна, читается весь дифф"
        )

    ctx = await _git_context(db, task_id)
    if ctx is None:
        return [], "воркспейс недоступен — читается весь дифф"
    workspace, _ = ctx
    try:
        ancestor = await plugins.git_ops.is_ancestor(workspace, prev_sha, current)
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("ancestry check failed for task #%s: %s", task_id, exc)
        ancestor = None
    if ancestor is None:
        return [], "историю проверить не удалось — читается весь дифф"
    if not ancestor:
        return [], (
            f"коммит {prev_sha[:12]} не предок текущего — ветку перебазировали "
            "или переписали, читается весь дифф"
        )

    try:
        delta = await plugins.git_ops.branch_diff(workspace, prev_sha, current)
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("delta diff failed for task #%s: %s", task_id, exc)
        delta = None
    if delta is None:
        return [], "дельту прочитать не удалось — читается весь дифф"
    paths = [p for p in changed_paths(delta) if not is_generated(p)]
    if not paths:
        return [], (
            f"с поколения #{prev.get('generation')} код не менялся — читается весь дифф"
        )
    return paths, (
        f"дельта к поколению #{prev.get('generation')} ({prev_sha[:12]}): "
        f"{len(paths)} файл(ов)"
    )


def diff_plan(
    diff: str | None,
    base: str,
    branch: str,
    delta_paths: list[str] | None = None,
    delta_note: str = "",
    prior_findings: list[str] | None = None,
) -> tuple[str, str]:
    """What the reviewer should read, and the note for the task update (#874).

    Returns ``(block, note)``. The block carries a ready ``git diff`` command
    with ``:(exclude)`` pathspecs, the names of the generated files left out,
    and the files whose size will not fit a single pass. Nothing disappears
    quietly: every exclusion is named where the reviewer reads it.

    ``delta_paths`` narrows the command to the files a resubmission touched
    (#880). The command still diffs against the BASE, not against the previous
    commit: a defect born from the new edit meeting old code in the same file
    stays visible, which bare changed lines would have hidden. The coverage is
    stated in the block, because "read the delta" and "read the whole diff"
    are different claims about the same report (#549).
    """
    if diff is None:
        return (
            f"ПРЕДМЕТ РЕВЬЮ: дифф {base}...{branch}. Прочитать его хабу не "
            "удалось, поэтому список исключений не составлен — читай дифф "
            "целиком и сам реши, что в нём сгенерировано.",
            "дифф не прочитан, исключения не составлены",
        )
    kept, dropped = split_generated(diff)
    excludes = "".join(f" ':(exclude){path}'" for path in dropped)
    scope = "".join(f" '{path}'" for path in (delta_paths or []))
    lines = [
        f"ПРЕДМЕТ РЕВЬЮ — команда диффа (выполни ЕЁ, а не свою):\n"
        f"  git diff {base}...{branch} --{excludes}{scope}"
    ]
    if delta_paths:
        lines.append(
            f"ОХВАТ: прочитана ДЕЛЬТА, а не весь дифф — {delta_note}. "
            "Файлы взяты ЦЕЛИКОМ и против базовой ветки, поэтому старый код "
            "рядом с новой правкой виден. Остальные файлы ветки уже читались "
            "на прошлом поколении и сейчас НЕ пересматриваются — если "
            "найдёшь причину усомниться в этом, скажи об этом в отчёте."
        )
    elif delta_note:
        lines.append(f"ОХВАТ: прочитан ВЕСЬ дифф ветки — {delta_note}.")
    if prior_findings:
        listed = "; ".join(prior_findings[:20])
        lines.append(
            "НА ПРОШЛОМ ПОКОЛЕНИИ БЫЛИ ПОДТВЕРЖДЕНЫ: "
            f"{listed}. Проверь, что правки их действительно закрыли — это "
            "первое, что надо посмотреть, и не считай их закрытыми по факту "
            "того, что файл изменился."
        )
    if dropped:
        lines.append(
            "Исключены как сгенерированные (их никто не писал и не будет "
            f"править): {', '.join(dropped)}."
        )
    oversized = [
        (path, count)
        for path, count in file_line_counts(kept)
        if count > REVIEW_FILE_LINE_CAP
    ]
    if oversized:
        named = ", ".join(f"{path} ({count} строк)" for path, count in oversized)
        lines.append(
            f"НЕ ПОМЕСТЯТСЯ В ОДИН ПРОХОД (потолок {REVIEW_FILE_LINE_CAP} "
            f"строк на файл): {named}. Прочитай сколько успеешь, остальные "
            "файлы всё равно прочитай, а непрочитанный остаток перечисли в "
            "lost_dimensions и сдай incomplete=true. Один большой файл не "
            "имеет права съесть бюджет, которого ждали остальные."
        )
    note_bits = []
    if delta_note:
        note_bits.append(delta_note)
    if dropped:
        note_bits.append(f"исключено сгенерированных: {len(dropped)}")
    if oversized:
        note_bits.append(f"крупных файлов: {len(oversized)}")
    return "\n".join(lines), "; ".join(note_bits) or "исключать нечего"


def process_surface_reasons(diff: str) -> list[str]:
    """Which process surfaces this diff ADDS code on (#820).

    Only added lines count, and only outside comments: a diff that merely
    mentions ``wait_for`` in a docstring — as this very module does — must not
    buy the expensive profile, or 'deep' quietly becomes the default and the
    saving #807 exists for is gone.

    Generated files are dropped first (#874). A lock file or a snapshot could
    otherwise buy the expensive harness by carrying one marker string in a line
    nobody wrote — the cheapest possible way to lose the saving #807 exists for.
    """
    diff, _ = split_generated(diff)
    reasons: list[str] = []
    for group, markers in _PROCESS_SURFACES:
        hits: list[str] = []
        for raw in diff.splitlines():
            if not raw.startswith("+") or raw.startswith("+++"):
                continue
            code = raw[1:]
            if _is_comment(code):
                continue
            # A marker inside a trailing comment is talk too.
            code = code.split("#", 1)[0]
            for marker in markers:
                if marker in code and marker not in hits:
                    hits.append(marker)
        if hits:
            reasons.append(f"{group}: {', '.join(sorted(hits))}")
    return reasons


# Which risks buy the expensive harness (#827). The catalogue mixes two
# different things: some risks are about the code and its behaviour, others
# about the statement and the product. A multi-agent code review answers the
# first kind and cannot answer the second.
#
# Measured on the first live dispatch (#818, 21.08.2026): the run went deep
# because the task honestly declared a high risk reading "the daily message
# turns into noise and devalues the bot". Dogfooding answers that; reading
# the diff does not. We paid for a harness that had nothing to say.
_TECHNICAL_RISK_KINDS = frozenset(
    {
        "security",
        "breaking_change",
        "data_migration",
        "performance",
        "unknown_unknowns",
    }
)
_PRODUCT_RISK_KINDS = frozenset({"ambiguous_requirements", "large_scope", "other"})


def _risk_profile_reason(risks: Any) -> str | None:
    """Why the declared risks buy deep, or None when they do not (#827).

    Two rules, and the asymmetry between them is deliberate:

    * ``kind=security`` buys deep at ANY severity — unchanged from #807,
      because a security risk somebody rated 'low' is still a security risk.
    * a ``high`` severity buys deep only for TECHNICAL kinds. A product or
      statement risk stays with the class: it is not that such a task is
      safe, it is that this particular instrument cannot read it.

    A kind nobody recognises counts as technical at high severity. Not
    knowing what a risk is must never be the cheap answer (#582) — and it
    also closes the obvious way around the rule.
    """
    if not isinstance(risks, list):
        return None
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        kind = str(risk.get("kind") or "").strip()
        severity = str(risk.get("severity") or "").strip()
        if kind == "security":
            return "заявлен риск security"
        if severity != "high":
            continue
        if kind in _TECHNICAL_RISK_KINDS:
            return f"заявлен технический риск high: {kind}"
        if kind not in _PRODUCT_RISK_KINDS:
            return f"заявлен риск high с нераспознанным видом: {kind or 'не указан'}"
    return None


def pick_review_profile(
    task: dict[str, Any], diff: str | None = None
) -> tuple[str, list[str]]:
    """Which review profile this submission deserves, and why (#807, #820).

    Returns ``(profile, reasons)``. The reasons exist because "why was this
    reviewed cheaply" and "why did this cost a full harness run" are both
    questions somebody asks later, and a bare profile name answers neither.

    deep is the multi-agent harness; lite is a single pass over the branch
    diff under a token ceiling. The rule leans toward deep on every kind of
    ignorance:

    * a human who pressed "request machine review" asked for the real thing;
    * a high or security risk is exactly what the expensive harness is for;
    * an UNCOMPUTED risk class is not a low one (#582) — an unknown path is
      not cheaper than a known-harmless one, and treating a missing class as
      lite would let any task skip the harness by never being classified.

    Everything else is ordinary work inside known contracts, and paying
    434k tokens per confirmed finding for it is what made "review every
    submission" unaffordable in the first place.
    """
    if (task.get("machine_review_override") or "").strip() == "require":
        return DEEP, ["ревью запрошено человеком"]

    # #820: the diff decides before the class does. A missing diff is not a
    # harmless one — the same rule the ladder uses for "class not computed".
    if diff is None:
        return DEEP, ["дифф сдачи прочитать не удалось"]
    surfaces = process_surface_reasons(diff)
    if surfaces:
        return DEEP, [f"процессная поверхность — {r}" for r in surfaces]
    try:
        risks = json.loads(task.get("risks") or "[]")
    except ValueError:
        risks = []
    risk_reason = _risk_profile_reason(risks)
    if risk_reason:
        return DEEP, [risk_reason]
    raw_class = (task.get("risk_class") or "").strip()
    if not raw_class:
        return DEEP, ["класс риска не посчитан"]
    try:
        risk_class = RiskClass(raw_class)
    except ValueError:
        # An unreadable class is an unknown class, and unknown means deep.
        return DEEP, [f"класс риска нечитаем: {raw_class}"]
    order = list(RiskClass)
    if order.index(risk_class) >= order.index(_DEEP_FROM_CLASS):
        return DEEP, [f"класс риска {risk_class.value}"]
    return LITE, [f"класс риска {risk_class.value}, процессных поверхностей нет"]


# Repository review rules (#873). Until now the reviewer got the diff and
# nothing about the code it came from: the prompt named no known defect class,
# while ``_PROCESS_SURFACES`` above already listed the ones this repository
# actually burned itself on — and spent them on routing the profile alone.
# Both findings the cheap profile missed on 21.08.2026 were of those classes.
#
# The layout is Cursor's (``.cursor/BUGBOT.md``): a root file plus every file
# met while walking up from each changed file, nearest to the change last so
# the most specific rules are read last. Two rules are ours:
#
# * the rules are read from the BASE ref, never from the branch under review.
#   Read from the branch, a submission could relax the review it is about to
#   receive inside the very diff being reviewed.
# * a missing file is a STATED absence. "There are no rules here" and "nothing
#   ever broke here" are different claims, and silence says the second (#725).
REVIEW_RULES_FILE = ".hub/REVIEW_RULES.md"

# A quarter of the cheap profile's ceiling and not a token more: rules that eat
# the budget they were written to protect are worse than no rules at all. The
# hub cannot tokenise the reviewer's model, so the share is enforced in
# characters at a stated approximation — an honest ratio beats a precise number
# nobody here can compute.
RULES_BUDGET_SHARE = 0.25
CHARS_PER_TOKEN = 4


def rules_char_cap() -> int:
    """How many characters of rules the cheap profile can afford.

    The one place the constant still earns its keep (#893): rules are text the
    HUB puts into the prompt, so their size is genuinely ours to bound. What
    the reviewer then spends is not — measured at 777k-1.97M per lite run
    against a stated 40k ceiling, so the number is no longer told to anyone
    as a budget.
    """
    budget = max(int(config.REVIEW_LITE_TOKEN_BUDGET), 0)
    return int(budget * RULES_BUDGET_SHARE * CHARS_PER_TOKEN)


def changed_paths(diff: str) -> list[str]:
    """Repository paths the diff touches, in order of first appearance.

    Taken from the ``+++`` headers, so a deleted file (``+++ /dev/null``) does
    not contribute a directory whose rules nobody needs.
    """
    paths: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+++ "):
            continue
        raw = line[4:].strip()
        if not raw or raw == "/dev/null":
            continue
        if raw.startswith("b/"):
            raw = raw[2:]
        if raw and raw not in paths:
            paths.append(raw)
    return paths


def rules_candidates(paths: list[str]) -> list[str]:
    """Rules files that apply to these changes: root first, nearest last.

    The root file is always a candidate, including when the diff could not be
    read: repository-wide rules do not stop applying because we failed to list
    the changed files.
    """
    ordered: list[tuple[int, str]] = [(0, REVIEW_RULES_FILE)]
    known = {REVIEW_RULES_FILE}
    for path in paths:
        parts = PurePosixPath(path).parent.parts
        for depth in range(1, len(parts) + 1):
            candidate = "/".join((*parts[:depth], REVIEW_RULES_FILE))
            if candidate not in known:
                known.add(candidate)
                ordered.append((depth, candidate))
    # Stable by depth: same-depth files keep the order their changes appeared
    # in, and the deepest — the one closest to the changed code — lands last.
    ordered.sort(key=lambda item: item[0])
    return [candidate for _, candidate in ordered]


async def collect_review_rules(
    db: aiosqlite.Connection, task_id: int, diff: str | None
) -> tuple[str, str]:
    """The repository's review rules for this submission, and what happened.

    Returns ``(block, note)``. The block goes into the reviewer's prompt; the
    note goes into the task update, so "the reviewer worked without rules" is
    readable afterwards instead of being inferred from its absence.

    Every outcome names itself: rules found, no rules file, rules truncated,
    the repository could not be read. The last two are the ones that would
    otherwise pass for the first.
    """
    ctx = await _git_context(db, task_id)
    if ctx is None:
        return (
            "ПРАВИЛА РЕПОЗИТОРИЯ ПРОЧИТАТЬ НЕ УДАЛОСЬ (нет доступа к "
            "воркспейсу проекта). Это не значит, что правил нет.",
            "правила прочитать не удалось — нет воркспейса",
        )
    workspace, base = ctx
    sections: list[tuple[str, str]] = []
    for candidate in rules_candidates(changed_paths(diff or "")):
        try:
            text = await plugins.git_ops.file_at_ref(workspace, base, candidate)
        except Exception as exc:  # noqa: BLE001 - degradation is the contract
            log.warning("could not read %s for task #%s: %s", candidate, task_id, exc)
            text = None
        if text and text.strip():
            sections.append((candidate, text.strip()))
    if not sections:
        return (
            f"ПРАВИЛ РЕПОЗИТОРИЯ НЕТ: файла {REVIEW_RULES_FILE} на ветке "
            f"{base} не найдено. Это отсутствие данных, а не утверждение, "
            "что здесь ничего не ломается.",
            f"правил нет — {REVIEW_RULES_FILE} на {base} отсутствует",
        )

    header = (
        f"ПРАВИЛА РЕПОЗИТОРИЯ (ветка {base}). Это места, где здесь "
        "ИСТОРИЧЕСКИ ЛОМАЛОСЬ, а не исчерпывающий чеклист: проверь их "
        "обязательно И СМОТРИ ШИРЕ — дефект, которого нет в списке, "
        "остаётся дефектом."
    )
    body: list[str] = []
    used = len(header)
    cap = rules_char_cap()
    dropped: list[str] = []
    for path, text in sections:
        chunk = f"\n--- {path} ---\n{text}"
        if used + len(chunk) > cap:
            dropped.append(path)
            continue
        body.append(chunk)
        used += len(chunk)
    note = f"правила из {len(sections) - len(dropped)} файл(ов)"
    if dropped:
        cut = (
            f"\n[ОБРЕЗАНО по потолку {cap} символов — не поместились: "
            f"{', '.join(dropped)}. Непрочитанные правила это «не проверено», "
            "а не «правил нет».]"
        )
        body.append(cut)
        note += f"; обрезано, не поместились: {', '.join(dropped)}"
    return header + "".join(body), note


# The text-delivery contract (#1036). Named fence, JSON inside, last block in
# the answer wins — a model that quotes the format while explaining itself must
# not be able to shadow its own report with the example.
REPORT_FENCE = "haiplane-review"
# #1084: what "the main path" is depends on what the run actually has. With a
# code in the prompt it is HTTP, and saying otherwise costs the run its first
# minutes on tools that are not there — while the code expires in five.
_MAIN_PATH_MCP = (
    "1) Если инструменты хаба тебе доступны — сдай hub_submit_machine_review, "
    "это основной путь.\n"
)
_MAIN_PATH_HTTP = (
    "1) Основной путь — HTTP из блока «ДОСТУП К ХАБУ» выше: тем же токеном "
    "POST на /api/tasks/<id>/machine-review. Инструментов MCP у тебя нет, "
    "не трать на них ходы.\n"
)

REPORT_BLOCK_INSTRUCTION = (
    "СДАЧА ОТЧЁТА — ДВА ПУТИ, ОБА ОБЯЗАТЕЛЬНЫ.\n"
    "{main_path}"
    "2) НЕЗАВИСИМО от этого в САМОМ КОНЦЕ ответа повтори отчёт блоком:\n"
    f"```{REPORT_FENCE}\n"
    '{"raw_count": <число находок до проверки>, "incomplete": true|false, '
    '"findings_confirmed": [{"title": "...", "severity": "high|medium|low", '
    '"category": "...", "locator": "lines|file|none", "file": "путь", '
    '"start_line": 1, "detail": "..."}], '
    '"findings_rejected": [{"title": "...", "category": "...", "reason": "..."}], '
    '"unresolved": [{"title": "...", "why": "..."}], '
    '"lost_dimensions": ["..."], "harness_skill": "...", '
    '"tokens_spent": <число или null>, "model": "<твоя модель>"}\n'
    "```\n"
    "Правила блока: он ОДИН и он последний; incomplete обязателен и без "
    "дефолта — «0 подтверждённых» без него не значит ничего; находка, которую "
    "никто не смог рассудить, идёт в unresolved, а НЕ в findings_rejected. "
    "Если инструменты хаба недоступны — этот блок единственный способ "
    "доставить работу, и без него прогон пропадёт целиком."
)


def _delivery_block(task_id: int, code: str, base_url: str) -> str:
    """How to file the report through the contract, with no MCP (#1084).

    Empty when there is no code to spend: an instruction naming a credential
    the run does not have would teach it to invent one. The text block below
    stays either way — it is the fallback, and it is what the poller reads
    when this path does not happen.
    """
    if not code or not base_url:
        return ""
    return (
        "ДОСТУП К ХАБУ — ПЕРВОЕ ДЕЙСТВИЕ, ДО ЧТЕНИЯ КОДА. У тебя нет "
        "инструментов MCP, поэтому отчёт сдаётся обычным HTTP. Код ниже "
        "живёт МИНУТЫ, а твой прогон длится дольше — обменяй его сразу, "
        "иначе он протухнет к моменту отчёта:\n"
        f"  curl -sS -X POST {base_url}/api/auth/chat-pair/redeem "
        "-H 'Content-Type: application/json' "
        f'-d \'{{"code":"{code}"}}\'\n'
        "В ответе поле token — сохрани его в переменную, в ответ не печатай. "
        "Постановку, критерии и предыдущие находки читай тем же токеном:\n"
        f"  curl -sS {base_url}/api/tasks/{task_id}/review-brief "
        '-H "Authorization: Bearer $TOKEN"\n'
        "Готовый отчёт сдай им же:\n"
        f"  curl -sS -X POST {base_url}/api/tasks/{task_id}/machine-review "
        '-H "Authorization: Bearer $TOKEN" '
        "-H 'Content-Type: application/json' -d @report.json\n"
        "Сессия видит ровно две ручки этой задачи — обзор ревью и приём "
        "отчёта. Ни вердикт, ни клейм, ни правка задачи ей не доступны: "
        "не пытайся, это не ограничение прошивки, а граница роли.\n\n"
    )


def _review_prompt(
    task_id: int,
    branch: str,
    model_id: str,
    profile: str,
    rules_block: str,
    diff_block: str,
    prepass_block: str,
    delivery_block: str = "",
) -> str:
    common = (
        f"Ты — независимый код-ревьюер задачи #{task_id} хаба Haiplane "
        f"(ветка {branch}). Строгие правила: НИЧЕГО не коммить, не пушить и "
        "не менять — только читать код и запускать проверки.\n\n"
        # The rules travel with BOTH profiles: the expensive harness has no
        # more knowledge of this repository's history than the cheap pass.
        f"{rules_block}\n\n"
        # So does the diff plan (#874): the deep harness reads the same branch
        # and has the same reason not to spend its passes on lock files.
        f"{diff_block}\n\n"
        # And the prepass (#875): both profiles pay model prices for what a
        # linter already proved, and the expensive one pays them per pass.
        f"{prepass_block}\n\n"
        # #1036: the report has to survive a run with no MCP. Since 22.08 the
        # hub's MCP stopped reaching cloud runs at all — the reviewer works,
        # finishes, and its findings die in the final text nobody parses. So
        # the text becomes a second, weaker delivery: same fields, stated
        # once, at the very end, where a machine can find them.
        f"{delivery_block}"
        # .replace, не .format: сам шаблон несёт JSON отчёта в фигурных
        # скобках, и форматирование прочитало бы "raw_count" как поле.
        + REPORT_BLOCK_INSTRUCTION.replace(
            "{main_path}", _MAIN_PATH_HTTP if delivery_block else _MAIN_PATH_MCP
        )
        + "\n\n"
    )
    if profile == LITE:
        # No token ceiling is stated (#893). It used to say "бюджет 40000
        # токенов", and eight runs measured against the provider's bill cost
        # 777k-1.97M each: the number bounded nothing, because the reviewer
        # counts its own tokens while the bill counts context, tool calls and
        # repeat passes. What DOES shape the run is behaviour — one pass over
        # the diff instead of a walk through the repository — so that is what
        # the prompt asks for, in words the report can be checked against.
        return (
            common + "Это ЛЁГКОЕ ревью: ОДИН проход. Порядок: "
            f"1) hub_get_review_brief(task_id={task_id}) — предмет ревью; "
            "2) прочитай дифф КОМАНДОЙ ИЗ ПРЕДМЕТА РЕВЬЮ выше и только его — "
            "не исследуй репозиторий целиком, контекст берётся из диффа; "
            "3) один проход по изменённым файлам: ищи дефекты корректности, "
            "потерянные граничные случаи, несоответствие заявленным AC; "
            f"4) сдай hub_submit_machine_review(task_id={task_id}, "
            "harness_skill='lite-diff-review', ...) с реальными raw_count, "
            f"находками, tokens_spent и model='{model_id}'. "
            "ЧЕСТНОСТЬ ОХВАТА: если дифф прочитан не целиком — сдавай "
            "incomplete=true и перечисли непрочитанные файлы в "
            "lost_dimensions. Ноль находок при обрезанном диффе — это "
            "«не проверено», а не «чисто»; выдать одно за другое хуже, чем "
            "не найти ничего. Вердикт НЕ выноси — он не твой."
        )
    return (
        common + "Порядок: "
        "1) hub_get_skill('multi-agent-review') и работай по нему; "
        f"2) hub_get_review_brief(task_id={task_id}) — предмет ревью; "
        "3) исполни фазы измерений и адъюдикации ЧЕСТНО — отчёт без "
        "исполнения запрещён скиллом v8 и виден серверу; "
        f"4) сдай hub_submit_machine_review(task_id={task_id}, ...) с "
        f"реальными raw_count, находками, tokens_spent и model='{model_id}'. "
        "Вердикт НЕ выноси — он не твой."
    )


async def _git_context(
    db: aiosqlite.Connection, task_id: int
) -> tuple[str, str] | None:
    """``(workspace, base branch)`` of the task's project, or None (#873).

    None means "we could not look", which every caller has to turn into a
    named answer of its own rather than into a quiet default.
    """
    try:
        from hub import services

        ctx = await services.project_git_context(db, task_id)
        workspace = (ctx.get("repo") or "").strip()
        base = (ctx.get("base_branch") or config.PAIR_BASE_BRANCH).strip()
        if not workspace or not base:
            return None
        return workspace, base
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("could not read the git context of task #%s: %s", task_id, exc)
        return None


async def _submission_diff(
    db: aiosqlite.Connection, task_id: int, branch: str
) -> str | None:
    """The submitted branch diff, or None when it cannot be read (#820).

    None is a real answer, not an error to swallow: the caller turns it into
    the expensive profile rather than into a quiet lite run over code nobody
    looked at.
    """
    ctx = await _git_context(db, task_id)
    if ctx is None or not branch:
        return None
    workspace, base = ctx
    try:
        return await plugins.git_ops.branch_diff(workspace, base, branch)
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("could not read the diff of task #%s: %s", task_id, exc)
        return None


# How many cloud runs one submission may buy (#879). Two: the cheap default
# and, when it declares it did not finish, one heavy top-up.
#
# A ceiling rather than a loop, and counted from the dispatch rows rather than
# from a flag — a flag would have to live somewhere and would drift from the
# fact it claims. A reviewer that keeps declaring itself incomplete is a
# problem for a person to look at, not a reason to keep buying runs.
REVIEW_LADDER_MAX_STEPS = 2


async def maybe_top_up_incomplete(db: aiosqlite.Connection, task_id: int) -> bool:
    """Buy the heavy profile when the cheap run said it did not finish (#879).

    The trigger is the reviewer's OWN declaration of what it did not read.
    That is the only honest signal left: #893 removed the budget guard because
    a run that burned 1.5M tokens while reporting 36k sailed through as
    complete, and a guard reading the checked party's estimate of itself is
    not a guard. The declaration, unlike the number, is checkable against the
    diff.

    Returns True when a top-up was dispatched. Every other path leaves the task
    exactly where it was — on its way to a human — and says why in the task's
    own updates rather than in silence.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        return False
    task = dict(row)
    generation = task.get("submission_generation") or 0
    if task.get("status") != "review" or generation <= 0:
        return False

    # #1025: the ladder climbs on OUR run's own declaration. A foreign
    # incomplete report at an active dispatch must not buy a top-up — the
    # dispatch is still waiting for its own report.
    dispatch_row = await repo.get_review_dispatch_for_generation(
        db, task_id, generation
    )
    dispatch = dict(dispatch_row) if dispatch_row is not None else None
    report = await _dispatch_report(db, task_id, generation, dispatch)
    if report is None:
        return False
    if not report.get("incomplete"):
        return False

    # Order matters here. The ceiling is checked BEFORE the profile, because
    # the report that hits it is the top-up's own — a deep one — and a
    # profile-first check would return on it silently, leaving the ladder's
    # loudest moment unannounced.
    steps = await repo.count_review_dispatches(db, task_id, generation)
    if steps >= REVIEW_LADDER_MAX_STEPS:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Неполный отчёт после {steps} прогон(ов): потолок лестницы "
            f"{REVIEW_LADDER_MAX_STEPS} достигнут, добор НЕ ставится. "
            "Ревью этой сдачи так и не состоялось полностью — решение за "
            "человеком (#879).",
        )
        await db.commit()
        return False

    # The profile comes from the dispatch, never from the report about itself
    # (#807, #750). Only a CHEAP run earns a top-up: an unknown profile is not
    # a cheap one, and a deep run that did not finish has nothing above it to
    # climb to — both go to the human, and both say so.
    profile = (report.get("profile") or "").strip()
    if profile != LITE:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Неполный отчёт, добор не положен: профиль "
            + (f"«{profile}»" if profile else "не заявлен")
            + " — выше дешёвого подниматься некуда. Решение за человеком (#879).",
        )
        await db.commit()
        return False

    dispatched = await maybe_dispatch_review(db, task_id, force_profile=DEEP)
    if not dispatched:
        # maybe_dispatch_review already alerted when policy asked and the call
        # failed; when policy never asked, silence there is correct. Either
        # way the human is the next reader, and the cause is on the card.
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Дешёвый прогон сдал неполный отчёт, а добор тяжёлым профилем "
            "поставить не удалось. Решение за человеком (#879).",
        )
        await db.commit()
    return dispatched


#: Форжи, до которых дотягивается облачный ревьюер Cursor (#1119).
#:
#: Живёт ЗДЕСЬ, а не на форже: это факт про Cursor, а не про хостинг, и
#: адаптер хостинга не должен знать, какие ревьюеры существуют на свете.
#:
#: Измерено 31.08.2026 живыми вызовами с одинаковым телом, менялся только
#: адрес: https://gitverse.ru/... → 400 validation_error; git@gitverse.ru:... →
#: 500 internal; контроль https://github.com/... → 201, агент создан. Плюс
#: документация (repos[].url описан как «GitHub repository URL», endpoint
#: списка репозиториев называется «List GitHub repositories») и прямой ответ
#: сотрудника Cursor на форуме 21.01.2026.
#:
#: Если Cursor добавит хосты, этот кортеж — единственное место, где это
#: правится, и дата выше говорит, когда проверку пора повторить.
CLOUD_REVIEW_FORGES: tuple[str, ...] = ("github",)

# Каким способом добыт отчёт (#1180). Колонка, а не префикс в agent_id:
# свип спрашивает Cursor про КАЖДУЮ активную строку, и локальный прогон,
# отличимый только по виду идентификатора, стоил бы запроса к чужому API на
# каждом тике — и получал бы в ответ «такого агента нет», то есть ложную
# причину вместо своей.
CLOUD_CHANNEL = "cloud"
LOCAL_CHANNEL = "local"


async def _policy_and_novelty_allow(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    project: Any,
    force_profile: str = "",
) -> bool:
    """Два тихих отказа диспетчера, стоящих рядом по одной причине.

    Оба отвечают на «звать ли ревьюера» ДО всякой подготовки вызова, и оба
    молчат в том смысле, что не являются поломкой: политика не просила —
    ревью и не должно быть; код уже прочитан — второе чтение не купит
    ничего нового.

    Расходятся они на доборе: политику он спрашивает (выключенный контур
    выключен и для лестницы), а проверку новизны — нет, потому что она
    отвечает не на его вопрос.

    Собраны в одну функцию, потому что maybe_dispatch_review стоит на
    потолке сложности вплотную: 60 из 60. Любая строка, добавленная туда,
    красит бюджет, и это верное поведение измерителя — чинить надо
    измеряемое.
    """
    # #805: one reader, and it answers "call a reviewer?" — not "who signs
    # the verdict?". Those were the same question only because they shared a
    # key, which forced the hub's own project to choose between no review
    # and no human.
    if not review_dispatch_enabled(gate_policy_of(project)):
        return False
    if force_profile:
        # Добор лестницы #879 проверку новизны не проходит и не должен.
        # Найдено кросс-модельным ревью, и это второй раз, когда правило
        # экономии мешало добору — с другой стороны механизма.
        #
        # Причина в том, что вопросы РАЗНЫЕ. Страж спрашивает «читали ли
        # уже этот код», и на новую сдачу это верный вопрос. Добор
        # спрашивает «дочитал ли НАШ прогон», и ответ на него даёт только
        # собственное заявление прогона — отчёт чужой генерации на том же
        # sha про это не знает ничего. Ответить вторым на первый значит
        # закрыть лестницу утверждением не по делу.
        #
        # Без потолка это не оставляет: у добора свой, и он строже —
        # REVIEW_LADDER_MAX_STEPS ограничивает число прогонов на
        # генерацию, а подниматься выше дешёвого профиля некуда.
        return True
    return not await _this_code_was_already_read(db, task)


#: Метка записи об отсутствующем ревьюере, по которой она находится снова.
#: Внутри — номер поколения сдачи: дедуп обязан быть в его пределах, иначе
#: одна запись за всю жизнь задачи молчала бы про каждую следующую сдачу
#: (ровно так #443 пролежала неделю). Образец — stale_rung_raised: ключ
#: разбирается из текста, а не хранится второй колонкой.
NO_REVIEWER_MARK = "[без ревьюера: сдача {generation}]"


async def _name_the_missing_reviewer(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    project: Any,
) -> bool:
    """Сказать в карточке, что второго читателя не будет, и почему. Всегда False.

    #1216, измерено на #1202: проект snip-portal (форж gitverse, gate_policy
    без ключа review) сдавался дважды, и обе сдачи встали в review без единой
    записи про диспетч. Отказ по политике стоит РАНЬШЕ развилки по форжу, где
    #1180 научила хаб называть причину, — то есть механизм именованного отказа
    построен, а управление до него не доходит.

    Отказ остаётся отказом: политика по-прежнему имеет право не звать
    ревьюера, и диспетч здесь не включается. Меняется только одно — карточка
    больше не выдаёт «не позвали» за «не потребовалось».

    Причина складывается из двух, и они разного рода. Первая — политика: про
    неё review_reach не знает ничего, и она пишется своими словами. Вторая —
    досягаемость канала, и вот её текст берётся у review_reach целиком, слово
    в слово с тем, что человек видит в форме проекта и в отказе на записи
    политики (#1188). Второй копии этого знания не заводим.
    """
    if review_dispatch_enabled(gate_policy_of(project)):
        # Отказала не политика, а проверка новизны: код уже читали, и она
        # говорит об этом сама (#1152). Накрывать её вторым алертом значило бы
        # объяснить одно состояние двумя разными причинами.
        return False
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    mark = NO_REVIEWER_MARK.format(generation=generation)
    if await _already_told_about_the_missing_reviewer(db, task_id, mark):
        return False
    reach = await review_reach(db, project_policy.forge_of(project))
    # Досягаемость канала добирается только когда её нет: на github с
    # выключенной политикой человеку достаточно знать, что дело в политике —
    # включит, и ревьюер придёт.
    channel = "" if reach.runnable else f" К тому же {reach.reason}."
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Машинное ревью НЕ вызвано: политика проекта не просит диспетча "
        f"(ключ review в gate_policy), поэтому второго читателя у этой сдачи "
        f"не будет.{channel} Вердикт остаётся человеку — и он выносится без "
        f"машинного отчёта (#757, #1216). {mark}",
    )
    await db.commit()
    log.info(
        "task #%s gen %s stands in review with no reviewer: policy does not "
        "ask for a dispatch",
        task_id,
        generation,
    )
    return False


async def _already_told_about_the_missing_reviewer(
    db: aiosqlite.Connection, task_id: int, mark: str
) -> bool:
    """Была ли запись про ЭТУ сдачу. Свип ходит по карточке снова и снова."""
    rows = await fetchall(
        db,
        "SELECT 1 FROM task_updates WHERE task_id=? AND kind='alert' "
        "AND content LIKE ? LIMIT 1",
        (task_id, f"%{mark}%"),
    )
    return bool(rows)


async def _this_code_was_already_read(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> bool:
    """Отказать во втором чтении того же кода, сказав об этом в карточке.

    #1152: измерено на живой базе прода. Число приводится ВМЕСТЕ С ЗАПРОСОМ,
    которым получено, — иначе проверить его нельзя, и ревью справедливо
    оставило это в unresolved: обоснование строгости, которое не
    воспроизводится, обоснованием не является.

        WITH dispatched AS (
            SELECT d.id, d.task_id, s.sha,
                   COALESCE(d.provider_tokens, 0) AS tok,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.task_id, s.sha ORDER BY d.id
                   ) AS n
            FROM review_dispatches d
            JOIN submissions s
              ON s.task_id = d.task_id
             AND s.generation = d.submission_generation
            WHERE s.sha <> ''
        )
        SELECT COUNT(*), SUM(tok) FROM dispatched WHERE n > 1;

    Соединение идёт через submissions потому, что у review_dispatches своей
    колонки sha нет: вершину знает сдача, а не вызов.

    Результат на 03.09.2026: 81 прогон с известной вершиной, из них 8 ЛИШНИХ
    (9.9%) ценой 13 099 589 токенов провайдера. То есть примерно каждый
    десятый прогон покупал второе чтение того же диффа, и случай не единичный.

    Прежняя редакция комментария называла «8 из 87 и порядка 15M» без
    запроса. Порядок величины тот же, знаменатель отличается — база с тех пор
    выросла, — но проверить это было нельзя, и в этом была вся претензия.

    Ключ по КОДУ, а не по поколению. Пересдача поднимает поколение, и ключ
    по нему пропустил бы ровно этот случай: генерации разные, sha тот же.
    Именно так дубль и возникает — сорвавшийся ретрай, две сессии, дважды
    нажатая кнопка.

    Вынесено отдельной функцией не ради красоты: три строки в
    maybe_dispatch_review подняли её с 60 до 63 при потолке 60, и бюджет
    сложности честно покраснел. Поднять потолок значило бы починить
    измеритель вместо измеряемого.
    """
    already = await _report_already_covers_this_sha(db, task)
    if not already:
        return False
    await _refuse_second_read(db, int(task["id"]), already)
    return True


async def _report_already_covers_this_sha(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> int | None:
    """Id отчёта, уже прочитавшего ЭТОТ коммит, или None.

    Сравнивается закреплённый sha текущей сдачи со sha тех генераций, по
    которым отчёт уже есть. Не поколение: пересдача его поднимает, и
    дубль на неизменившемся коде выглядел бы новой работой.

    Отсутствие закреплённого sha означает, что сравнивать нечего — ревью
    заказывается. Незнание не есть совпадение (#762), и направление
    ошибки выбрано в пользу прогона: лишнее чтение стоит денег, а
    пропущенное — сдачи без отчёта.

    Два отчёта покрытием НЕ считаются, и оба исключения названы ревью
    первой сдачи:

    неполный
        отчёт сам заявляет, что дочитал не всё, и лестница #879
        существует затем, чтобы добрать непрочитанное. Считать его
        чтением значило бы запереть добор утверждением, которое отчёт
        о себе опровергает;
    самоотчёт
        отчёт исполнителя о собственной работе не отменяет независимого
        ревьюера. Тот же автор уже однажды закрыл чужой диспетчер как
        выполненный (#1011, #1025); здесь он закрывал бы его до старта.
    """
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    pinned = (task.get("submission_sha") or "").strip()
    if not pinned:
        return None
    rows = await fetchall(
        db,
        "SELECT mr.id AS review_id, s.sha AS sha "
        "FROM machine_reviews mr "
        "JOIN submissions s ON s.task_id = mr.task_id "
        "AND s.generation = mr.submission_generation "
        "WHERE mr.task_id = ? AND mr.submission_generation != ? "
        # Неполный отчёт не покрывает код: он САМ говорит, что дочитал не
        # всё, и лестница #879 существует ровно затем, чтобы добрать
        # непрочитанное. Назвать его чтением значило бы запереть добор
        # утверждением, которое отчёт опровергает о себе — та же ошибка,
        # против которой стоит #762, только с другой стороны.
        "AND COALESCE(mr.incomplete, 0) = 0 "
        # Самоотчёт исполнителя не отменяет независимого ревьюера. Автор,
        # приславший отчёт о собственной работе, уже однажды закрыл чужой
        # диспетчер как выполненный (#1011, #1025) — здесь он закрывал бы
        # его ещё до старта. «Код прочитан» имеет смысл только про того,
        # кто читал его со стороны.
        "AND COALESCE(mr.self_reviewed, 0) = 0",
        (task_id, generation),
    )
    for row in rows:
        if ((dict(row).get("sha") or "").strip()) == pinned:
            return int(dict(row)["review_id"])
    return None


async def _refuse_second_read(
    db: aiosqlite.Connection, task_id: int, review_id: int
) -> None:
    """Сказать в карточке, что прогона не будет, и почему.

    Молчаливый отказ неотличим от сломавшегося диспетчера: человек, не
    дождавшийся отчёта, пойдёт искать поломку там, где её нет. Отказ
    называет отчёт, который уже покрывает этот код, — то есть даёт то,
    ради чего прогон и заказывали бы.
    """
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        # alert, а не status: остальные отказы диспетчера видны как
        # алерты, и отказ, который тише соседей, читается как «ничего не
        # произошло» ровно там, где ревьюера не позвали.
        "alert",
        (
            f"Кросс-модельное ревью не заказано: этот код уже прочитан, "
            f"отчёт #{review_id} покрывает ту же вершину. Пересдача подняла "
            "поколение, но дифф не изменился, и второй прогон вернул бы то "
            "же чтение за те же деньги (#1152). Отчёт по этому коду читается "
            f"как есть — он #{review_id}; неполный отчёт этим правилом не "
            "закрывается и добор ставится обычным порядком (#879)."
        ),
        author_kind="hub",
    )
    await db.commit()
    log.info(
        "review dispatch skipped for task #%s: report #%s covers the same sha",
        task_id,
        review_id,
    )


@dataclass(frozen=True)
class ReviewOrder:
    """Готовый заказ ревью: всё, что ревьюер получает, кроме транспорта (#1180).

    Существует затем, чтобы «позвать ревьюера в облако» и «позвать ревьюера
    локально» не стали двумя описаниями одного заказа. Расходиться им нельзя:
    профиль, правила репозитория, предмет ревью и предпас — это то, ЧТО
    прочитано, и две копии этого знания рано или поздно ответят по-разному на
    вопрос «было ли чтение».
    """

    model: str
    profile: str
    reasons: list[str]
    prompt: str
    rules_note: str
    diff_note: str
    prepass: Any


async def prepare_review_order(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    *,
    branch: str,
    generation: int,
    force_profile: str,
    principal_id: int | None,
) -> ReviewOrder:
    """Собрать заказ: профиль по диффу, правила, предмет ревью, доступ.

    ``principal_id`` — принципал ТОГО ревьюера, который будет исполнять заказ:
    под ним минтуется одноразовый код доступа, и его же ждёт диспетчер как
    владельца отчёта (#1025). Разные транспорты — разные принципалы, и это
    единственное, что заказу нужно знать о том, кто его исполнит.
    """
    task_id = int(task["id"])
    model_id = pick_review_model((task.get("submission_model") or "").strip())
    # #820: the profile is decided against the SUBMITTED diff, not against the
    # areas the author declared — self-assessment cannot exempt work from
    # oversight (#582). An unreadable diff buys deep, it does not excuse it.
    diff = await _submission_diff(db, task_id, branch)
    if force_profile:
        profile, profile_reasons = force_profile, ["добор после неполного прогона"]
    else:
        profile, profile_reasons = pick_review_profile(task, diff)
    rules_block, rules_note = await collect_review_rules(db, task_id, diff)
    # #874: which base the reviewer diffs against. Unknown base falls back to
    # the configured one rather than to nothing — a command the reviewer cannot
    # run would send it back to inventing its own, which is what we are fixing.
    ctx = await _git_context(db, task_id)
    base = ctx[1] if ctx else config.PAIR_BASE_BRANCH
    # #880: a resubmission reads what changed since the previous generation,
    # not the whole branch again. Every reason the delta cannot be trusted
    # falls back to the full diff and says so.
    delta_paths, delta_note = await generation_delta(db, task, base)
    prior = await previous_findings(db, task_id, generation)
    diff_block, diff_note = diff_plan(
        diff, base, branch, delta_paths, delta_note, prior
    )
    # #875: what the toolchain already proved on THIS commit. Built from the
    # task row the caller already read, so no extra query for the common case.
    # Imported here, not at module level: review_evidence reaches back into
    # this module's siblings, and the top-level cycle is the reason every
    # other cross-service call in this file is local too.
    from hub.services import review_evidence

    prepass = await review_evidence.prepass_state(db, task)
    hub_base = instance_base_url().rstrip("/")
    code = await _access_code(db, task_id, generation, principal_id)
    return ReviewOrder(
        model=model_id,
        profile=profile,
        reasons=profile_reasons,
        prompt=_review_prompt(
            task_id,
            branch,
            model_id,
            profile,
            rules_block,
            diff_block,
            review_evidence.prepass_block(prepass),
            _delivery_block(task_id, code, hub_base),
        ),
        rules_note=rules_note,
        diff_note=diff_note,
        prepass=prepass,
    )


async def _access_code(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    principal_id: int | None,
) -> str:
    """Одноразовый код доступа ревьюера к хабу, или пустая строка (#1084).

    Cursor теряет mcpServers по дороге в облачный ран, поэтому заголовок с
    токеном до рана не доезжает — а сеть доезжает: ран отчитался 401 ОТ этого
    хаба, то есть запрос дошёл и вернулся. Локальный прогон живёт под теми же
    правилами по другой причине: класть токен в argv нельзя (виден в ``ps``),
    а класть в окружение — значит завести второй способ выдать ревьюеру
    идентичность и второе место, где его отзывают.

    Нет принципала (токен пуст, отозван или открытый режим) — нет кода: ран
    оставит отчёт текстом, ровно как до #1084.
    """
    if principal_id is None:
        return ""
    from hub.services import chat_pair

    code, _ttl = await chat_pair.issue_code(
        db,
        principal_id,
        kind="reviewer",
        bound_task_id=task_id,
        # THE pin. Without it every guard below is dead code: issue_code
        # stores NULL, redeem_code skips the comparison, the session
        # carries no generation and the intake check is falsy. The tests
        # that "covered" this minted their codes by hand WITH a generation
        # and so agreed with a path production never took.
        bound_generation=generation,
    )
    return code


#: Сколько раз хаб пробует создать ревьюера, когда ответ не дошёл, а сверка
#: показала, что агента нет. Потолок, а не настойчивость: на #1175 хаб
#: повторял одну и ту же неудачу семнадцать минут подряд.
_LOST_ANSWER_ATTEMPTS = 2


class _Started(NamedTuple):
    """Чем кончилась попытка получить ревьюера (#1199)."""

    agent_id: str
    run_id: str
    adopted: bool
    blind: bool
    attempts: int
    refusal: cursor_cloud.Refusal | None


def _lost_call_detail(started: _Started) -> str:
    """Причина НАБЛЮДЁННАЯ, а не сочинённая (#1199).

    Прежний текст утверждал «Cloud Agents API не принял запрос» там, где
    ответа не было вовсе, и отправлял читающего проверять схему беты вместо
    времени ответа. Сообщение, называющее чужую причину, дороже отсутствия
    сообщения: оно уводит.
    """
    refusal = started.refusal
    if started.blind:
        return (
            "ответ провайдера не дошёл, и СПРОСИТЬ его, создался ли агент, "
            "тоже не вышло — подбирать вслепую нельзя, повторять тоже "
            f"({refusal.detail if refusal else 'причина не названа'})"
        )
    if refusal is not None and refusal.is_transport:
        return (
            "ответ провайдера не дошёл, и агента с нашей меткой у него не "
            f"нашлось за попыток: {started.attempts} "
            f"({refusal.detail or 'причина не названа'})"
        )
    if refusal is not None:
        return f"провайдер отказал: HTTP {refusal.status}" + (
            f", {refusal.code}" if refusal.code else ""
        )
    return "ответ провайдера не содержал идентификатора агента"


async def _attempt_ordinal(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> int:
    """Какой это по счёту заказ ревью на эту сдачу (#1199, находка №269).

    Метка без номера попытки не различала бы добор лестницы (#879): он
    заказывает ВТОРОГО ревьюера на ту же генерацию, то есть под тем же
    именем. Сверка после оборвавшегося добора подобрала бы агента первого,
    дешёвого прогона — чужого судью вместо своего.

    Считается по строкам, которые хаб уже завёл, а не по счётчику в памяти:
    после перезапуска счётчик начался бы заново, а строки остаются.
    """
    rows = await fetchall(
        db,
        "SELECT COUNT(*) AS n FROM review_dispatches "
        "WHERE task_id=? AND submission_generation=?",
        (task_id, generation),
    )
    return int(dict(rows[0]).get("n") or 0) + 1 if rows else 1


async def _create_or_adopt(
    marker: str,
    *,
    task_id: int,
    repo_url: str,
    starting_ref: str,
    model_id: str,
    prompt_text: str,
    hub_mcp_url: str,
    reviewer_token: str,
) -> _Started:
    """Создать ревьюера, а если ответ не дошёл — спросить, не создан ли он.

    Измерено 06.09.2026: три «отказа» подряд, и по каждому у провайдера
    нашёлся живой оплаченный агент (08:49:06Z, 08:50:53Z, 11:28:05Z).
    Наивный повтор покупал бы второго каждый раз, поэтому порядок такой:
    спросить, подобрать, и только на подтверждённой пустоте повторить.
    """
    adopted = False
    blind = False
    attempts = 1

    async def _attempt() -> tuple[str, str, cursor_cloud.Refusal | None]:
        created, refusal = await cursor_cloud.create_agent_attempt(
            repo_url=repo_url,
            starting_ref=starting_ref,
            model_id=model_id,
            prompt_text=prompt_text,
            hub_mcp_url=hub_mcp_url,
            reviewer_token=reviewer_token,
            name=marker,
        )
        agent = (created or {}).get("agent") or {}
        run = (created or {}).get("run") or {}
        # Идентификатор прогона разрешается ЗДЕСЬ, где известны обе половины
        # ответа: провайдер кладёт его то в run.id, то в agent.latestRunId.
        return (
            str(agent.get("id") or ""),
            str(run.get("id") or agent.get("latestRunId") or ""),
            refusal,
        )

    agent_id, run_id, refusal = await _attempt()
    while not agent_id and refusal is not None and refusal.is_transport:
        seen = await cursor_cloud.find_agent_by_name(marker)
        if not seen.asked:
            # «Не смогли спросить» — не «агента нет». Повторить сейчас
            # значило бы купить второго вслепую, подобрать похожего —
            # отдать сдаче чужого судью. Оба хуже, чем сказать как есть.
            blind = True
            break
        if seen.agent_id:
            agent_id = seen.agent_id
            # Идентификатор прогона забирается вместе с агентом: без него
            # свип не видит статус и не восстанавливает отчёт.
            run_id = seen.run_id or run_id
            adopted = True
            log.info(
                "review dispatch for #%s: answer lost, agent %s adopted",
                task_id,
                seen.agent_id,
            )
            break
        # Сверка прошла и агента нет — вызов действительно не состоялся,
        # повторить его безопасно. Потолок обязателен: семнадцать минут
        # одной и той же неудачи уже наблюдались на #1175.
        if attempts >= _LOST_ANSWER_ATTEMPTS:
            break
        attempts += 1
        agent_id, run_id, refusal = await _attempt()

    return _Started(agent_id, run_id, adopted, blind, attempts, refusal)


async def maybe_dispatch_review(
    db: aiosqlite.Connection, task_id: int, *, force_profile: str = ""
) -> bool:
    """Queue a cloud reviewer for a fresh submission when policy allows.

    Called after submit_for_review commits. Returns True when a dispatch
    was recorded. Every refusal is either silent (policy does not ask for
    it) or a single visible alert (policy asked, the call failed).

    ``force_profile`` skips the profile choice and runs the named one — the
    top-up step of the ladder (#879), where the profile is no longer a guess
    about the task but a fact about the run that just failed to finish.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        return False
    task = dict(row)
    if task.get("status") != "review" or task.get("review_job_id"):
        return False
    generation = task.get("submission_generation") or 0
    branch = (task.get("branch") or "").strip()
    if generation == 0 or not branch or not (task.get("submission_sha") or "").strip():
        return False

    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return False
    if not await _policy_and_novelty_allow(db, task, project, force_profile):
        # #1216: тихий отказ по политике перестаёт быть тихим. Помощник сам
        # разбирает, КТО отказал: у проверки новизны своя запись, и вторую
        # писать поверх неё нечего. Строка остаётся одной — функция стоит на
        # потолке в 60 операторов, и он тут верен.
        return await _name_the_missing_reviewer(db, task, project)

    gh_repo = (dict(project).get("repo") or "").strip()

    # Отказ ДО вызова, по объявленному форжу проекта (#1119). Не «попробуем и
    # разберём ответ»: ответы Cursor настоящую причину не называют — 400 даёт
    # «[invalid_argument] Error», 500 не даёт ничего, а третий известный вид
    # винит несуществующую ветку («Failed to verify existence of branch ...»).
    # Прокинь любой из них наружу — и человек пойдёт чинить ветку, с которой
    # всё в порядке. Отказ по форжу называет причину, которую можно устранить.
    forge = project_policy.forge_of(project)
    if forge not in CLOUD_REVIEW_FORGES:
        # #1180: отсюда путь больше не кончается. Форж, до которого облако не
        # дотягивается, — причина позвать ревьюера ИНАЧЕ, а не причина
        # остаться без второго читателя вовсе.
        return await dispatch_local_review(
            db, task, forge, branch, generation, force_profile
        )

    reviewer_token = (config.CURSOR_REVIEWER_HUB_TOKEN or "").strip()
    # #1083: three independent preconditions, and the message used to list all
    # three with "or" whichever one fired. Two of them live in the process
    # environment on the host, so the card could not say which — telling "no
    # API key" from "the project has no repo" took an ssh to the box. This
    # alert is the ONLY trace a failed dispatch leaves (best-effort means
    # nothing else breaks), so it names what is missing and only that.
    #
    # Names, never values: what goes into a card is the setting's name, which
    # is already written in the open in hub/config.py. No value, no prefix, no
    # length — a length is a guess narrowed.
    missing = [
        label
        for ok, label in (
            (cursor_cloud.is_configured(), "CURSOR_API_KEY (ключ Cursor API)"),
            (bool(gh_repo), "repo проекта (owner/name на его форже)"),
            (
                bool(reviewer_token),
                "CURSOR_REVIEWER_HUB_TOKEN (токен ревьюера)",
            ),
        )
        if not ok
    ]
    if missing:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Кросс-модельное ревью НЕ вызвано: не хватает конфигурации — "
            + "; ".join(missing)
            + ". Вердикт остаётся человеку (#757).",
        )
        await db.commit()
        return False

    # #1180: подготовка вызова — общая для обоих способов добыть отчёт.
    # Профиль, правила репозитория, дифф-план, предпас и одноразовый код
    # ревьюер получает один и тот же, где бы он ни исполнялся; расходятся
    # только транспорт и то, чей принципал подпишет отчёт.
    expected_principal = await reviewer_principal_id(db)
    order = await prepare_review_order(
        db,
        task,
        branch=branch,
        generation=generation,
        force_profile=force_profile,
        principal_id=expected_principal,
    )
    model_id, profile, profile_reasons = order.model, order.profile, order.reasons
    started = await _create_or_adopt(
        cursor_cloud.agent_marker(
            "review",
            task_id,
            generation,
            await _attempt_ordinal(db, task_id, generation),
        ),
        task_id=task_id,
        repo_url=forge_urls.repo_url(forge, gh_repo),
        starting_ref=branch,
        model_id=model_id,
        prompt_text=order.prompt,
        hub_mcp_url=f"{instance_base_url().rstrip('/')}/mcp",
        reviewer_token=reviewer_token,
    )
    agent_id, run_id = started.agent_id, started.run_id
    if not agent_id:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Кросс-модельное ревью НЕ вызвано: {_lost_call_detail(started)}. "
            "Вердикт остаётся человеку; детали в логе хаба (#757).",
        )
        await db.commit()
        return False

    # #1025: pin whose report this dispatch waits for, resolved from the
    # reviewer token at dispatch time (above, where the code was minted under
    # it). An unresolved token is logged and falls back to the old
    # task+generation match rather than blocking the dispatch — degradation is
    # this module's contract.
    if expected_principal is None:
        log.warning(
            "reviewer token resolves to no principal — dispatch for task #%s "
            "matches its report by task+generation only",
            task_id,
        )
    await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id=agent_id,
        run_id=run_id,
        model=model_id,
        profile=profile,
        reviewer_principal_id=expected_principal,
    )
    profile_note = (
        f"профиль {profile} (один проход по диффу)"
        if profile == LITE
        else f"профиль {profile} (многоагентный харнесс)"
    )
    # "deep" on its own is not reviewable in hindsight; the reason is (#820).
    if profile_reasons:
        profile_note += " — " + "; ".join(profile_reasons)
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        f"Кросс-модельное ревью вызвано хабом: модель {model_id} "
        f"(семейство ≠ {task.get('submission_model') or 'не заявлено'}), "
        f"{profile_note}, агент {agent_id}. Правила репозитория: "
        f"{order.rules_note} (#873). Предмет ревью: {order.diff_note} (#874). "
        f"Предпас: {order.prepass.state}"
        + (f" ({', '.join(order.prepass.passed)})" if order.prepass.passed else "")
        + " (#875). "
        + "Отчёт придёт через "
        "hub_submit_machine_review от принципала cursor-cloud-reviewer "
        "(#757, #807)."
        + (
            " Ответ на создание не дошёл, и агент подобран по метке заказа "
            "(#1199): прогон был оплачен, второго не покупали."
            if started.adopted
            else ""
        ),
    )
    await repo.insert_event(
        db,
        kind="review_dispatched",
        task_id=task_id,
        actor="policy",
        payload={
            "model": model_id,
            "agent_id": agent_id,
            "adopted": started.adopted,
            "run_id": run_id,
            "generation": generation,
            "profile": profile,
            "profile_reasons": profile_reasons,
        },
    )
    await db.commit()
    log.info(
        "dispatched cross-model review for task #%s gen %s: %s (%s)",
        task_id,
        generation,
        model_id,
        agent_id,
    )
    return True


@dataclass(frozen=True)
class ReviewReach:
    """Чем ревью на ЭТОМ проекте вообще может быть добыто (#1188).

    Один читатель на троих: диспетчер (звать ли и кого), инвариант записи
    (хранить ли политику) и форма проекта (предлагать ли выбор). До #1180
    ответ сводился к форжу, и каждый спрашивал его сам; после — складывается
    из двух способов, и три копии этого знания разошлись бы на первой правке.
    Разошлись они уже: #1180 научила хаб исполнять dispatch на любом форже, а
    инвариант записи продолжал отказывать по форжу — политику, которую хаб
    умеет исполнить, нельзя было сохранить.

    ``reason`` заполнен ровно тогда, когда ``ways`` пуст: это то, что
    показывают человеку вместо выбора и печатают в отказе. Молча убранный
    пункт меню — такой же обман, как пункт без исполнения.
    """

    ways: tuple[str, ...]
    reason: str

    @property
    def runnable(self) -> bool:
        return bool(self.ways)


async def review_reach(db: aiosqlite.Connection, forge: str) -> ReviewReach:
    """Каким способом ревью добывается на проекте этого форжа, или почему никаким.

    Аргумент — ФОРЖ, а не строка проекта: инвариант записи судит о состоянии
    ПОСЛЕ патча, где форж может меняться тем же запросом, и подсунуть ему
    сохранённую строку значило бы проверить не то состояние (#1119 закрывала
    ровно эту дорогу).

    Облако спрашивается по форжу — это факт про Cursor с датой замера
    (#1119). Локальный путь — по конфигурации И по принципалу: токен может
    БЫТЬ и не разрешаться (отозван, открытый режим), а отчёт под принципалом
    автора гейт не засчитает при REVIEW_SELF_APPROVE=forbid, то есть прогон
    был бы оплачен впустую (#1128).
    """
    if forge in CLOUD_REVIEW_FORGES:
        return ReviewReach((CLOUD_CHANNEL,), "")
    missing = local_reviewer.not_ready()
    if await local_reviewer_principal_id(db) is None:
        missing = missing or ["LOCAL_REVIEWER_HUB_TOKEN не разрешается в принципала"]
    if not missing:
        return ReviewReach((LOCAL_CHANNEL,), "")
    return ReviewReach(
        (),
        f"облачный ревьюер не работает с форжем «{forge}» (проверено "
        "31.08.2026, #1119), а локальный не настроен: "
        + "; ".join(missing)
        + ". Порядок включения — deploy/LOCAL-REVIEW.md",
    )


async def dispatch_local_review(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    forge: str,
    branch: str,
    generation: int,
    force_profile: str = "",
) -> bool:
    """Добыть ревью там, куда облако не дотягивается. True — прогон запущен.

    Каждый отказ здесь НАЗЫВАЕТ причину в карточке. Молчаливый отказ на этом
    месте — это ровно то, что задача #1180 закрывает: на GitVerse вердикт
    выносился вообще без второго читателя, и по карточке это выглядело как
    «ревью не потребовалось».
    """
    task_id = int(task["id"])
    reach = await review_reach(db, forge)
    if not reach.runnable:
        # Причина та же, что увидит человек в форме проекта и в отказе на
        # записи: у неё один автор (#1188), иначе три места объясняли бы
        # одно состояние тремя разными словами.
        return await _refuse_local_review(db, task_id, reach.reason)
    principal_id = await local_reviewer_principal_id(db)
    spent = await _tokens_already_spent(db, task_id)
    if spent >= config.LOCAL_REVIEW_TOKEN_CEILING:
        return await _refuse_local_review(
            db,
            task_id,
            f"потолок стоимости исчерпан: на задачу уже потрачено {spent} "
            f"токенов при потолке {config.LOCAL_REVIEW_TOKEN_CEILING} "
            "(LOCAL_REVIEW_TOKEN_CEILING). Прогон не запущен — это НЕ "
            "«прочитано и чисто» (#1152)",
        )
    # #1180 + находка 7ed386a8: добор лестницы (#879) обязан доехать сюда
    # ТЕМ ЖЕ профилем, каким его заказали. Без проброса локальный путь снова
    # выбирал профиль сам и покупал второй однопроходный прогон вместо
    # харнесса — то есть лестница на не-GitHub не поднималась ни на ступень,
    # молча и за деньги.
    order = await prepare_review_order(
        db,
        task,
        branch=branch,
        generation=generation,
        force_profile=force_profile,
        principal_id=principal_id,
    )
    run_id = uuid.uuid4().hex[:12]
    dispatch_id = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id=f"local:{run_id}",
        run_id=run_id,
        model=order.model,
        profile=order.profile,
        reviewer_principal_id=principal_id,
        channel=LOCAL_CHANNEL,
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        f"Машинное ревью запущено ЛОКАЛЬНО: форж «{forge}» облачному ревьюеру "
        f"недоступен, прогон идёт на хосте хаба под песочницей (#1180). "
        f"Профиль {order.profile}, прогон {run_id}. Правила репозитория: "
        f"{order.rules_note} (#873). Предмет ревью: {order.diff_note} (#874). "
        "Отчёт придёт по контракту от принципала локального ревьюера — "
        "его независимость держит токен, а не машина (#728).",
    )
    await repo.insert_event(
        db,
        kind="review_dispatched",
        task_id=task_id,
        actor="policy",
        payload={
            "model": order.model,
            "agent_id": f"local:{run_id}",
            "run_id": run_id,
            "generation": generation,
            "profile": order.profile,
            "profile_reasons": order.reasons,
            "channel": LOCAL_CHANNEL,
        },
    )
    await db.commit()
    await _start_local_run(db, dispatch_id, task_id, generation, order.prompt)
    return True


async def _refuse_local_review(
    db: aiosqlite.Connection, task_id: int, reason: str
) -> bool:
    """Один алерт с названной причиной, и ничего больше. Всегда False."""
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Машинное ревью НЕ вызвано: {reason}. Вердикт остаётся человеку "
        "(#757, #1180).",
    )
    await db.commit()
    log.info("local review refused for task #%s: %s", task_id, reason)
    return False


async def _tokens_already_spent(db: aiosqlite.Connection, task_id: int) -> int:
    """Сколько токенов задача уже стоила по отчётам ревью.

    Считается по ОТЧЁТАМ, а не по числу прогонов: прогон, о котором ревьюер
    не отчитался, деньги всё равно стоил, но назвать сумму мы можем только
    там, где она сказана. Незнание здесь склоняется в сторону прогона —
    пропущенное ревью дороже лишнего, — и это тот же выбор направления
    ошибки, что в #762.
    """
    rows = await fetchall(
        db,
        "SELECT COALESCE(SUM(COALESCE(provider_tokens, tokens_spent, 0)), 0) AS total "
        "FROM machine_reviews WHERE task_id = ?",
        (task_id,),
    )
    return int(dict(rows[0])["total"]) if rows else 0


@dataclass(frozen=True)
class _LocalRunHandle:
    """Прогон, за которым смотрит ЭТОТ процесс хаба.

    Кроме самой задачи хранит координаты строки диспетчера: при остановке
    хаба её надо ЗАКРЫТЬ, а сделать это изнутри отменяемой корутины нельзя —
    там уже нет права ждать (найдено ревью, находка 30e3ac32).
    """

    task: asyncio.Task[None]
    db_path: str
    dispatch_id: int
    task_id: int
    generation: int


# Ссылка на задачу держится намеренно: задача без ссылки может быть собрана
# сборщиком мусора посреди работы, и ревьюер тогда умрёт молча. Тесты ждут
# прогоны через wait_for_local_runs().
_LOCAL_RUNS: dict[int, _LocalRunHandle] = {}


async def _start_local_run(
    db: aiosqlite.Connection,
    dispatch_id: int,
    task_id: int,
    generation: int,
    prompt: str,
) -> None:
    """Запустить прогон фоном и вернуть управление сдаче.

    Фоном, потому что прогон живёт минуты и десятки минут, а зовут нас из
    обработчика сдачи: ждать его там значило бы держать HTTP-запрос автора всё
    ревью. Своё соединение к базе — по той же причине, по которой его берут
    провижининг и поллер (#1065): соединение запроса закрывается сразу после
    ответа, и фон писал бы в закрытое.
    """
    path = await _main_db_path(db)
    task = asyncio.create_task(
        _supervise_local_run(
            db_path=path,
            live_db=None if path else db,
            dispatch_id=dispatch_id,
            task_id=task_id,
            generation=generation,
            prompt=prompt,
        )
    )
    _LOCAL_RUNS[dispatch_id] = _LocalRunHandle(
        task=task,
        db_path=path,
        dispatch_id=dispatch_id,
        task_id=task_id,
        generation=generation,
    )
    task.add_done_callback(lambda _t: _LOCAL_RUNS.pop(dispatch_id, None))


async def wait_for_local_runs() -> None:
    """Дождаться прогонов этого процесса — их естественного конца. Для тестов."""
    while _LOCAL_RUNS:
        await asyncio.gather(
            *[h.task for h in _LOCAL_RUNS.values()], return_exceptions=True
        )


async def cancel_local_runs() -> None:
    """Снять прогоны при остановке хаба и ЗАКРЫТЬ их строки.

    ЖДАТЬ на остановке нельзя: прогон живёт до получаса, а хаб на выключении
    имеет секунды. Отмена доходит до ревьюера настоящим убийством группы —
    ``local_reviewer`` ловит CancelledError и снимает процесс, — а не просто
    бросает корутину, оставив CLI жить сиротой (#d478b896).

    Строку диспетчера закрывает ЭТА функция, а не отменённая корутина
    (найдено ревью, находка 30e3ac32): у отменённой нет права ждать, а
    оставленная активной строка говорит «ревью идёт» о прогоне, который
    только что убили. Свип назвал бы это потерей лишь через сорок пять
    минут, и всё это время карточка врала бы в настоящем времени.
    """
    handles = list(_LOCAL_RUNS.values())
    for handle in handles:
        handle.task.cancel()
    if not handles:
        return
    await asyncio.gather(*[h.task for h in handles], return_exceptions=True)
    for handle in handles:
        await _close_cancelled_run(handle)


async def _close_cancelled_run(handle: _LocalRunHandle) -> None:
    """Отметить снятый при остановке прогон — по имени причины."""
    if not handle.db_path:
        return
    from hub import db as db_module

    conn = None
    try:
        conn = await db_module.connect(handle.db_path)
        rows = await fetchall(
            conn, "SELECT * FROM review_dispatches WHERE id = ?", (handle.dispatch_id,)
        )
        if not rows:
            return
        # Строку читаем целиком, а не подставляем один id: сопоставление
        # отчёта с прогоном идёт по принципалу ревьюера (#1025), и заглушка
        # без него молча вернула бы к старому правилу «любой отчёт этой
        # генерации» — то есть могла бы закрыть прогон чужой работой.
        review = await _dispatch_report(
            conn, handle.task_id, handle.generation, dict(rows[0])
        )
        if review is None:
            await repo.add_task_update(
                conn,
                handle.task_id,
                "hub",
                "alert",
                "Локальное машинное ревью снято при остановке хаба: процесс "
                "убит вместе с хабом, отчёта нет. Это НЕ «ревью ничего не "
                "нашло» — вердикт остаётся человеку (#1180).",
            )
        await repo.set_review_dispatch_status(
            conn, handle.dispatch_id, "done" if review is not None else "failed"
        )
        await conn.commit()
    except Exception:  # noqa: BLE001 - остановка хаба не падает из-за уборки
        log.exception("could not close the cancelled local run #%s", handle.dispatch_id)
    finally:
        if conn is not None:
            await conn.close()


async def _main_db_path(db: aiosqlite.Connection) -> str:
    """Файл базы, с которым работает ЭТО соединение, или пустая строка.

    Спрашивается у соединения, а не берётся из конфига намеренно: фон обязан
    писать в ту же базу, где живёт задача, а не в ту, что назначена в
    окружении. База в памяти файла не имеет — тогда фон работает на переданном
    соединении, потому что второго к ней не бывает.
    """
    try:
        rows = await fetchall(db, "PRAGMA database_list")
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("could not read the database path: %s", exc)
        return ""
    for row in rows:
        entry = dict(row)
        if entry.get("name") == "main":
            return (entry.get("file") or "").strip()
    return ""


async def _supervise_local_run(
    *,
    db_path: str,
    live_db: aiosqlite.Connection | None,
    dispatch_id: int,
    task_id: int,
    generation: int,
    prompt: str,
) -> None:
    run = await local_reviewer.run_review(prompt)
    conn = None
    try:
        if db_path:
            from hub import db as db_module

            conn = await db_module.connect(db_path)
        target = conn if conn is not None else live_db
        if target is None:
            log.error(
                "local review #%s finished with nowhere to record it", dispatch_id
            )
            return
        await _settle_local_run(target, dispatch_id, task_id, generation, run)
    except Exception:  # noqa: BLE001 - фон не имеет права уронить хаб
        log.exception("could not settle the local review of task #%s", task_id)
    finally:
        if conn is not None:
            await conn.close()


async def _settle_local_run(
    db: aiosqlite.Connection,
    dispatch_id: int,
    task_id: int,
    generation: int,
    run: local_reviewer.LocalRun | None,
) -> None:
    """Закрыть локальный прогон: отчёт по контракту, текстом или причина.

    Три исхода и ни одного молчаливого. «Ревью не состоялось» и «ревью ничего
    не нашло» — разные вещи (#419, #725), и здесь они не сливаются даже когда
    процесс умер, не сказав ни слова.
    """
    rows = await fetchall(
        db, "SELECT * FROM review_dispatches WHERE id = ?", (dispatch_id,)
    )
    if not rows:
        return
    dispatch = dict(rows[0])
    review = await _dispatch_report(db, task_id, generation, dispatch)
    if review is not None:
        await repo.set_review_dispatch_status(db, dispatch_id, "done")
        await db.commit()
        return
    from hub.services.machine_review_intake import ORIGIN_LOCAL_TEXT

    report = parse_report_block(run.output if run else None)
    if report is not None and await _store_report(
        db, dispatch, report, ORIGIN_LOCAL_TEXT
    ):
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "status",
            "Отчёт локального ревью восстановлен из вывода прогона: по "
            "контракту он не пришёл, и агент оставил его блоком в тексте. "
            "Записан с пометкой происхождения — это слабее сданного по "
            "контракту: прогон писал его о себе сам (#1036).",
        )
        await repo.set_review_dispatch_status(db, dispatch_id, "done")
        await db.commit()
        return
    await repo.add_task_update(db, task_id, "hub", "alert", _local_failure_reason(run))
    await repo.set_review_dispatch_status(db, dispatch_id, "failed")
    await db.commit()


def _local_failure_reason(run: local_reviewer.LocalRun | None) -> str:
    """Почему прогона нет — по имени, а не «что-то пошло не так»."""
    if run is None:
        return (
            "Локальное машинное ревью НЕ состоялось: прогон не удалось "
            "запустить — нет каталога, бинаря или прав (детали в логе хаба). "
            "Это не «прочитано и чисто»: вердикт остаётся человеку (#1180)."
        )
    if run.timed_out:
        return (
            f"Локальное машинное ревью снято по таймауту "
            f"({config.LOCAL_REVIEW_TIMEOUT_SEC} с): процесс и вся его группа "
            "убиты, хаб продолжает работу, отчёта нет. Вердикт остаётся "
            "человеку (#1180)."
        )
    tail = (run.output or "").strip()[-1500:]
    dropped = (
        f", {run.dropped} байт вывода выброшено сверх лимита" if run.dropped else ""
    )
    return (
        f"Локальное машинное ревью завершилось без отчёта: код возврата "
        f"{run.rc}, прогон занял {run.duration_ms // 1000} с{dropped}, "
        "разбираемого блока в выводе нет. Вердикт остаётся человеку (#1180)."
        + (f"\n\nХвост вывода:\n{tail}" if tail else "")
    )


def instance_base_url() -> str:
    """This hub's own public base URL, as this installation is configured.

    #1005: the vendor's host used to sit here as a fallback, and it could
    never fire — ``hub_base_url`` falls back to ``http://HOST:PORT`` and so
    always answers. A dead constant naming the authors' server is the kind of
    default that turns somebody else's installation into a client of ours the
    moment the code around it changes; the empty answer is the honest one.
    """
    from hub.hub_instance import instance_echo_fields

    return instance_echo_fields().get("base_url", "")


async def reviewer_principal_id(db: aiosqlite.Connection) -> int | None:
    """The principal behind CURSOR_REVIEWER_HUB_TOKEN, or None (#1025)."""
    return await principal_of_token(db, config.CURSOR_REVIEWER_HUB_TOKEN)


async def local_reviewer_principal_id(db: aiosqlite.Connection) -> int | None:
    """The principal behind LOCAL_REVIEWER_HUB_TOKEN, or None (#1180).

    Свой токен, а не общий с облачным: независимость отчёта держится тем, чей
    токен его принёс (#728), и отзывать локального ревьюера надо уметь
    отдельно — он исполняется на хосте хаба, а облачный нет.
    """
    return await principal_of_token(db, config.LOCAL_REVIEWER_HUB_TOKEN)


async def principal_of_token(db: aiosqlite.Connection, raw: str) -> int | None:
    """Кому принадлежит токен, или None (#1025).

    The same hash lookup auth performs, without its side effects. None — an
    empty token, an env-map token, a rotated key — leaves the dispatch under
    the old task+generation matching rule rather than inventing an identity.
    """
    token = (raw or "").strip()
    if not token:
        return None
    # Open mode never reads the bearer header, so every report lands with
    # principal_id NULL — a pinned dispatch would be unsatisfiable there and
    # die by a FALSE grace alert, with the reviewer's own report flagged as
    # foreign. Worse than the old rule, which is exactly what degradation
    # must never be: no pin in open mode.
    from hub import auth

    if auth._is_open_mode():
        return None
    from hub.services.admin import hash_api_key

    rows = await fetchall(
        db,
        "SELECT principal_id FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
        (hash_api_key(token),),
    )
    return int(dict(rows[0])["principal_id"]) if rows else None


def _expected_principal(dispatch: dict[str, Any] | None) -> int | None:
    """Whose report settles this dispatch, or None for the old rule (#1025).

    None — a dispatch recorded before the column existed, or one whose token
    never resolved — keeps the task+generation match: history must not change
    its meaning retroactively.
    """
    if dispatch is None:
        return None
    raw = dispatch.get("reviewer_principal_id")
    return int(raw) if raw is not None else None


async def _dispatch_report(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    dispatch: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The report THIS dispatch is waiting for, or None (#1025).

    With a known reviewer principal only that principal's report counts,
    matched by the token-derived ``principal_id`` on the row — never by
    ``submitted_by``, which is the caller naming itself. Before this rule the
    author's parallel report closed the hub's own dispatch as done, silenced
    the no-report alert and fed the author's numbers to the usage
    cross-check (#1011 gen 1: 71296 declared against 2574930 billed read as
    a 36x discrepancy of a run that had submitted nothing).
    """
    expected = _expected_principal(dispatch)
    if dispatch is None or expected is None:
        row = await repo.get_latest_machine_review(db, task_id)
        if row is None:
            return None
        review = dict(row)
        if (review.get("submission_generation") or 0) != generation:
            return None
        return review
    rows = await repo.machine_reviews_of_generation(db, task_id, generation)
    own = [dict(r) for r in rows if dict(r).get("principal_id") == expected]
    # The ladder (#879) runs two SAME-principal dispatches on one generation,
    # so principal+generation alone still matched the lite report to the deep
    # dispatch — settled 'done' before its run reported, grace alert
    # unreachable. Rungs pair with reports by order instead: the k-th
    # dispatch of the generation waits for the principal's k-th report.
    # Exact for every current path, because a second dispatch exists only
    # after the first report bought it (top-up is the sole same-generation
    # re-dispatch).
    dispatch_ids = await fetchall(
        db,
        "SELECT id FROM review_dispatches WHERE task_id = ? "
        "AND submission_generation = ? ORDER BY id",
        (task_id, generation),
    )
    order = [int(dict(r)["id"]) for r in dispatch_ids]
    try:
        rung = order.index(int(dispatch["id"]))
    except (ValueError, KeyError, TypeError):
        return None  # the dispatch row is gone or unreadable — nothing to match
    return own[rung] if rung < len(own) else None


def _provider_token_total(usage: dict[str, Any] | None) -> int | None:
    """The billed total, or None when the provider did not answer (#1026)."""
    total = ((usage or {}).get("totalUsage") or {}).get("totalTokens")
    if isinstance(total, int) and total >= 0:
        return total
    return None


async def _stamp_dispatch_usage(
    db: aiosqlite.Connection, dispatch: dict[str, Any]
) -> int | None:
    """Ask the provider what this run billed; leave NULL when unknown (#1026)."""
    usage = await cursor_cloud.get_usage(
        dispatch["agent_id"], dispatch["run_id"] or None
    )
    total = _provider_token_total(usage)
    if total is not None:
        await repo.set_review_dispatch_provider_tokens(db, dispatch["id"], total)
    return total


def parse_report_block(text: str | None) -> Any | None:
    """The report a run left in its own text, or None (#1036).

    Returns a validated ``MachineReviewSubmit``. None means the text carried
    no usable report — no fence, no JSON, or fields the contract refuses. The
    caller must treat that as "no report", never as an empty one: inventing
    structure out of prose is exactly how "could not read" turns into "read
    and clean", which is the failure this whole area keeps circling.

    The LAST block wins. A model explaining itself may quote the format on the
    way, and the example must not be able to shadow the real answer.
    """
    if not text:
        return None
    from hub.models import MachineReviewSubmit

    blocks = re.findall(
        rf"```{re.escape(REPORT_FENCE)}\s*(.*?)```", text, flags=re.DOTALL
    )
    for raw in reversed(blocks):
        try:
            payload = json.loads(raw.strip())
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            return MachineReviewSubmit(**payload)
        except Exception:  # noqa: BLE001 - a refused report is simply not one
            log.warning("run text carried a report the contract refused")
            continue
    return None


async def _recover_report_from_run(
    db: aiosqlite.Connection, dispatch: dict[str, Any], run: dict[str, Any]
) -> bool:
    """Record the report a finished run left in its text. True when stored.

    The path exists because the contract path stopped working: Cursor no
    longer delivers the hub's MCP into a cloud run, so since 22.08 reviewers
    finish their work and leave it in ``result`` — measured, paid for, and
    unread. The transcription is deliberately narrow.

    * It runs only after the grace window, so a report still on its way
      through MCP always wins (that path is checked first, above).
    * The stored row belongs to the dispatch's OWN reviewer principal.
      Anything else and the report would read as foreign to its own dispatch,
      which is the defect #1025 closed.
    * Its origin is recorded in the data, because a report typed into a text
      field cannot be checked the way a submitted one can.
    * No block, or a block the contract refuses, stores NOTHING. The text is
      kept in the feed as prose and the dispatch fails as before.
    """
    report = parse_report_block(run.get("result"))
    task_id = int(dispatch["task_id"])
    if report is None:
        tail = (run.get("result") or "").strip()
        if tail:
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "alert",
                "Прогон ревью завершился без отчёта по контракту, а в тексте "
                "рана нет разбираемого блока — структура НЕ восстанавливается "
                "по прозе. Текст сохранён как есть, находки из него никем не "
                f"подтверждены (#1036):\n\n{tail[:4000]}",
            )
        return False
    from hub.services.machine_review_intake import ORIGIN_RUN_TEXT

    if not await _store_report(db, dispatch, report, ORIGIN_RUN_TEXT):
        return False
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        "Отчёт ревью восстановлен из текста прогона: MCP до рана не дошёл, и "
        f"агент {dispatch['agent_id']} ({dispatch['model']}) оставил отчёт "
        "блоком в ответе. Он записан с пометкой происхождения — это слабее "
        "отчёта, сданного по контракту: прогон писал его о себе сам (#1036).",
    )
    return True


async def _sweep_orphan_local(
    db: aiosqlite.Connection, dispatch: dict[str, Any]
) -> None:
    """Локальная строка, за которой больше некому смотреть.

    Живой прогон этого процесса пропускается: его закроет собственная
    корутина, и вмешательство свипа отняло бы у неё исход. Всё остальное —
    прогон, начатый ДРУГИМ процессом хаба до перезапуска, — по истечении
    того же grace, что у облака, закрывается названной причиной. Оставить
    такую строку активной значило бы навсегда занять место в лестнице (#879)
    прогоном, которого уже нет.
    """
    dispatch_id = int(dispatch["id"])
    if dispatch_id in _LOCAL_RUNS:
        return
    task_id = int(dispatch["task_id"])
    generation = int(dispatch["submission_generation"] or 0)
    review = await _dispatch_report(db, task_id, generation, dispatch)
    if review is not None:
        await repo.set_review_dispatch_status(db, dispatch_id, "done")
        await db.commit()
        return
    # Grace НЕ короче собственного таймаута прогона (найдено ревью, раздел
    # unresolved: 4c9701ec). Облачные 15 минут против получасового локального
    # таймаута означали бы, что свип объявляет потерянным прогон, который
    # честно работает и ещё имеет право сдать отчёт. «Не дождались» и
    # «не состоялось» — разные вещи, и первое не должно печататься вторым.
    minutes = config.CURSOR_REVIEW_GRACE_MINUTES + math.ceil(
        config.LOCAL_REVIEW_TIMEOUT_SEC / 60
    )
    grace = await fetchall(
        db,
        "SELECT 1 FROM review_dispatches WHERE id=? "
        "AND created_at <= datetime('now', ?)",
        (dispatch_id, f"-{minutes} minutes"),
    )
    if not grace:
        return
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        "Локальное машинное ревью потеряно: прогон запускал другой процесс "
        "хаба, и после перезапуска досматривать его некому — отчёта за "
        f"{minutes} мин не пришло (таймаут прогона плюс grace). Это НЕ «ревью "
        "ничего не нашло»: вердикт остаётся человеку (#1180).",
    )
    await repo.set_review_dispatch_status(db, dispatch_id, "failed")
    await db.commit()


async def _store_report(
    db: aiosqlite.Connection,
    dispatch: dict[str, Any],
    report: Any,
    origin: str,
) -> bool:
    """Записать отчёт, оставленный прогоном в СВОЁМ тексте. True — записан.

    Владелец отчёта берётся из строки диспетчера, а не из того, как отчёт
    называет себя сам: иначе он прочитался бы как чужой собственному вызову —
    дефект, закрытый в #1025. Происхождение пишется в данные: отчёт,
    переписанный хабом из текста, — факт слабее сданного по контракту, и
    метрики со стюардом должны уметь взвесить их по-разному (#1036).

    Одна реализация на оба канала намеренно: облачный и локальный прогон
    оставляют текст по одной и той же причине и с одинаковой доказательной
    силой, и две копии этого правила разошлись бы на первой же правке.
    """
    task_id = int(dispatch["task_id"])
    from hub.services.machine_review_intake import record_machine_review

    try:
        await record_machine_review(
            db,
            task_id,
            report,
            principal_id=dispatch.get("reviewer_principal_id"),
            username=(dispatch.get("model") or "cursor-cloud-reviewer"),
            origin=origin,
        )
    except Exception:  # noqa: BLE001 - the sweep must survive a bad report
        log.exception("could not record the report recovered for task #%s", task_id)
        return False
    return True


async def sweep_review_dispatches(db: aiosqlite.Connection) -> None:
    """Poller pass over active dispatches: settle finished runs.

    - report arrived → cross-check tokens against the provider's usage
      (mismatch is an audit flag, never a mechanical block) → done;
    - run terminal, no report, grace expired → one loud alert → failed;
    - run still going / API unreachable → leave for the next pass.
    """
    for row in await repo.list_active_review_dispatches(db):
        dispatch = dict(row)
        task_id = dispatch["task_id"]
        if dispatch.get("channel") == LOCAL_CHANNEL:
            # Локальный прогон досматривает своя корутина (#1180). Свипу тут
            # остаётся один случай — процесс хаба, перезапущенный посреди
            # прогона: корутины больше нет, и без этой ветки строка осталась
            # бы «активной» навсегда.
            await _sweep_orphan_local(db, dispatch)
            continue
        review = await _dispatch_report(
            db, task_id, dispatch["submission_generation"], dispatch
        )
        if review is not None:
            total = await _stamp_dispatch_usage(db, dispatch)
            reported = review.get("tokens_spent")
            if total is not None:
                # #828: keep the number on the report too so existing
                # practice metrics over machine_reviews stay honest.
                await repo.set_machine_review_provider_tokens(
                    db,
                    task_id,
                    dispatch["submission_generation"],
                    total,
                    review_id=int(review["id"]),
                )
            if isinstance(total, int) and total > 0:
                mismatch = reported is None or (
                    abs(total - reported) / total > _USAGE_MISMATCH_SHARE
                )
                if mismatch:
                    await repo.add_task_update(
                        db,
                        task_id,
                        "hub",
                        "alert",
                        f"Отчёт ревью расходится с данными провайдера: "
                        f"tokens_spent={reported}, Cursor usage={total}. "
                        "Сигнал аудиту — сверка по данным, не по дисциплине "
                        "(#757).",
                    )
            await repo.set_review_dispatch_status(db, dispatch["id"], "done")
            await db.commit()
            continue

        run = await cursor_cloud.get_run(dispatch["agent_id"], dispatch["run_id"])
        if run is None:
            continue  # API hiccup or run still unknown — retry next pass
        if (run.get("status") or "").upper() not in _TERMINAL_RUN_STATUSES:
            continue
        grace_rows = await fetchall(
            db,
            "SELECT 1 FROM review_dispatches WHERE id=? "
            "AND created_at <= datetime('now', ?)",
            (dispatch["id"], f"-{config.CURSOR_REVIEW_GRACE_MINUTES} minutes"),
        )
        if not grace_rows:
            continue
        await _stamp_dispatch_usage(db, dispatch)
        if await _recover_report_from_run(db, dispatch, run):
            await repo.set_review_dispatch_status(db, dispatch["id"], "done")
            await db.commit()
            continue
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Кросс-модельное ревью вызвано, но отчёт НЕ сдан: агент "
            f"{dispatch['agent_id']} ({dispatch['model']}) завершил ран со "
            f"статусом {run.get('status')}, machine-review актуальной "
            "генерации отсутствует. Вердикт остаётся человеку (#757).",
        )
        task_row = await repo.get_task(db, task_id)
        task_status = dict(task_row)["status"] if task_row else ""
        if task_status != "review":
            await repo.insert_event(
                db,
                kind="review_dispatch_failed",
                task_id=task_id,
                actor="hub",
                payload={
                    "dispatch_id": dispatch["id"],
                    "model": dispatch.get("model") or "",
                    "run_status": run.get("status"),
                    "task_status": task_status,
                },
            )
        await repo.set_review_dispatch_status(db, dispatch["id"], "failed")
        await db.commit()


# ---------------------------------------------------------------------------
# Круг ревью: заходы, где находки ЗАКРЫВАЛИСЬ (#1235)
# ---------------------------------------------------------------------------
#
# Два потолка уже стоят, и оба ловят ПОВТОР БЕЗ ИЗМЕНЕНИЯ: бюджет циклов
# считает возвраты работы автору (review_cycle), потолок драфта (#1161)
# стоит на неизменившейся ревизии постановки. Круг, наблюдённый 09.09.2026
# на #1171, #1208 и #1169, не ловится ни тем, ни другим по построению: код
# менялся по-настоящему, каждая пересдача несла реальную работу и честно
# покупала новый глубокий прогон, который находил следующий слой. На #1171
# review_cycle оставался нулём при третьем заходе.
#
# Поэтому здесь СЧИТАЮТСЯ ПОКОЛЕНИЯ, а не возвраты, и ничего не
# останавливается. Ревью не глушится, пересдача не запрещается, статус не
# меняется: находки в таком круге настоящие (из 25 неразрешённых за день
# настоящими оказались 24), и механизм, который упёрся бы в потолок на
# задаче, где каждый заход закрывает настоящие дефекты, вытолкнул бы к
# человеку именно ту работу, которая шла верно. Хаб называет круг и зовёт
# человека решать — этим его полномочия и кончаются.

#: Исходы, которыми автор говорит, что дефекта в коде больше нет — он его
#: ПОПРАВИЛ. ``fixed`` из словаря подтверждённых находок, ``real_fixed`` —
#: из словаря неразрешённых (#1085).
#:
#: Свой набор, а не ``models._SELF_EVIDENT_OUTCOMES``, хотя состав сегодня
#: совпадает буква в букву. Тот отвечает на вопрос «обязан ли этот исход
#: нести строку объяснения», этот — на вопрос «была ли на этом заходе
#: сделана работа». Два разных вопроса, совпавших в ответе, разойдутся на
#: первом же новом слове в любом из словарей, и заход тогда считался бы по
#: признаку из чужой задачи.
#:
#: ``false_positive``, ``not_a_defect``, ``wont_fix``, ``deferred``,
#: ``real_deferred`` и ``not_judged`` сюда не входят намеренно: круг, ради
#: которого всё это заведено, — про поколения, где автор ЧИНИЛ и получал
#: новый слой. Пересдача, на которой все находки объявлены ложными, — это
#: не круг, а разговор о точности харнесса, и у него свои метрики.
CIRCLE_CLOSING_OUTCOMES: frozenset[str] = frozenset({"fixed", "real_fixed"})

#: Метка записи о круге, по которой она находится снова. Внутри — номер
#: поколения: дедуп обязан быть в его пределах, иначе одна запись за всю
#: жизнь задачи молчала бы про каждый следующий заход. Образец —
#: ``NO_REVIEWER_MARK`` выше, и по той же причине ключ разбирается из
#: текста, а не хранится второй колонкой.
CIRCLE_MARK = "[круг ревью: сдача {generation}]"


@dataclass(frozen=True)
class CircleLap:
    """Один заход: находки прошлого поколения закрыты, пришли новые.

    ``generation`` — поколение, В КОТОРОМ пришли новые находки, то есть то,
    по которому заход и виден. ``closed`` — сколько находок ПРЕДЫДУЩЕГО
    поколения автор закрыл правкой, ``arrived`` — сколько новых принёс
    отчёт этого. Оба числа рядом намеренно: одно без другого не отличает
    круг от обычной работы.
    """

    generation: int
    closed: int
    arrived: int
    repeated_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewCircle:
    """Сколько заходов подряд задача уже сделала и назван ли круг."""

    laps: tuple[CircleLap, ...]
    threshold: int

    @property
    def count(self) -> int:
        return len(self.laps)

    @property
    def named(self) -> bool:
        """Пора ли звать человека. Порог из конфига, сравнение — здесь одно."""
        return self.threshold > 0 and self.count >= self.threshold

    @property
    def repeated_categories(self) -> tuple[str, ...]:
        """Категории, которые новый заход повторил за предыдущим.

        Отдельно от числа заходов, потому что это ДРУГОЙ признак и он
        важнее: три захода с находками разного рода — работа, идущая
        вглубь, а повтор категории означает, что харнесс ходит по одному и
        тому же месту.
        """
        seen: list[str] = []
        for lap in self.laps:
            for category in lap.repeated_categories:
                if category not in seen:
                    seen.append(category)
        return tuple(seen)

    def breakdown(self) -> list[str]:
        """По строке на заход: «закрыто / пришло новых», в порядке заходов."""
        lines: list[str] = []
        for ordinal, lap in enumerate(self.laps, start=1):
            line = (
                f"заход {ordinal} (сдача {lap.generation}): "
                f"закрыто {lap.closed}, пришло новых {lap.arrived}"
            )
            if lap.repeated_categories:
                line += f"; повтор категории: {', '.join(lap.repeated_categories)}"
            lines.append(line)
        return lines


def _findings_of(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Находки одного отчёта как ``(uid, категория)``.

    Обе секции сразу — и подтверждённые, и неразрешённые. Круг, который
    оплачивался живьём, состоял в основном из НЕРАЗРЕШЁННЫХ находок (#1171:
    ноль подтверждённых и пять новых неразрешённых), так что счёт по одним
    подтверждённым не увидел бы его вовсе.

    У неразрешённой записи категории нет — модель её не несёт (#1085), — и
    здесь она остаётся пустой, а не выдумывается. Пустая категория в
    сравнение на повтор не входит: «неизвестно» и «то же самое» — разные
    ответы, и склеить их значило бы объявить повтор там, где о категории
    ничего не сказано.
    """
    from hub.services.finding_identity import finding_uids, unresolved_uids

    out: list[tuple[str, str]] = []
    try:
        confirmed = json.loads(row.get("findings_confirmed") or "[]")
    except ValueError:
        confirmed = []
    if isinstance(confirmed, list):
        entries = [f for f in confirmed if isinstance(f, dict)]
        for uid, finding in zip(finding_uids(entries), entries):
            out.append((uid, str(finding.get("category") or "").strip()))
    try:
        unresolved = json.loads(row.get("unresolved") or "[]")
    except ValueError:
        unresolved = []
    if isinstance(unresolved, list):
        entries = [f for f in unresolved if isinstance(f, dict)]
        for uid in unresolved_uids(entries):
            out.append((uid, ""))
    return out


async def review_circle(db: aiosqlite.Connection, task_id: int) -> ReviewCircle:
    """Заходы этой задачи: «находки закрыли — пришли новые», подряд.

    ЗАХОД — это пара поколений: в поколении N отчёт нашёл находки, автор их
    ЗАКРЫЛ правкой (``CIRCLE_CLOSING_OUTCOMES``), а отчёт поколения N+1
    принёс находки, которых в N не было. Условий ДВА, и каждое стоит
    против своей ошибки:

    * ЗАКРЫТИЕ находок поколения N правкой. Оно же закрывает и требование
      «находки в N были»: закрыть можно только то, что нашли, поэтому
      пересдача с чистым отчётом заходом не станет (AC-2). Отдельной
      третьей проверки «находки были» здесь нет намеренно — она
      недостижима, а условие, которое не может сработать, врёт читателю
      про свою работу: мутация, снимающая его, не роняет ни одного теста.
      Проверяется именно закрытие, а не наличие: пересдача, на которой
      автор объявил все находки ложными или отложил их, работой по коду не
      была — это разговор о точности харнесса, и у него свои метрики.
    * НОВЫЕ находки в N+1 — по ``finding_uid``, который выводится из
      содержания находки (#1007), поэтому тот же дефект, найденный снова,
      имеет тот же id и новым не считается.

    ПОДРЯД — буквально: считается ХВОСТОВАЯ серия, оканчивающаяся на самом
    свежем поколении с отчётом. Заход, прервавшийся чистым отчётом,
    обнуляет счёт, потому что круг на этом и кончился; хранить его как
    заслугу значило бы позвать человека к задаче, которая уже вышла.

    Поколения без отчёта в счёт не входят и цепь не рвут: отчёта нет —
    значит, о находках этого поколения не известно ничего, а «неизвестно»
    не равно «чисто».
    """
    rows = await fetchall(
        db,
        "SELECT submission_generation, findings_confirmed, unresolved "
        "FROM machine_reviews WHERE task_id=? "
        "ORDER BY submission_generation, id",
        (task_id,),
    )
    per_generation: dict[int, list[tuple[str, str]]] = {}
    for raw in rows:
        row = dict(raw)
        generation = int(row.get("submission_generation") or 0)
        per_generation.setdefault(generation, []).extend(_findings_of(row))

    closed_rows = await fetchall(
        db,
        "SELECT submission_generation, finding_uid, outcome "
        "FROM finding_outcomes WHERE task_id=?",
        (task_id,),
    )
    closed: dict[int, set[str]] = {}
    for raw in closed_rows:
        row = dict(raw)
        if str(row.get("outcome") or "") not in CIRCLE_CLOSING_OUTCOMES:
            continue
        generation = int(row.get("submission_generation") or 0)
        closed.setdefault(generation, set()).add(str(row.get("finding_uid") or ""))

    generations = sorted(per_generation)
    laps: list[CircleLap] = []
    for previous, current in zip(generations, generations[1:]):
        before = per_generation[previous]
        seen = {uid for uid, _ in before}
        shut = seen & closed.get(previous, set())
        fresh = [(uid, cat) for uid, cat in per_generation[current] if uid not in seen]
        if not shut or not fresh:
            # Цепь оборвалась: дальше считается заново, а не поверх.
            laps = []
            continue
        # Пустая категория отсеивается ОДИН раз, на стороне предыдущего
        # поколения: непустого имени, равного пустому, не бывает, поэтому
        # второй такой же отсев на стороне новых находок не мог бы ничего
        # изменить — и мутация, снимающая его, не роняла ни одного теста.
        earlier = {cat for _, cat in before if cat}
        repeated = sorted({cat for _, cat in fresh if cat in earlier})
        laps.append(
            CircleLap(
                generation=current,
                closed=len(shut),
                # Уникальные uid, а не длина списка: находки ВСЕХ отчётов
                # поколения слиты в один список, а лестница добора (#879)
                # кладёт на поколение два отчёта, и второй часто повторяет
                # находки первого. Длина списка назвала бы человеку слой
                # вдвое толще настоящего — ровно то число, по которому он
                # решает, продолжать круг или нет. Категории считать
                # заново не нужно: ``repeated`` уже множество.
                arrived=len({uid for uid, _ in fresh}),
                repeated_categories=tuple(repeated),
            )
        )
    return ReviewCircle(laps=tuple(laps), threshold=config.REVIEW_CIRCLE_THRESHOLD)


async def name_the_circle(db: aiosqlite.Connection, task_id: int) -> bool:
    """Назвать круг в карточке и позвать человека. True — записали сейчас.

    Ничего не останавливает и остановить не может: статуса не трогает,
    диспетч не отменяет, пересдачу не запрещает. Всё, что здесь
    происходит, — запись в карточке и событие в ленте, потому что решение,
    где остановиться, принимает человек, а узнать о круге он обязан от
    хаба, а не из чьего-то пересказа.

    Три исхода названы явно и по именам. Сигнал о круге легко прочесть как
    разрешение перестать чинить настоящие дефекты, а это самый дорогой из
    возможных выводов: находки настоящие.
    """
    circle = await review_circle(db, task_id)
    if not circle.named:
        return False
    row = await repo.get_task(db, task_id)
    if row is None:  # pragma: no cover - зовут сразу после записи отчёта
        return False
    generation = int(dict(row).get("submission_generation") or 0)
    mark = CIRCLE_MARK.format(generation=generation)
    if await _already_named_the_circle(db, task_id, mark):
        return False
    repeated = circle.repeated_categories
    category_line = (
        (
            f" ОТДЕЛЬНО: новые находки повторяют категорию предыдущих "
            f"({', '.join(repeated)}) — это признак, что харнесс ходит по "
            f"одному и тому же месту, и он важнее числа заходов."
        )
        if repeated
        else ""
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        (
            f"Задача идёт по кругу {circle.count}-й раз: каждый заход "
            f"закрывал находки и получал новые. "
            + "; ".join(circle.breakdown())
            + f".{category_line} Ревью НЕ выключено и пересдача не запрещена — "
            "находки настоящие, и молча перестать их искать было бы хуже "
            "круга. Решает человек, и исходов три: принять как есть, "
            "отпустить оставшееся в отдельную задачу или продолжать этот "
            f"круг сознательно (порог REVIEW_CIRCLE_THRESHOLD={circle.threshold}, "
            f"#1235). {mark}"
        ),
    )
    await repo.insert_event(
        db,
        kind="review_circle_named",
        task_id=task_id,
        actor="hub",
        payload={
            "generation": generation,
            "laps": circle.count,
            "threshold": circle.threshold,
            "breakdown": [
                {
                    "generation": lap.generation,
                    "closed": lap.closed,
                    "arrived": lap.arrived,
                    "repeated_categories": list(lap.repeated_categories),
                }
                for lap in circle.laps
            ],
            "repeated_categories": list(repeated),
        },
    )
    await db.commit()
    log.info(
        "task #%s goes in circles: %s laps, generation %s, repeated categories %s",
        task_id,
        circle.count,
        generation,
        ", ".join(repeated) or "—",
    )
    return True


async def _already_named_the_circle(
    db: aiosqlite.Connection, task_id: int, mark: str
) -> bool:
    """Называли ли круг на ЭТОЙ сдаче.

    Лестница добора (#879) кладёт на одно поколение два отчёта, и второй
    заходов не прибавляет — а без дедупа добавил бы вторую одинаковую
    запись в карточку.
    """
    rows = await fetchall(
        db,
        "SELECT 1 FROM task_updates WHERE task_id=? AND kind='alert' "
        "AND content LIKE ? LIMIT 1",
        (task_id, f"%{mark}%"),
    )
    return bool(rows)
