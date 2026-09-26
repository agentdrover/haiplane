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
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, NamedTuple

import aiosqlite

from hub.db import deserialize_str_list, fetchall
from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.integrations import local_reviewer
from hub.integrations import forge as forge_urls
from hub.integrations.registry import plugins
from hub.models import (
    INCOMPLETE_REASON_ENVIRONMENT,
    INCOMPLETE_REASON_PROFILE,
    RiskClass,
)
from hub.services import call_sites, project_policy
from hub.services.model_family import family
from hub.services.orchestration import ORIGINAL_READ_SQL
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


def pick_cascade_model(implementer_model: str, already_read: set[str]) -> str:
    """Модель второй оси каскада (#1243), или "" — если брать некого.

    Три фильтра, и каждый — ограничение постановки, а не вкус:
    имя должно быть в SUBSCRIPTION_LAUNCHABLE_MODELS (наблюдённая попытка
    создания, а не каталог — ровно на этом различении ошиблась #1237);
    семейство не совпадает с исполнителем (гейт монокультуры #758);
    модель ещё не читала эту сдачу — иначе это не «другая» модель, а повтор.
    """
    launchable = set(config.SUBSCRIPTION_LAUNCHABLE_MODELS)
    impl_family = family(implementer_model)
    for candidate in config.REVIEW_CASCADE_MODELS:
        if candidate not in launchable or candidate in already_read:
            continue
        if impl_family and family(candidate) == impl_family:
            continue
        return candidate
    return ""


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
    Taken from the generation the delta starts at (#1400): a generation with
    no recorded report has no findings, and the ones that matter are those
    of the last review that did land.
    """
    previous = await _last_read_submission(db, task_id, generation)
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


@dataclass(frozen=True)
class DeltaSubject:
    """What a resubmission puts in front of the reviewer, split by AUTHORSHIP.

    ``paths`` is what the review command narrows to and what the profile is
    judged on; ``base_paths`` arrived with the base branch and is NAMED to the
    reviewer rather than removed from sight. ``author_diff`` is the author's
    own patch series — empty when origin could not be established, and then
    the caller falls back to the whole submitted diff, loudly.
    """

    paths: list[str]
    base_paths: list[str]
    author_diff: str
    note: str


async def _last_read_submission(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> aiosqlite.Row | None:
    """The newest submission BEFORE this generation whose code was READ (#1400).

    "Read" is ``CODE_READ`` — the same rule the second-read guard uses: a
    complete, independent report. A generation whose report never landed
    (discarded as stale by #1260, crashed, still in flight), or whose only
    report is incomplete or the author's own, is skipped, so its changes stay
    in the subject instead of falling between two reviews nobody finished.
    """
    rows = await fetchall(
        db,
        "SELECT s.* FROM submissions s WHERE s.task_id=? AND s.generation<? "  # nosec B608 - константа модуля, не ввод
        "AND EXISTS (SELECT 1 FROM machine_reviews mr WHERE mr.task_id=s.task_id "
        f"AND mr.submission_generation=s.generation AND {CODE_READ}) "
        "ORDER BY s.generation DESC LIMIT 1",
        (task_id, generation),
    )
    return rows[0] if rows else None


async def _skipped_note(
    db: aiosqlite.Connection, task_id: int, base_generation: int, generation: int
) -> str:
    """Why the delta starts before N−1: name each skipped generation's cause.

    Two causes, told apart because "not recorded" would be a lie about a
    report that exists but does not count as reading the code.
    """
    gaps = list(range(base_generation + 1, generation))
    if not gaps:
        return ""
    rows = await fetchall(
        db,
        "SELECT DISTINCT submission_generation AS g FROM machine_reviews "
        "WHERE task_id=? AND submission_generation>? AND submission_generation<?",
        (task_id, base_generation, generation),
    )
    uncounted = {int(dict(r)["g"]) for r in rows}
    missing = [f"#{n}" for n in gaps if n not in uncounted]
    partial = [f"#{n}" for n in gaps if n in uncounted]
    parts = []
    if missing:
        parts.append(
            f"отчёт по {missing[0]} не записан"
            if len(missing) == 1
            else f"отчёты по {', '.join(missing)} не записаны"
        )
    if partial:
        parts.append(
            f"отчёт по {', '.join(partial)} неполный или самоотчёт — "
            "чтением кода не засчитан"
        )
    return "; ".join(parts)


async def _delta_base(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[dict | None, str]:
    """Where the delta may start: the newest submission whose code was read (#1400).

    Returns the base submission and, when it is not the previous generation,
    which generations were skipped and why; no base means the whole diff, and
    the string then says why. "Previous generation" is not enough: on #1378 the
    report on generation 2 was discarded as stale (#1260) when generation 3
    arrived, and a delta to #2 left generation 2's fixes read by nobody.
    """
    if await repo.previous_submission(db, task_id, generation) is None:
        return None, "предыдущая сдача не записана — читается весь дифф"
    reviewed = await _last_read_submission(db, task_id, generation)
    if reviewed is None:
        return None, (
            "ни по одному прежнему поколению полный независимый отчёт ревью "
            "не записан — читается весь дифф"
        )
    prev = dict(reviewed)
    return prev, await _skipped_note(
        db, task_id, int(prev.get("generation") or 0), generation
    )


async def generation_delta(
    db: aiosqlite.Connection, task: dict, base: str
) -> DeltaSubject:
    """What changed since the previous submission, and WHOSE change it is.

    ``paths`` empty means the whole diff is the subject, and ``note`` always
    says which of those it is and on what grounds.

    Three facts have to hold, and each is checked rather than assumed:

    1. the previous submission was recorded — before #880 nothing kept it —
       and the base is the newest one a recorded REPORT covered (#1400), not
       simply generation N−1: an unreviewed generation stays in the delta;
    2. its commit is an ANCESTOR of the current one. That is the rebase and
       force-push test: after either, "what changed since last time" compares
       commits that no longer share a history;
    3. the base branch has not moved. A project that repointed its default
       branch is asking a different question about the same two commits.

    Anything unproven means the full diff. Reviewing a delta we cannot justify
    would be the one failure this feature must not have — silently reading less
    than the report claims.

    #1249 adds a fourth fact, and it is about AUTHORSHIP rather than about
    trust. The previous submission is an ancestor of the current tip, so a
    plain ``prev..current`` diff also carries whatever the author pulled in by
    merging the base branch — on #1172 generation 2 that was 23 of 25 files,
    written by other tasks that had already passed the gate. The subject is
    therefore split: the author's own commits (those the base branch does not
    already contain) decide what the command narrows to and what the profile
    is bought for, and the files that came with the base are named beside
    them. Named, not dropped: #1238 found a defect that exists only where two
    branches meet, and a reviewer blind to a moved base could not have seen
    it. Origin that cannot be established is not origin "base" — it reads
    everything, and says so.
    """
    task_id = int(task.get("id") or 0)
    generation = int(task.get("submission_generation") or 0)
    current = (task.get("submission_sha") or "").strip()
    if generation <= 1 or not current:
        return DeltaSubject([], [], "", "первая сдача — предмет ревью весь дифф")

    prev, skipped = await _delta_base(db, task_id, generation)
    if prev is None:
        return DeltaSubject([], [], "", skipped)
    prev_sha = (prev.get("sha") or "").strip()
    if not prev_sha:
        return DeltaSubject(
            [], [], "", "у предыдущей сдачи не закреплён коммит — читается весь дифф"
        )
    if (prev.get("base_branch") or "") != base:
        return DeltaSubject(
            [],
            [],
            "",
            f"базовая ветка сменилась ({prev.get('base_branch') or '—'} → {base}) "
            "— дельта невалидна, читается весь дифф",
        )

    ctx = await _git_context(db, task_id)
    if ctx is None:
        return DeltaSubject([], [], "", "воркспейс недоступен — читается весь дифф")
    workspace, _ = ctx
    try:
        ancestor = await plugins.git_ops.is_ancestor(workspace, prev_sha, current)
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("ancestry check failed for task #%s: %s", task_id, exc)
        ancestor = None
    if ancestor is None:
        return DeltaSubject(
            [], [], "", "историю проверить не удалось — читается весь дифф"
        )
    if not ancestor:
        return DeltaSubject(
            [],
            [],
            "",
            f"коммит {prev_sha[:12]} не предок текущего — ветку перебазировали "
            "или переписали, читается весь дифф",
        )

    try:
        delta = await plugins.git_ops.branch_diff(workspace, prev_sha, current)
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("delta diff failed for task #%s: %s", task_id, exc)
        delta = None
    if delta is None:
        return DeltaSubject(
            [], [], "", "дельту прочитать не удалось — читается весь дифф"
        )
    paths = [p for p in changed_paths(delta) if not is_generated(p)]
    if not paths:
        return DeltaSubject(
            [],
            [],
            "",
            f"с поколения #{prev.get('generation')} код не менялся — читается весь дифф",
        )
    head = f"дельта к поколению #{prev.get('generation')} ({prev_sha[:12]})"
    if skipped:
        head += (
            f", {skipped} — правки после #{prev.get('generation')} в предмете (#1400)"
        )
    return await _split_by_origin(
        task_id, workspace, base, prev_sha, current, paths, head
    )


async def _split_by_origin(
    task_id: int,
    workspace: str,
    base: str,
    prev_sha: str,
    current: str,
    paths: list[str],
    head: str,
) -> DeltaSubject:
    """Whose change is which, inside a delta already proven trustworthy (#1249).

    Asked of git by REACHABILITY from the base branch — never guessed from a
    commit message or an author name, because a merge commit says "Merge ..."
    whoever wrote the code inside it. Every answer git cannot give leaves the
    WHOLE delta as the subject with the cause named; none of them narrows it.
    """
    try:
        own = await plugins.git_ops.delta_without_base(
            workspace, base, prev_sha, current
        )
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("origin split failed for task #%s: %s", task_id, exc)
        own = None
    if own is None:
        return DeltaSubject(
            paths,
            [],
            "",
            f"{head}: {len(paths)} файл(ов), происхождение коммитов установить "
            "не удалось — привезённое базой не отделено, читается вся дельта",
        )
    author_set = {p for p in changed_paths(own) if not is_generated(p)}
    author = [p for p in paths if p in author_set]
    brought = [p for p in paths if p not in author_set]
    if not author:
        # The round added nothing of the author's own — a bare merge of the
        # base. Narrowing to zero files would hand the reviewer an empty
        # command, so the whole delta stays the subject and says why.
        return DeltaSubject(
            paths,
            [],
            "",
            f"{head}: {len(paths)} файл(ов), все привезены базой — своей правки "
            "в этом круге нет, читается вся дельта",
        )
    if not brought:
        # Сказать здесь «база не двигалась» значило бы заявить больше, чем
        # спрошено (находки 6ea44f6f, 21ad18b5). Спрошена ДОСТИЖИМОСТЬ, и
        # пустой ``brought`` получается ещё двумя способами: автор влил базу
        # сжатием — коммиты базы тогда не предки по хешу и целиком считаются
        # его работой (#1240); или тронул РОВНО те же файлы, что приехали, и
        # разбор по путям отдал их автору целиком. Оба промаха в безопасную
        # сторону — предмет шире, профиль дороже, — но карточка обязана
        # называть измеренное, а не вывод из него.
        return DeltaSubject(
            author,
            [],
            own,
            f"{head}: {len(author)} файл(ов) автора, файлов только из базы нет "
            "(база, влитая сжатием, по хешу не отличима и считается правкой "
            "автора, #1240)",
        )
    return DeltaSubject(
        author,
        brought,
        own,
        f"{head}: {len(author)} файл(ов) автора и {len(brought)} привезено базой",
    )


def diff_plan(
    diff: str | None,
    base: str,
    branch: str,
    delta_paths: list[str] | None = None,
    delta_note: str = "",
    prior_findings: list[str] | None = None,
    base_paths: list[str] | None = None,
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

    ``base_paths`` are the files that arrived with the base branch rather than
    from the author (#1249). They are NAMED, not removed: a defect can exist
    only where two branches meet — #1238 was exactly that — and a reviewer who
    never learns the base moved cannot look for one.
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
        if base_paths:
            named = ", ".join(base_paths[:30])
            more = "" if len(base_paths) <= 30 else f" и ещё {len(base_paths) - 30}"
            lines.append(
                f"ПРИВЕЗЕНО БАЗОЙ, НЕ АВТОРОМ: {len(base_paths)} файл(ов) — "
                f"{named}{more}. Это чужая работа: она уже прошла свой гейт, "
                "и находки по ней адресованы не этому автору. Но база "
                "СДВИНУЛАСЬ под правкой, а дефект бывает только на стыке двух "
                "веток — если увидишь, что привезённое ломается о правку "
                "автора, это находка, и назови её именно так."
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


# Which risks buy the expensive harness (#827, #1415). The catalogue mixes
# two different things: some risks are about the code and its behaviour,
# others about the statement and the product. A multi-agent code review
# answers the first kind and cannot answer the second.
#
# Measured on the first live dispatch (#818, 21.08.2026): the run went deep
# because the task honestly declared a high risk reading "the daily message
# turns into noise and devalues the bot". Dogfooding answers that; reading
# the diff does not. We paid for a harness that had nothing to say.
#
# #1415 narrowed it further, to security alone. Measured 23.09.2026 over 328
# reports: deep bought by a DECLARED risk was the weakest reason of all — 0.54
# confirmed findings per report against 1.02 for lite. Priced from the
# Cursor export of 25.09.2026, deep costs ≈4M tokens a run (10-14M when it
# takes several passes) and lite ≈1.7M. From 13 to 25.09 declared risk bought
# deep 36 times: security 14, technical high 13, high of an unrecognised kind
# 9 — the last 22 now go by the class and the process surfaces like any other
# submission. The technical/product split of #827 went with them: with
# severity out of the rule there is nothing left for it to sort. The revisit
# condition stays on the task: an escape (#528) after lite on a former
# "technical high" brings that kind back.


def _declares_security(risks: Any) -> bool:
    """Does any declared risk have ``kind=security``, at whatever severity?"""
    return isinstance(risks, list) and any(
        isinstance(r, dict) and str(r.get("kind") or "").strip() == "security"
        for r in risks
    )


def _risk_profile_reason(risks: Any) -> str | None:
    """Why the declared risks buy deep, or None when they do not (#827, #1415).

    One rule: ``kind=security`` buys deep at ANY severity — unchanged from
    #807, because a security risk somebody rated 'low' is still a security
    risk.

    A ``high`` severity no longer buys deep by itself, whether the kind is
    technical, product or unrecognised (#1415, measured 23.09: 0.54 confirmed
    per report, the weakest reason). That is not "unknown is cheap" (#582):
    the class and the process surfaces still read the actual change, and an
    uncomputed class still buys deep. What goes is paying ≈4M tokens for a
    word the author typed into the statement.
    """
    return "заявлен риск security" if _declares_security(risks) else None


# What counts as documentation (#1415). An explicit list, not "anything that
# is not code": a file the hub or an agent EXECUTES or LOADS as instructions
# is behaviour even when it is Markdown — task templates under hub/, test
# fixtures, workflow and PR templates, skills and agent prompts. Those keep
# the old rule; so does any path this list does not name.
#
# Two choices on purpose, both erring toward the old rule (a false deep costs
# tokens, a false lite costs a missed defect): a denied directory name matches
# at ANY depth, so a nested ``pkg/templates/x.md`` stays behaviour even though
# ``docs/hub/x.md`` then stays deep too; and ``.html`` is not documentation,
# because a page under docs/ can carry script.
_DOC_SUFFIXES = (".md", ".markdown", ".rst", ".adoc")
_NOT_DOC_DIRS = frozenset(
    {
        "hub",
        "tests",
        "test",
        "scripts",
        "skills",
        "agents",
        "templates",
        "cli_templates",
        "fixtures",
        "prompts",
    }
)
_NOT_DOC_NAMES = frozenset({"AGENTS.md", "CLAUDE.md"})


def is_documentation(path: str) -> bool:
    """Is this repository path documentation by the explicit list above?"""
    parts = path.split("/")
    if not parts[-1].endswith(_DOC_SUFFIXES) or parts[-1] in _NOT_DOC_NAMES:
        return False
    # Hidden directories (.github, .claude, .hub) carry configuration and
    # instructions for tools, not prose for readers.
    return not any(d.startswith(".") or d in _NOT_DOC_DIRS for d in parts[:-1])


_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13}


def _unquote_path(raw: str) -> str | None:
    """A path as git prints it in a header, or None when it cannot be read.

    With ``core.quotepath`` (the default) a name with non-ASCII bytes, quotes
    or control characters comes C-quoted: ``"a/docs/\\320\\267.md"``.
    Undecodable means unknown, never documentation.
    """
    raw = raw.strip()
    if not raw.startswith('"'):
        return raw
    if len(raw) < 2 or not raw.endswith('"'):
        return None
    body, out, i = raw[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out += ch.encode("utf-8")
            i += 1
        elif body[i + 1 : i + 4].isdigit() and len(body[i + 1 : i + 4]) == 3:
            out.append(int(body[i + 1 : i + 4], 8) & 0xFF)
            i += 4
        elif i + 1 < len(body):
            nxt = body[i + 1]
            out.append(_C_ESCAPES.get(nxt, ord(nxt) if nxt.isascii() else 0))
            i += 2
        else:
            return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _git_line_paths(rest: str) -> list[str | None]:
    """The two sides of ``diff --git <a> <b>``, quoted or not."""
    if rest.startswith('"'):
        end = rest.find('" ', 1)
        if end < 0:
            return [None]
        return [_unquote_path(rest[: end + 1]), _unquote_path(rest[end + 2 :])]
    left, sep, right = rest.partition(" b/")
    if not sep or not left.startswith("a/"):
        return [None]
    return [left, "b/" + right]


def _header_paths(line: str) -> list[str | None] | None:
    """Paths a FILE HEADER line names; None when the line is not a header.

    ``None`` inside the list means "a header whose path could not be read".
    """
    if line.startswith("diff --git "):
        found = _git_line_paths(line[len("diff --git ") :])
    elif line.startswith(("diff --cc ", "diff --combined ")):
        # A merge's combined diff (#1249 feeds one on resubmission): a binary
        # resolution is named ONLY here, with no ---/+++ after it (7efb59b1).
        found = [_unquote_path(line.split(" ", 2)[2])]
    elif line.startswith(("--- ", "+++ ")):
        found = [_unquote_path(line[4:])]
    elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
        found = [_unquote_path(line.split(" ", 2)[2])]
    else:
        return None
    return [
        None if p is None else p.removeprefix("a/").removeprefix("b/") for p in found
    ]


def _diff_touched_paths(diff: str) -> list[str | None]:
    """Every path the diff touches — deleted and renamed-away ones included.

    :func:`changed_paths` skips ``+++ /dev/null`` on purpose, which is right
    for rules lookup and wrong here: deleting ``hub/x.py`` next to a README
    edit is a code change, and so is renaming code into ``docs/``.

    Headers are read only OUTSIDE a hunk: after ``@@`` a line starting with
    ``---`` is a deleted line of text (44f0ab2a), until the next ``diff``
    line opens another file. A ``diff`` line that names no readable path
    yields ``None`` — unknown, which the caller must never read as prose.
    """
    paths: list[str | None] = []
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff "):
            in_hunk = False
            found = _header_paths(line)
            paths.extend(found if found is not None else [None])
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk:
            continue
        found = _header_paths(line)
        if found:
            paths.extend(p for p in found if p != "/dev/null")
    return list(dict.fromkeys(paths))


def is_docs_only_diff(diff: str) -> bool:
    """Is every path this diff touches documentation? An empty diff is not.

    Conservative by construction: a path that could not be read counts as
    not documentation, so the old rule decides.
    """
    paths = _diff_touched_paths(diff)
    return bool(paths) and all(p is not None and is_documentation(p) for p in paths)


# Маленькая пересдача (#1416). 13-25.09: 73 из 125 deep — пересдачи, и на
# пересдаче deep даёт подтверждённых почти столько же, сколько lite (0,48
# против 0,43 на отчёт). Фиксированная цена харнесса (~4M токенов, 10-14M
# многопрогонный) при паре правленых строк не окупается. Большие пересдачи
# остаются на deep: unresolved-находок он там даёт в 5 раз больше.
SMALL_DELTA_KEY = "small_delta_lines"
#: Слова причины; по ним сводка ревью находит такие пересдачи.
SMALL_DELTA_REASON_MARK = "маленькая пересдача"


def small_delta_lines_of(policy: dict[str, Any]) -> int | None:
    """Порог маленькой пересдачи в строках; None — правило выключено.

    Ключ проекта главнее REVIEW_SMALL_DELTA_LINES. Ноль выключает. Нечитаемое
    значение — тоже: ошибка в настройке не должна удешевлять ревью молча.
    """
    raw: Any = None
    if isinstance(policy, dict) and SMALL_DELTA_KEY in policy:
        raw = _cap_value(policy[SMALL_DELTA_KEY])
    if raw is None:
        text = str(config.REVIEW_SMALL_DELTA_LINES or "").strip()
        raw = int(text) if text.isdigit() else None
    return raw or None


def author_delta_lines(diff: str) -> int:
    """Изменённые строки авторской дельты: добавленные плюс удалённые (#1416).

    Заголовки файлов читаются только ВНЕ ханка (строка «---» внутри ханка —
    удалённый текст). Комбинированный дифф слияния (``@@@``, #1249 подаёт
    его на пересдаче) несёт по колонке на родителя, и строка считается, если
    хоть в одной колонке правка. Сгенерированные файлы не считаются, как и
    везде при выборе профиля (#874). Серия коммитов считается как есть:
    правка и её откат дают две строки — перебор в сторону deep.
    """
    count, width, in_hunk, skip = 0, 1, False, False
    for line in diff.splitlines():
        if line.startswith("diff "):
            in_hunk = False
            skip = _all_generated(_header_paths(line))
        elif line.startswith("@@"):
            in_hunk = True
            width = max(len(line) - len(line.lstrip("@")) - 1, 1)
        elif not in_hunk:
            if line.startswith("+++ "):
                # «+++ /dev/null» у удалённого файла имени не несёт — решает
                # строка «diff» над ним.
                if line[4:].strip() != "/dev/null":
                    skip = _all_generated(_header_paths(line))
                # Двойники и обрезанные диффы бывают без «@@»: строки
                # после «+++» — уже содержимое файла.
                in_hunk, width = True, 1
        elif not skip and any(c in "+-" for c in line[:width]):
            count += 1
    return count


def _all_generated(paths: list[str | None] | None) -> bool:
    """Все названные пути — сгенерированные; нечитаемый путь — не такой."""
    found = [p for p in paths or [] if p != "/dev/null"]
    return bool(found) and all(p is not None and is_generated(p) for p in found)


def pick_review_profile(
    task: dict[str, Any],
    diff: str | None = None,
    small_delta: tuple[int, int] | None = None,
) -> tuple[str, list[str]]:
    """Which review profile this submission deserves, and why (#807, #820).

    Returns ``(profile, reasons)``. The reasons exist because "why was this
    reviewed cheaply" and "why did this cost a full harness run" are both
    questions somebody asks later, and a bare profile name answers neither.

    deep is the multi-agent harness; lite is a single pass over the branch
    diff under a token ceiling. The rule leans toward deep on every kind of
    ignorance:

    * a human who pressed "request machine review" asked for the real thing;
    * a security risk is exactly what the expensive harness is for — a
      declared high of any other kind no longer is (#1415, see
      :func:`_risk_profile_reason`);
    * an UNCOMPUTED risk class is not a low one (#582) — an unknown path is
      not cheaper than a known-harmless one, and treating a missing class as
      lite would let any task skip the harness by never being classified.

    Everything else is ordinary work inside known contracts, and paying
    434k tokens per confirmed finding for it is what made "review every
    submission" unaffordable in the first place.

    A diff made of documentation alone (#1415, :func:`is_documentation`) is
    lite whatever the class, unless a human asked or security was declared:
    there is no executable code for the harness to run against, and spike
    #1402 bought deep order 430 for one document at ≈4M tokens against ≈1.7M
    for lite (Cursor export, 25.09.2026). The caller passes the AUTHOR's part
    of the diff (#1249), so documents a base merge brought in cannot hide the
    author's code, nor can the author's document ride on the base's.

    ``small_delta`` is ``(lines, limit)`` of a resubmission whose delta was
    PROVEN and whose author's part was separated (#1416); the caller passes
    None otherwise. When the author's lines fit the limit, deep bought by the
    rest of the rule becomes lite, and the cancelled reason stays named. A
    human request and a security risk keep deep at any size: they are checked
    first. The ladder top-up and the cascade come with ``force_profile`` and
    never reach this function.
    """
    if (task.get("machine_review_override") or "").strip() == "require":
        return DEEP, ["ревью запрошено человеком"]

    # #820: the diff decides before the class does. A missing diff is not a
    # harmless one — the same rule the ladder uses for "class not computed".
    if diff is None:
        return DEEP, ["дифф сдачи прочитать не удалось"]
    try:
        risks = json.loads(task.get("risks") or "[]")
    except ValueError:
        risks = []
    profile, reasons = _profile_by_rule(task, diff, risks)
    if small_delta is None or profile != DEEP or _declares_security(risks):
        return profile, reasons
    lines, limit = small_delta
    if lines > limit:
        return profile, reasons
    return LITE, [
        f"{SMALL_DELTA_REASON_MARK}: {lines} ≤ {limit} строк авторской дельты",
        *(f"отменён повод deep: {r}" for r in reasons),
    ]


def _profile_by_rule(
    task: dict[str, Any], diff: str, risks: Any
) -> tuple[str, list[str]]:
    """The rule after the human request: documents, surfaces, risk, class."""
    if is_docs_only_diff(diff) and not _declares_security(risks):
        return LITE, ["дифф только из документации"]
    surfaces = process_surface_reasons(diff)
    if surfaces:
        return DEEP, [f"процессная поверхность — {r}" for r in surfaces]
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


# Суточный потолок deep на проект (#1414). Правило выше решает, заслуживает
# ли сдача харнесс; потолок решает, может ли проект его сегодня купить. Рычаги
# «правило профиля» срезали 8-15% и ничего не гарантировали при всплеске
# сдач: прод, облачный канал — 21, 40 и 29 deep за 22-24.09.
DEEP_DAILY_CAP_KEY = "deep_daily_cap"
#: Слова причины понижения; по ним сводка ревью находит такие сдачи.
DEEP_CAP_REASON_MARK = "суточный потолок"


def _cap_value(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def deep_daily_cap_of(policy: dict[str, Any]) -> int | None:
    """Потолок deep в сутки для проекта; None — потолка нет.

    Ключ проекта главнее глобального REVIEW_DEEP_DAILY_CAP. Нечитаемое
    значение — не потолок: запись такое не пропускает, а положенное мимо API
    не должно молча перевести проект на lite.
    """
    if isinstance(policy, dict) and DEEP_DAILY_CAP_KEY in policy:
        cap = _cap_value(policy[DEEP_DAILY_CAP_KEY])
        if cap is not None:
            return cap
    raw = str(config.REVIEW_DEEP_DAILY_CAP or "").strip()
    return _cap_value(int(raw)) if raw.isdigit() else None


def _deep_cap_exempt(task: dict[str, Any]) -> bool:
    """Ручной запрос человека и риск security потолком не режутся (#1414)."""
    if (task.get("machine_review_override") or "").strip() == "require":
        return True
    try:
        risks = json.loads(task.get("risks") or "[]")
    except ValueError:
        return False
    return _declares_security(risks)


async def deep_cap_exhausted(
    db: aiosqlite.Connection, task: dict[str, Any], generation: int
) -> int | None:
    """Потолок, если облачный deep этой сдаче сегодня не положен; иначе None.

    Положен — значит место под потолком забронировано атомарно
    (repo.claim_deep_seat): два параллельных заказа вместе потолок не
    превышают. Сдача, уже занявшая место, второго не занимает. Исключения —
    ручной запрос и риск security (#1414).
    """
    if _deep_cap_exempt(task):
        return None
    task_id = int(task["id"])
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return None
    cap = deep_daily_cap_of(gate_policy_of(project))
    if cap is None:
        return None
    if await repo.claim_deep_seat(db, int(project["id"]), task_id, generation, cap):
        return None
    return cap


async def apply_deep_daily_cap(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    generation: int,
    profile: str,
    reasons: list[str],
) -> tuple[str, list[str]]:
    """Deep по правилу сверх суточного потолка становится lite с причиной."""
    if profile != DEEP:
        return profile, reasons
    cap = await deep_cap_exhausted(db, task, generation)
    if cap is None:
        return profile, reasons
    why = "; ".join(reasons) or "причина не названа"
    return LITE, [
        f"deep по правилу ({why}), но {DEEP_CAP_REASON_MARK} {cap} исчерпан — lite"
    ]


async def _forced_deep_over_cap(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    generation: int,
    force_profile: str,
    force_model: str,
) -> bool:
    """Принудительный облачный deep сверх потолка не покупается (#1414).

    Добор лестницы (#879) и вторая ось (#1243) приходят с force_profile=deep
    и мимо правила профиля — значит, и мимо понижения. Понизить их некуда:
    добор lite после неполного lite — тот же прогон второй раз. Поэтому
    отказ с названной причиной; лестница после него не повторяется —
    вызывающий уже пишет, что решение за человеком.
    """
    if force_profile != DEEP:
        return False
    cap = await deep_cap_exhausted(db, task, generation)
    if cap is None:
        return False
    what = "переспрос другой моделью" if force_model else "добор"
    await repo.add_task_update(
        db,
        int(task["id"]),
        "hub",
        "alert",
        f"Облачный deep для этой сдачи не заказан: {DEEP_CAP_REASON_MARK} deep "
        f"исчерпан — {what} не куплен (потолок {cap} в сутки на проект, #1414).",
    )
    await db.commit()
    return True


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
    '"lost_dimensions": ["..."], "incomplete_reason": "environment|profile|", '
    '"harness_skill": "...", '
    '"tokens_spent": <число или null>, "model": "<твоя модель>"}\n'
    "```\n"
    "Правила блока: он ОДИН и он последний; incomplete обязателен и без "
    "дефолта — «0 подтверждённых» без него не значит ничего; находка, которую "
    "никто не смог рассудить, идёт в unresolved, а НЕ в findings_rejected. "
    "При incomplete=true назови ПРИЧИНУ одним словом в incomplete_reason: "
    "«environment» — смотреть было нечем (нет чем запустить проверки, не "
    "разрешается база для сравнения), «profile» — инструменты были, а охвата "
    "на объём диффа не хватило. Это РАЗНЫЕ ответы человека: первое лечится "
    "настройкой окружения, второе — ещё одним прогоном. Не заявишь — причина "
    "останется неизвестной, и угадывать её по твоему тексту никто не будет. "
    "Если инструменты хаба недоступны — этот блок единственный способ "
    "доставить работу, и без него прогон пропадёт целиком."
)


# #1238. Что делать ДО того, как объявить, что среда не дала измерить.
#
# Заведено по отчёту #334 (задача #1169, профиль deep, 12 агентов): прогон
# честно записал «в среде нет uv/pytest — сюит не исполнялся, только чтение»
# и «локальный ref develop не резолвится». Ни того, ни другого никто не
# просил попробовать обойти, и оба измерения пропали при полной оплате.
#
# Обещаний про среду провайдера здесь нет — только порядок попыток и
# требование назвать отказ отказом, если ни одна не сработала. Сработает ли
# он, покажет счётчик отказов среды, а не этот комментарий.
_ENVIRONMENT_ATTEMPT_HEAD = (
    "ЧЕМ СМОТРЕТЬ — СНАЧАЛА ПОПРОБУЙ, ПОТОМ ЗАЯВЛЯЙ ОТКАЗ.\n"
    "Тесты: `uv run pytest -q`; нет uv — `python -m pytest -q`; нет pytest — "
    "`python -m pip install -q pytest` и повтори. Код возврата смотри "
    "отдельным `echo $?`, а не по хвосту вывода.\n"
    "База для сравнения: если ссылка на базовую ветку не разрешается, "
    "`git fetch origin <база>` и сравнивай с `origin/<база>`. Взять ДРУГОЙ "
    "диапазон — значит судить о другом наборе изменений; если пришлось, "
    "скажи об этом прямо.\n"
)

# #1357. Ревью PR haiplane#442 (#1336) сдало холодный старт, healthz и базу
# без демо-сида как «no Docker daemon»: демона в снимке среды нет, а блок выше
# учил ставить только pytest. Проба в облаке Cursor 24.09: sudo без пароля,
# docker.io и docker-compose-v2 ставятся из apt, dockerd отвечает за секунду
# без обходов. И ловушка той же пробы: в среде уже крутился хаб на 8080,
# контейнер упал на bind, а curl /healthz ответил «ok» — от хостового
# процесса. Отсюда правило про состояние контейнера и переназначение порта.
#
# Абзац идёт только задачам, чьи проверки сами про контейнер: обычному
# Python-диффу установка демона — минуты оплаченного прогона ни за что.
_ENVIRONMENT_CONTAINER_ATTEMPT = (
    "Контейнер: проверки этой задачи требуют Docker. Нет команды `docker` "
    "или `docker info` не отвечает — это ещё не отказ среды: подними демон "
    "во временной машине, файлы репозитория не трогай. На Ubuntu: "
    "`sudo apt-get update && sudo apt-get install -y docker.io "
    "docker-compose-v2`, затем `sudo dockerd > /tmp/dockerd.log 2>&1 &` и "
    "жди, пока `sudo docker info` не ответит; не встал — один повтор с "
    "`--iptables=false --bridge=none`. Дальше выполняй команды задачи через "
    "sudo.\n"
    "Порт может быть занят процессом самой машины, и тогда на запрос "
    "отвечает он, а не контейнер. Ответ засчитывай, только когда "
    "`sudo docker compose ps` показывает контейнер в состоянии running; "
    "занятый порт переназначь отдельным override-файлом вне репозитория "
    '(`ports: !override ["<свободный>:<порт>"]`).\n'
    "Не вышло — назови в lost_dimensions шаг, на котором остановилось: нет "
    "sudo, пакет не встал, dockerd не стартовал, контейнер не поднялся.\n"
)

_ENVIRONMENT_ATTEMPT_TAIL = (
    "Если после попыток измерение так и не сделано — incomplete=true и "
    'incomplete_reason="environment", а в lost_dimensions перечисли, чего '
    "именно не хватило. Чтение кода вместо прогона тестов прогоном не "
    "называется.\n\n"
)

ENVIRONMENT_ATTEMPT_BLOCK = _ENVIRONMENT_ATTEMPT_HEAD + _ENVIRONMENT_ATTEMPT_TAIL

# Команда, а не слово: «Docker» в чеклисте может описывать саму правку промпта.
# У docker-compose после имени — пробел или конец: `\b` пропускал имя файла
# docker-compose.yml и пакет docker-compose-v2 (ревью #1357, поколение 1).
_CONTAINER_COMMAND = re.compile(
    r"\bdocker(?:-compose(?=\s|$)|\s+(?:compose|build|buildx|run|info)\b)",
    re.IGNORECASE,
)


def task_needs_container(task: dict[str, Any]) -> bool:
    """Требуют ли проверки задачи Docker: validation_commands или чеклист (#1357)."""
    for field in ("validation_commands", "review_checklist"):
        raw = task.get(field)
        items = raw if isinstance(raw, list) else deserialize_str_list(raw)
        if any(_CONTAINER_COMMAND.search(str(item)) for item in items):
            return True
    return False


def environment_attempt_block(needs_container: bool) -> str:
    """Порядок попыток до «среда отказала»; абзац про Docker — по требованию."""
    middle = _ENVIRONMENT_CONTAINER_ATTEMPT if needs_container else ""
    return _ENVIRONMENT_ATTEMPT_HEAD + middle + _ENVIRONMENT_ATTEMPT_TAIL


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
    only_tests_block: str = "",
    needs_container: bool = False,
) -> str:
    common = (
        f"Ты — независимый код-ревьюер задачи #{task_id} хаба Haiplane "
        f"(ветка {branch}). Строгие правила: НИЧЕГО не коммить, не пушить и "
        "не менять в файлах репозитория — только читать код и запускать "
        # #1357: «не менять» читалось как запрет трогать машину, хотя блок
        # попыток #1238 и так велит ставить pytest. Граница — репозиторий.
        "проверки. Поставить во временную машину недостающий инструмент — "
        "не правка репозитория, а часть проверки.\n\n"
        # The rules travel with BOTH profiles: the expensive harness has no
        # more knowledge of this repository's history than the cheap pass.
        f"{rules_block}\n\n"
        # So does the diff plan (#874): the deep harness reads the same branch
        # and has the same reason not to spend its passes on lock files.
        f"{diff_block}\n\n"
        # And the prepass (#875): both profiles pay model prices for what a
        # linter already proved, and the expensive one pays them per pass.
        f"{prepass_block}\n\n"
        # #1254: symbols only tests call, as a question with two answers.
        # Empty adds nothing, so an order without candidates is unchanged.
        + only_tests_block
        # #1238: and the order of attempts before "the environment refused".
        # Both profiles get it: the deep harness lost the test dimension on
        # exactly the same missing tool as a cheap one would.
        + environment_attempt_block(needs_container)
        # #1036: the report has to survive a run with no MCP. Since 22.08 the
        # hub's MCP stopped reaching cloud runs at all — the reviewer works,
        # finishes, and its findings die in the final text nobody parses. So
        # the text becomes a second, weaker delivery: same fields, stated
        # once, at the very end, where a machine can find them.
        + f"{delivery_block}"
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


#: Окно, за которое считается повторяемость отказов среды (#1238).
ENVIRONMENT_REFUSAL_WINDOW_DAYS = 30


def is_environment_refusal(report: Mapping[str, Any] | None) -> bool:
    """Отчёт неполон ПОТОМУ ЧТО среда отказала — по заявлению ревьюера (#1238).

    ЕДИНСТВЕННОЕ место, где это решается. Два условия, и оба обязательны:
    прогон объявил себя неполным И назвал причиной ``environment``. Пустая
    причина — «не заявлена»; так выглядят все отчёты, написанные до
    появления поля, и засчитать их отказом среды задним числом значило бы
    сочинить за них показание.

    Ничего не выводится из ``lost_dimensions``: разбор прозы по подстрокам
    и есть то угадывание, ради замены которого поле заведено.
    """
    if report is None:
        return False
    if not report.get("incomplete"):
        return False
    return (report.get("incomplete_reason") or "") == INCOMPLETE_REASON_ENVIRONMENT


#: Хвост алерта лестницы, когда неполнота — отказ среды (#1238).
#:
#: Лестница (#879) от этого не меняется: она и до, и после решает по профилю
#: и по потолку — перечень её исходов вне области этой задачи. Меняется
#: только то, ЧТО человек прочитает в карточке, когда за отказ примутся.
ENVIRONMENT_REFUSAL_NOTE = (
    " ПРИЧИНА — ОТКАЗ СРЕДЫ по заявлению ревьюера: смотреть было нечем "
    "(нечем запустить проверки или не с чем сравнить дифф). Повтор прогона "
    "в той же среде даст тот же отказ — лечится настройкой окружения "
    "ревьюера, а не ещё одним ревью (#1238)."
)


def _ladder_cause_note(report: Mapping[str, Any] | None) -> str:
    """Что дописать в алерт лестницы про ПРИЧИНУ неполноты (#1238)."""
    if is_environment_refusal(report):
        return ENVIRONMENT_REFUSAL_NOTE
    return ""


async def count_environment_refusals(
    db: aiosqlite.Connection,
    since_days: int = ENVIRONMENT_REFUSAL_WINDOW_DAYS,
) -> dict[str, Any]:
    """Сколько отчётов за окно неполны по отказу среды — с размером выборки.

    Повторяемость до этой задачи не считал никто: два случая за 09.09.2026
    заметил человек, читавший карточки подряд, а не механизм. Считается
    здесь, рядом с определением отказа, чтобы счёт и признак не разъехались.

    Доля печатается ТОЛЬКО когда есть от чего её брать. Знаменатель — не
    все неполные отчёты, а те из них, что назвали причину: пока причину не
    назвал никто, «0 отказов среды из 48 неполных» читалось бы как «со
    средой всё хорошо», хотя измерено ровно ничего. Такой случай называется
    словом «недобор» (#1153), а не нулём.
    """
    rows = await fetchall(
        db,
        "SELECT incomplete, incomplete_reason FROM machine_reviews "
        "WHERE created_at >= datetime('now', ?)",
        (f"-{int(since_days)} days",),
    )
    reports_total = len(rows)
    incomplete_rows = [dict(row) for row in rows if row["incomplete"]]
    environment = sum(1 for row in incomplete_rows if is_environment_refusal(row))
    profile_exhausted = sum(
        1
        for row in incomplete_rows
        if (row.get("incomplete_reason") or "") == INCOMPLETE_REASON_PROFILE
    )
    declared = environment + profile_exhausted
    incomplete_total = len(incomplete_rows)
    if declared:
        share_note = (
            f"отказ среды: {environment} из {declared} неполных отчётов, "
            f"назвавших причину (неполных всего {incomplete_total} из "
            f"{reports_total} отчётов за {since_days} дн.)"
        )
    else:
        share_note = (
            f"недобор: причину неполноты не назвал ни один отчёт (неполных "
            f"{incomplete_total} из {reports_total} за {since_days} дн.), "
            "поэтому доля отказов среды не измерена — это не ноль"
        )
    return {
        "since_days": int(since_days),
        "reports_total": reports_total,
        "incomplete_total": incomplete_total,
        "reason_declared": declared,
        "reason_unstated": incomplete_total - declared,
        "environment_refusals": environment,
        "profile_exhausted": profile_exhausted,
        # Доля есть только при непустом знаменателе; иначе её нет, и вместо
        # числа стоит None рядом со словом «недобор» в share_note.
        "environment_share": (round(environment / declared, 3) if declared else None),
        "share_note": share_note,
    }


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

    # #1243: a submission that already went along the second axis is that
    # axis's business, with its own ceiling — checked BEFORE the ladder's,
    # because the report here is the stronger model's own, and the ladder's
    # step count (which does include that run) would announce the wrong
    # ceiling in the wrong words.
    attempts = await _count_marked_alerts(
        db, task_id, MODEL_CASCADE_MARK.format(generation=generation)
    )
    if attempts:
        return await _ask_a_stronger_model(db, task, generation, report, attempts)

    # The profile comes from the dispatch, never from the report about itself
    # (#807, #750).
    profile = (report.get("profile") or "").strip()
    if profile == DEEP and _second_axis_applies(report):
        # #1243: the ladder has nothing above deep; the second axis keeps the
        # profile and changes the model. BEFORE the ladder's ceiling (finding
        # 123ea6f62ddd2919): lite → deep top-up → deep incomplete is two paid
        # rungs, and the ceiling there used to answer a question that is not
        # the ladder's — the report is deep, the next step is a model, not a
        # profile. The ceiling still guards what it was built for: a third
        # PROFILE step.
        return await _ask_a_stronger_model(db, task, generation, report, 0)

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
            "человеком (#879)." + _ladder_cause_note(report),
        )
        await db.commit()
        return False

    # Only a CHEAP run earns a top-up: an unknown profile is not a cheap one,
    # and a deep run that did not finish has nothing above it to climb to —
    # both go to the human, and both say so.
    if profile != LITE:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Неполный отчёт, добор не положен: профиль "
            + (f"«{profile}»" if profile else "не заявлен")
            + " — выше дешёвого подниматься некуда. Решение за человеком "
            "(#879)." + _ladder_cause_note(report),
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


# ---------------------------------------------------------------------------
# Вторая ось каскада: та же работа, тот же профиль, другая модель (#1243)
# ---------------------------------------------------------------------------
#
# Лестница выше поднимает ПРОФИЛЬ и на deep честно говорит «выше подниматься
# некуда». Замер спайка #1168: 30 из 51 неполного отчёта за 90 дней — уже
# deep, то есть большая часть неполноты лежит ровно на той ступени, где
# лестница молчит. Эта ось профиль не трогает и меняет МОДЕЛЬ.
#
# Чем она НЕ является. Не переспросом #1242: тот срабатывает там, где отчёта
# нет вовсе, эта — только на ЗАЯВЛЕННОЙ неполноте существующего отчёта. Не
# лекарством от отказа среды (#1238): другая модель в той же среде упрётся
# в то же, поэтому отказ среды на эту ось не пускается.
#
# Ценность второй попытки НЕ доказана, доказан только размер корзины; поэтому
# у оси свой счёт исхода (count_model_cascade_outcomes), без которого
# добавка к счёту была бы измерена, а польза — нет.

#: Метка записи о переспросе по второй оси. Номер поколения внутри — счёт в
#: пределах сдачи, по образцу ASK_AGAIN_MARK. Запись делается ДО вызова:
#: попытка видна и тогда, когда вызов не оставил строки.
MODEL_CASCADE_MARK = "[вторая ось ревью: сдача {generation}]"
MODEL_CASCADE_EXHAUSTED_MARK = "[вторая ось ревью исчерпана: сдача {generation}]"

#: Вид события, по которому считается исход второй оси.
MODEL_CASCADE_EVENT = "review_model_cascade"

#: Сколько вторых попыток С ОТЧЁТОМ нужно, чтобы печатать долю полных. Ниже —
#: слово «недобор» (#1153). Пять — меньше месяца ожидаемого потока (около
#: девяти в месяц по спайку #1168), и достаточно, чтобы «хотя бы половина»
#: из условия пересмотра не решалась одним случаем.
MODEL_CASCADE_MIN_SAMPLE = 5


def _second_axis_applies(report: Mapping[str, Any]) -> bool:
    """Пускать ли неполный deep-отчёт на вторую ось.

    Ось выключена потолком 0 — рубильник владельца. Отказ среды на неё не
    пускается: переспрос моделью отказ среды не лечит (#1238).
    """
    return config.REVIEW_MODEL_CASCADE_MAX > 0 and not is_environment_refusal(report)


async def _models_that_read(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> list[str]:
    """Модели оплаченных прогонов этой сдачи, в порядке заказа."""
    rows = await fetchall(
        db,
        "SELECT model FROM review_dispatches WHERE task_id = ? "
        "AND submission_generation = ? AND agent_id != '' AND model != '' "
        "ORDER BY id",
        (task_id, generation),
    )
    return list(dict.fromkeys(str(dict(r)["model"]) for r in rows))


async def _alert(db: aiosqlite.Connection, task_id: int, text: str) -> None:
    await repo.add_task_update(db, task_id, "hub", "alert", text)
    await db.commit()


async def _second_axis_blocked(
    db: aiosqlite.Connection, task: dict[str, Any], model: str
) -> str:
    """Почему вторую ось нельзя пройти на этой сдаче, или "" — если можно."""
    project = await repo.resolve_project_for_task(db, int(task["id"]))
    forge = project_policy.forge_of(project) if project is not None else ""
    if forge not in CLOUD_REVIEW_FORGES:
        # Локальный ревьюер модель не выбирает — заказ «другой модели» туда
        # был бы ещё одним прогоном той же модели под чужим именем.
        return f"форж «{forge or 'не известен'}» облаку недоступен, а локальный ревьюер модель не выбирает"
    if not model:
        return (
            "нет модели, которая наблюдалась запускающейся, не совпадает "
            "семейством с исполнителем и ещё не читала эту сдачу "
            "(REVIEW_CASCADE_MODELS, SUBSCRIPTION_LAUNCHABLE_MODELS)"
        )
    return ""


async def _ask_a_stronger_model(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    generation: int,
    report: Mapping[str, Any],
    attempts: int,
) -> bool:
    """Переспросить ТУ ЖЕ сдачу тем же профилем другой моделью (#1243).

    Возвращает True, когда заказ поставлен. Каждый иной исход называется в
    карточке: тихая повторная покупка хуже отсутствия повтора, а тихий отказ
    — это решение человека по отчёту, чей автор сам сказал, что не дочитал.
    """
    task_id = int(task["id"])
    limit = config.REVIEW_MODEL_CASCADE_MAX
    used = await _models_that_read(db, task_id, generation)
    read_by = ", ".join(used) or "не записано"
    if attempts >= limit or not _second_axis_applies(report):
        await _alert(
            db,
            task_id,
            MODEL_CASCADE_EXHAUSTED_MARK.format(generation=generation)
            + f" Неполный отчёт и после переспроса другой моделью: потолок "
            f"второй оси — {attempts} из {limit} попыток на сдачу, третий "
            f"агент НЕ покупается. Модели, читавшие эту сдачу: {read_by}. "
            "Ревью так и не состоялось полностью — решение за человеком "
            "(#1243)." + _ladder_cause_note(report),
        )
        return False
    implementer = (task.get("submission_model") or "").strip()
    model = pick_cascade_model(implementer, set(used))
    blocked = await _second_axis_blocked(db, task, model)
    if blocked:
        await _alert(
            db,
            task_id,
            "Неполный отчёт профиля «deep»: выше дешёвого подниматься некуда, "
            f"а вторая ось (другая модель) недоступна — {blocked}. Решение "
            "за человеком (#879, #1243)." + _ladder_cause_note(report),
        )
        return False
    await _alert(
        db,
        task_id,
        MODEL_CASCADE_MARK.format(generation=generation)
        + f" Переспрос {attempts + 1} из {limit}: отчёт профиля deep сам "
        "объявил себя неполным, а выше по профилю подниматься некуда, поэтому "
        f"ТА ЖЕ сдача тем же профилем заказывается другой модели — {model} "
        f"(читали: {read_by}; семейство ≠ исполнителя "
        f"{implementer or 'не заявлено'}). Модель взята из наблюдённых "
        "запусков, а не из каталога (#1237, #1243).",
    )
    if not await maybe_dispatch_review(
        db, task_id, force_profile=DEEP, force_model=model
    ):
        await _alert(
            db,
            task_id,
            f"Переспрос моделью {model} поставить не удалось. Решение за "
            "человеком (#1243).",
        )
        return False
    row = await repo.get_review_dispatch_for_generation(db, task_id, generation)
    await repo.insert_event(
        db,
        kind=MODEL_CASCADE_EVENT,
        task_id=task_id,
        actor="policy",
        payload={
            "generation": generation,
            "model": model,
            "attempt": attempts + 1,
            "dispatch_id": dict(row)["id"] if row is not None else None,
            "after_review_id": report.get("id"),
        },
    )
    await db.commit()
    return True


async def _cascade_outcome(db: aiosqlite.Connection, event: dict[str, Any]) -> str:
    """Чем кончилась одна вторая попытка: complete | incomplete | pending.

    Отчёт попытки — первый отчёт той же сдачи ПОСЛЕ того, что её купил, и
    (когда принципал ревьюера известен) от него. Сопоставление по ступеням
    (_dispatch_report) тут не годится: без принципала оно отдаёт последний
    отчёт поколения, то есть ещё не ответившая попытка читалась бы
    неполной — по отчёту, который её и купил.
    """
    payload = json.loads(event.get("payload") or "{}")
    after = int(payload.get("after_review_id") or 0)
    expected = None
    if payload.get("dispatch_id"):
        rows = await fetchall(
            db,
            "SELECT reviewer_principal_id FROM review_dispatches WHERE id = ?",
            (payload["dispatch_id"],),
        )
        expected = dict(rows[0])["reviewer_principal_id"] if rows else None
    reviews = await repo.machine_reviews_of_generation(
        db, int(event["task_id"]), int(payload.get("generation") or 0)
    )
    later = [
        dict(r)
        for r in reviews
        if int(dict(r)["id"]) > after
        and (expected is None or dict(r).get("principal_id") == expected)
    ]
    if not later:
        return "pending"
    return "incomplete" if later[0].get("incomplete") else "complete"


async def count_model_cascade_outcomes(
    db: aiosqlite.Connection,
    since_days: int = ENVIRONMENT_REFUSAL_WINDOW_DAYS,
) -> dict[str, Any]:
    """Исход второй оси: сколько попыток и сколько дали ПОЛНЫЙ отчёт (#1243).

    Обе цифры идут с размером выборки. Доля полных печатается только при
    MODEL_CASCADE_MIN_SAMPLE попыток с отчётом; ниже — слово «недобор», а не
    число (#1153): «1 из 1» решения о выкате не выдерживает.
    """
    rows = await fetchall(
        db,
        "SELECT task_id, payload FROM events WHERE kind = ? "
        "AND created_at >= datetime('now', ?) ORDER BY id",
        (MODEL_CASCADE_EVENT, f"-{int(since_days)} days"),
    )
    outcomes = [await _cascade_outcome(db, dict(r)) for r in rows]
    attempts = len(outcomes)
    complete = outcomes.count("complete")
    incomplete = outcomes.count("incomplete")
    reported = complete + incomplete
    enough = reported >= MODEL_CASCADE_MIN_SAMPLE
    if enough:
        share_note = (
            f"полных отчётов после переспроса другой моделью: {complete} из "
            f"{reported} с отчётом (попыток {attempts} за {since_days} дн., "
            f"ждут отчёта {attempts - reported})"
        )
    else:
        share_note = (
            f"недобор: вторых попыток по модели {attempts} за {since_days} дн., "
            f"с отчётом {reported}, полных {complete} из {reported}; доля "
            f"печатается от {MODEL_CASCADE_MIN_SAMPLE} попыток с отчётом"
        )
    return {
        "since_days": int(since_days),
        "attempts": attempts,
        "reported": reported,
        "pending": attempts - reported,
        "complete": complete,
        "incomplete": incomplete,
        "min_sample": MODEL_CASCADE_MIN_SAMPLE,
        "complete_share": round(complete / reported, 3) if enough else None,
        "share_note": share_note,
    }


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

# Состояние строки заказа между «облако отчёта не дало» и «вторая дверь
# открыта» (#1252). Не украшение и не третий исход: это ДОЛГ, записанный в
# базу, потому что между двумя этими событиями лежит подготовка заказа с
# сетью внутри, а поллер глотает исключение и идёт дальше. Закрытая строка
# свипу невидима — значит закрывать её раньше, чем долг отдан, нельзя.
SECOND_DOOR_OWED = "second_door"


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
    if await _this_code_was_already_read(db, task):
        return False
    # #1255: третий тихий отказ той же природы — новое чтение уже не купит
    # ничего нового, потому что находки перестали убывать. Стоит ПОСЛЕ
    # проверки новизны: повтор того же кода — её вопрос, а здесь код менялся.
    # Добор выше сюда не доходит намеренно: он дочитывает УЖЕ оплаченное
    # поколение, а не покупает следующее.
    return not await findings_stopped_converging(db, task)


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
        # #1361: вершина другая — но правка автора может быть той же, если в
        # ветку приехала только база. Тогда отчёт переносится, а не покупается.
        return await _carry_the_report_over(db, task)
    # #1265: пересдача того же sha из review теперь штатный повтор (таймаут,
    # две сессии) и приходит сюда на ТОМ ЖЕ поколении снова и снова. Отказ
    # говорится один раз на отчёт — тем же приёмом, каким свип не повторяет
    # алерт о недоступном ревьюере (Cursor #383, 78312fbb487b30ca).
    if not await _already_told_about_the_missing_reviewer(
        db, int(task["id"]), f"отчёт #{already} покрывает ту же вершину"
    ):
        await _refuse_second_read(db, int(task["id"]), already)
    return True


#: Отчёт — независимое чтение, а не самоотчёт исполнителя о своей работе.
#: Одно условие на всех читателей machine_reviews, которым это важно: страж
#: новизны не считает самоотчёт покрытием кода (#1011, #1025), траектория
#: несходимости (#1255) не берёт его ни точкой, ни сбросом. Второй редакции
#: этого правила не заводим. Псевдоним таблицы в запросе — ``mr``.
INDEPENDENT_READ = "COALESCE(mr.self_reviewed, 0) = 0"

#: «Код прочитан»: отчёт ПОЛНЫЙ и независимый. Одно определение на двух
#: читателей — страж повторного чтения (``_report_already_covers_this_sha``)
#: и базу дельты пересдачи (#1400). Неполный отчёт сам говорит, что дочитал
#: не всё; самоотчёт — не чтение со стороны. Ни тот ни другой не может ни
#: запереть добор, ни стать точкой, от которой дельта перестаёт читать.
CODE_READ = f"COALESCE(mr.incomplete, 0) = 0 AND {INDEPENDENT_READ}"


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

    Фильтр по поколению — ``<=``, не ``!=`` (#1265, круг 2). До #1265
    пересдача ВСЕГДА поднимала поколение, так что к моменту этой проверки
    отчёт нужного sha неизбежно лежал на генерации МЕНЬШЕ текущей — «не
    равно» и «меньше» были одним и тем же условием, потому что «равно»
    никогда не случалось. Теперь пересдача того же sha из review поколение
    не поднимает: решение о заказе идёт на ТОЙ ЖЕ генерации, на которой
    лежит и отчёт. Условие переписано на явную границу, а не снято вовсе,
    потому что отчёт СТРОГО БУДУЩЕЙ генерации в принципе невозможен —
    задача не пишет отчёт для сдачи, которой ещё не было.
    """
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    pinned = (task.get("submission_sha") or "").strip()
    if not pinned:
        return None
    rows = await fetchall(
        db,
        "SELECT mr.id AS review_id, s.sha AS sha "  # nosec B608 - константа модуля, не ввод
        "FROM machine_reviews mr "
        "JOIN submissions s ON s.task_id = mr.task_id "
        "AND s.generation = mr.submission_generation "
        "WHERE mr.task_id = ? AND mr.submission_generation <= ? "
        # Неполный отчёт не покрывает код: он САМ говорит, что дочитал не
        # всё, и лестница #879 существует ровно затем, чтобы добрать
        # непрочитанное. Назвать его чтением значило бы запереть добор
        # утверждением, которое отчёт опровергает о себе — та же ошибка,
        # против которой стоит #762, только с другой стороны.
        # Самоотчёт исполнителя не отменяет независимого ревьюера. Автор,
        # приславший отчёт о собственной работе, уже однажды закрыл чужой
        # диспетчер как выполненный (#1011, #1025) — здесь он закрывал бы
        # его ещё до старта. «Код прочитан» имеет смысл только про того,
        # кто читал его со стороны. Оба условия — в CODE_READ (#1400).
        f"AND {CODE_READ}",
        (task_id, generation),
    )
    for row in rows:
        if ((dict(row).get("sha") or "").strip()) == pinned:
            return int(dict(row)["review_id"])
    return None


async def _latest_full_report(
    db: aiosqlite.Connection, task_id: int, before_generation: int
) -> dict[str, Any] | None:
    """Последний независимый отчёт прошлых поколений со sha его сдачи (#1361).

    Берётся ПОСЛЕДНИЙ, а не лучший: если последнее чтение неполное, переносить
    нечего — лестница #879 существует затем, чтобы его добрать, и полный отчёт
    поколением раньше говорит о другом коде.
    """
    rows = await fetchall(
        db,
        "SELECT mr.*, s.sha AS sha "  # nosec B608 - константа модуля, не ввод
        "FROM machine_reviews mr "
        "JOIN submissions s ON s.task_id = mr.task_id "
        "AND s.generation = mr.submission_generation "
        "WHERE mr.task_id = ? AND mr.submission_generation < ? "
        f"AND {INDEPENDENT_READ} "
        "ORDER BY mr.submission_generation DESC, mr.id DESC LIMIT 1",
        (task_id, before_generation),
    )
    return dict(rows[0]) if rows else None


async def _carry_the_report_over(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> bool:
    """Перенести отчёт на пересдачу, где слита только база (#1361). True — перенесён.

    Измерено 23.09.2026: #1333 сдача 3 — только слияние develop, заказано
    deep-ревью, ~4,7 млн токенов, отчёт чистый; #1334 и #1337 — ещё два
    полных прогона за то же. Хаб сам писал «своей правки в этом круге нет» и
    всё равно платил за новое чтение.

    «Правка та же» решает ОДНО правило на гейт и на ревью —
    ``orchestration.base_merge_kept_the_verdict`` над
    ``base_merge.author_edit_same``: упорядоченные строки +/- по файлам.
    Новая строка разрешения конфликта или перестановка — новая работа, и
    ревью заказывается как раньше. Не смогли прочитать дифф — тоже.

    Перенос — не новое чтение, и он так и записан: в новом поколении лежит
    копия отчёта с ``carried_from_review_id`` исходного, без токенов и
    длительности (денег он не стоил), а метрики чтения его не считают.
    Копия нужна, а не одна запись в ленте: гейт и стюард спрашивают отчёт
    ТЕКУЩЕГО поколения и без него встали бы на «machine-review устарел».
    """
    from hub.services.orchestration import (
        base_merge_kept_the_verdict,
        report_has_evidence,
    )

    pinned = (task.get("submission_sha") or "").strip()
    generation = int(task.get("submission_generation") or 0)
    source = await _latest_full_report(db, int(task["id"]), generation)
    if (
        not pinned
        or source is None
        or bool(source.get("incomplete"))
        or not report_has_evidence(source)
        or not (source.get("sha") or "").strip()
    ):
        return False
    same, why = await base_merge_kept_the_verdict(
        db, task, source["sha"].strip(), pinned
    )
    if not same:
        return False
    root = int(source.get("carried_from_review_id") or source["id"])
    carried = await repo.insert_machine_review(
        db,
        task_id=int(task["id"]),
        submission_generation=generation,
        harness_skill=source.get("harness_skill") or "",
        harness_version=source.get("harness_version"),
        agent_count=source.get("agent_count"),
        orchestrator=source.get("orchestrator") or "",
        model=source.get("model") or "",
        raw_count=int(source.get("raw_count") or 0),
        findings_confirmed=source.get("findings_confirmed") or "[]",
        findings_rejected=source.get("findings_rejected") or "[]",
        submitted_by=source.get("submitted_by") or "",
        incomplete=False,
        unresolved=source.get("unresolved") or "[]",
        lost_dimensions=source.get("lost_dimensions") or "[]",
        profile=source.get("profile") or "",
        principal_id=source.get("principal_id"),
        carried_from_review_id=root,
    )
    await repo.add_task_update(
        db,
        int(task["id"]),
        "hub",
        "alert",
        (
            f"Кросс-модельное ревью не заказано: отчёт #{root} перенесён с "
            f"поколения {int(source['submission_generation'])}: правка та же, "
            f"слита база. {why}. Перенос — не новое чтение: в этом поколении "
            f"лежит отчёт #{carried} с пометкой «перенесён», провайдер не "
            "зван, в метриках чтения он не считается (#1361). CI на новой "
            "вершине по-прежнему обязателен."
        ),
        author_kind="hub",
    )
    await db.commit()
    log.info(
        "review dispatch skipped for task #%s: report #%s carried over as #%s",
        task["id"],
        root,
        carried,
    )
    return True


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
            f"отчёт #{review_id} покрывает ту же вершину. Код этой сдачи не "
            "изменился, и второй прогон вернул бы то же чтение за те же "
            "деньги (#1152). Отчёт по этому коду читается "
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
    #: Одноразовый код, уже вписанный в ``prompt``. Хранится отдельно не ради
    #: удобства: срок его жизни отсчитывается от ЧЕКАНКИ, а локальный прогон
    #: может простоять в очереди дольше этого срока — тогда исполнителю нужно
    #: отмерить срок заново, а для этого нужен сам код (#1208). Пусто там, где
    #: кода не выдавали: открытый режим, отозванный токен.
    access_code: str = ""
    #: #1254: символы, которых вне tests/ не зовёт никто, — названные в
    #: ``prompt`` как предмет проверки. Пусто — разбор прошёл и не назвал
    #: никого; None — разбор не состоялся. Уезжает в строку заказа, и бриф
    #: судит итог ровно по этому набору.
    only_tests: tuple[str, ...] | None = None


async def _small_delta(
    db: aiosqlite.Connection, task_id: int, subject: DeltaSubject
) -> tuple[int, int] | None:
    """``(строки, порог)`` для правила маленькой пересдачи (#1416), или None.

    None, когда дельта не доказана (предмет — весь дифф: первая сдача, не
    предок, смена базы, нет прочитанного поколения) или авторская часть не
    отделена от привезённого базой: мерить тогда нечего, правило молчит.
    """
    if not subject.paths or not subject.author_diff:
        return None
    project = await repo.resolve_project_for_task(db, task_id)
    limit = small_delta_lines_of(gate_policy_of(project) if project else {})
    if limit is None:
        return None
    return author_delta_lines(subject.author_diff), limit


async def _only_tests_of(
    ctx: tuple[str, str] | None, diff: str | None
) -> list[call_sites.SymbolReport] | None:
    """Кандидаты only_tests по диффу сдачи (#1254); None — не смотрели.

    Источник один — ``call_sites.analyse``, тот же, что у брифа. Неудача
    разбора заказ не роняет: без кандидатов он остаётся прежним.
    """
    if ctx is None or not diff:
        return None
    try:
        report = await asyncio.to_thread(call_sites.analyse, ctx[0], diff)
    except Exception as exc:  # noqa: BLE001 - советующий блок, не гейт
        log.warning("only_tests: call-site walk failed: %s", exc)
        return None
    return call_sites.only_tests_symbols(report)


async def prepare_review_order(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    *,
    branch: str,
    generation: int,
    force_profile: str,
    principal_id: int | None,
    force_model: str = "",
    cloud: bool = False,
) -> ReviewOrder:
    """Собрать заказ: профиль по диффу, правила, предмет ревью, доступ.

    ``principal_id`` — принципал ТОГО ревьюера, который будет исполнять заказ:
    под ним минтуется одноразовый код доступа, и его же ждёт диспетчер как
    владельца отчёта (#1025). Разные транспорты — разные принципалы, и это
    единственное, что заказу нужно знать о том, кто его исполнит.

    ``force_model`` — модель второй оси каскада (#1243): её уже выбрал
    pick_cascade_model, и заказ не выбирает заново.

    ``cloud`` — заказ облачного канала: только он тратит квоту провайдера и
    только он подчиняется суточному потолку deep (#1414). Локальный
    ревьюер квоту Cursor не тратит — решение владельца 25.09.
    """
    task_id = int(task["id"])
    model_id = force_model or pick_review_model(
        (task.get("submission_model") or "").strip()
    )
    # #820: the profile is decided against the SUBMITTED diff, not against the
    # areas the author declared — self-assessment cannot exempt work from
    # oversight (#582). An unreadable diff buys deep, it does not excuse it.
    diff = await _submission_diff(db, task_id, branch)
    # #874: which base the reviewer diffs against. Unknown base falls back to
    # the configured one rather than to nothing — a command the reviewer cannot
    # run would send it back to inventing its own, which is what we are fixing.
    ctx = await _git_context(db, task_id)
    base = ctx[1] if ctx else config.PAIR_BASE_BRANCH
    # #880: a resubmission reads what changed since the previous generation,
    # not the whole branch again. Every reason the delta cannot be trusted
    # falls back to the full diff and says so.
    subject = await generation_delta(db, task, base)
    # #1249: the profile is bought for the AUTHOR's change. On a resubmission
    # that merged the base branch, the plain delta also carries other people's
    # work — code that already passed its own gate — and a process surface
    # found only there used to buy the deep harness on somebody else's behalf.
    # An origin that could not be established leaves ``author_diff`` empty and
    # the whole submitted diff decides, exactly as before.
    profile_diff = subject.author_diff or diff
    if force_profile:
        profile, profile_reasons = (
            force_profile,
            ["профиль задан заказом: добор лестницы или его замена"],
        )
    else:
        profile, profile_reasons = pick_review_profile(
            task, profile_diff, await _small_delta(db, task_id, subject)
        )
        if cloud:
            profile, profile_reasons = await apply_deep_daily_cap(
                db, task, generation, profile, profile_reasons
            )
    rules_block, rules_note = await collect_review_rules(db, task_id, diff)
    prior = await previous_findings(db, task_id, generation)
    diff_block, diff_note = diff_plan(
        diff,
        base,
        branch,
        subject.paths,
        subject.note,
        prior,
        subject.base_paths,
    )
    # #875: what the toolchain already proved on THIS commit. Built from the
    # task row the caller already read, so no extra query for the common case.
    # Imported here, not at module level: review_evidence reaches back into
    # this module's siblings, and the top-level cycle is the reason every
    # other cross-service call in this file is local too.
    from hub.services import review_evidence

    prepass = await review_evidence.prepass_state(db, task)
    # #1246: the reviewer is told whether the submission is checked — by the
    # prepass — and sees the author's lines about runs only as his word.
    validation = review_evidence.validation_standing(
        prepass, await review_evidence.latest_submission_text(db, task_id)
    )
    only_tests = await _only_tests_of(ctx, diff)
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
            review_evidence.prepass_block(prepass, validation),
            _delivery_block(task_id, code, hub_base),
            call_sites.only_tests_block(only_tests),
            needs_container=task_needs_container(task),
        ),
        rules_note=rules_note,
        diff_note=diff_note,
        prepass=prepass,
        access_code=code,
        only_tests=None if only_tests is None else tuple(s.symbol for s in only_tests),
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
    #: Параметры модели, с которыми ушёл последний заказ (#1417).
    params: list[cursor_cloud.ModelParam] | None = None
    #: Параметры, которые провайдер отверг 400-м, и сам отказ (#1417).
    #: Пусто — не отвергались. Вина их доказана, только если после них
    #: голый заказ создал агента (см. ``_name_the_dropped_params``).
    refused_params: list[cursor_cloud.ModelParam] | None = None
    params_refusal: cursor_cloud.Refusal | None = None


#: Коды 400, при которых повтор без параметров не нужен: лимит счёта
#: (#1036) — голый заказ купил бы тот же отказ вторым запросом, ровно то,
#: что #1199 запретил. Только они (#1423): invalid_model и
#: model_not_available Cursor отдаёт и на отвергнутые params — 25.09.2026
#: ``[{fast:false}]`` для grok-4.6 пришёл как invalid_model, и исключение
#: остановило ревью для всех задач. Недоступная модель стоит тут лишний
#: запрос без агента; потерянное ревью стоит больше.
_ACCOUNT_LIMIT_CODES: frozenset[str] = frozenset(
    {
        "usage_limit_exceeded",
        "insufficient_quota",
    }
)


def _params_were_refused(
    params: list[cursor_cloud.ModelParam],
    refusal: cursor_cloud.Refusal | None,
) -> bool:
    """400 на заказ с параметрами — повод один раз заказать без них (#1417).

    Cursor на 400 причину толком не называет («[invalid_argument] Error»),
    поэтому признак — сам факт: параметры были, провайдер отверг, и это
    не лимит счёта. Любой другой код 400, invalid_model в том числе (#1423),
    — повод к одному голому заказу. Повтор ничего не стоит — отвергнутый
    заказ агента не создаёт, — а вину параметров доказывает только его успех
    (см. ``_name_the_dropped_params``).
    """
    return (
        bool(params)
        and refusal is not None
        and refusal.status == 400
        and refusal.code not in _ACCOUNT_LIMIT_CODES
    )


async def _name_the_dropped_params(
    db: aiosqlite.Connection, task_id: int, model_id: str, started: _Started
) -> str:
    """Записать отказ параметров, если он доказан; вернуть фактический вариант.

    Alert пишется только тогда, когда голый заказ прошёл: иначе отказ не про
    параметры, и сообщение о них назвало бы чужую причину (#1199).
    """
    refused = started.params_refusal
    if refused is not None and started.agent_id:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Заказ ревьюера "
            f"{cursor_cloud.model_variant(model_id, started.refused_params)} "
            f"провайдер отверг: HTTP {refused.status}"
            + (f", {refused.code}" if refused.code else "")
            + (f" ({refused.detail[:120]})" if refused.detail else "")
            + ". Повтор без параметров прошёл — прогон идёт в варианте модели "
            "по умолчанию (у Cursor это Fast, вдвое дороже). Проверьте "
            "CURSOR_REVIEW_MODEL_PARAMS по GET /v1/models (#1417).",
        )
    return cursor_cloud.model_variant(model_id, started.params)


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
    model_params: list[cursor_cloud.ModelParam] | None = None,
) -> _Started:
    """Создать ревьюера, а если ответ не дошёл — спросить, не создан ли он.

    Измерено 06.09.2026: три «отказа» подряд, и по каждому у провайдера
    нашёлся живой оплаченный агент (08:49:06Z, 08:50:53Z, 11:28:05Z).
    Наивный повтор покупал бы второго каждый раз, поэтому порядок такой:
    спросить, подобрать, и только на подтверждённой пустоте повторить.

    ``model_params`` (#1417): 400 на заказ с ними — один заказ без них.
    Отвергнутый заказ агента не создаёт, так что второго ревьюера этот
    повтор не покупает; дальше (сверка, повтор по обрыву) — без параметров.
    """
    adopted = False
    blind = False
    attempts = 1
    params = list(model_params or [])
    refused_params: list[cursor_cloud.ModelParam] | None = None
    params_refusal: cursor_cloud.Refusal | None = None

    async def _attempt() -> tuple[str, str, cursor_cloud.Refusal | None]:
        created, refusal = await cursor_cloud.create_agent_attempt(
            repo_url=repo_url,
            starting_ref=starting_ref,
            model_id=model_id,
            prompt_text=prompt_text,
            hub_mcp_url=hub_mcp_url,
            reviewer_token=reviewer_token,
            name=marker,
            model_params=params,
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
    if not agent_id and _params_were_refused(params, refusal):
        refused_params, params_refusal, params = params, refusal, []
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

    return _Started(
        agent_id,
        run_id,
        adopted,
        blind,
        attempts,
        refusal,
        params,
        refused_params,
        params_refusal,
    )


async def _cloud_config_missing(
    db: aiosqlite.Connection, task_id: int, gh_repo: str, reviewer_token: str
) -> bool:
    """Назвать в карточке, какой настройки облачного вызова нет (#1083).

    Вынесено из maybe_dispatch_review (#1399): та стоит на потолке в 60
    операторов, а бронь заказа добавила ей строк.
    """
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
        return True
    return False


async def _claim_the_order(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    profile: str,
    replaces_dispatch_id: int | None,
    force_model: str,
) -> bool:
    """Один облачный заказ профиля на сдачу (#1399).

    Прод: #1301 gen2 (deep, 347/348) и #1378 gen1 (lite, 412/413) — по два
    оплаченных ревьюера на одну сдачу. Второй триггер — повторный
    hub_submit_for_review того же коммита из review: #1265 оставляет
    поколение прежним и снова зовёт диспетчер (lifecycle.py,
    _same_sha_noop_response), а страж новизны (_this_code_was_already_read)
    смотрит только на ОТЧЁТЫ — пока ревьюер работает, отчёта нет.

    Бронь не берут два задуманных вторых вызова: переспрос после отказа
    (``replaces_dispatch_id``, #1242) и вторая ось каскада (``force_model``,
    #1243) — у каждого свой потолок. Добор лестницы (#879) бронь берёт, но
    под другим профилем (deep после lite) и поэтому проходит.
    """
    if replaces_dispatch_id is not None or force_model:
        return True
    if await repo.claim_review_order(db, task_id, generation, profile):
        return True
    log.info(
        "review dispatch for task #%s gen %s (%s) skipped: already ordered",
        task_id,
        generation,
        profile or "primary",
    )
    return False


async def _release_the_order(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    profile: str,
    replaces_dispatch_id: int | None,
    force_model: str,
) -> None:
    """Снять бронь, если её брал ЭТОТ вызов (#1399).

    Переспрос и вторая ось брони не берут — и снимать не должны: иначе их
    отказ снял бы бронь параллельного первичного триггера.
    """
    if replaces_dispatch_id is None and not force_model:
        await repo.release_review_order(db, task_id, generation, profile)


async def _prepare_claimed_order(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    *,
    branch: str,
    generation: int,
    force_profile: str,
    principal_id: int | None,
    force_model: str,
    replaces_dispatch_id: int | None,
) -> ReviewOrder | None:
    """Бронь, подготовка заказа и последнее слово перед тратой (#1399).

    Бронь — ПЕРВОЙ, до подготовки (находка a1d2d6301a527145): подготовка
    выпускает код доступа ревьюера, а issue_code гасит невыкупленные коды
    того же принципала на эту сдачу. Повторный триггер, дошедший до выпуска
    раньше отказа брони, оставлял уже оплаченного ревьюера с мёртвым кодом.
    Ключ брони — ``force_profile``: у первичного заказа профиль ещё не выбран.

    Любой отказ после брони её снимает, в том числе исключение подготовки:
    иначе сдача стояла бы без ревью весь срок брони.
    """
    task_id = int(task["id"])
    if not await _claim_the_order(
        db, task_id, generation, force_profile, replaces_dispatch_id, force_model
    ):
        return None
    try:
        if await _forced_deep_over_cap(
            db, task, generation, force_profile, force_model
        ):
            await _release_the_order(
                db,
                task_id,
                generation,
                force_profile,
                replaces_dispatch_id,
                force_model,
            )
            await db.commit()
            return None
        order = await prepare_review_order(
            db,
            task,
            branch=branch,
            generation=generation,
            force_profile=force_profile,
            principal_id=principal_id,
            force_model=force_model,
            cloud=True,
        )
        # Последнее слово перед тратой. Подготовка заказа выше ходит в сеть
        # за диффом и правилами, и за это окно сдача могла смениться — ЛЮБОЙ
        # облачный заказ, не только переспрос (находка dcd7da1fa88023c5):
        # тот же фильтр свежести, что у второй двери (#1252). Для переспроса
        # ещё и отчёт, доехавший в это окно (находка 17a9ca6ea3451163). Отказ
        # здесь тихий: у переспроса ОБА отказа — и по отчёту, и по свежести —
        # называет сам переспрос (_name_a_cancelled_retry, находка
        # 40a8fd8b727c82ec), а первичный заказ сменившейся сдачи заменит
        # заказ её новой сдачи, как и у второй двери (#1252).
        live = await _submission_still_live(db, task, branch, generation) and not (
            replaces_dispatch_id is not None
            and await repo.machine_reviews_of_generation(db, task_id, generation)
        )
    except BaseException:
        await _release_the_order(
            db, task_id, generation, force_profile, replaces_dispatch_id, force_model
        )
        await db.commit()
        raise
    if not live:
        await _release_the_order(
            db, task_id, generation, force_profile, replaces_dispatch_id, force_model
        )
        await db.commit()
        return None
    return order


async def maybe_dispatch_review(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    force_profile: str = "",
    replaces_dispatch_id: int | None = None,
    force_model: str = "",
) -> bool:
    """Queue a cloud reviewer for a fresh submission when policy allows.

    Called after submit_for_review commits. Returns True when a dispatch
    was recorded. Every refusal is either silent (policy does not ask for
    it) or a single visible alert (policy asked, the call failed).

    ``force_profile`` skips the profile choice and runs the named one — the
    top-up step of the ladder (#879), where the profile is no longer a guess
    about the task but a fact about the run that just failed to finish.

    ``replaces_dispatch_id`` is set only by the ask-again pass (#1242): the
    new order CONTINUES the rung of the failed one it names instead of
    opening a new one, both for the ladder count and for report matching.

    ``force_model`` is set only by the second axis of the cascade (#1243):
    the same work, the same profile, a model chosen by pick_cascade_model.
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
    if await _cloud_config_missing(db, task_id, gh_repo, reviewer_token):
        return False

    # #1180: подготовка вызова — общая для обоих способов добыть отчёт.
    # Профиль, правила репозитория, дифф-план, предпас и одноразовый код
    # ревьюер получает один и тот же, где бы он ни исполнялся; расходятся
    # только транспорт и то, чей принципал подпишет отчёт.
    expected_principal = await reviewer_principal_id(db)
    order = await _prepare_claimed_order(
        db,
        task,
        branch=branch,
        generation=generation,
        force_profile=force_profile,
        principal_id=expected_principal,
        force_model=force_model,
        replaces_dispatch_id=replaces_dispatch_id,
    )
    if order is None:
        return False
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
        model_params=await cursor_cloud.review_params_for(model_id),
    )
    agent_id, run_id = started.agent_id, started.run_id
    variant = await _name_the_dropped_params(db, task_id, model_id, started)
    if not agent_id:
        # МЕСТО ПРИМЕНЕНИЯ №1 второй двери (#1252): отказ СИНХРОННЫЙ — агент
        # не создался, денег не потрачено. Алерт остаётся на месте: отказ
        # облака — наблюдённый факт, и он стоит в карточке независимо от
        # того, добыл ли отчёт кто-то второй.
        detail = _lost_call_detail(started)
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Кросс-модельное ревью НЕ вызвано: {detail}. "
            "Вердикт остаётся человеку; детали в логе хаба (#757).",
        )
        await db.commit()
        if started.blind:
            # Пустой agent_id тут значит НЕ «создать не вышло», а «спросить,
            # создался ли, не вышло» — то самое состояние, которое _Started
            # отличает признаком blind и ради которого повтор запрещён двумя
            # строками выше. Открыть на нём вторую дверь значит купить второго
            # ревьюера поверх, возможно, уже оплаченного первого и получить
            # два соперничающих отчёта на одну сдачу. Отказом считается
            # только НАБЛЮДЁННЫЙ факт (deploy/LOCAL-REVIEW.md): либо
            # провайдер отверг создание, либо сверка подтвердила, что агента
            # нет. Слепота — ни то, ни другое, и остаётся человеку.
            #
            # #1399 (находка 815b0841472792be): по той же причине бронь
            # остаётся — снять её значило бы разрешить следующему триггеру
            # купить второго агента поверх, возможно, оплаченного. Держится
            # она срок брони (REVIEW_ORDER_CLAIM_TTL_MINUTES).
            return False
        owed = await _owe_the_refused_call(
            db,
            task,
            detail,
            model=model_id,
            profile=profile,
            principal_id=expected_principal,
            replaces_dispatch_id=replaces_dispatch_id,
            only_tests=order.only_tests,
        )
        # #1399 (находка 6d0bd1c790fb24eb): бронь снимается только ПОСЛЕ
        # того, как долг второй двери записан и разобран. Раньше — и
        # триггер, пришедший между алертом и заглушкой, покупал облачного
        # ревьюера, пока вторая дверь открывала локального.
        await _release_the_order(
            db, task_id, generation, force_profile, replaces_dispatch_id, force_model
        )
        await db.commit()
        return owed

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
        replaces_dispatch_id=replaces_dispatch_id,
        only_tests=order.only_tests,
    )
    # #1399: строка заказа и снятие брони — одна транзакция (коммит ниже).
    await _release_the_order(
        db, task_id, generation, force_profile, replaces_dispatch_id, force_model
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
        f"Кросс-модельное ревью вызвано хабом: модель {variant} "
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
            "model_params": started.params,
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


async def _owe_the_refused_call(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    detail: str,
    *,
    model: str,
    profile: str,
    principal_id: int | None,
    replaces_dispatch_id: int | None,
    only_tests: tuple[str, ...] | None,
) -> bool:
    """Синхронный отказ облака: долг второй двери и след для переспроса.

    Вынесено из ``maybe_dispatch_review`` (#1242): та стоит на потолке в 60
    операторов, а у этого пути появилась развилка.

    Заглушка переживает вызов, если вторая дверь НЕ открылась (#1242). До
    этого она удалялась всегда, и синхронный отказ без настроенного
    локального пути не оставлял в базе ничего, кроме алерта: переспросу было
    не за что зацепиться, и сдача стояла без отчёта навсегда — ровно так
    #1162 и #1281 получили 22.09 «HTTP 429» и не получили повтора. Осевшая
    заглушка (``failed``, пустой ``agent_id``) ничего не стоила и ступенью
    лестницы не считается (``count_review_dispatches``); она — единственная
    запись о том, что вызов был и сорвался.
    """
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    # Долг второй двери записывается ДО попытки её открыть (#1266), тем
    # же способом, каким это уже делает асинхронный путь (owe_second_
    # door, _close_a_run_without_a_report): строка-заглушка коммитится
    # первой, и только потом идёт рискованный вызов (review_reach,
    # счёт токенов, сетевая prepare_review_order). Без этого порядка
    # падение ВНУТРИ open_second_door не оставляло свипу ничего, что
    # повторить, — вторая дверь терялась навсегда, потому что строки не
    # было вовсе. agent_id пуст здесь НЕ временно: ничего не создано и
    # не оплачено, и count_review_dispatches (#1266) такую строку в шаг
    # лестницы не считает.
    stub_id = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id="",
        run_id="",
        model=model,
        profile=profile,
        reviewer_principal_id=principal_id,
        channel=CLOUD_CHANNEL,
        replaces_dispatch_id=replaces_dispatch_id,
        only_tests=only_tests,
    )
    await repo.owe_second_door(db, stub_id, detail)
    await db.commit()
    stub_row = await repo.get_review_dispatch_for_generation(db, task_id, generation)
    stub = dict(stub_row) if stub_row is not None else None
    if stub is None:  # pragma: no cover - defensive, row was just committed
        return False
    # Дальше долг разбирает ТОТ ЖЕ путь, что и асинхронный отказ:
    # _settle_second_door сама решает, открывать ли дверь, и сама же
    # закрывает строку. Успех отсюда виден тем же наблюдением, каким
    # _second_door_already_opened судит повтор — живой или удавшийся
    # локальный заказ по ЭТОМУ долгу, а не догадкой по пути, которым
    # сюда пришли.
    await _settle_second_door(db, stub, task_row=task)
    opened = await _second_door_already_opened(db, stub)
    # Открытая дверь: заглушка была ТОЛЬКО страховкой от падения внутри
    # вызова выше, и AC-1 #1252 требует ровно одну строку, локальную.
    # Падение МЕЖДУ _settle_second_door и этим удалением не теряет долг:
    # строка к тому моменту уже закрыта самой _settle_second_door.
    #
    # Закрытая дверь (#1242): заглушка ОСТАЁТСЯ закрытой в failed. Это
    # след сорвавшегося вызова, по которому свип переспрашивает облако
    # (_ask_again_lost_reviews); удалить её значило вернуть случай 22.09 —
    # отказ без отчёта и без повтора. Прежнее «ни одной строки» по AC-3
    # #1252 уточнено этим: локального прогона по-прежнему нет, строка —
    # облачная и ничего не стоившая.
    if opened:
        await repo.delete_review_dispatch(db, stub["id"])
        await db.commit()
    return opened


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
    local_missing: tuple[str, ...] = ()
    """Чего не хватает ЛОКАЛЬНОМУ пути, пустой кортеж — хватает всего.

    Отдельно от ``reason``, потому что ``reason`` заполнен только когда ways
    пуст, а на форже с облаком ways непуст всегда. Отказать локальному
    заказу там надо всё равно, и назвать причину — тоже (#1252).
    """

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

    #1252: способы считаются ОБА, и на форже с облаком тоже. Раньше здесь
    стоял ранний выход — «облако дотягивается» отвечало за весь вопрос, и
    локальная готовность на github не проверялась вовсе. Из-за этого у
    сдачи, чей облачный прогон отказал, второй двери не было даже там, где
    локальный ревьюер настроен и работает.

    Порядок в ``ways`` — порядок попыток: облако первое, локальный путь
    второй. Он читается как предпочтение, а не как множество.

    Ответ для трёх прежних читателей не меняется: на форже с облаком
    ``ways`` по-прежнему непуст, а ``reason`` по-прежнему пуст — то есть ни
    форма проекта, ни инвариант записи нового состояния не видят.
    """
    ways: list[str] = []
    if forge in CLOUD_REVIEW_FORGES:
        ways.append(CLOUD_CHANNEL)
    missing = local_reviewer.not_ready()
    if await local_reviewer_principal_id(db) is None:
        missing = missing or ["LOCAL_REVIEWER_HUB_TOKEN не разрешается в принципала"]
    if not missing:
        ways.append(LOCAL_CHANNEL)
    if ways:
        return ReviewReach(tuple(ways), "", tuple(missing))
    return ReviewReach(
        (),
        f"облачный ревьюер не работает с форжем «{forge}» (проверено "
        "31.08.2026, #1119), а " + local_path_refusal(missing),
        tuple(missing),
    )


def local_path_refusal(missing: Sequence[str]) -> str:
    """Почему локального пути нет — одними и теми же словами (#1188).

    Автор текста один на два места: отказ, когда способов НЕТ вовсе, и отказ
    локальному заказу на форже, где облако есть, но отчёта не дало. Две копии
    объясняли бы одно состояние двумя разными словами на первой же правке.
    """
    return (
        "локальный путь не настроен: "
        + "; ".join(missing)
        + ". Порядок включения — deploy/LOCAL-REVIEW.md"
    )


async def open_second_door(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    forge: str,
    branch: str,
    generation: int,
    force_profile: str = "",
    *,
    cloud_refusal: str,
    late_report_recheck: dict[str, Any] | None = None,
) -> bool:
    """Вторая дверь: локальный путь ПОСЛЕ наблюдённого отказа облака (#1252).

    Зовётся из ДВУХ мест, и это не украшение: отказ у облака бывает двух
    видов, и лежат они в разных путях кода. Синхронный — агент не создался
    (``maybe_dispatch_review``). Асинхронный — агент создался, прогон дошёл
    до терминального статуса, а отчёта нет (``sweep_review_dispatches``); там
    прогон УЖЕ оплачен. Закрыть один и забыть второй значит оставить
    половину сдач без отчёта ровно так же, как сегодня.

    ``cloud_refusal`` — НАБЛЮДЁННАЯ причина, а не догадка о недоступности:
    текст отказа провайдера или терминальный статус прогона. Он доезжает до
    карточки, потому что отчёт без имени автора читается как облачный и
    вводит человека в заблуждение (#1252, AC-4).

    Ненастроенный локальный путь не пишет НИЧЕГО: алерт об отказе облака уже
    стоит и остаётся единственным следом. Настройка, которой нет, не имеет
    права менять сегодняшнее поведение — поэтому здесь спрашивается
    ``review_reach`` (единственный автор этого знания, #1188), а не
    ``dispatch_local_review``, который на форже с облаком счёл бы
    достижимость облака своей и запустил прогон.

    Свежесть сдачи перечитывается ЗДЕСЬ, а не у каждого зовущего: между
    заказом облака и этой развилкой лежит сеть — синхронный путь ждал ответа
    на создание агента, асинхронный ждал расписания свипа. За это время
    задачу могли вернуть в работу, пересдать или переставить на другую
    ветку, и локальный прогон купил бы чтение кода, которого на живой сдаче
    уже нет. Правило одно, мест применения два, и второе место — ровно тот
    класс, что уже ловили: один потребитель правила ≠ все.

    ``late_report_recheck`` — заказ, чей провал мы разбираем (когда он
    известен), пробрасывается дальше в ``dispatch_local_review`` для ПОСЛЕДНЕЙ
    проверки отчёта непосредственно перед вставкой строки (находка по
    e69a3d5): здесь, тремя await раньше, отчёт ещё не увидеть.
    """
    if not await _submission_still_live(db, task, branch, generation):
        return False
    reach = await review_reach(db, forge)
    if LOCAL_CHANNEL not in reach.ways:
        return False
    return await dispatch_local_review(
        db,
        task,
        forge,
        branch,
        generation,
        force_profile,
        cloud_refusal=cloud_refusal,
        late_report_recheck=late_report_recheck,
    )


async def _submission_still_live(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    branch: str,
    generation: int,
) -> bool:
    """Та ли ещё сдача ждёт отчёта, по СВЕЖЕЙ строке задачи (#1252).

    Читается из базы, а не из ``task``: словарь на руках — снимок, сделанный
    до сетевого ожидания, и именно поэтому он ничего не доказывает.
    """
    row = await repo.get_task(db, int(task["id"]))
    if row is None:
        return False
    fresh = dict(row)
    return (
        fresh.get("status") == "review"
        and int(fresh.get("submission_generation") or 0) == generation
        and (fresh.get("branch") or "").strip() == branch
    )


async def dispatch_local_review(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    forge: str,
    branch: str,
    generation: int,
    force_profile: str = "",
    *,
    cloud_refusal: str = "",
    late_report_recheck: dict[str, Any] | None = None,
) -> bool:
    """Добыть ревью там, куда облако не дотягивается. True — прогон запущен.

    Каждый отказ здесь НАЗЫВАЕТ причину в карточке. Молчаливый отказ на этом
    месте — это ровно то, что задача #1180 закрывает: на GitVerse вердикт
    выносился вообще без второго читателя, и по карточке это выглядело как
    «ревью не потребовалось».

    ``late_report_recheck`` — заказ второй двери, чей провал мы разбираем
    (находка по e69a3d5). Между рунг-проверкой в ``_second_door_after_run`` и
    вставкой строки ниже лежат перечитывание свежести сдачи и достижимости
    (``open_second_door``), счёт потраченного и подготовка заказа —
    ``prepare_review_order`` только что сходила за диффом и правилами
    репозитория. Отчёт, доехавший за это время, ту раннюю проверку не видит
    вовсе. Здесь — ПОСЛЕДНЕЕ слово, ближе к вставке уже некуда: деньги тратит
    именно она.
    """
    task_id = int(task["id"])
    reach = await review_reach(db, forge)
    principal_id = await local_reviewer_principal_id(db)
    if LOCAL_CHANNEL not in reach.ways or principal_id is None:
        # Спрашивается ЛОКАЛЬНЫЙ канал, а не ``runnable``: на форже из
        # CLOUD_REVIEW_FORGES ways непуст из-за облака даже тогда, когда
        # локального пути нет вовсе, и старая проверка пропускала заказ
        # дальше. Принципал перечитывается ЗДЕСЬ и здесь же судится: между
        # готовностью и заказом лежит await, а токен ревьюера могут отозвать
        # — и заказ уехал бы под NULL, то есть отчёт перестал бы быть чужим
        # автору, ради чего прогон и покупается (#1128, #1252).
        #
        # Причина та же, что увидит человек в форме проекта и в отказе на
        # записи: у неё один автор (#1188), иначе три места объясняли бы
        # одно состояние тремя разными словами.
        missing = reach.local_missing or (
            "LOCAL_REVIEWER_HUB_TOKEN (токен принципала ревьюера) "
            "не разрешается в принципала",
        )
        return await _refuse_local_review(
            db, task_id, reach.reason or local_path_refusal(missing)
        )
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
    # #1252: причина, по которой отчёт добывается ЗДЕСЬ, а не в облаке, —
    # разная в двух случаях, и обе называются. «Облако сюда не дотягивается»
    # — свойство форжа (#1180). «Облако отказало» — наблюдённый факт про
    # конкретную сдачу, и человеку нужен именно он: без него отчёт второго
    # поставщика читается как облачный. Считается ДО последнего слова — не
    # зависит от базы, и нужна и вставке (second_door_reason), и карточке.
    why = (
        f"облако отчёта НЕ дало — {cloud_refusal}; отчёт добывается ВТОРЫМ "
        "поставщиком, локальным (#1252)"
        if cloud_refusal
        else f"форж «{forge}» облачному ревьюеру недоступен"
    )
    # ЕДИНСТВЕННОЕ последнее слово перед тратой денег (#1266). Рунг-проверка
    # в _second_door_after_run — только первая; prepare_review_order выше
    # сама по себе не быстрая (дифф, правила репозитория), а свежесть сдачи
    # могла смениться в том же окне (пересдача, снятие с ревью) — не только
    # отчёт заказа, который мы заменяем. Обе проверки здесь — уже
    # существующие функции (_submission_still_live — тот же дешёвый фильтр,
    # что стоит и в начале open_second_door, но авторитетен только он;
    # _dispatch_report — тот же, что и рунг-проверка выше), третьей копии ни
    # одной из них не заводится. Тихий отказ (без своего алерта): карточка
    # уже называет причину провалившегося заказа, вторую на то же состояние
    # не заводим (#1188).
    if not await _submission_still_live(db, task, branch, generation):
        return False
    if late_report_recheck is not None and (
        await _dispatch_report(db, task_id, generation, late_report_recheck) is not None
    ):
        return False
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
        replaces_dispatch_id=(
            int(late_report_recheck["id"]) if late_report_recheck is not None else None
        ),
        second_door_reason=why,
        only_tests=order.only_tests,
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        f"Машинное ревью запущено ЛОКАЛЬНО: {why}, "
        f"прогон идёт на хосте хаба под песочницей (#1180). "
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
    await _start_local_run(
        db,
        dispatch_id,
        task_id,
        generation,
        order.prompt,
        order.access_code,
        principal_id,
    )
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
    """Сколько токенов задача уже стоила — по всему, где сумма СКАЗАНА.

    Считается по отчётам, а не по числу прогонов: прогон, о котором ревьюер
    не отчитался, деньги всё равно стоил, но назвать сумму мы можем только
    там, где она сказана. Незнание здесь склоняется в сторону прогона —
    пропущенное ревью дороже лишнего, — и это тот же выбор направления
    ошибки, что в #762.

    #1252: посылка «у прогона без отчёта суммы нет» на пути второй двери
    неверна. Свип строкой выше спрашивает счёт у провайдера и кладёт его в
    ``review_dispatches.provider_tokens`` (#1026) — то есть в тот момент,
    когда открывается вторая дверь, оплаченный прогон УЖЕ назван, а потолок
    считал его нулём и обходился. Слагаемое второе: заказы, не доставившие
    отчёт. Они не пересекаются с первым — счёт доставленного отчёта лежит и
    на строке ``machine_reviews`` (``set_machine_review_provider_tokens``), и
    такой заказ закрыт как ``done``; двойного счёта тут нет.

    NULL остаётся незнанием и по-прежнему склоняется в сторону прогона: ноль
    в сумме, а не запрет.
    """
    rows = await fetchall(
        db,
        "SELECT COALESCE(SUM(COALESCE(provider_tokens, tokens_spent, 0)), 0) AS total "
        "FROM machine_reviews WHERE task_id = ?",
        (task_id,),
    )
    reported = int(dict(rows[0])["total"]) if rows else 0
    unreported = await fetchall(
        db,
        "SELECT COALESCE(SUM(provider_tokens), 0) AS total FROM review_dispatches "
        "WHERE task_id = ? AND status <> 'done' AND provider_tokens IS NOT NULL",
        (task_id,),
    )
    return reported + (int(dict(unreported[0])["total"]) if unreported else 0)


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
    access_code: str = "",
    principal_id: int | None = None,
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
            access_code=access_code,
            principal_id=principal_id,
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


async def _prompt_at_slot(
    db_path: str,
    live_db: aiosqlite.Connection | None,
    *,
    prompt: str,
    code: str,
    task_id: int,
    generation: int,
    principal_id: int | None,
) -> str:
    """Промт с ЗАНОВО выписанным кодом доступа — в момент, когда слот взят.

    Почему заново, а не продлением срока (так было на c6af952, #1208).
    Продление двигало ``expires_at`` у СУЩЕСТВУЮЩЕЙ строки, а строки к этому
    моменту может уже не быть: по часовому кругу поллера отрабатывает
    ``chat_pair.purge_expired``, и протухший код она не щадит — ``DELETE FROM
    chat_pair_codes WHERE expires_at < datetime('now')``. Длинная очередь —
    ровно тот случай, ради которого всё это писалось, — переживает и срок, и
    сборщика; продлять там нечего (найдено ревьюером Codex 11.09.2026 на
    c6af952).

    Второй путь, названный ревьюером, — исключить коды стоящих в очереди
    заказов из сборки протухшего — отвергнут по существу: он оставляет
    протухший код ЖИТЬ в таблице неограниченно долго и всё равно требует
    продления, чтобы им можно было воспользоваться, то есть заводит два
    механизма там, где хватает одного. Свежий код короткого срока, выписанный
    тогда, когда он нужен, снимает и срок, и сборщика разом.

    Почему подменяется БЛОК, а не токен кода. Блок доставки собирается
    ``_delivery_block`` детерминированно, поэтому его можно собрать заново со
    старым кодом и убедиться, что в промте он ровно один. Подменять голый
    токен значило бы верить, что восьмизначная строка не встретилась в
    диффе, — а проверять это нечем.

    Заказ при этом не расходится с облачным: ``prepare_review_order`` для
    обоих транспортов остаётся ОДИН, и код в нём выписывается одинаково.
    Локальный путь лишь переписывает свой блок доставки перед самым запуском.

    Любая неудача возвращает ИСХОДНЫЙ промт и говорит об этом в журнал.
    Прогон она не отменяет: у отчёта есть слабый путь через stdout, и
    подменять «отчёт пришёл хуже» на «ревью не состоялось» — тот самый обмен,
    против которого написан весь этот модуль. Но молчать нельзя — журнал
    здесь единственный след того, что отчёт поедет слабым путём.
    """
    if not code:
        # Кода не выдавали вовсе: открытый режим или отозванный токен. Блока
        # доставки в промте тогда нет, и подменять нечего — говорить об этом
        # значило бы звать оператора искать то, чего не было.
        return prompt
    conn = None
    try:
        if db_path:
            from hub import db as db_module

            conn = await db_module.connect(db_path)
        target = conn if conn is not None else live_db
        if target is None:
            return prompt
        hub_base = instance_base_url().rstrip("/")
        stale = _delivery_block(task_id, code, hub_base)
        if prompt.count(stale) != 1:
            log.warning(
                "local review: the delivery block of task #%s is not where it "
                "was put; the report will have to come back through stdout",
                task_id,
            )
            return prompt
        fresh = await _access_code(target, task_id, generation, principal_id)
        if not fresh:
            log.warning(
                "local review: no access code could be minted for task #%s at "
                "start; the report will have to come back through stdout",
                task_id,
            )
            return prompt
        return prompt.replace(stale, _delivery_block(task_id, fresh, hub_base), 1)
    except Exception:  # noqa: BLE001 - фон не имеет права уронить прогон
        log.exception("could not mint the access code of the local review")
        return prompt
    finally:
        if conn is not None:
            await conn.close()


async def _supervise_local_run(
    *,
    db_path: str,
    live_db: aiosqlite.Connection | None,
    dispatch_id: int,
    task_id: int,
    generation: int,
    prompt: str,
    access_code: str = "",
    principal_id: int | None = None,
) -> None:
    run = await local_reviewer.run_review(
        prompt,
        prompt_at_slot=lambda ready: _prompt_at_slot(
            db_path,
            live_db,
            prompt=ready,
            code=access_code,
            task_id=task_id,
            generation=generation,
            principal_id=principal_id,
        ),
    )
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
    stored = (
        _REPORT_NOT_STORED
        if report is None
        else await _store_report(db, dispatch, report, ORIGIN_LOCAL_TEXT)
    )
    if stored == _REPORT_STALE:
        # #1260: отказ уже назван в карточке. «Прогон не состоялся» поверх
        # него было бы ложью: отчёт был, но о прежней сдаче.
        await repo.set_review_dispatch_status(db, dispatch_id, "failed")
        await db.commit()
        return
    if stored == _REPORT_STORED:
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
            "Локальное машинное ревью НЕ состоялось: прогона не было — нет "
            "каталога, бинаря, прав либо песочница не переживает снятие "
            "этого прогона (детали в логе хаба). Это не «прочитано и чисто»: "
            "вердикт остаётся человеку (#1180)."
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
    #
    # #1252: ступень считается СРЕДИ ЗАКАЗОВ ТОГО ЖЕ ПРИНЦИПАЛА И КАНАЛА, а
    # не среди всех. Вторая дверь сделала «все заказы поколения» неверной
    # меркой: облачный и локальный заказы живут в одном поколении, а
    # принципалы у них разные — иначе отчёт не был бы независимым (#1128).
    # Локальный заказ оказывался ступенью 1, а у локального принципала отчёт
    # был первым (индекс 0), и годный контрактный отчёт не опознавался своим
    # же заказом: прогон объявлялся упавшим, а закрытого заказа не
    # оставалось вовсе — то есть пустое ревью не могло дать автовердикт.
    #
    # #1242: заказ, ЗАМЕЩЁННЫЙ переспросом того же канала, ступени не
    # занимает. Он кончился без отчёта, и повтор — продолжение той же
    # ступени, а не следующая: без этого свежий ревьюер ждал бы второго
    # отчёта принципала, а его первый отчёт оставался бы ничьим. Замена
    # второй дверью (другой канал) сюда не попадает намеренно — у неё свой
    # порядок и свой разбор (#1252).
    channel = dispatch.get("channel") or CLOUD_CHANNEL
    dispatch_ids = await fetchall(
        db,
        "SELECT id FROM review_dispatches WHERE task_id = ? "
        "AND submission_generation = ? AND reviewer_principal_id = ? "
        "AND channel = ? AND id NOT IN (SELECT replaces_dispatch_id "
        "FROM review_dispatches WHERE task_id = ? AND submission_generation = ? "
        "AND channel = ? AND replaces_dispatch_id IS NOT NULL) ORDER BY id",
        (task_id, generation, expected, channel, task_id, generation, channel),
    )
    order = [int(dict(r)["id"]) for r in dispatch_ids]
    try:
        rung = order.index(int(dispatch["id"]))
    except (ValueError, KeyError, TypeError):
        return None  # the dispatch row is gone or unreadable — nothing to match
    return own[rung] if rung < len(own) else None


async def dispatch_for_report(
    db: aiosqlite.Connection, task_id: int, generation: int, report: dict[str, Any]
) -> dict[str, Any] | None:
    """The dispatch THIS report belongs to — the inverse of ``_dispatch_report``.

    #1266 round 2 (715481fedbf41130): the report shown in the brief is the
    LATEST report (``get_latest_machine_review``, ordered by report id), but
    ``get_settled_review_dispatch`` names the LATEST DONE dispatch (ordered
    by dispatch id). Those are different rows in the AC-5 shape: a local
    replacement can settle with its own report before an earlier cloud order
    settles with ITS late report — the local dispatch then has the higher id
    even though its report is the older one. Naming the channel from "latest
    done dispatch" then paints a cloud report as local.

    Rather than inventing a second matching rule, this walks every dispatch
    of the generation and asks the SAME question ``_dispatch_report`` already
    answers authoritatively (principal+channel+rung) — "is THIS dispatch's
    own report exactly the one being shown" — and returns the first match.
    """
    report_id = report.get("id")
    if report_id is None:
        return None
    rows = await fetchall(
        db,
        "SELECT * FROM review_dispatches WHERE task_id=? "
        "AND submission_generation=? ORDER BY id",
        (task_id, generation),
    )
    for row in rows:
        dispatch = dict(row)
        matched = await _dispatch_report(db, task_id, generation, dispatch)
        if matched is not None and matched.get("id") == report_id:
            return dispatch
    return None


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
) -> str:
    """Record the report a finished run left in its text; see _store_report.

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
        return _REPORT_NOT_STORED
    from hub.services.machine_review_intake import ORIGIN_RUN_TEXT

    stored = await _store_report(db, dispatch, report, ORIGIN_RUN_TEXT)
    if stored != _REPORT_STORED:
        return stored
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
    return _REPORT_STORED


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


# Исходы записи отчёта из текста прогона (#1260). Три, а не True/False:
# «отчёт о прежней сдаче» — не «отчёт не разобрался», и путь, закрывающий
# прогон, обязан их различать, иначе причина отказа тонет в общей ветке.
_REPORT_STORED = "stored"
_REPORT_STALE = "stale"
_REPORT_NOT_STORED = "not_stored"
_GENERATION_MOVED = "chat_pair_generation_moved"


async def _store_report(
    db: aiosqlite.Connection,
    dispatch: dict[str, Any],
    report: Any,
    origin: str,
) -> str:
    """Записать отчёт, оставленный прогоном в СВОЁМ тексте; вернуть исход.

    Владелец отчёта берётся из строки диспетчера, а не из того, как отчёт
    называет себя сам: иначе он прочитался бы как чужой собственному вызову —
    дефект, закрытый в #1025. Происхождение пишется в данные: отчёт,
    переписанный хабом из текста, — факт слабее сданного по контракту, и
    метрики со стюардом должны уметь взвесить их по-разному (#1036).

    Одна реализация на оба канала намеренно: облачный и локальный прогон
    оставляют текст по одной и той же причине и с одинаковой доказательной
    силой, и две копии этого правила разошлись бы на первой же правке.

    Закрепление сдачи — тоже из строки диспетчера (#1260), по тому же
    принципу, что и владелец: прогон судил дифф поколения, на которое его
    заказали, и отчёт, доехавший после пересдачи, не засчитывается новой.
    Отказ пишется в карточку названной причиной ДО общего except: иначе он
    был бы неотличим от испорченного отчёта и пропал бы в журнале.
    """
    task_id = int(dispatch["task_id"])
    from fastapi import HTTPException

    from hub.services.machine_review_intake import record_machine_review

    try:
        await record_machine_review(
            db,
            task_id,
            report,
            principal_id=dispatch.get("reviewer_principal_id"),
            username=(dispatch.get("model") or "cursor-cloud-reviewer"),
            origin=origin,
            expected_generation=int(dispatch["submission_generation"]),
        )
    except HTTPException as exc:
        detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("reason") != _GENERATION_MOVED:
            log.exception("could not record the report recovered for task #%s", task_id)
            return _REPORT_NOT_STORED
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Отчёт ревью, оставленный прогоном в тексте, НЕ записан: "
            f"{detail.get('message')} Прогон судил прежний дифф, и засчитать "
            "его текущей сдаче значило бы считать прочитанным код, которого "
            "ревью не видело. Строка прогона закрыта (#1260).",
        )
        return _REPORT_STALE
    except Exception:  # noqa: BLE001 - the sweep must survive a bad report
        log.exception("could not record the report recovered for task #%s", task_id)
        return _REPORT_NOT_STORED
    return _REPORT_STORED


async def sweep_review_dispatches(db: aiosqlite.Connection) -> None:
    """Poller pass over active dispatches: settle finished runs.

    - report arrived → cross-check tokens against the provider's usage
      (mismatch is an audit flag, never a mechanical block) → done;
    - run terminal, no report, grace expired → one loud alert → failed;
    - run still going / API unreachable → leave for the next pass;
    - the last order of a live submission failed without any report →
      ask again, up to a per-generation ceiling (#1242).
    """
    # #1242: переспрос — ДО разбора активных строк. Заказ, закрытый упавшим
    # на этом проходе, переспрашивается на следующем, а не в ту же минуту.
    await _ask_again_lost_reviews(db)
    for row in await repo.list_active_review_dispatches(db):
        dispatch = dict(row)
        task_id = dispatch["task_id"]
        if dispatch.get("status") == SECOND_DOOR_OWED:
            # Долг, записанный прошлым проходом (или прошлой жизнью процесса):
            # облако уже отказало вслух, вторая дверь ещё не открыта.
            await _settle_second_door(db, dispatch)
            continue
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
        recovered = await _recover_report_from_run(db, dispatch, run)
        if recovered != _REPORT_NOT_STORED:
            # #1260: отчёт о прежней сдаче уже назван в карточке; вторая
            # дверь за него не покупается — он был, но судил другой дифф.
            await repo.set_review_dispatch_status(
                db, dispatch["id"], "done" if recovered == _REPORT_STORED else "failed"
            )
            await db.commit()
            continue
        await _close_a_run_without_a_report(db, dispatch, run)


async def _close_a_run_without_a_report(
    db: aiosqlite.Connection, dispatch: dict[str, Any], run: dict[str, Any]
) -> None:
    """Прогон дошёл до конца и отчёта не оставил: назвать это и открыть дверь.

    #1252: отчёт перечитывается НЕПОСРЕДСТВЕННО перед тем, как назвать его
    отсутствующим. Между первой проверкой в свипе и этим местом лежат два
    сетевых ожидания — терминальное состояние прогона и расход, — и
    контрактный отчёт, доехавший в это окно, заставал решение «отчёта нет»
    уже принятым: алерт «отчёт НЕ сдан», вторая дверь, лишние деньги и два
    соперничающих отчёта на одну сдачу.

    Эта проверка — первая из ДВУХ, и вторая важнее. Здесь закрывается ложь в
    карточке; деньги тратятся ниже по пути, и последнее слово перед заказом
    говорит ``_second_door_after_run``. Окно между двумя проверками — запись
    долга, чтение задачи и проекта, а на возобновлении ещё и весь перерыв
    между проходами свипа — как раз то, где отчёт успевает доехать. Один
    потребитель правила ≠ все.

    Доехавший отчёт здесь НЕ закрывается на месте: строка остаётся активной,
    и следующий проход разбирает её обычным путём — со сверкой расхода по
    данным провайдера (#1026). Второй автор этого закрытия разошёлся бы с
    первым на первой же правке.
    """
    task_id = int(dispatch["task_id"])
    run_status = str(run.get("status") or "")
    if (
        await _dispatch_report(db, task_id, dispatch["submission_generation"], dispatch)
        is not None
    ):
        return
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
    # МЕСТО ПРИМЕНЕНИЯ №2 второй двери (#1252): отказ АСИНХРОННЫЙ — агент
    # создался, прогон дошёл до терминального статуса, отчёта нет. Прогон тут
    # УЖЕ оплачен, и это единственное, чем этот случай отличается от
    # синхронного: без отчёта сдача стоит одинаково мёртво.
    #
    # Строка НЕ закрывается раньше, чем долг отдан. Закрытая строка невидима
    # свипу, и сбой между двумя записями — упавший процесс или исключение в
    # подготовке заказа, где git ходит в сеть, — уносил бы обещанную вторую
    # дверь навсегда: поллер глотает исключение и идёт дальше.
    await repo.owe_second_door(db, dispatch["id"], run_status)
    await db.commit()
    dispatch["run_status"] = run_status
    await _settle_second_door(db, dispatch, task_row=task_row)


async def _settle_second_door(
    db: aiosqlite.Connection,
    dispatch: dict[str, Any],
    *,
    task_row: Any | None = None,
) -> None:
    """Отдать долг второй двери и только потом закрыть строку (#1252).

    Зовётся из двух моментов одного и того же пути: сразу после записи долга
    и на возобновлении, когда предыдущая попытка не дошла до конца. Алерт об
    отказе облака здесь НЕ повторяется — он записан вместе с долгом, и
    повторять его на каждом проходе значило бы превратить один наблюдённый
    факт в поток.

    #1252: долг отдаётся ОДИН раз, и это проверяется НАБЛЮДЕНИЕМ, а не
    порядком записей. Возобновление существует именно потому, что хаб может
    умереть посередине, — а умереть он может и после того, как локальный
    заказ закоммичен, но до того, как долг помечен закрытым. Тогда при
    рестарте видны обе строки, облачная разбирается первой (ORDER BY id), и
    без этой проверки она покупала бы ВТОРОЙ прогон на то же поколение.

    Находка внешнего ревьюера по коммиту e69a3d5 (P1) и её остаток по #1266:
    строка ЭТОГО заказа закрывалась в ``failed`` БЕЗУСЛОВНО, даже когда у неё
    самой уже нашёлся СВОЙ отчёт — потому что ``_second_door_already_opened``
    спрашивалась РАНЬШЕ. На возобновлении после падения это не гипотетика:
    локальная замена уже закоммичена (долг «отдан» с точки зрения этой
    проверки), а поздний облачный отчёт ЭТОГО же заказа мог доехать в то же
    самое окно. Статус берётся из того, что в самом деле нашлось для ЭТОГО
    заказа, а не из того, приоткрыта ли дверь: СВОЙ отчёт красноречивее
    замены, потому что замена — это и есть ответ на его отсутствие, а не
    независимое свидетельство.
    """
    task_id = int(dispatch["task_id"])
    generation = int(dispatch["submission_generation"])
    if await _dispatch_report(db, task_id, generation, dispatch) is not None:
        await repo.set_review_dispatch_status(db, dispatch["id"], "done")
        await db.commit()
        return
    if await _second_door_already_opened(db, dispatch):
        await repo.set_review_dispatch_status(db, dispatch["id"], "failed")
        await db.commit()
        return
    if task_row is None:
        task_row = await repo.get_task(db, task_id)
    settled_by_its_own_report = await _second_door_after_run(
        db, dispatch, task_row, dispatch.get("run_status") or "терминальным"
    )
    await repo.set_review_dispatch_status(
        db, dispatch["id"], "done" if settled_by_its_own_report else "failed"
    )
    await db.commit()


async def _second_door_already_opened(
    db: aiosqlite.Connection, dispatch: dict[str, Any]
) -> bool:
    """Есть ли по ЭТОМУ долгу живой или удавшийся локальный заказ (#1252).

    Упавший локальный заказ сюда НЕ считается: он говорит «вторую дверь
    попробовали и она не сработала», и запретить по нему новую попытку
    значило бы закрыть дверь именно там, где она нужнее всего. Считается
    только заказ, который ещё идёт или уже принёс отчёт, — то есть долг,
    который в самом деле отдан.

    Находка внешнего ревьюера по коммиту e69a3d5 (P1): task_id+generation+
    channel одних отличает «долг отдан» от «долг ещё не отдан», но НЕ
    отличает РАЗНЫЕ долги внутри одного поколения. Лестница (#879) может
    внутри одной сдачи открыть вторую дверь дважды — дешёвый прогон
    провалился, локальный LITE ответил, а его неполный отчёт купил тяжёлый
    облачный добор, который тоже провалился. Без ``id > dispatch['id']``
    этот запрос находил ПЕРВЫЙ (LITE) локальный заказ и считал долг ВТОРОГО
    (DEEP) провала уже оплаченным — тяжёлый добор так и не заказывался.

    ``id`` растёт монотонно в порядке вставки (как и везде в этом модуле —
    рунг-сопоставление, ``get_settled_review_dispatch``), и локальный заказ,
    отвечающий на провал ИМЕННО этого облачного заказа, обязан появиться
    ПОСЛЕ него: раньше него в базе может быть только заказ, оплативший
    какой-то более ранний долг.
    """
    rows = await fetchall(
        db,
        "SELECT 1 FROM review_dispatches WHERE task_id = ? "
        "AND submission_generation = ? AND channel = ? "
        "AND status IN ('active', 'done') AND id > ? LIMIT 1",
        (
            int(dispatch["task_id"]),
            int(dispatch["submission_generation"]),
            LOCAL_CHANNEL,
            int(dispatch["id"]),
        ),
    )
    return bool(rows)


async def _second_door_after_run(
    db: aiosqlite.Connection,
    dispatch: dict[str, Any],
    task_row: Any,
    run_status: Any,
) -> bool:
    """Позвать второго поставщика по прогону, кончившемуся без отчёта (#1252).

    Гейты здесь — про то, что сдача ВСЁ ЕЩЁ ждёт отчёта, а не про сам отказ:
    задача в review, поколение то же, что у провалившегося заказа, политика
    по-прежнему просит ревьюера. Свип бежит по расписанию, и между заказом и
    его разбором задачу могли вернуть в работу, пересдать или снять политику.

    Возвращает True, когда у ЭТОГО заказа в итоге нашёлся СВОЙ отчёт (гонка с
    поздним отчётом облака — заказ обязан закрыться им как ``done``, а не
    ``failed``), и False во всех остальных случаях (дверь открыта, отказана
    или не понадобилась). Решение принимает вызывающий (``_settle_second_
    door``), потому что статус ЭТОГО заказа — его забота, а не этой функции.
    """
    if task_row is None:
        return False
    task = dict(task_row)
    generation = int(dispatch["submission_generation"])
    if task.get("status") != "review":
        return False
    if int(task.get("submission_generation") or 0) != generation:
        return False
    branch = (task.get("branch") or "").strip()
    if not branch:
        return False
    project = await repo.resolve_project_for_task(db, int(task["id"]))
    if project is None or not review_dispatch_enabled(gate_policy_of(project)):
        return False
    # Первая из ДВУХ проверок отчёта (#1252). Ещё одну, ПОСЛЕДНЮЮ, делает
    # dispatch_local_review непосредственно перед вставкой строки заказа —
    # между ЭТИМ местом и той вставкой ``open_second_door`` перечитывает
    # свежесть сдачи и достижимость, а сам заказ считает потолок стоимости и
    # готовит дифф/правила (``prepare_review_order``), и отчёт, доехавший в
    # это куда более длинное окно, эта проверка одна не увидит. Проверка
    # именно рунг-совпадением (#1025), а не «есть ли хоть какой-то отчёт
    # этого поколения»: у добора лестницы (#879) отчёт предыдущей ступени
    # законно есть, и запрет по нему закрыл бы дверь перед заказом, который
    # как раз и заказывали вторым.
    if await _dispatch_report(db, int(task["id"]), generation, dispatch) is not None:
        return True
    await open_second_door(
        db,
        task,
        project_policy.forge_of(project),
        branch,
        generation,
        # Замена обязана доехать ТЕМ ЖЕ профилем, каким заказывали упавший
        # прогон (#1252, тот же класс, что находка 7ed386a8 на #1180). Без
        # проброса dispatch_local_review считает профиль заново и на сдаче
        # низкого риска понижает добор до lite — а вместе с упавшим заказом
        # эта замена выводит счёт заходов за REVIEW_LADDER_MAX_STEPS, то есть
        # неполный отчёт lite уже НЕ сможет позвать новый deep. Лестница
        # ломается молча и в сторону более дешёвого прогона.
        #
        # Форсируется только deep: понижать замену запрещено, а навязывать
        # lite там, где сегодняшний выбор сказал бы deep, — тот же дефект
        # зеркально.
        DEEP if (dispatch.get("profile") or "").strip() == DEEP else "",
        # #1266 раунд 2 (7a78080de93938ae): пустой agent_id здесь значит
        # СИНХРОННЫЙ отказ на СОЗДАНИИ (заглушка долга из maybe_dispatch_
        # review, а не прогон, дошедший до терминального статуса) — агента и
        # рана не было вовсе, и текст «прогон... кончился статусом» был бы
        # ложью про событие, которого не случилось. run_status для заглушки
        # несёт НАБЛЮДЁННУЮ причину отказа (_lost_call_detail), а не код
        # статуса рана, и печатается как есть, без обёртки «кончился».
        cloud_refusal=_cloud_refusal_text(dispatch, run_status),
        late_report_recheck=dispatch,
    )
    # Последнее слово — уже ПОСЛЕ попытки открыть дверь, а не только до неё
    # (находка по e69a3d5). Что бы ни случилось внутри — дверь открылась,
    # отказала по конфигурации/потолку, или её остановила ПОСЛЕДНЯЯ проверка
    # внутри dispatch_local_review, — единственная правда сейчас в базе:
    # если СВОЙ отчёт этого заказа тем временем нашёлся, статус решает он, а
    # не путь, которым мы сюда пришли.
    return await _dispatch_report(db, int(task["id"]), generation, dispatch) is not None


def _cloud_refusal_text(dispatch: dict[str, Any], run_status: Any) -> str:
    """Наблюдённая причина, по которой облачный заказ отчёта не дал.

    Один автор на два места: вторая дверь (#1252) и переспрос (#1242)
    называют один и тот же сбой одними словами.
    """
    # #1266 раунд 2 (7a78080de93938ae): пустой agent_id здесь значит
    # СИНХРОННЫЙ отказ на СОЗДАНИИ (заглушка долга из maybe_dispatch_
    # review, а не прогон, дошедший до терминального статуса) — агента и
    # рана не было вовсе, и текст «прогон... кончился статусом» был бы
    # ложью про событие, которого не случилось. run_status для заглушки
    # несёт НАБЛЮДЁННУЮ причину отказа (_lost_call_detail), а не код
    # статуса рана, и печатается как есть, без обёртки «кончился».
    if not (dispatch.get("agent_id") or "").strip():
        return f"провайдер отказал в создании агента: {run_status}"
    return (
        f"прогон облачного агента {dispatch['agent_id']} "
        f"({dispatch.get('model') or 'модель не названа'}) кончился "
        f"статусом {run_status} и отчёта не оставил"
    )


# ---------------------------------------------------------------------------
# Переспрос несостоявшегося ревью (#1242)
# ---------------------------------------------------------------------------
#
# Облачный заказ, не давший отчёта, — синхронный отказ (429, 400) или
# прогон, дошедший до терминального статуса без отчёта, — до #1242 был
# концом пути, если вторая дверь (#1252) не открылась: локальный путь не
# настроен или его прогон не запустился. Сдача стояла в review без отчёта
# навсегда. Переспрос живёт в свипе, а не рядом с ним: кандидат — последний
# заказ поколения, облачный и упавший, у сдачи, которая всё ещё его ждёт.
#
# Чем он НЕ является. Не лестницей добора (#879): та поднимает профиль по
# СУЩЕСТВУЮЩЕМУ неполному отчёту, переспрос срабатывает только там, где
# отчёта этого поколения нет вовсе. Не обходом стража второй читки: повтор
# идёт через maybe_dispatch_review без force_profile, то есть через тот же
# страж; пропускает он его законно — код этой вершины никто не прочитал. И не
# второй дверью: открытая дверь делает последним заказом локальный, и
# переспрос облака её не дублирует.

#: Сколько раз облако переспрашивается на ОДНО поколение сдачи. Не на
#: задачу: пересдача — новый код и новый счёт. Два — потому что сбои, ради
#: которых это заведено (429 по лимиту, упавший ран), разовые; постоянный
#: сбой после двух повторов — вопрос к форме вызова, а не к числу попыток.
REVIEW_ASK_AGAIN_MAX = 2

#: Пауза между сбоем и переспросом, от создания упавшего заказа. Отказ 429 —
#: это «не сейчас»; переспрос в ту же минуту купил бы тот же отказ.
REVIEW_ASK_AGAIN_PAUSE_MINUTES = 10

#: Метки записей, по которым переспрос считается и находится снова. Номер
#: поколения внутри — по образцу NO_REVIEWER_MARK: счёт в пределах сдачи.
#: Запись делается ДО вызова, поэтому попытка видна и тогда, когда вызов не
#: оставил строки (слепой исход #1199, упавший процесс).
ASK_AGAIN_MARK = "[переспрос ревью: сдача {generation}]"
ASK_AGAIN_EXHAUSTED_MARK = "[переспрос ревью исчерпан: сдача {generation}]"


async def _ask_again_lost_reviews(db: aiosqlite.Connection) -> None:
    """Переспросить облако по сдачам, чей последний заказ упал без отчёта."""
    rows = await fetchall(
        db,
        "SELECT d.* FROM review_dispatches d JOIN tasks t ON t.id = d.task_id "
        "WHERE d.status = 'failed' AND d.channel = ? AND t.status = 'review' "
        "AND t.submission_generation = d.submission_generation "
        "AND d.id = (SELECT MAX(x.id) FROM review_dispatches x "
        "WHERE x.task_id = d.task_id "
        "AND x.submission_generation = d.submission_generation) "
        "AND d.created_at <= datetime('now', ?) ORDER BY d.id",
        (CLOUD_CHANNEL, f"-{REVIEW_ASK_AGAIN_PAUSE_MINUTES} minutes"),
    )
    for row in rows:
        await _ask_again(db, dict(row))


async def _ask_again(db: aiosqlite.Connection, failed: dict[str, Any]) -> None:
    task_id = int(failed["task_id"])
    generation = int(failed["submission_generation"])
    # Отчёт этого поколения есть — любой, в том числе неполный или чужой:
    # переспрос не покупается. Неполнота — предмет лестницы (#879).
    if await repo.machine_reviews_of_generation(db, task_id, generation):
        return
    mark = ASK_AGAIN_MARK.format(generation=generation)
    asked = await _count_marked_alerts(db, task_id, mark)
    retried = await _count_retry_orders(db, task_id, generation)
    cause = _cloud_refusal_text(failed, failed.get("run_status") or "не записан")
    if asked >= REVIEW_ASK_AGAIN_MAX or asked > retried:
        await _name_the_exhausted_retries(db, task_id, generation, asked, cause)
        return
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"{mark} Переспрос ревью {asked + 1} из {REVIEW_ASK_AGAIN_MAX}: заказ "
        f"#{failed['id']} отчёта не дал — {cause}. Отчёта этой сдачи нет "
        "вовсе, поэтому ревьюер зовётся снова; ступень лестницы (#879) он "
        "не съедает (#1242).",
    )
    await db.commit()
    task_row = await repo.get_task(db, task_id)
    branch = (dict(task_row).get("branch") or "").strip() if task_row else ""
    if await maybe_dispatch_review(db, task_id, replaces_dispatch_id=int(failed["id"])):
        return
    await _name_a_cancelled_retry(db, task_id, generation, branch, asked + 1)


async def _name_a_cancelled_retry(
    db: aiosqlite.Connection, task_id: int, generation: int, branch: str, n: int
) -> None:
    """Назвать переспрос, отменённый последним словом перед тратой.

    Запись о переспросе стоит ДО вызова и обещает ревьюера (находка
    2007bee302b71bf4). Два отказа последнего слова своего алерта не пишут —
    отчёт, успевший лечь, и сдача, сменившаяся за подготовку (находка
    40a8fd8b727c82ec), — поэтому их называет переспрос. Остальные отказы
    (конфигурация, политика, страж второй читки, слепой исход) диспетчер
    называет сам, и второго объяснения им здесь не заводим.
    """
    if await repo.machine_reviews_of_generation(db, task_id, generation):
        why = "отчёт этой сдачи успел лечь, пока заказ готовился"
    elif not await _submission_still_live(db, {"id": task_id}, branch, generation):
        why = "сдача сменилась, пока заказ готовился, — облако не зовётся"
    else:
        return
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        # Без метки: счёт попыток ведут записи о вызове, а не об отмене.
        f"Переспрос ревью {n} отменён: {why}; второго ревьюера не покупали (#1242).",
    )
    await db.commit()


async def _count_marked_alerts(
    db: aiosqlite.Connection, task_id: int, mark: str
) -> int:
    rows = await fetchall(
        db,
        "SELECT COUNT(*) AS n FROM task_updates WHERE task_id = ? "
        "AND kind = 'alert' AND instr(content, ?) > 0",
        (task_id, mark),
    )
    return int(dict(rows[0])["n"]) if rows else 0


async def _count_retry_orders(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> int:
    """Сколько облачных заказов поколения оставили переспросы.

    Замена второй дверью — строка другого канала и сюда не входит. Меньше
    записанных попыток — значит, какая-то попытка строки не оставила: слепой
    исход (#1199), отказ конфигурации или политики, упавший процесс.
    Повторять вслепую нельзя — это и есть «наивный повтор» #1199.
    """
    rows = await fetchall(
        db,
        "SELECT COUNT(*) AS n FROM review_dispatches WHERE task_id = ? "
        "AND submission_generation = ? AND channel = ? "
        "AND replaces_dispatch_id IS NOT NULL",
        (task_id, generation, CLOUD_CHANNEL),
    )
    return int(dict(rows[0])["n"]) if rows else 0


async def _name_the_exhausted_retries(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    asked: int,
    cause: str,
) -> None:
    """Один алерт человеку на поколение: сколько раз спросили и что сорвалось."""
    mark = ASK_AGAIN_EXHAUSTED_MARK.format(generation=generation)
    if await _count_marked_alerts(db, task_id, mark):
        return
    # Находка c82b1af523ba9dd7: попытка без строки заказа — это не всегда
    # слепой исход #1199. Отказ конфигурации, политики или стража второй
    # читки тоже не оставляет строки, и его причина уже стоит в карточке
    # алертом диспетчера. Переспрос исхода не знает — и не выдумывает его:
    # останавливается (повтор ни одного из этих исходов не меняет, а слепой
    # запрещает) и ссылается на алерт, который причину называет.
    why = (
        f"потолок {REVIEW_ASK_AGAIN_MAX} переспросов на сдачу исчерпан"
        if asked >= REVIEW_ASK_AGAIN_MAX
        else "прошлый переспрос не оставил заказа, причина — в алерте "
        "после него (конфигурация, политика, страж второй читки, сменившаяся "
        "сдача или ответ, который не дошёл, #1199); не зная исхода, хаб не "
        "повторяет"
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"{mark} Ревью этой сдачи так и не состоялось: {why}. Переспросов: "
        f"{asked}, последний упавший заказ — {cause}. Ещё одного ревьюера хаб "
        "не покупает; решение за человеком. Если сбой один и тот же — смотреть "
        "форму вызова, а не число попыток (#1242).",
    )
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
    не равно «чисто». НЕПОЛНЫЙ отчёт, не принёсший находок, — тот же
    случай, и потому он тоже пропускается: он САМ говорит, что дочитал не
    всё, а лестница добора (#879) существует ровно затем, чтобы добрать
    непрочитанное. Этот же файл уже исключает ``incomplete`` из «код
    прочитан» по той же причине. Пустой ПОЛНЫЙ отчёт круг по-прежнему
    кончает: харнесс дочитал и не нашёл ничего.
    """
    rows = await fetchall(
        db,
        "SELECT submission_generation, findings_confirmed, unresolved, incomplete "  # nosec B608 - константа модуля, не ввод
        "FROM machine_reviews WHERE task_id=? "
        # #1361: перенесённый отчёт — не новый круг: те же находки на том же
        # коде, и засчитать их кругом значило бы назвать экономию повтором.
        f"AND {ORIGINAL_READ_SQL} "
        "ORDER BY submission_generation, id",
        (task_id,),
    )
    per_generation: dict[int, list[tuple[str, str]]] = {}
    for raw in rows:
        row = dict(raw)
        found = _findings_of(row)
        if not found and bool(row.get("incomplete")):
            # Сведений ноль: отчёт не дочитал и ничего не принёс. Строка
            # остаётся в machine_reviews навсегда, поэтому, обнуляй мы по
            # ней хвост, КАЖДАЯ следующая сдача снова упиралась бы в ту же
            # пару и снова сбрасывала счёт — круг переставал бы быть
            # видимым насовсем.
            continue
        generation = int(row.get("submission_generation") or 0)
        per_generation.setdefault(generation, []).extend(found)

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


# ---------------------------------------------------------------------------
# Несходимость находок по поколениям (#1255)
# ---------------------------------------------------------------------------
#
# Круг (#1235) НАЗЫВАЕТ задачу, но прогон всё равно покупается. Здесь другой
# вопрос и другое действие: убывают ли ОТКРЫТЫЕ находки от поколения к
# поколению. Если нет — следующий прогон не покупается, а человеку задаётся
# вопрос «постановка или код» с самой последовательностью перед глазами.
# Решает человек; хаб только перестаёт платить за ответ, который уже виден.

#: Сколько поколений с отчётом правило смотрит, прежде чем судить.
#:
#: ИЗМЕРЕНО, а не выбрано: прогон правила по окнам 2..5 на 26 размеченных
#: траекториях (tests/test_review_dispatch.py::NON_CONVERGENCE_HISTORY):
#: 6 из постановки #1255 (confirmed + unresolved, замер 11.09.2026) и 20 из
#: ленты событий прода (/api/events, machine_review_completed — только
#: confirmed, 23.09.2026). Сошедшихся 22, несошедшихся 4.
#: Окно 2 останавливает зря три сошедшиеся (#1204 на плато [6,2,2,2],
#: #1265, #1271). Окно 4 и шире пропускает все три короткие несошедшиеся
#: (#1081 [5,6,6], #1084 [3,6,3], #1186 [7,2,13]). Окно 3 — единственное,
#: при котором сошедшихся остановлено зря НОЛЬ из 22, а поймано четыре из
#: четырёх, #1208 — на четвёртом поколении. Оговорка: несошедшиеся примеры
#: есть только в постановке — в ленте нет unresolved, а по одной confirmed
#: несходимость #1208 не видна. Тест
#: test_the_threshold_is_measured_on_the_history_not_chosen повторяет этот
#: прогон и падает, если число здесь разойдётся с замером.
NON_CONVERGENCE_WINDOW = 3

#: Меньше скольких открытых находок задача считается сходящейся. Тот же
#: край, что в разметке замера #1255: «убыло и осталось не больше одной» —
#: сошлась. Измерено по ленте: без него плато из единиц (#1261 [0,1,1,1,0])
#: останавливалось зря, хотя следующей сдачей задача сошлась.
NON_CONVERGENCE_FLOOR = 2

#: Исходы, которыми находка ЗАКРЫТА разбором, а не правкой: автор или
#: человек сказал «это не дефект» или «уходит в другую задачу». Такая
#: находка не открыта ни в одном поколении, где её нашли, — иначе честный
#: разбор читался бы как несходимость (AC-4). ``fixed``/``real_fixed`` сюда
#: НЕ входят намеренно: «починено» проверяет следующий отчёт, и находка,
#: найденная снова, остаётся открытой, а новый слой после починки — это и
#: есть та смена одного набора дефектов на другой, которую правило ловит.
#: ``not_judged`` — «не смотрел», это не закрытие.
DISPOSED_OUTCOMES: frozenset[str] = frozenset(
    {"false_positive", "wont_fix", "deferred", "real_deferred", "not_a_defect"}
)

#: Метка вопроса в карточке: сдача, на которой остановлен прогон, и
#: поколение постановки в этот момент. По ней вопрос находится снова, и по
#: ней же видно, ответил ли человек (_non_convergence_answered).
NON_CONVERGENCE_MARK = (
    "[несходимость находок: сдача {generation}, постановка {statement}]"
)
_NON_CONVERGENCE_MARK_RE = re.compile(
    r"\[несходимость находок: сдача (\d+), постановка (\d+)\]"
)


def _fires_at_last(counts: Sequence[int], window: int) -> bool:
    """Выполнено ли условие несходимости на ПОСЛЕДНЕЙ позиции ``counts``.

    Судится только ХВОСТ после последнего нуля: чистый отчёт — схождение, и
    находки после него — новый заход, а не продолжение старого (тот же
    приём, которым review_circle обнуляет круг на чистом отчёте). Без этого
    [0, 1, 0, 2] (#1204 по ленте событий) читался бы как «ниже нуля не
    опустилась» и останавливался зря.

    Условие стоит, когда в хвосте не меньше ``window`` отчётов, на последнем
    открытых находок НЕ МЕНЬШЕ ``NON_CONVERGENCE_FLOOR`` и верно одно из
    двух:

    * находок не меньше, чем в первом отчёте хвоста: после window−1 кругов
      правок задача не ушла ниже старта (#1081, #1084, #1186, #1208);
    * наименьший счёт не обновлялся ``window`` отчётов подряд — колебание,
      не опускающееся ниже уже достигнутого.
    """
    zeros = [i for i, count in enumerate(counts) if count == 0]
    tail = counts[zeros[-1] + 1 :] if zeros else counts
    if len(tail) < window or tail[-1] < NON_CONVERGENCE_FLOOR:
        return False
    best_at = min(range(len(tail)), key=lambda i: (tail[i], i))
    return tail[-1] >= tail[0] or len(tail) - 1 - best_at >= window


def non_convergence_point(
    counts: Sequence[int], window: int = NON_CONVERGENCE_WINDOW
) -> int | None:
    """Позиция (с 1), на которой правило срабатывает впервые, или None.

    Длинная, но убывающая с колебаниями траектория (#1167, #1204) не
    ловится: минимум она обновляет раньше, чем истечёт окно, и ниже старта
    уходит сразу.
    """
    for position in range(1, len(counts) + 1):
        if _fires_at_last(counts[:position], window):
            return position
    return None


@dataclass(frozen=True)
class FindingTrajectory:
    """Открытые находки по поколениям С ОТЧЁТОМ в хабе, по порядку."""

    generations: tuple[int, ...]
    counts: tuple[int, ...]

    def fires_now(self) -> bool:
        """Стоит ли условие на последнем известном поколении."""
        return _fires_at_last(self.counts, NON_CONVERGENCE_WINDOW)

    def sequence(self) -> str:
        """``сдача 1: 3, сдача 2: 4, …`` — то, по чему решает человек."""
        return ", ".join(
            f"сдача {g}: {c}" for g, c in zip(self.generations, self.counts)
        )


async def _disposed_uids(db: aiosqlite.Connection, task_id: int) -> set[str]:
    """Находки, закрытые разбором: исход автора или диспозиция человека."""
    marks = ",".join("?" for _ in DISPOSED_OUTCOMES)
    params = (task_id, *sorted(DISPOSED_OUTCOMES))
    rows = await fetchall(
        db,
        f"SELECT finding_uid FROM finding_outcomes WHERE task_id=? "  # nosec B608
        f"AND outcome IN ({marks}) "
        f"UNION SELECT finding_uid FROM finding_dispositions WHERE task_id=? "
        f"AND disposition IN ({marks})",
        params + params,
    )
    return {str(dict(r)["finding_uid"] or "") for r in rows} - {""}


async def finding_trajectory(
    db: aiosqlite.Connection, task_id: int, before_generation: int
) -> FindingTrajectory:
    """Счёт ОТКРЫТЫХ находок по поколениям до ``before_generation``.

    СВЁРТКА нескольких отчётов одного поколения — ОБЪЕДИНЕНИЕ по
    ``finding_uid``. Лестница (#879) кладёт на поколение lite и добор deep;
    второй часто повторяет первый. Сумма посчитала бы повтор дважды, а
    максимум спрятал бы находки, которые нашёл только один из двух, — хотя
    они такие же открытые. Объединение по uid — тот же приём, которым
    review_circle считает «пришло новых».

    НЕПОЛНЫЙ отчёт без находок пропускается: «машина не дочитала» — не
    «чисто» (#1234), и нулём, то есть схождением, он не становится.

    Поколения без отчёта в хабе (внешний ревьюер, #1252) не видны вовсе:
    правило сужено до них честно, и вопрос человеку это говорит.
    """
    rows = await fetchall(
        db,
        "SELECT mr.submission_generation, mr.findings_confirmed, mr.unresolved, "  # nosec B608 - константа модуля, не ввод
        "mr.incomplete FROM machine_reviews mr "
        "WHERE mr.task_id=? AND mr.submission_generation < ? "
        # Самоотчёт — не чтение со стороны: ни точка траектории, ни сброс её
        # хвоста чистым нулём (находка e6250c505eb42e95).
        f"AND {INDEPENDENT_READ} "
        # #1361: перенос — не точка траектории, чтения в нём не было.
        f"AND {ORIGINAL_READ_SQL} "
        "ORDER BY mr.submission_generation, mr.id",
        (task_id, before_generation),
    )
    per_generation: dict[int, set[str]] = {}
    for raw in rows:
        row = dict(raw)
        uids = {uid for uid, _ in _findings_of(row)}
        if not uids and bool(row.get("incomplete")):
            continue
        generation = int(row.get("submission_generation") or 0)
        per_generation.setdefault(generation, set()).update(uids)
    disposed = await _disposed_uids(db, task_id)
    generations = tuple(sorted(per_generation))
    return FindingTrajectory(
        generations=generations,
        counts=tuple(len(per_generation[g] - disposed) for g in generations),
    )


def non_convergence_question(trajectory: FindingTrajectory, submissions: int) -> str:
    """Текст вопроса человеку. Один на все места, где он звучит (#1188)."""
    return (
        "Следующий прогон машинного ревью НЕ заказан: открытые находки не "
        f"убывают ({trajectory.sequence()}). Вопрос человеку — постановка "
        "или код? Если находки меняют один набор дефектов на другой при "
        "настоящих правках, причина обычно в постановке (так было на "
        "#1208: ограничение, которое девять кругов никто не оспорил). "
        "Ответ — вердикт по этой сдаче или правка постановки; после него "
        "круги продолжаются. Посчитаны только сдачи с отчётом в хабе: "
        f"{len(trajectory.generations)} из {submissions}; ревью, пришедшее "
        "мимо хаба, здесь не видно. Порог "
        f"NON_CONVERGENCE_WINDOW={NON_CONVERGENCE_WINDOW} измерен на истории (#1255)."
    )


async def _non_convergence_asked(
    db: aiosqlite.Connection, task_id: int
) -> tuple[int, int] | None:
    """(сдача, постановка) из заданного вопроса, или None — не спрашивали."""
    rows = await fetchall(
        db,
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert' "
        "AND content LIKE ? ORDER BY id DESC LIMIT 1",
        (task_id, "%[несходимость находок: сдача %"),
    )
    if not rows:
        return None
    found = _NON_CONVERGENCE_MARK_RE.search(str(dict(rows[0])["content"]))
    if found is None:  # pragma: no cover - метку пишет только этот модуль
        return None
    return int(found.group(1)), int(found.group(2))


def _non_convergence_answered(task: dict[str, Any], asked: tuple[int, int]) -> bool:
    """Ответил ли человек: вердикт по остановленной сдаче или новая постановка."""
    generation, statement = asked
    return (
        int(task.get("review_verdict_generation") or 0) >= generation
        or int(task.get("statement_generation") or 0) > statement
    )


async def findings_stopped_converging(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> bool:
    """Не покупать прогон: находки не сходятся. True — прогон остановлен.

    Вопрос задаётся ОДИН раз, на достижении порога. Пока человек не
    ответил, следующие сдачи тоже стоят без прогона — молча: вопрос уже в
    карточке. После ответа правило больше не останавливает эту задачу —
    круги продолжаются, это решение человека, а не хаба.
    """
    task_id = int(task["id"])
    asked = await _non_convergence_asked(db, task_id)
    if asked is not None:
        return not _non_convergence_answered(task, asked)
    generation = int(task.get("submission_generation") or 0)
    trajectory = await finding_trajectory(db, task_id, generation)
    if not trajectory.fires_now():
        return False
    mark = NON_CONVERGENCE_MARK.format(
        generation=generation, statement=int(task.get("statement_generation") or 0)
    )
    question = non_convergence_question(trajectory, generation - 1)
    await repo.add_task_update(db, task_id, "hub", "alert", f"{question} {mark}")
    await repo.insert_event(
        db,
        kind="review_non_convergence",
        task_id=task_id,
        actor="hub",
        payload={
            "generation": generation,
            "generations": list(trajectory.generations),
            "counts": list(trajectory.counts),
            "window": NON_CONVERGENCE_WINDOW,
            "question": question,
        },
    )
    await db.commit()
    log.info(
        "task #%s: findings do not converge (%s), review run not bought",
        task_id,
        trajectory.sequence(),
    )
    return True
