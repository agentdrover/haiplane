"""Assemble a project release view without walking git history per task."""

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, Field

from hub import config
from hub import repository as repo
from hub import repository_releases as releases
from hub.integrations.registry import plugins
from hub.services import defect_release, egress_watch, project_policy, release_alert
from hub.services.release_deploy_watch import DEPLOYED_LATE, NOT_IN_PROD, UNOBSERVED

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
HEAD_TIMEOUT_SECONDS = 3.0
HEAD_CACHE_SECONDS = 300
log = logging.getLogger(__name__)


class DeployHeadView(BaseModel):
    heads: dict[str, str] = Field(default_factory=dict)
    source: Literal["remote", "last_fetch", "unknown"] = "unknown"
    reason: str = ""


class DeployRelease(BaseModel):
    sha: str
    ref: str
    deployed_at: str


class DeployFeedEntry(DeployRelease):
    id: int
    status: str
    source: str
    backup: str | None = None
    release_pr: int | None = None
    release_merged_at: str | None = None
    lag_minutes: float | None = None
    task_ids: list[int] = Field(default_factory=list)


class DeployPipeline(BaseModel):
    project: str
    develop_branch: str
    release_branch: str
    heads: DeployHeadView
    deployed: DeployRelease | None = None
    waiting_release: int | None = None
    in_main_not_deployed: int | None = None
    in_main_not_deployed_reason: str = ""
    note: str = ""
    stuck: list[str] = Field(default_factory=list)


class DeploysView(BaseModel):
    pipeline: DeployPipeline
    releases: list[DeployFeedEntry]
    limit: int


@dataclass(frozen=True)
class _CachedHeads:
    workspace: str
    branches: tuple[str, ...]
    expires_at: float
    value: DeployHeadView


_head_cache: dict[int, _CachedHeads] = {}


async def _local_heads(workspace: str, branches: tuple[str, ...]) -> DeployHeadView:
    values = await asyncio.gather(
        *(plugins.git_ops.head_sha(workspace, branch) for branch in branches)
    )
    heads = {name: sha for name, sha in zip(branches, values, strict=True) if sha}
    return _head_view(heads, branches, "last_fetch")


def _head_view(
    heads: dict[str, str],
    branches: tuple[str, ...],
    source: Literal["remote", "last_fetch"],
) -> DeployHeadView:
    missing = [branch for branch in branches if not heads.get(branch)]
    return DeployHeadView(
        heads=heads,
        source=source,
        reason=f"головы не прочитаны: {', '.join(missing)}" if missing else "",
    )


async def _remote_heads(
    project_id: int, workspace: str, branches: tuple[str, ...]
) -> DeployHeadView:
    cached = _head_cache.get(project_id)
    if (
        cached
        and (cached.workspace, cached.branches) == (workspace, branches)
        and time.monotonic() < cached.expires_at
    ):
        return cached.value
    observed = await plugins.git_ops.remote_heads(branches, repo=workspace)
    if observed is None:
        return DeployHeadView(reason="origin не ответил")
    heads = _head_view(observed, branches, "remote")
    _head_cache[project_id] = _CachedHeads(
        workspace, branches, time.monotonic() + HEAD_CACHE_SECONDS, heads
    )
    return heads


async def _heads(
    db: aiosqlite.Connection, project: Any, branches: tuple[str, ...]
) -> DeployHeadView:
    workspace = (project["workspace_path"] or "").strip()
    if not workspace:
        if project["slug"] != "default":
            return DeployHeadView(reason="у проекта нет рабочей копии")
        workspace = str(config.WORKSPACE_REPO_LINK)
    if not Path(workspace).is_absolute():
        return DeployHeadView(reason="путь рабочей копии ещё не разрешён")
    branches = tuple(dict.fromkeys(branches))
    egress = await egress_watch.egress_status(db)
    try:
        reader = (
            _local_heads(workspace, branches)
            if egress["state"] == egress_watch.DOWN
            else _remote_heads(int(project["id"]), workspace, branches)
        )
        return await asyncio.wait_for(reader, timeout=HEAD_TIMEOUT_SECONDS)
    except TimeoutError:
        return DeployHeadView(reason="истёк срок чтения голов веток (3 с)")
    except OSError:
        log.exception("Cannot read deploy heads for project %s", project["id"])
        return DeployHeadView(reason="не удалось прочитать головы веток")


