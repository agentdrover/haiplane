"""Did this task's work reach production, or only the default branch (#497).

The hub records two facts now: the merge it performed (``pipeline_merges``,
#534) and the deploy CI reported (``releases``, #839 + #496). Until they were
compared, "merged" was read as "delivered" — and on 21.08.2026 that reading was
wrong in the way that costs most: task #823 sat ``completed`` with its PR
merged into develop while the deploy job was skipped, because deployment runs
from main. The only way to see it was to open GitHub's logs.

Three states, never two:

``in_prod``      the merge commit is in the history of the deployed commit.
``not_in_prod``  it is not — the work is merged and waiting for a release.
``unknown``      the question could not be answered, and the reason says why.

The third exists because the alternative is worse than useless. An
installation with no delivery facts, a project without a workspace, or a git
that would not answer are all "we do not know" — printing them as
``not_in_prod`` would turn silence into a denial, which is the same defect the
evidence blocks were cleaned of (#725) and the empty release table refuses to
commit (#839).

Computed, never stored. The answer changes with every deploy — a task that is
``not_in_prod`` at noon is ``in_prod`` after the next release without anything
about the task changing. A cached flag would be one more thing that goes stale,
which is precisely the class of defect this epic exists to remove.
"""

from __future__ import annotations

import logging
from typing import Any

from hub import repository as repo
from hub.integrations.registry import plugins

log = logging.getLogger("hub")

IN_PROD = "in_prod"
NOT_IN_PROD = "not_in_prod"
UNKNOWN = "unknown"


def _answer(state: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "state": state,
        "reason": reason,
        "merge_sha": "",
        "deployed_sha": "",
        "deployed_at": "",
        **extra,
    }


async def ensure_commit_available(
    db: Any, sha: str, ref: str = "", *, project_id: int | None = None
) -> bool:
    """Make sure the workspace carries ``sha``, fetching once if it does not.

    Called where a deploy is RECORDED (#883): deploys happen a few times a day,
    cards are read constantly, and paying for the network on every render to
    learn something that changes once per release is the wrong trade. Returns
    whether the commit is available afterwards; the caller decides what an
    unavailable one means — here it never means "not deployed".
    """
    sha = (sha or "").strip()
    if not sha:
        return False
    # The workspace comes from the PROJECT, not from a task: a deploy callback
    # names no task. project_git_context resolves through a task id and would
    # answer an empty context here, which would have made this whole path a
    # no-op — caught before the first test, and worth stating so the next
    # reader does not reintroduce it.
    try:
        row = (
            await repo.get_project(db, project_id)
            if project_id
            else await repo.get_project_by_slug(db, "default")
        )
    except Exception as exc:  # noqa: BLE001 - best effort by contract
        log.warning("pre-fetch of %s: no project row: %s", sha[:12], exc)
        return False
    workspace = (dict(row).get("workspace_path") or "").strip() if row else ""
    if not workspace:
        return False
    if await plugins.git_ops.commit_exists(workspace, sha):
        return True
    fetched, error = await plugins.git_ops.fetch_commit(workspace, sha, ref)
    if not fetched:
        log.warning("pre-fetch of %s failed: %s", sha[:12], error)
    return fetched


async def delivery_state(db: Any, task_id: int) -> dict[str, Any]:
    """Whether this task's merge is part of what production is running."""
    from hub import services

    merge_sha = await repo.merge_sha_for_task(db, task_id)
    if not merge_sha:
        return _answer(
            UNKNOWN,
            "хаб не мержил эту задачу — сверять с выкатом нечего. "
            "Это не значит, что работа не доехала: значит, что факта мержа у хаба нет",
        )

    release = await repo.latest_successful_release(db)
    if release is None:
        return _answer(
            UNKNOWN,
            "хаб не знает, что сейчас раскатано: успешных выкатов не записано. "
            "Пустая история выкатов — незнание, а не отрицание",
            merge_sha=merge_sha,
        )

    deployed_sha = str(release.get("deployed_sha") or "")
    deployed_at = str(release.get("deployed_at") or "")
    known = {
        "merge_sha": merge_sha,
        "deployed_sha": deployed_sha,
        "deployed_at": deployed_at,
    }

    try:
        ctx = await services.project_git_context(db, task_id)
        workspace = ctx.get("repo")
    except Exception as exc:  # noqa: BLE001 - a card must render regardless
        log.warning("delivery state for #%s: no project context: %s", task_id, exc)
        return _answer(UNKNOWN, f"рабочую копию определить не удалось: {exc}", **known)
    if not workspace:
        return _answer(
            UNKNOWN,
            "у проекта нет рабочей копии на этом хосте — достижимость коммита "
            "проверить негде",
            **known,
        )

    # #883: the objects have to be here before git can be asked about them.
    # The workspace tracks the base branch, so a commit deployed from another
    # ref is simply absent — and that absence was answering "could not check"
    # for every task on this installation. Checked first, and only fetched
    # when missing: a present commit must cost no network at all.
    if await plugins.git_ops.commit_exists(workspace, deployed_sha) is False:
        fetched, fetch_error = await plugins.git_ops.fetch_commit(
            workspace, deployed_sha, str(release.get("ref") or "")
        )
        if not fetched:
            return _answer(
                UNKNOWN,
                f"коммита {deployed_sha[:12]} нет в рабочей копии, и подтянуть "
                f"его не удалось: {fetch_error or 'причина не названа'}. "
                "Это не «не раскатано»",
                **known,
            )

    reachable = await plugins.git_ops.is_ancestor(workspace, merge_sha, deployed_sha)
    if reachable is None:
        return _answer(
            UNKNOWN,
            f"git не смог ответить, входит ли {merge_sha[:12]} в историю "
            f"{deployed_sha[:12]} — возможно, коммитов нет в этой копии. "
            "Это не «не раскатано»",
            **known,
        )
    if reachable:
        return _answer(
            IN_PROD,
            f"мерж {merge_sha[:12]} входит в раскатанный {deployed_sha[:12]}"
            + (f" от {deployed_at}" if deployed_at else ""),
            **known,
        )
    return _answer(
        NOT_IN_PROD,
        f"мерж {merge_sha[:12]} не входит в раскатанный {deployed_sha[:12]}: "
        "работа смёржена, но ждёт релиза",
        **known,
    )
