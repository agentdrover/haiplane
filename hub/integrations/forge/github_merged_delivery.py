"""Read GitHub's evidence for a merged PR and its target branch."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeGuard
from urllib.parse import quote

from hub.integrations.forge.github import GitHubForge

SHA = re.compile(r"[a-f0-9]{40}")
REPOSITORY = re.compile(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+")


class UnprovedMerge(ValueError):
    """The forge reported a fact that contradicts the recorded delivery."""


class MergeEvidenceUnavailable(ValueError):
    """The forge did not provide enough data to check one of the facts."""


@dataclass(frozen=True)
class MergedPullRequest:
    merge_sha: str
    merged_at: str
    merged_by: str
    base_tip: str


def full_sha(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and bool(SHA.fullmatch(value))


def _check_ref(ref: Any, repository: str, branch: str, side: str) -> str:
    if not isinstance(ref, dict) or not isinstance(ref.get("repo"), dict):
        raise MergeEvidenceUnavailable(f"{side}_ref_unavailable")
    name, ref_name, sha = ref["repo"].get("full_name"), ref.get("ref"), ref.get("sha")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(ref_name, str)
        or not ref_name
    ):
        raise MergeEvidenceUnavailable(f"{side}_ref_unavailable")
    if not full_sha(sha):
        raise MergeEvidenceUnavailable(f"{side}_ref_unavailable")
    if name.casefold() != repository.casefold() or ref_name != branch:
        raise UnprovedMerge(f"{side}_mismatch")
    return sha


async def _read(
    forge: GitHubForge, path: str, workspace: str | None, subject: str
) -> dict[str, Any]:
    try:
        data, absent = await forge._api_json(path, repo=workspace)
    except (OSError, TimeoutError) as exc:
        raise MergeEvidenceUnavailable(f"{subject}_unavailable") from exc
    if not isinstance(data, dict):
        reason = "not_found" if absent else "unavailable"
        raise MergeEvidenceUnavailable(f"{subject}_{reason}")
    return data


def _merge_fields(pr: dict[str, Any]) -> tuple[str, str, str]:
    sha = pr.get("merge_commit_sha")
    actor = (
        pr["merged_by"].get("login") if isinstance(pr.get("merged_by"), dict) else None
    )
    if not full_sha(sha) or not isinstance(actor, str) or not actor:
        raise MergeEvidenceUnavailable("merge_identity_unavailable")
    try:
        merged_at = datetime.fromisoformat(pr["merged_at"])
        if merged_at.tzinfo is None:
            raise ValueError
    except (KeyError, ValueError, TypeError) as exc:
        raise MergeEvidenceUnavailable("merge_time_unavailable") from exc
    return sha, merged_at.isoformat(), actor


async def verify(
    *,
    repository: str,
    workspace: str | None,
    number: int,
    base: str,
    branch: str,
    head: str,
) -> MergedPullRequest:
    if not REPOSITORY.fullmatch(repository):
        raise UnprovedMerge("repository_unavailable")
    forge = GitHubForge()
    api = f"repos/{repository}"
    pr = await _read(forge, f"{api}/pulls/{number}", workspace, "pull_request")
    if not isinstance(pr.get("number"), int) or not isinstance(pr.get("merged"), bool):
        raise MergeEvidenceUnavailable("pull_request_incomplete")
    if pr["number"] != number:
        raise UnprovedMerge("pull_request_mismatch")
    if pr.get("state") not in ("open", "closed"):
        raise MergeEvidenceUnavailable("pull_request_incomplete")
    if pr["state"] != "closed" or pr["merged"] is not True:
        raise UnprovedMerge("merge_not_confirmed")
    _check_ref(pr.get("base"), repository, base, "base")
    if _check_ref(pr.get("head"), repository, branch, "head") != head:
        raise UnprovedMerge("approved_head_mismatch")
    sha, merged_at, merged_by = _merge_fields(pr)
    tip = await _prove_ancestor(forge, api, workspace, base, sha)
    return MergedPullRequest(sha, merged_at, merged_by, tip)


async def _prove_ancestor(
    forge: GitHubForge, api: str, workspace: str | None, base: str, sha: str
) -> str:
    branch = await _read(
        forge, f"{api}/branches/{quote(base, safe='')}", workspace, "base_tip"
    )
    if not isinstance(branch.get("name"), str):
        raise MergeEvidenceUnavailable("base_tip_unavailable")
    if branch["name"] != base:
        raise UnprovedMerge("base_tip_mismatch")
    commit = branch.get("commit")
    tip = commit.get("sha") if isinstance(commit, dict) else None
    if not full_sha(tip):
        raise MergeEvidenceUnavailable("base_tip_unavailable")
    if tip == sha:
        return tip
    compare = await _read(
        forge, f"{api}/compare/{sha}...{tip}", workspace, "merge_comparison"
    )
    ancestor = compare.get("merge_base_commit")
    if not isinstance(ancestor, dict) or not full_sha(ancestor.get("sha")):
        raise MergeEvidenceUnavailable("merge_comparison_incomplete")
    if compare.get("status") not in ("ahead", "behind", "diverged", "identical"):
        raise MergeEvidenceUnavailable("merge_comparison_incomplete")
    if ancestor["sha"] != sha or compare["status"] != "ahead":
        raise UnprovedMerge("merge_not_proven_in_base")
    return tip
