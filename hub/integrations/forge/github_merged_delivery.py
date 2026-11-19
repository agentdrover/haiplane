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
    """The forge did not prove one of the required delivery facts."""


@dataclass(frozen=True)
class MergedPullRequest:
    merge_sha: str
    merged_at: str
    merged_by: str
    base_tip: str


def full_sha(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and bool(SHA.fullmatch(value))


def _matches_ref(ref: Any, repository: str, branch: str) -> bool:
    if not isinstance(ref, dict) or not isinstance(ref.get("repo"), dict):
        return False
    name = ref["repo"].get("full_name")
    return (
        isinstance(name, str)
        and name.casefold() == repository.casefold()
        and ref.get("ref") == branch
        and full_sha(ref.get("sha"))
    )


def _merge_fields(pr: dict[str, Any]) -> tuple[str, str, str]:
    sha = pr.get("merge_commit_sha")
    actor = (
        pr["merged_by"].get("login") if isinstance(pr.get("merged_by"), dict) else None
    )
    if not full_sha(sha) or not isinstance(actor, str) or not actor:
        raise UnprovedMerge("merge_identity_unavailable")
    try:
        merged_at = datetime.fromisoformat(pr["merged_at"])
        if merged_at.tzinfo is None:
            raise ValueError
    except (KeyError, ValueError, TypeError) as exc:
        raise UnprovedMerge("merge_time_unavailable") from exc
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
    pr, _ = await forge._api_json(f"{api}/pulls/{number}", repo=workspace)
    if not isinstance(pr, dict) or pr.get("number") != number:
        raise UnprovedMerge("pull_request_unavailable")
    if pr.get("state") != "closed" or pr.get("merged") is not True:
        raise UnprovedMerge("merge_not_confirmed")
    if not _matches_ref(pr.get("base"), repository, base):
        raise UnprovedMerge("base_mismatch")
    if (
        not _matches_ref(pr.get("head"), repository, branch)
        or pr["head"]["sha"] != head
    ):
        raise UnprovedMerge("approved_head_mismatch")
    sha, merged_at, merged_by = _merge_fields(pr)
    tip = await _prove_ancestor(forge, api, workspace, base, sha)
    return MergedPullRequest(sha, merged_at, merged_by, tip)


async def _prove_ancestor(
    forge: GitHubForge, api: str, workspace: str | None, base: str, sha: str
) -> str:
    branch, _ = await forge._api_json(
        f"{api}/branches/{quote(base, safe='')}", repo=workspace
    )
    if not isinstance(branch, dict) or branch.get("name") != base:
        raise UnprovedMerge("base_tip_unavailable")
    commit = branch.get("commit")
    tip = commit.get("sha") if isinstance(commit, dict) else None
    if not full_sha(tip):
        raise UnprovedMerge("base_tip_unavailable")
    if tip == sha:
        return tip
    compare, _ = await forge._api_json(f"{api}/compare/{sha}...{tip}", repo=workspace)
    ancestor = compare.get("merge_base_commit") if isinstance(compare, dict) else None
    if (
        not isinstance(ancestor, dict)
        or ancestor.get("sha") != sha
        or compare.get("status") != "ahead"
    ):
        raise UnprovedMerge("merge_not_proven_in_base")
    return tip
