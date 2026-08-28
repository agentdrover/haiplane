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

import json
import logging
from pathlib import PurePosixPath
from typing import Any

import aiosqlite

from hub.db import fetchall
from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.integrations.registry import plugins
from hub.models import RiskClass
from hub.services.model_family import family
from hub.services.project_policy import gate_policy_of, review_dispatch_enabled

log = logging.getLogger(__name__)

# Real ids from GET /v1/models (checked live 2026-08-20). Order = preference;
# the first whose family differs from the implementer's wins.
_REVIEW_MODEL_PREFERENCES: tuple[str, ...] = (
    "grok-4.6",
    "gpt-5.3-codex",
    "gemini-3.1-pro",
    "claude-sonnet-5",
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


def _review_prompt(
    task_id: int,
    branch: str,
    model_id: str,
    profile: str,
    rules_block: str,
    diff_block: str,
    prepass_block: str,
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
    # #805: one reader, and it answers "call a reviewer?" — not "who signs
    # the verdict?". Those were the same question only because they shared a
    # key, which forced the hub's own project to choose between no review
    # and no human.
    if not review_dispatch_enabled(gate_policy_of(project)):
        return False

    gh_repo = (dict(project).get("repo") or "").strip()
    reviewer_token = (config.CURSOR_REVIEWER_HUB_TOKEN or "").strip()
    if not cursor_cloud.is_configured() or not gh_repo or not reviewer_token:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Кросс-модельное ревью НЕ вызвано: не хватает конфигурации "
            "(ключ Cursor API, репозиторий проекта или ревьюер-токен). "
            "Вердикт остаётся человеку (#757).",
        )
        await db.commit()
        return False

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
    prepass_block = review_evidence.prepass_block(prepass)
    hub_mcp_url = f"{instance_base_url().rstrip('/')}/mcp"
    created = await cursor_cloud.create_review_agent(
        repo_url=f"https://github.com/{gh_repo}",
        starting_ref=branch,
        model_id=model_id,
        prompt_text=_review_prompt(
            task_id,
            branch,
            model_id,
            profile,
            rules_block,
            diff_block,
            prepass_block,
        ),
        hub_mcp_url=hub_mcp_url,
        reviewer_token=reviewer_token,
    )
    agent_info = (created or {}).get("agent") or {}
    run_info = (created or {}).get("run") or {}
    agent_id = agent_info.get("id") or ""
    if not agent_id:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Кросс-модельное ревью НЕ вызвано: Cloud Agents API не принял "
            "запрос (бета могла измениться). Вердикт остаётся человеку; "
            "детали в логе хаба (#757).",
        )
        await db.commit()
        return False

    # #1025: pin whose report this dispatch waits for, resolved from the
    # reviewer token at dispatch time. An unresolved token is logged and
    # falls back to the old task+generation match rather than blocking the
    # dispatch — degradation is this module's contract.
    expected_principal = await reviewer_principal_id(db)
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
        run_id=run_info.get("id") or agent_info.get("latestRunId") or "",
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
        f"{rules_note} (#873). Предмет ревью: {diff_note} (#874). "
        f"Предпас: {prepass.state}"
        + (f" ({', '.join(prepass.passed)})" if prepass.passed else "")
        + " (#875). "
        + "Отчёт придёт через "
        "hub_submit_machine_review от принципала cursor-cloud-reviewer "
        "(#757, #807).",
    )
    await repo.insert_event(
        db,
        kind="review_dispatched",
        task_id=task_id,
        actor="policy",
        payload={
            "model": model_id,
            "agent_id": agent_id,
            "run_id": run_info.get("id") or "",
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


def instance_base_url() -> str:
    from hub.hub_instance import instance_echo_fields

    return instance_echo_fields().get("base_url") or "https://agenthai.ru"


async def reviewer_principal_id(db: aiosqlite.Connection) -> int | None:
    """The principal behind CURSOR_REVIEWER_HUB_TOKEN, or None (#1025).

    The same hash lookup auth performs, without its side effects. None — an
    empty token, an env-map token, a rotated key — leaves the dispatch under
    the old task+generation matching rule rather than inventing an identity.
    """
    token = (config.CURSOR_REVIEWER_HUB_TOKEN or "").strip()
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
        await repo.set_review_dispatch_status(db, dispatch["id"], "failed")
        await db.commit()
