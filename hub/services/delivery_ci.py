"""The project snapshot used by the existing CI delivery probes."""

from typing import Any

from hub import repository as repo
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.services import project_policy


async def _project_fingerprint(db: Any, task_id: int) -> str | None:
    project = await repo.resolve_project_for_task(db, task_id)
    if (
        project is None
        or project["slug"] != "home-infra"
        or str(project["repo"] or "").lower() != "agentdrover/home-infra"
    ):
        return None
    return project_policy.ci_contract_of(project).fingerprint


async def check_task(
    db: Any, task_id: int, pr_number: int, ctx: dict[str, Any], *, exact_head: str = ""
) -> CIProbeResult:
    repo_path = ctx.get("repo")
    gh_repo = ctx.get("gh_repo")
    forge = ctx.get("forge", "")
    if str(gh_repo or "").lower() != "agentdrover/home-infra":
        if exact_head:
            return CIProbeResult(
                CIProbeOutcome.unavailable, "ci_exact_head_unsupported"
            )
        return await plugins.git_ops.check_pr_ci(
            pr_number, repo=repo_path, gh_repo=gh_repo, forge=forge
        )
    fingerprint = await _project_fingerprint(db, task_id)
    if fingerprint is None:
        return CIProbeResult(CIProbeOutcome.unavailable, "ci_project_unproved")
    exact = {"exact_head": exact_head} if exact_head else {}
    result = await plugins.git_ops.check_pr_ci(
        pr_number,
        repo=repo_path,
        gh_repo=gh_repo,
        forge=forge,
        contract_fingerprint=fingerprint,
        **exact,
    )
    if await _project_fingerprint(db, task_id) != fingerprint:
        return CIProbeResult(CIProbeOutcome.unavailable, "ci_contract_changed")
    if (
        exact_head
        and result.outcome is CIProbeOutcome.passed
        and result.head_sha != exact_head
    ):
        return CIProbeResult(CIProbeOutcome.unavailable, "ci_exact_head_unproved")
    return result
