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
import time
from typing import Any

from hub import config
from hub import repository as repo
from hub.integrations.registry import plugins

log = logging.getLogger("hub")

IN_PROD = "in_prod"
NOT_IN_PROD = "not_in_prod"
UNKNOWN = "unknown"

# #937: the ANSWER is still computed, never stored — what is cached is the
# FACT of a failed network fetch for (workspace, sha), so a dashboard that
# renders fifty cards does not repeat the same doomed round-trip on every
# render. A deploy that later makes the sha fetchable is picked up after the
# TTL, or immediately once commit_exists starts answering True.
FETCH_MISS_TTL_SECONDS = 600.0
_FETCH_MISS_CAP = 512
_fetch_misses: dict[tuple[str, str], float] = {}


def _fetch_miss_fresh(workspace: str, sha: str) -> float | None:
    """Seconds since the cached miss, or None when there is no fresh miss."""
    at = _fetch_misses.get((workspace, sha))
    if at is None:
        return None
    age = time.monotonic() - at
    if age >= FETCH_MISS_TTL_SECONDS:
        _fetch_misses.pop((workspace, sha), None)
        return None
    return age


def _record_fetch_miss(workspace: str, sha: str) -> None:
    if len(_fetch_misses) >= _FETCH_MISS_CAP:
        now = time.monotonic()
        for key, at in list(_fetch_misses.items()):
            if now - at >= FETCH_MISS_TTL_SECONDS:
                _fetch_misses.pop(key, None)
        if len(_fetch_misses) >= _FETCH_MISS_CAP:
            _fetch_misses.clear()
    _fetch_misses[(workspace, sha)] = time.monotonic()


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

    # #937: the release the hub records is a deploy of the HUB's repository.
    # For a task whose project lives in a different repo, that sha cannot
    # exist in the project's workspace — asking git (and then the network)
    # was a guaranteed miss paid on every dashboard render.
    project_repo = (ctx.get("gh_repo") or "").strip().lower()
    hub_repo = (config.REPO_NAME or "").strip().lower()
    if project_repo and hub_repo and project_repo != hub_repo:
        return _answer(
            UNKNOWN,
            f"релиз хаба ({hub_repo}) не применим к проекту в {project_repo} — "
            "у этого проекта свой репозиторий, факт его выката хаб не записывает",
            **known,
        )

    # #883: the objects have to be here before git can be asked about them.
    # The workspace tracks the base branch, so a commit deployed from another
    # ref is simply absent — and that absence was answering "could not check"
    # for every task on this installation. Checked first, and only fetched
    # when missing: a present commit must cost no network at all.
    if await plugins.git_ops.commit_exists(workspace, deployed_sha) is False:
        miss_age = _fetch_miss_fresh(workspace, deployed_sha)
        if miss_age is not None:
            return _answer(
                UNKNOWN,
                f"коммита {deployed_sha[:12]} нет в рабочей копии; недавний "
                f"промах fetch ({int(miss_age)}с назад) — повторная попытка "
                "отложена. Это не «не раскатано»",
                **known,
            )
        fetched, fetch_error = await plugins.git_ops.fetch_commit(
            workspace, deployed_sha, str(release.get("ref") or "")
        )
        if not fetched:
            _record_fetch_miss(workspace, deployed_sha)
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


async def merged_into_base(db: Any, task_row: dict[str, Any]) -> bool | None:
    """Is this task's submitted commit already in its project's base branch (#885)?

    True — yes, wherever the merge came from. False — looked and it is not.
    None — could not look, which is NOT the same as "not delivered" (#725).

    Exists because delivery was read from ``pipeline_merges`` alone — merges
    the hub performed itself (#534). A merge made outside the gate leaves no
    row there, so the blocker read as undelivered while its code sat in the
    base branch. On 21.08.2026 the edge #830 → #818 said exactly that, an hour
    after #818 was merged. A warning that is wrong in the obvious case teaches
    the reader to skip the line, and then it is silent in the case that
    mattered.

    Cheap by construction: callers ask ONLY for blockers with no pipeline
    merge, so in the normal path this runs zero git commands.
    """
    sha = (task_row.get("submission_sha") or "").strip()
    if not sha:
        return None
    try:
        from hub import services

        ctx = await services.project_git_context(db, task_row["id"])
        workspace = (ctx.get("repo") or "").strip()
        base = (ctx.get("base_branch") or "").strip() or config.PAIR_BASE_BRANCH
    except Exception as exc:  # noqa: BLE001 - advisory path, never fatal
        log.warning("delivery check for #%s: no git context: %s", task_row["id"], exc)
        return None
    if not workspace:
        return None
    # origin/<base> rather than <base>: the shared clone sits on the base
    # branch but may be behind, and the question is about what has landed
    # upstream, not about this checkout.
    return await plugins.git_ops.is_ancestor(workspace, sha, f"origin/{base}")


async def blocker_delivery(db: Any, blocker: dict[str, Any]) -> dict[str, Any]:
    """Fill in ``delivered``/``reason`` for one blocker row (#885).

    The gate's own merges answer first and cost nothing. Only when there is
    none does the base branch get asked — and its answer is kept distinct:
    delivered outside the gate clears the block, and says so, because manual
    merges into the base branch are against the rules here and the drift guard
    (#534) has its own opinion about them. Hiding that would trade one silent
    wrong answer for another.
    """
    if blocker.get("delivered"):
        return {**blocker, "delivery_path": "gate"}
    row = await repo.get_task(db, blocker["task_id"])
    task = dict(row) if row is not None else {}
    reached = await merged_into_base(db, task) if task else None
    # A blocker that never pinned a commit has nothing to look for, so the
    # second source staying silent is not news — saying "could not check"
    # there would add noise to a reason that is already complete.
    had_something_to_check = bool((task.get("submission_sha") or "").strip())
    if reached is True:
        return {
            **blocker,
            "delivered": True,
            "delivery_path": "outside_gate",
            "reason": "код в базовой ветке, но мерж прошёл мимо гейта",
        }
    if reached is None and had_something_to_check and blocker.get("reason"):
        # Keep the original reason and say the second source stayed silent —
        # "could not look" must not read as "looked and it is not there".
        return {
            **blocker,
            "delivery_path": "unknown",
            "reason": f"{blocker['reason']}; проверить базовую ветку не удалось",
        }
    return {**blocker, "delivery_path": "none"}


async def with_delivery(
    db: Any, blockers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The same enrichment for every reader (#885) — the gate (#484), the task
    context (#485), REST (#486) and MCP (#487) must not answer differently."""
    return [await blocker_delivery(db, b) for b in blockers]
