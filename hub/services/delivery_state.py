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
