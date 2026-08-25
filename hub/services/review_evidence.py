"""Where the review brief's evidence came from — and where it did not (#725).

Observed on the brief for #643 (project spike-bo, 19.08.2026). The brief said

    diff_command: git diff develop...task-643/memo-spike-impl

on a project whose default branch is ``main`` and which has no ``develop`` ref
at all. The command could not run, and everything that needs a diff went quiet
behind it: ``call_sites`` answered "the diff named no changed lines" over a
branch carrying +3260 lines in 67 files, and the risk class was recomputed
against an empty diff. Three blocks read as three independent unknowns. There
was one cause.

Beside them stood ``sha_check: match`` with an empty reason. That check is real
— it compares the branch tip against the SHA the submission pinned — but it
answers a question nobody was worried about, and a lone green line next to
three unknowns reads as "verification happened".

So this module answers two questions the brief could not answer before:

1. WHICH base, and does it exist. The base comes from the project's own
   ``default_branch`` rather than a constant, and is resolved in the project's
   workspace before the command is printed. Three outcomes, never two:
   ``resolved``, ``unresolved`` (we looked and it is not there), ``unverified``
   (there was nothing to look in). The second and third are different answers
   and are never collapsed.

2. WHICH checks actually ran. One coverage verdict over all evidence blocks,
   which states plainly how many produced a signal and what stopped the rest.
   The rule it enforces: an indicator may read confident only when the check
   behind it ran over the data it claims to cover. Partial coverage belongs in
   the verdict, not in a footnote under it.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from hub import config
from hub.integrations.registry import plugins

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hub.models import ReviewReport

log = logging.getLogger("hub")

BASE_RESOLVED = "resolved"
BASE_UNRESOLVED = "unresolved"
BASE_UNVERIFIED = "unverified"
# #947: the base resolved to a commit the remote branch does not carry. The
# name is there, git answers with a sha, every check downstream runs happily —
# and compares the branch against a base that no longer exists upstream. On
# 2026-08-24 that produced a brief with an empty diff next to sha_check=match:
# two green-looking facts, neither of them about the code. "Resolved to
# something stale" is not a shade of "resolved"; it is closer to "unresolved",
# and it is reported as its own answer because the fix is different — nobody
# needs to name a different base, somebody needs to fetch.
BASE_STALE = "stale"


def base_blocks_diff(state: str) -> bool:
    """Would a diff taken against this base mislead the reader (#947)?

    One predicate for the two states that must not silently produce sections,
    so a third state added later cannot be forgotten at one of the call sites.
    """
    return state in (BASE_UNRESOLVED, BASE_STALE)


# Prefix every block that went silent because the diff base did not resolve.
# A downstream block saying "unknown" on its own invites the reviewer to look
# for three separate causes; naming the one cause is the whole point.
DISABLED_BY_BASE = "disabled by the diff base: "

COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
COVERAGE_NONE = "none"


async def resolve_diff_base(
    db: Any, task_id: int, branch: str, *, pr_base: str = ""
) -> dict[str, Any]:
    """Which base this task's diff is taken against, and whether it exists.

    ``source`` says where the name came from, so a wrong base can be fixed at
    its origin instead of being argued about in the brief. Best effort by
    contract: any failure answers ``unverified`` with the cause — a brief must
    not become unreadable because git was unreachable.
    """
    from hub.services.orchestration import project_git_context

    out: dict[str, Any] = {
        "base": "",
        "source": "",
        "state": BASE_UNVERIFIED,
        "reason": "",
        "sha": "",
    }
    try:
        ctx = await project_git_context(db, task_id)
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("diff base for #%s: no project context: %s", task_id, exc)
        out["reason"] = f"project git context failed: {exc}"
        return out

    if (pr_base or "").strip():
        out["base"], out["source"] = pr_base.strip(), "pull request base ref"
    elif (ctx.get("base_branch") or "").strip():
        out["base"], out["source"] = (
            ctx["base_branch"].strip(),
            "project default_branch",
        )
    else:
        out["base"], out["source"] = (
            config.PAIR_BASE_BRANCH,
            "fallback default (the project declares no default_branch)",
        )

    if not (branch or "").strip():
        out["reason"] = "task has no branch, so there is no diff to take"
        return out

    workspace = ctx.get("repo")
    if not workspace:
        out["reason"] = (
            "the project has no workspace, so the base could not be verified "
            "here — run the command in your own checkout"
        )
        return out

    try:
        state, detail = await plugins.git_ops.resolve_ref(out["base"], workspace)
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("diff base for #%s: resolve failed: %s", task_id, exc)
        out["reason"] = f"could not resolve {out['base']}: {exc}"
        return out

    if state == "resolved":
        out["state"], out["sha"] = BASE_RESOLVED, detail
        # Resolved is not the end of the question (#947). The ref exists here;
        # whether it still stands for anything upstream is a second fact, and
        # the brief that skipped it showed an empty diff as agreement.
        try:
            freshness, why = await plugins.git_ops.base_freshness(
                workspace, out["base"], detail
            )
        except Exception as exc:  # noqa: BLE001 - degradation is the contract
            log.warning("diff base for #%s: freshness failed: %s", task_id, exc)
            out["reason"] = f"свежесть базы {out['base']} не проверена: {exc}"
            return out
        if freshness == "stale":
            out["state"] = BASE_STALE
            out["reason"] = (
                f"база сравнения протухла: {why}. Дифф против неё не берётся — "
                "пустой дифф здесь означал бы не «изменений нет», а «сравнивали "
                "не с тем». Клон проекта нужно сверить с remote, после чего "
                "пересобрать бриф"
            )
            return out
        if freshness != "current":
            out["reason"] = (
                f"база {out['base']} разрешена, но не сверена с remote: {why}"
            )
        return out
    if state == "missing":
        out["state"] = BASE_UNRESOLVED
        out["reason"] = (
            f"the base ref '{out['base']}' ({out['source']}) does not exist in "
            f"the project workspace: {detail}. The diff cannot be taken, so "
            "every check that reads it is disabled below — this is one failure, "
            "not several independent unknowns"
        )
        return out
    out["reason"] = f"the base ref '{out['base']}' could not be verified: {detail}"
    return out


def diff_command_for(diff_base: dict[str, Any], branch: str) -> str:
    """The command, or nothing when we know it would not run (#725).

    An unresolved base yields no command on purpose: a command that cannot run
    is worse than none, because it is read as an offer to verify.
    """
    if not (branch or "").strip():
        return ""
    if base_blocks_diff(str(diff_base.get("state") or "")):
        return ""
    return f"git diff {diff_base.get('base', '')}...{branch}"


def sha_check_statement(
    sha_check: str, submission_sha: str, current_tip: str, branch: str
) -> str:
    """What the SHA comparison actually compared (#725).

    ``match`` used to carry an empty reason. Read beside three blocks that
    produced nothing, a bare green word is taken as evidence about the code —
    while this check only knows where a branch pointer stands. Saying so is the
    difference between an honest narrow check and a misleading broad one.
    """
    if sha_check != "match":
        return ""
    return (
        f"branch {branch} still stands at the submitted commit "
        f"{(current_tip or submission_sha)[:12]}. This compares WHERE the "
        "branch points, not what the diff contains and not whether anything "
        "was verified"
    )


async def live_check_state(
    db: Any, task_id: int, *, delivered_sha: str = ""
) -> dict[str, Any]:
    """Did anyone watch this task behave after it shipped (#814, feature #811).

    Three states, never two. "Nobody looked" and "there was nothing to look at"
    are different claims, and collapsing them is how a brief starts reassuring
    about work it never examined — the failure #725 catalogued for the blocks
    around this one.

    The newest record wins, but a record taken against another build is still
    reported: the observation happened, it simply does not speak for what was
    delivered. Saying that out loud is the same rule sha_check applies to a
    submission.
    """
    from hub import repository as repo

    rows = await repo.list_live_checks(db, task_id, limit=1)
    if not rows:
        return {
            "state": "unknown",
            "reason": (
                "живая проверка не записывалась: поведение в проде никто не наблюдал"
            ),
            "delivered_sha": delivered_sha or "",
        }
    row = dict(rows[0])
    outcome = row.get("outcome") or "done"
    sha = row.get("sha") or ""
    mismatch = bool(delivered_sha and sha and sha != delivered_sha)
    reason = row.get("reason") or ""
    if outcome == "not_applicable":
        return {
            "state": "not_applicable",
            "reason": reason or "наблюдаемой поверхности нет",
            "sha": sha,
            "delivered_sha": delivered_sha or "",
            "sha_mismatch": mismatch,
            "recorded_agent": row.get("recorded_agent") or "",
            "created_at": row.get("created_at") or "",
        }
    return {
        "state": "done",
        "reason": (
            f"свидетельство снято на другом коммите ({sha[:12]}), "
            f"а доставлен {delivered_sha[:12]} — оно не говорит о раскатанном"
            if mismatch
            else ""
        ),
        "probe": row.get("probe") or "",
        "observation": row.get("observation") or "",
        "sha": sha,
        "delivered_sha": delivered_sha or "",
        "sha_mismatch": mismatch,
        "recorded_agent": row.get("recorded_agent") or "",
        "created_at": row.get("created_at") or "",
    }


def evidence_coverage(
    *,
    diff_base: dict[str, Any],
    branch: str,
    call_sites_status: str,
    has_test_acs: bool,
    locator_resolution: list[Any],
    ac_test_results: list[Any],
    ci_state: str,
    freshness: dict[str, Any] | None,
    sha_check: str,
    live_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One verdict over every evidence block in the brief (#725).

    Scored over a full day of briefs, the blocks read as five independent
    findings when they were one absence: four produced no signal at all, one
    reassured wrongly, and one was correct about a question nobody asked. The
    reviewer's own summary has to say that, in the same place the green words
    are, or the next reader repeats the inference.

    Three outcomes per check, not two. "Did not run" and "had nothing to run
    over" are different, and a task with no test-AC must not be reported as
    having lost evidence it never had — an inflated warning is muted, and then
    the real one is muted with it.

    ``sha_check`` is deliberately not counted as coverage: it is real, but it
    answers where a branch points. Letting it lift the verdict is exactly the
    arithmetic that made one green line look like an analysis.
    """
    ran: list[str] = []
    missing: list[dict[str, str]] = []
    not_applicable: list[dict[str, str]] = []

    def _note(name: str, ok: bool, reason: str, *, applicable: bool = True) -> None:
        if not applicable:
            not_applicable.append({"check": name, "reason": reason})
        elif ok:
            ran.append(name)
        else:
            missing.append({"check": name, "reason": reason})

    base_state = diff_base.get("state")
    has_branch = bool((branch or "").strip())
    _note(
        "diff_base",
        base_state == BASE_RESOLVED,
        diff_base.get("reason") or "the diff base did not resolve",
        applicable=has_branch,
    )
    _note(
        "call_sites",
        call_sites_status == "analysed",
        (
            DISABLED_BY_BASE + str(diff_base.get("reason") or "")
            if base_blocks_diff(str(base_state or ""))
            else "the call-site analysis produced no signal"
        ),
        applicable=has_branch,
    )
    _note(
        "locator_resolution",
        bool(locator_resolution)
        and all(getattr(r, "status", "") != "unknown" for r in locator_resolution),
        (
            "no AC locator was resolved against a real test"
            if has_test_acs
            else "no acceptance criterion is verifiable by a test"
        ),
        applicable=has_test_acs,
    )
    _note(
        "ac_test_results",
        bool(ac_test_results),
        (
            "no test result was recorded for this submission"
            if has_test_acs
            else "no acceptance criterion is verifiable by a test"
        ),
        applicable=has_test_acs,
    )
    _note("ci_run_report", ci_state == "current", "no run evidence for this commit")
    # #814: the most expensive class of check — did anyone watch it behave in
    # production. Counted here rather than shown beside the count, because a
    # block outside the counter leaves the headline lying in the reassuring
    # direction: it would say "5 of 6 blocks" while a seventh question went
    # unasked. A mismatched sha counts as no signal: the observation is real,
    # but not about what shipped.
    #
    # Applicable only once something HAS shipped. A brief is normally read
    # before delivery, and demanding evidence that could not exist yet is the
    # inflated warning this function was written to avoid (#534): it would fire
    # on every first review and be muted within a day, taking the real ones
    # with it. Evidence recorded anyway is always counted — someone looked.
    live = live_check or {}
    live_state = live.get("state") or "unknown"
    delivered = bool((live.get("delivered_sha") or "").strip())
    _note(
        "live_check",
        live_state == "done" and not live.get("sha_mismatch"),
        live.get("reason") or "поведение в проде никто не наблюдал",
        applicable=(
            live_state != "not_applicable" and (delivered or live_state == "done")
        ),
    )
    freshness_state = (freshness or {}).get("state") or "not_checked"
    _note(
        "statement_freshness",
        freshness_state in ("deliveries_since", "no_overlap"),
        (freshness or {}).get("reason")
        or "the statement was not compared against later deliveries",
    )

    if not missing:
        state, headline = (
            COVERAGE_COMPLETE,
            "every applicable evidence block produced a signal",
        )
    elif not ran:
        state = COVERAGE_NONE
        headline = (
            f"NO evidence block produced a signal ({len(missing)} of "
            f"{len(missing)} could not run). Nothing here has been verified — "
            "read this brief as a list of what to check yourself"
        )
    else:
        state = COVERAGE_PARTIAL
        headline = (
            f"{len(missing)} of {len(ran) + len(missing)} evidence blocks "
            "produced no signal. Their subjects are unverified, not clean"
        )
    if (
        state != COVERAGE_COMPLETE
        and base_blocks_diff(str(base_state or ""))
        and (has_branch)
    ):
        headline += (
            "; the diff base did not resolve, which disabled the checks "
            "that read the diff"
            if base_state == BASE_UNRESOLVED
            else (
                "; the diff base is stale — it names a commit the remote base "
                "branch does not carry, so the diff was not taken and an empty "
                "one must not be read as agreement"
            )
        )
    if sha_check == "match":
        headline += (
            ". sha_check=match is not part of this count: it says the branch "
            "has not moved since submission, nothing about the code"
        )
    return {
        "state": state,
        "headline": headline,
        "checks_ran": ran,
        "checks_missing": missing,
        "checks_not_applicable": not_applicable,
    }


# What a passing check proves, in words the reviewer can act on (#875). The
# map is deliberately narrow and per-tool: the grant is "ruff found nothing",
# never "style is fine". A tool that is not here grants nothing, which is the
# safe direction — an over-broad promise silences a real defect.
_CHECK_MEANING: dict[str, str] = {
    "lint": "стиль и простые дефекты, которые ловит ruff",
    "format": "форматирование",
    "types": "несоответствия типов, которые ловит mypy",
    "tests": "объявленные тесты репозитория",
    "security": "шаблоны небезопасного кода, которые ловит bandit",
    "audit": "известные уязвимости зависимостей",
}


async def prepass_state(db, task: dict):
    """Which deterministic checks passed on the commit under review (#875).

    Three answers, never two. The distinction that matters here is between "no
    check ran" and "a check ran and found nothing": only the second may buy the
    reviewer's silence on a class, and collapsing them would hand out that
    silence for free — the same substitution #549 removed from coverage.
    """
    from hub import repository as repo_module
    from hub.models import PrepassState
    from hub.services.ci_report import CHECK_FAIL, CHECK_PASS, CHECK_SKIPPED

    pinned = (task.get("submission_sha") or "").strip()
    if not pinned:
        return PrepassState(
            state="unknown",
            reason="коммит сдачи не закреплён — предпас не с чем сверить",
        )
    row = await repo_module.get_ci_run_report(db, int(task.get("id") or 0), pinned)
    if row is None:
        return PrepassState(
            state="unknown",
            head_sha=pinned,
            reason=(
                f"CI не присылал отчёт о прогоне для коммита {pinned[:12]} — "
                "какие проверки прошли, неизвестно"
            ),
        )
    try:
        checks = json.loads(dict(row).get("checks") or "{}")
    except ValueError:
        checks = {}
    if not isinstance(checks, dict) or not checks:
        return PrepassState(
            state="unknown",
            head_sha=pinned,
            reason=(
                f"отчёт о коммите {pinned[:12]} есть, но он не назвал ни одной "
                "проверки — считать их пройденными нельзя"
            ),
        )
    passed = sorted(k for k, v in checks.items() if v == CHECK_PASS)
    failed = sorted(k for k, v in checks.items() if v == CHECK_FAIL)
    skipped = sorted(k for k, v in checks.items() if v == CHECK_SKIPPED)
    return PrepassState(
        # A failed check is louder than a passing one: it means the code under
        # review is known-broken in a way a tool already proved, and the
        # reviewer should be told rather than left to rediscover it.
        state="failed" if failed else ("covered" if passed else "unknown"),
        reason=(
            f"упали: {', '.join(failed)}"
            if failed
            else ("" if passed else "все названные проверки пропущены")
        ),
        passed=passed,
        failed=failed,
        skipped=skipped,
        head_sha=pinned,
    )


def prepass_block(state) -> str:
    """The prepass as the reviewer reads it, in its prompt (#875)."""
    if state.state == "unknown":
        return (
            "ДЕТЕРМИНИРОВАННЫЙ ПРЕДПАС: данных нет — "
            f"{state.reason}. Ничего не считай проверенным: ищи как обычно."
        )
    lines: list[str] = []
    if state.passed:
        covered = ", ".join(
            f"{name} ({_CHECK_MEANING[name]})" if name in _CHECK_MEANING else name
            for name in state.passed
        )
        lines.append(
            "ДЕТЕРМИНИРОВАННЫЙ ПРЕДПАС на этом же коммите ПРОШЁЛ: "
            f"{covered}. НЕ трать проход на эти классы — инструмент уже "
            "доказал их отсутствие, и находка такого класса будет ложной. "
            "Всё остальное ищи как обычно: список говорит, что проверено "
            "инструментом, а не что дефектов больше нет."
        )
    if state.failed:
        lines.append(
            f"ВНИМАНИЕ, проверки УПАЛИ: {', '.join(state.failed)}. Код под "
            "ревью уже сломан по этим проверкам — это факт для отчёта, а не "
            "твоя находка."
        )
    if state.skipped:
        lines.append(f"Пропущены (ничего не доказывают): {', '.join(state.skipped)}.")
    return " ".join(lines)


async def attach_dispositions(db, view) -> None:
    """Fill a report view with what the gate said its findings turned out to be.

    Lives here because BOTH builders need it — the card (``review_report``) and
    the brief (``build_review_brief``). #808 already learned that two readers
    assembling the same report separately drift apart; a second copy of this
    read would be that mistake again.

    An empty list means nobody judged the findings. It never means they were
    all fine (#549).
    """
    from hub import repository as repo_module
    from hub.models import FindingDispositionView

    view.dispositions = [
        FindingDispositionView(**dict(row))
        for row in await repo_module.list_finding_dispositions(db, view.id)
    ]


async def review_report(
    db: Any, task_row: dict[str, Any], mr_row: Any = None
) -> "ReviewReport":
    """The verdict gate's report, built once for both its readers (#808).

    The human at the gate and the reviewing agent must see the same thing;
    building it twice is how two renderings of one fact start to disagree.

    Everything unreadable is said out loud rather than defaulted: no report
    for this submission is ``state='none'`` (not an empty panel), a report of
    an earlier submission is ``state='stale'`` (not a current one), and a
    diff volume that could not be measured is None with a reason (not zero,
    which would claim the branch changed nothing — #518).
    """
    from hub.models import MachineReviewView, ReviewReport

    generation = task_row.get("submission_generation") or 0
    machine_review = None
    state = "none"
    if mr_row is not None:
        machine_review = MachineReviewView(**dict(mr_row))
        machine_review.is_current = machine_review.submission_generation == generation
        await attach_dispositions(db, machine_review)
        state = "current" if machine_review.is_current else "stale"

    branch = (task_row.get("branch") or "").strip()
    report = ReviewReport(
        state=state,
        branch=branch,
        submission_sha=(task_row.get("submission_sha") or "").strip(),
        machine_review=machine_review,
    )

    if not branch:
        report.diff_note = "у задачи нет ветки — объём диффа не измерялся"
        return report
    try:
        from hub import services

        ctx = await services.project_git_context(db, task_row["id"])
        workspace = ctx.get("repo")
        base = ctx.get("base_branch") or config.PAIR_BASE_BRANCH
        if not workspace:
            report.diff_note = "у проекта нет workspace — объём диффа не измерялся"
            return report
        diff = await plugins.git_ops.branch_diff(workspace, base, branch)
    except Exception as exc:  # noqa: BLE001 - advisory block, never fatal
        log.warning("review report diff failed for task #%s: %s", task_row["id"], exc)
        report.diff_note = f"объём диффа прочитать не удалось: {exc}"
        return report

    if diff is None:
        report.diff_note = (
            f"объём диффа прочитать не удалось: {branch} против {base} не сравнился"
        )
        return report

    files: set[str] = set()
    lines = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.add(line[6:])
        elif line.startswith("--- a/") and line[6:] != "dev/null":
            files.add(line[6:])
        elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            lines += 1
    report.diff_files = len(files)
    report.diff_lines = lines
    return report
