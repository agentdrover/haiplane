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
from hub import repository as repo
from hub.integrations.protocols import CIProbeOutcome
from hub.integrations.registry import plugins
from hub.services.project_policy import release_auto_enabled, gate_policy_of

log = logging.getLogger("hub")

# The delivery gate writes «feat(task): <slug> (#NNN)», so the numbers can be
# read back out of the range. A subject without a number is still shown — the
# release carries it either way, and hiding it would repeat the lie this
# module exists to stop.
_TASK_NUMBER = re.compile(r"\(#(\d+)\)\s*$")


def release_body(subjects: list[str]) -> tuple[str, list[int]]:
    """The body of the release PR, and the task ids it carries."""
    task_ids: list[int] = []
    lines: list[str] = []
    for subject in subjects:
        match = _TASK_NUMBER.search(subject)
        if match:
            task_ids.append(int(match.group(1)))
        lines.append(f"- {subject}")
    header = (
        "Релиз собран политикой проекта (#812): develop → main целиком.\n\n"
        "Всё, что уедет этим релизом:\n"
    )
    footer = (
        "\n\nПорядок задач между собой политика не понимает — до depends_on "
        "(#478) это тупой конвейер. Деплой по-прежнему делает CI на мерже в "
        "main.\n"
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
    base = config.RELEASE_BRANCH
    head = ctx.get("base_branch") or config.PAIR_BASE_BRANCH
    try:
        subjects = await plugins.git_ops.release_range(
            base, head, repo=ctx.get("repo"), gh_repo=ctx.get("gh_repo")
        )
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure
        log.warning("release range unavailable for #%s: %s", task_id, exc)
        return None
    if not subjects:
        return None

    body, task_ids = release_body(subjects)
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
    except Exception as exc:  # noqa: BLE001
        log.warning("release PR not opened for #%s: %s", task_id, exc)
        return None
    if pr_number:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "status",
            f"Релиз готовится: PR #{pr_number} ({head} → {base}) несёт "
            f"{len(subjects)} коммит(ов), задачи: "
            + (", ".join(f"#{t}" for t in task_ids) or "номера не распознаны"),
        )
        await db.commit()
    return pr_number


async def merge_ready_release(
    db: aiosqlite.Connection, project_row: Any
) -> tuple[bool, str]:
    """Merge the open release PR of this project when CI is green (#812).

    Returns ``(merged, reason)``. A red or missing CI is a reason, reported
    once — the poller walks this every cycle, and a line per cycle is how a
    real signal gets muted (#534).
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
    base = config.RELEASE_BRANCH
    head = (project.get("default_branch") or "").strip() or config.PAIR_BASE_BRANCH
    try:
        pr_number = await plugins.git_ops.pr_for_branch(
            head, repo=ctx["repo"], gh_repo=ctx["gh_repo"]
        )
        if not pr_number:
            return False, ""
        ci = await plugins.git_ops.check_pr_ci(
            pr_number, repo=ctx["repo"], gh_repo=ctx["gh_repo"]
        )
        if ci.outcome != CIProbeOutcome.passed:
            return False, (
                f"релизный PR #{pr_number} не смержен: ci_{ci.outcome.value} "
                f"({ci.reason})"
            )
        merged = await plugins.git_ops.merge_pr(
            pr_number,
            0,
            f"release {head} → {base}",
            repo=ctx["repo"],
            gh_repo=ctx["gh_repo"],
        )
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure
        return False, f"релиз не удалось провести: {exc}"
    if not merged:
        return False, f"релизный PR #{pr_number} не смержен: GitHub отказал"
    return True, f"релиз PR #{pr_number} смержен в {base}"
