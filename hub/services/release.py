"""Carrying develop into main by policy instead of by hand (#812).

The delivery gate (#605) merges a task's branch into develop on its own. The
last step — open a release pull request, wait for CI, merge it, wait for the
deploy job — stayed manual, and on 21.08.2026 one session repeated it six
times in an afternoon. No decision is taken in those four steps: the content
was approved task by task, and the CI run repeats what was already green on
the branch. What the repetition does add is a place to forget: twice that day
a task was called delivered while its deploy was still running.

Two facts learned from those releases are built in here rather than left to
whoever presses the button:

1. **A release carries develop whole.** It takes other sessions' work with it —
   that happened twice in one day, in both directions. So the body lists every
   task in the range, not the one that triggered it. A release note naming one
   task while shipping three is a record that lies.

2. **Order between tasks is not understood here.** #806 (which removes an
   exemption from review) and #807 (which makes review cheap) had to ship in
   that order, and they did — by luck. Until dependencies are first-class
   (#478) this module stays a conveyor: it never claims to know what should go
   first, which is why the policy defaults to manual.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import aiosqlite

from hub import config
from hub import git_policy
from hub import repository as repo
from hub.db import log_activity
from hub.integrations.protocols import CIProbeOutcome, MergeabilityOutcome
from hub.integrations.registry import plugins
from hub.services.project_policy import (
    base_branch_of,
    gate_policy_of,
    release_auto_enabled,
    release_base_of,
)

log = logging.getLogger("hub")

# The delivery gate writes «feat(task): <slug> (#NNN)», so the numbers can be
# read back out of the range. A subject without a number is still shown — the
# release carries it either way, and hiding it would repeat the lie this
# module exists to stop.
_TASK_NUMBER = re.compile(r"\(#(\d+)\)\s*$")


def release_body(
    subjects: list[str],
    head: str = "",
    base: str = "",
) -> tuple[str, list[int]]:
    """The body of the release PR, and the task ids it carries.

    ``head``/``base`` are named rather than assumed (#475): the record must
    say which branches this release actually moved, and on a project that is
    not the hub they are not develop and main.
    """
    task_ids: list[int] = []
    lines: list[str] = []
    for subject in subjects:
        match = _TASK_NUMBER.search(subject)
        if match:
            task_ids.append(int(match.group(1)))
        lines.append(f"- {subject}")
    head = head or config.PAIR_BASE_BRANCH
    base = base or config.RELEASE_BRANCH
    header = (
        f"Релиз собран политикой проекта (#812): {head} → {base} целиком.\n\n"
        "Всё, что уедет этим релизом:\n"
    )
    footer = (
        "\n\nПорядок задач между собой политика не понимает — до depends_on "
        f"(#478) это тупой конвейер. Деплой по-прежнему делает CI на мерже в "
        f"{base}.\n"
    )
    return header + "\n".join(lines) + footer, task_ids


async def _git_context(db: aiosqlite.Connection, task_id: int) -> dict[str, Any]:
    from hub.services.orchestration import project_git_context

    return await project_git_context(db, task_id)


async def open_release_for_task(db: aiosqlite.Connection, task_id: int) -> int | None:
    """Open or refresh the release PR after this task landed in develop.

    Returns the PR number, or None when the project releases by hand, when
    the range is empty, or when GitHub could not be asked — each of which is
    a reason to do nothing, never a reason to fail the done report that
    called this.
    """
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None or not release_auto_enabled(gate_policy_of(project)):
        return None

    ctx = await _git_context(db, task_id)
    base = release_base_of(project)
    head = ctx.get("base_branch") or base_branch_of(project)
    pr_number, subjects, task_ids, reason = await open_release_for_range(
        ctx, base, head
    )
    if reason:
        # A cause, not a failure: the done report that called this must not
        # fail because the release could not be prepared.
        log.warning("release for #%s: %s", task_id, reason)
        return None
    if pr_number:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "status",
            _release_note(pr_number, head, base, subjects, task_ids),
        )
        await db.commit()
    return pr_number


def _release_note(
    pr_number: int, head: str, base: str, subjects: list[str], task_ids: list[int]
) -> str:
    """What a task's feed says when the release carrying it is opened."""
    return (
        f"Релиз готовится: PR #{pr_number} ({head} → {base}) несёт "
        f"{len(subjects)} коммит(ов), задачи: "
        + (", ".join(f"#{t}" for t in task_ids) or "номера не распознаны")
    )


async def open_release_for_range(
    ctx: dict[str, Any], base: str, head: str
) -> tuple[int | None, list[str], list[int], str]:
    """Open or refresh the release PR carrying ``head`` over ``base``.

    Returns ``(pr_number, subjects, task_ids, reason)``. Nothing to release —
    the two ends coincide, or the range is empty — is ``(None, [], [], "")``:
    silence, because the poller walks this every cycle and a line per cycle is
    how a real signal gets muted (#534). A non-empty ``reason`` is a failure
    to name once: the range could not be read, or GitHub refused the PR.

    No task id enters here on purpose (#931). "The base branch is ahead of the
    release branch" is a state of the project, not an event of a task, and the
    poller reaches this path with no task to blame — while delivery, which
    does have one, keeps writing the record to it.
    """
    # #475: both ends of the release come from the project. head is the branch
    # work lands on (default_branch), base is where releases land
    # (default_branch_policy.release_base). A project whose two ends coincide —
    # spike-bo delivers straight to main — has nothing to release, and opening
    # a main→main PR is a failure, not a release.
    if head == base:
        return None, [], [], ""

    # #968: the range is not the question. A squash release writes a NEW
    # commit on the release branch instead of carrying the originals, so
    # base..head keeps every commit ever released and never empties. Counting
    # them read as undelivered work forever: on 26.08.2026 the poller opened
    # twenty release PRs in ninety minutes, nineteen empty, each redeploying
    # production. Ask about content instead, and keep the three answers apart —
    # "could not compare" is named, never mistaken for "all delivered" (#725).
    differs = await plugins.git_ops.content_differs(
        base, head, repo=ctx.get("repo"), gh_repo=ctx.get("gh_repo")
    )
    if differs is None:
        return (
            None,
            [],
            [],
            f"нечего ли релизить {head} → {base}, выяснить не удалось: "
            "git не сравнил содержимое веток",
        )
    if not differs:
        return None, [], [], ""

    try:
        subjects = await plugins.git_ops.release_range(
            base, head, repo=ctx.get("repo"), gh_repo=ctx.get("gh_repo")
        )
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure
        return None, [], [], f"диапазон релиза {head} → {base} не прочитан: {exc}"
    if not subjects:
        return None, [], [], ""

    body, task_ids = release_body(subjects, head, base)
    title = f"release: {len(task_ids) or len(subjects)} задач(и) из {head} в {base}"
    try:
        pr_number = await plugins.git_ops.open_release_pr(
            base,
            head,
            title,
            body,
            repo=ctx.get("repo"),
            gh_repo=ctx.get("gh_repo"),
        )
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure
        return None, subjects, task_ids, f"релизный PR {head} → {base} не открыт: {exc}"
    if not pr_number:
        return (
            None,
            subjects,
            task_ids,
            f"релизный PR {head} → {base} не открыт: GitHub отказал",
        )
    return pr_number, subjects, task_ids, ""


async def merge_ready_release(
    db: aiosqlite.Connection, project_row: Any
) -> tuple[bool, str]:
    """Merge the open release PR of this project when CI is green (#812).

    Returns ``(merged, reason)``. A red or missing CI is a reason, reported
    once — the poller walks this every cycle, and a line per cycle is how a
    real signal gets muted (#534).

    When there is no open release PR and the base branch is ahead, this opens
    one (#931): the release is a state of the project, not an event of some
    task's delivery.
    """
    project = dict(project_row)
    if not release_auto_enabled(gate_policy_of(project_row)):
        return False, ""
    # The project row names the local clone in workspace_path and the GitHub
    # repository in repo — the same two keys project_git_context reads.
    ctx = {
        "repo": (project.get("workspace_path") or "").strip() or None,
        "gh_repo": (project.get("repo") or "").strip() or None,
    }
    base = release_base_of(project_row)
    head = base_branch_of(project_row)
    if head == base:
        return False, ""
    try:
        pr_number = await plugins.git_ops.pr_for_branch(
            head, repo=ctx["repo"], gh_repo=ctx["gh_repo"]
        )
        if not pr_number:
            return await _open_release_for_tail(db, project_row, ctx, base, head)
        ci = await plugins.git_ops.check_pr_ci(
            pr_number, repo=ctx["repo"], gh_repo=ctx["gh_repo"]
        )
        if ci.outcome != CIProbeOutcome.passed:
            return False, (
                f"релизный PR #{pr_number} не смержен: ci_{ci.outcome.value} "
                f"({ci.reason})"
            )
        # #970: green CI is not permission to merge. PR #83 held both answers
        # at once — checks passed, mergeable=CONFLICTING — and asking only the
        # first one meant the poller learned the second by being refused, and
        # could report it only as «GitHub отказал». That sentence names who
        # said no; a conflict, a revoked token and a deleted base branch all
        # produce it and none is fixed the same way.
        state, why = await plugins.git_ops.check_pr_mergeable(
            pr_number, repo=ctx["repo"], gh_repo=ctx["gh_repo"]
        )
        if state != MergeabilityOutcome.mergeable:
            # Never merged on anything but a definite yes. "Not computed yet"
            # is the ordinary state of a release PR seconds after the poller
            # opened it, so it is reported as itself and asked again next
            # cycle — reading it as a conflict would cry wolf on every
            # release, and a muted guard misses the real one (#725).
            return False, (
                f"релизный PR #{pr_number} не смержен: {state.value} ({why})"
            )
        merged = await plugins.git_ops.merge_pr(
            pr_number,
            0,
            f"release {head} → {base}",
            repo=ctx["repo"],
            gh_repo=ctx["gh_repo"],
            # #949: the head of a release PR IS the integration branch. The
            # default deletes the head — right for task branches, and the very
            # act that removed develop on every auto-release of 24–25.08.
            delete_branch=False,
        )
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure
        return False, f"релиз не удалось провести: {exc}"
    if not merged:
        return False, f"релизный PR #{pr_number} не смержен: GitHub отказал"
    await _stamp_released_merges(db, project_row, pr_number, ctx)
    note = await _keep_the_integration_branch(db, project_row, head, base, ctx)
    # After the stamp, never before: #950 marks every UNRELEASED merge as
    # carried by this release, and the return commit was not — it rides out
    # with the next one. Recording it earlier would stamp it with a release
    # that did not contain it.
    back = await _return_the_release(db, project_row, head, base, ctx, pr_number)
    return True, f"релиз PR #{pr_number} смержен в {base}{note}{back}"


async def _open_release_for_tail(
    db: aiosqlite.Connection,
    project_row: Any,
    ctx: dict[str, Any],
    base: str,
    head: str,
) -> tuple[bool, str]:
    """Open the release PR for what already lies in the base branch (#931).

    Until this, a release was an event of delivery: only the end of a
    successful done report opened the pull request. Everything that landed in
    the base branch without a delivery after it waited for the next task to be
    delivered and rode out with it, unmentioned — the tail that existed the
    moment release=auto was switched on (#927: #929 and #879 sat there), and
    anything left behind by an open that failed once. The poller walks every
    project every cycle anyway; here it makes the promise the owner switched
    auto on for: there is something unshipped — there is a release PR.

    Never merges in the same pass. The PR was created seconds ago and its CI
    has not started; the next cycle judges it like any other release.
    """
    pr_number, subjects, task_ids, reason = await open_release_for_range(
        ctx, base, head
    )
    if reason:
        return False, reason
    if not pr_number:
        return False, ""
    slug = dict(project_row).get("slug") or "?"
    log.info(
        "release: %s — PR #%s открыт обходом поллера (%s → %s), %d коммит(ов), "
        "задачи: %s",
        slug,
        pr_number,
        head,
        base,
        len(subjects),
        ", ".join(f"#{t}" for t in task_ids) or "номера не распознаны",
    )
    await _note_release_opened(
        db, project_row, pr_number, head, base, subjects, task_ids
    )
    return False, ""


async def _note_release_opened(
    db: aiosqlite.Connection,
    project_row: Any,
    pr_number: int,
    head: str,
    base: str,
    subjects: list[str],
    task_ids: list[int],
) -> None:
    """Record a release nobody's delivery triggered (#931).

    Opening by delivery has a task to write to — the one that triggered it.
    Here there is no trigger, so the note goes to every task the range names,
    and to the activity feed, which is where #962 put the release policy so it
    stops being visible only to whoever reads server logs. A number that is not
    this project's task — a commit carrying a number from another repository —
    is skipped rather than annotated with someone else's release.

    Best effort by contract: the PR is open either way, and a release must not
    read as failed because its bookkeeping did not land.
    """
    note = _release_note(pr_number, head, base, subjects, task_ids)
    project = dict(project_row)
    slug = project.get("slug") or "?"
    written = 0
    try:
        for task_id in task_ids:
            row = await repo.get_task(db, task_id)
            if row is None or dict(row).get("project_id") != project.get("id"):
                continue
            await repo.add_task_update(db, task_id, "hub", "status", note)
            written += 1
        await log_activity(
            db,
            "release",
            f"{slug}: релизный PR #{pr_number} открыт обходом ({head} → {base})",
            f"{len(subjects)} коммит(ов) без новой доставки; задачи в ленте: "
            f"{written} из {len(task_ids)}",
        )
        await db.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must not fail the release
        log.exception("release PR #%s opened, but not recorded", pr_number)


async def _stamp_released_merges(
    db: aiosqlite.Connection, project_row: Any, pr_number: int, ctx: dict[str, Any]
) -> None:
    """Tie every merge this release carried to the release's own commit (#950).

    Ancestry does not survive this flow: the release is a squash, and the
    integration branch may later be recreated from the release branch — both
    cut the line between a task's merge and what production runs. The stamp
    written here is the fact delivery_state falls back to when git can no
    longer answer, and this is the one moment it is certain: a release takes
    the base branch whole (#812), so everything unreleased goes with it.

    Best effort by contract: a release that shipped must not read as failed
    because its bookkeeping did not.
    """
    try:
        release_sha = await plugins.git_ops.merge_commit_sha(
            pr_number, repo=ctx.get("repo"), gh_repo=ctx.get("gh_repo")
        )
        if not release_sha:
            log.warning(
                "release #%s: merge commit unknown, merges left unstamped", pr_number
            )
            return
        stamped = await repo.mark_merges_released(
            db,
            project_id=int(dict(project_row)["id"]),
            release_pr=pr_number,
            release_sha=release_sha,
        )
        if stamped:
            log.info(
                "release #%s: stamped %d merge(s) as released at %s",
                pr_number,
                stamped,
                release_sha[:12],
            )
    except Exception:  # noqa: BLE001 - bookkeeping must not fail the release
        log.exception("release #%s: could not stamp released merges", pr_number)


async def _return_the_release(
    db: aiosqlite.Connection,
    project_row: Any,
    head: str,
    base: str,
    ctx: dict[str, Any],
    pr_number: int,
) -> str:
    """Give the release branch back to the branch it came from (#969).

    A squash release writes a new commit on the release branch that the
    integration branch does not contain, so the two drift apart by one commit
    per release — and nothing ever closed the gap. On 26.08.2026 five of them
    collided: release PR #83 stood conflicted in ``hub/db.py`` at green CI
    with 13 tasks undelivered, and the same manual repair had already been
    done twenty hours earlier (PR #36, then PR #85). Two identical hand
    operations in a day are a missing conveyor step.

    Reported next to the release, never instead of it. The release happened —
    the code is in production — and a failure here is a separate named cause,
    because letting it read as a failed release would send a task whose code
    is already deployed back for fixes.
    """
    try:
        state, detail = await plugins.git_ops.return_release_into_base(
            base, head, repo=ctx.get("repo"), gh_repo=ctx.get("gh_repo")
        )
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure
        log.warning("release: could not return %s into %s: %s", base, head, exc)
        return f"; возврат {base} в {head} не выполнен: {exc}"

    if state == "nothing":
        # The common case once this works: the poller walks here every cycle,
        # and a line per cycle is how a real signal gets muted (#534).
        return ""
    slug = dict(project_row).get("slug") or "?"
    if state == "returned":
        # Drift-guard judges by SHA: a commit on the base branch is expected
        # only when the hub recorded producing it (#534). An unrecorded return
        # would raise an alert about the hub's own merge on every release —
        # trading a hand-made rule violation for an automated one.
        try:
            await repo.record_pipeline_merge(
                db,
                pr_number=pr_number,
                merge_sha=detail,
                project_id=int(dict(project_row)["id"]),
            )
        except Exception:  # noqa: BLE001 - bookkeeping must not fail a release
            log.exception("release: return %s recorded nowhere", detail[:12])
        await log_activity(
            db,
            "release",
            f"{slug}: {base} возвращён в {head} после релиза",
            f"merge {detail[:12]}; расхождение закрыто в тот же момент, "
            "пока слияние тривиально",
        )
        return f"; {base} возвращён в {head} ({detail[:12]})"

    summary = (
        f"{slug}: возврат {base} в {head} не прошёл — конфликт"
        if state == "conflict"
        else f"{slug}: возврат {base} в {head} не проверен"
    )
    await log_activity(db, "release", summary, detail)
    return f"; возврат {base} в {head} не выполнен: {detail}"


async def _keep_the_integration_branch(
    db: aiosqlite.Connection,
    project_row: Any,
    head: str,
    base: str,
    ctx: dict[str, Any],
) -> str:
    """A release must not leave the project without a branch to deliver into (#947).

    24.08.2026 the release of this very hub merged develop into main and ended
    with develop gone from the repository. Nothing was broken in a way anything
    reported: the merge succeeded, the deploy ran, the project card said the
    clone agreed with the project. For a day no pull request could be opened
    (GitHub refuses a missing base) and no task could be pair-started.

    Restoring is safe by construction at exactly this moment: the merge just
    put the integration branch's content into the release branch, so the two
    agree, and the branch comes back pointing at the same tree it had. It is
    recorded in the activity feed either way, because a branch resurrected in
    silence is how the next deletion goes unexamined — and deletion may well
    have been deliberate.
    """
    try:
        state, detail = await plugins.git_ops.ensure_remote_branch(
            head, base, repo=ctx.get("repo"), gh_repo=ctx.get("gh_repo")
        )
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure
        log.warning("release: could not check branch %s: %s", head, exc)
        return f"; жива ли ветка {head}, проверить не удалось: {exc}"

    if state == "present":
        return ""
    slug = dict(project_row).get("slug") or "?"
    if state == "restored":
        # The clone check caches its answers for a minute (#947); the hub knows
        # better than its own cache here, so it drops it rather than letting a
        # card show a branch as missing right after restoring it.
        git_policy.forget_remote_presence(
            (dict(project_row).get("workspace_path") or "").strip(), head
        )
        summary = (
            f"{slug}: ветка {head} исчезла при релизе в {base} и восстановлена "
            f"от {base}"
        )
        await log_activity(
            db,
            "release",
            summary,
            f"restored {head} at {detail[:12]} from {base}",
        )
        return f"; {summary}"
    await log_activity(
        db,
        "release",
        f"{slug}: жива ли ветка {head} после релиза — неизвестно",
        detail,
    )
    return f"; состояние ветки {head} после релиза не проверено: {detail}"
