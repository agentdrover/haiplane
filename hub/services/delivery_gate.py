"""Work that never left the branch, named at the moment it is called done (#498).

The hub can now tell "merged" from "running in production" (#497). This closes
the earlier loss: a task finished with commits on its branch and no pull
request at all — delivery never started, and nothing said so. The report reads
exactly like a delivered one, and the gap surfaces weeks later, when someone
asks why the change is not there.

Silence is the default answer. The warning fires only when all three are true:
the task HAS a branch, the hub knows of no PR and no merge for it, and git says
the branch actually carries changes. Anything unknown — no branch, no
workspace, git not answering — stays quiet, because an accusation made out of
ignorance is worse than saying nothing. That is the same line #839, #497 and
#883 draw, turned the other way round: there, absence of data could not be
printed as denial; here, it cannot be printed as fault.
"""

from __future__ import annotations

import logging
from typing import Any

from hub import repository as repo
from hub.integrations.registry import plugins

log = logging.getLogger("hub")


async def undelivered_warning(db: Any, task: dict[str, Any]) -> str:
    """One sentence about work that has not started its way out, or ``""``."""
    from hub import services

    branch = (task.get("branch") or "").strip()
    if not branch:
        # Research, decisions, spikes: nothing was supposed to leave a branch.
        return ""
    if task.get("pr_number"):
        return ""

    task_id = int(task["id"])
    if await repo.merge_sha_for_task(db, task_id):
        return ""

    try:
        ctx = await services.project_git_context(db, task_id)
    except Exception as exc:  # noqa: BLE001 - advisory only, never fatal
        log.warning("delivery check for #%s: no project context: %s", task_id, exc)
        return ""
    workspace = ctx.get("repo")
    if not workspace:
        return ""

    base = ctx.get("base_branch") or ""
    try:
        changed = await plugins.git_ops.branch_diff_paths(
            branch, base_branch=base or None, repo=workspace
        )
    except Exception as exc:  # noqa: BLE001 - advisory only, never fatal
        log.warning("delivery check for #%s: %s", task_id, exc)
        return ""

    # None is "could not look", [] is "the branch changes nothing". Neither is
    # undelivered work, and neither may be reported as one.
    if not changed:
        return ""

    return (
        f"Работа не начала доставляться: на ветке {branch} есть изменения "
        f"({len(changed)} файлов), но хаб не знает ни PR, ни мержа для неё. "
        "Задача завершена, а изменения остались в ветке — если так и задумано, "
        "это стоит сказать вслух; если нет, доставку надо начать."
    )