async def _pipeline(
    db: aiosqlite.Connection, project: Any, observed: bool, include_legacy: bool
) -> DeployPipeline:
    project_id = int(project["id"])
    integration = project_policy.base_branch_of(project)
    release = project_policy.release_base_of(project)
    deployed = await defect_release.latest_release_in_scope(db, project_id)
    result = DeployPipeline(
        project=project["slug"],
        develop_branch=integration,
        release_branch=release,
        heads=DeployHeadView(),
        deployed=(
            DeployRelease(
                sha=deployed["deployed_sha"],
                ref=deployed["ref"],
                deployed_at=deployed["deployed_at"],
            )
            if deployed is not None
            else None
        ),
    )

    if project_policy.merge_is_delivery_of(project_policy.gate_policy_of(project)):
        result.note = "мерж = выкат"
    elif not observed:
        result.note = "выкаты не наблюдаются"
    else:
        result.waiting_release = await releases.unreleased_completed_count(
            db, project_id, include_legacy
        )
        if deployed is not None:
            result.in_main_not_deployed = await releases.release_merges_since(
                db, project_id, deployed["deployed_sha"].strip().lower()
            )
        if result.in_main_not_deployed is None:
            result.in_main_not_deployed_reason = (
                "SHA успешного выката отсутствует в истории релизных мержей"
                if deployed is not None
                else "нет записи об успешном выкате"
            )
    block = await release_alert.active_release_block(db, project_id)
    if block and block["reason"]:
        result.stuck.append(block["reason"])
    result.stuck.extend(
        await releases.open_deploy_reasons(
            db, project_id, (NOT_IN_PROD, UNOBSERVED), DEPLOYED_LATE
        )
    )
    result.heads = await _heads(db, project, (integration, release))
    return result


async def deploys_view(
    db: aiosqlite.Connection, project_id: int, limit: int = DEFAULT_LIMIT
) -> DeploysView:
    project = await repo.get_project(db, project_id)
    if project is None:
        raise LookupError(project_id)
    include_legacy = None in await defect_release.project_scope(db, project_id)
    limit = min(MAX_LIMIT, max(1, limit))
    observed = await releases.release_feed(db, project_id, include_legacy, limit)
    task_ids = await releases.release_tasks(
        db,
        project_id,
        include_legacy,
        {row["sha"].strip().lower() for row in observed if row["sha"].strip()},
    )
    return DeploysView(
        pipeline=await _pipeline(db, project, bool(observed), include_legacy),
        releases=[
            DeployFeedEntry(
                **row, task_ids=task_ids.get(row["sha"].strip().lower(), [])
            )
            for row in observed
        ],
        limit=limit,
    )


async def pipeline(db: aiosqlite.Connection, project_id: int) -> DeployPipeline:
    project = await repo.get_project(db, project_id)
    if project is None:
        raise LookupError(project_id)
    include_legacy = None in await defect_release.project_scope(db, project_id)
    observed = bool(await releases.release_feed(db, project_id, include_legacy, 1))
    return await _pipeline(db, project, observed, include_legacy)


def format_pipeline(view: DeployPipeline) -> str:
    lines = [f"Выкаты · {view.project}"]
    for branch in dict.fromkeys((view.develop_branch, view.release_branch)):
        sha = view.heads.heads.get(branch)
        lines.append(f"{branch} — {sha[:12] if sha else 'неизвестно'}")
    if view.heads.source == "last_fetch":
        lines.append("Головы веток по последнему fetch")
    if view.heads.reason:
        lines.append(view.heads.reason)
    if view.deployed:
        lines.append(
            f"На проде {view.deployed.sha[:12]} с {view.deployed.deployed_at} UTC"
        )
    if view.note:
        lines.append(view.note)
    if view.waiting_release is not None:
        lines.append(f"Ждут релиза — {view.waiting_release}")
    if view.in_main_not_deployed is not None:
        lines.append(
            f"Релизы в {view.release_branch} без выката — {view.in_main_not_deployed}"
        )
    elif view.in_main_not_deployed_reason:
        lines.append(f"Выкат релизов неизвестен — {view.in_main_not_deployed_reason}")
    lines.extend(f"Остановка — {reason}" for reason in view.stuck)
    return "\n".join(lines)


def format_deploys_view(view: DeploysView) -> str:
    lines = [format_pipeline(view.pipeline)]
    if view.releases:
        lines.append(f"Последние выкаты · до {view.limit}")
    for row in view.releases:
        parts = [row.deployed_at, row.status, row.sha[:12]]
        if row.release_pr is not None:
            parts.append(f"PR #{row.release_pr}")
        if row.lag_minutes is not None:
            parts.append(f"{row.lag_minutes:g} мин от мержа до выката")
        if row.task_ids:
            parts.append("задачи " + ", ".join(f"#{tid}" for tid in row.task_ids))
        lines.append(" · ".join(parts))
    return "\n".join(lines)
