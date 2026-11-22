"""Independent, read-only evidence for the single #2413 physical order (#2414)."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.integrations.forge.github import GitHubForge
from hub.services import ci_contract, ci_trust, project_policy, review_ci_gate
from hub.services.orchestration import review_approved_for_current_submission

TASK_ID = 2413
REPOSITORY = "agentdrover/home-infra"
BRANCH = "task-2413/k8-network-receipt"
DIRECTORY = "evidence"
FILENAME = "k8-network-receipt-2413.json"
_SHA = re.compile("[0-9a-f]{40}")
_NONCE = re.compile("[0-9a-f]{32}")


@dataclass(frozen=True)
class ReceiptVerification:
    commit_verified: bool = False
    ci_verified: bool = False
    submission_verified: bool = False
    verdict_verified: bool = False
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return all(
            (
                self.commit_verified,
                self.ci_verified,
                self.submission_verified,
                self.verdict_verified,
            )
        )


class _Unverified(Exception):
    """Fixed diagnostic code, safe to show without exposing raw forge responses."""


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise _Unverified("invalid_sha")
    return value


async def _read(forge: GitHubForge, path: str) -> dict[str, Any]:
    data, absent = await forge._api_json(f"repos/{REPOSITORY}/{path}", repo=None)
    if not isinstance(data, dict):
        raise _Unverified("github_object_missing" if absent else "github_unavailable")
    return data


async def _commit(forge: GitHubForge, sha: str) -> dict[str, Any]:
    obj = await _read(forge, f"git/commits/{sha}")
    if obj.get("sha") != sha:
        raise _Unverified("commit_identity")
    return obj


Entry = tuple[str, str, str]


async def _tree(forge: GitHubForge, sha: str) -> dict[str, Entry]:
    obj = await _read(forge, f"git/trees/{sha}")
    rows = obj.get("tree")
    if (
        obj.get("sha") != sha
        or obj.get("truncated") is not False
        or not isinstance(rows, list)
    ):
        raise _Unverified("incomplete_tree")
    entries = {}
    for row in rows:
        path = row["path"]
        if not isinstance(path, str) or not path or "/" in path or path in entries:
            raise _Unverified("invalid_tree")
        entries[path] = row["mode"], row["type"], _sha(row["sha"])
    return entries


async def _evidence_tree(forge: GitHubForge, entry: Entry | None) -> dict[str, Entry]:
    if entry is None:
        return {}
    if entry[:2] != ("040000", "tree"):
        raise _Unverified("invalid_evidence_directory")
    return await _tree(forge, entry[2])


async def _receipt_blob(forge: GitHubForge, before: str, after: str) -> str:
    old, new = await _tree(forge, before), await _tree(forge, after)
    old_dir, new_dir = old.pop(DIRECTORY, None), new.pop(DIRECTORY, None)
    if old != new or new_dir is None:
        raise _Unverified("unexpected_tree_change")
    old, new = (
        await _evidence_tree(forge, old_dir),
        await _evidence_tree(forge, new_dir),
    )
    receipt = new.pop(FILENAME, None)
    if receipt is None or receipt[:2] != ("100644", "blob") or old != new:
        raise _Unverified("receipt_not_single_added_file")
    return receipt[2]


def _contents(order_id: int, receipt_id: str, head_sha: str) -> bytes:
    if type(order_id) is not int or not 0 < order_id < 2**63:
        raise _Unverified("invalid_order")
    if not isinstance(receipt_id, str) or not _NONCE.fullmatch(receipt_id):
        raise _Unverified("invalid_receipt_id")
    record = dict(
        schema=1,
        task_id=TASK_ID,
        order_id=order_id,
        receipt_id=receipt_id,
        head_sha=_sha(head_sha),
    )
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


async def _verify_git(
    forge: GitHubForge, head: str, commit: str, expected: bytes
) -> None:
    obj = await _commit(forge, commit)
    parents = obj.get("parents")
    if not isinstance(parents, list) or len(parents) != 1 or parents[0]["sha"] != head:
        raise _Unverified("unexpected_parent")
    if not await forge.branch_contains("main", head, gh_repo=REPOSITORY):
        raise _Unverified("parent_not_confirmed_on_main")
    base = await _commit(forge, head)
    blob = await _receipt_blob(
        forge, _sha(base["tree"]["sha"]), _sha(obj["tree"]["sha"])
    )
    data = await _read(forge, f"git/blobs/{blob}")
    if (
        data.get("sha") != blob
        or data.get("size") != len(expected)
        or data.get("encoding") != "base64"
    ):
        raise _Unverified("invalid_receipt_blob")
    # GitHub's Git Blob API wraps base64 at fixed line lengths.
    if (
        base64.b64decode("".join(data["content"].splitlines()), validate=True)
        != expected
    ):
        raise _Unverified("receipt_content")
    ref = await _read(forge, f"git/ref/heads/{BRANCH}")
    if (
        ref.get("ref") != f"refs/heads/{BRANCH}"
        or ref["object"]["type"] != "commit"
        or ref["object"]["sha"] != commit
    ):
        raise _Unverified("receipt_branch_head")


async def _ci_gap(
    db: aiosqlite.Connection, commit_sha: str, contract: ci_contract.CiContract
) -> str:
    if (
        contract.workflow_path != ".github/workflows/tests.yml"
        or "tests" not in contract.required_checks
    ):
        return "wrong_ci_contract"
    report = await ci_trust.trusted_report(db, TASK_ID, commit_sha)
    if report is None:
        return "awaiting_trusted_ci"
    facts = dict(report)
    try:
        provenance = json.loads(facts["provenance"])
    except (TypeError, ValueError):
        return "invalid_ci_provenance"
    if (
        not isinstance(provenance, dict)
        or provenance.get("head_repository") != REPOSITORY
    ):
        return "wrong_ci_repository"
    if not ci_contract.path_matches(
        contract, str(provenance.get("workflow_path") or "")
    ):
        return "wrong_ci_workflow"
    # The card's operational validation instructions need a separate verdict.
    # CI proves the contracted checks; the reporter records prose as unknown.
    if review_ci_gate.failed_checks(facts) or ci_trust.green_gap(facts, contract):
        return "ci_not_green"
    return ""


async def recheck_network_receipt(
    db: aiosqlite.Connection, *, commit_sha: str, commit_verified: bool
) -> ReceiptVerification:
    """Re-read local gates in the caller's final transaction after GitHub verification."""
    if not commit_verified or not _SHA.fullmatch(commit_sha):
        return ReceiptVerification(reason="unverified_commit")
    row = await repo.get_task(db, TASK_ID)
    project = await repo.resolve_project_for_task(db, TASK_ID)
    if row is None or project is None:
        return ReceiptVerification(reason="missing_task")
    if (project["slug"], project["repo"], project["forge"]) != (
        "home-infra",
        REPOSITORY,
        "github",
    ):
        return ReceiptVerification(reason="wrong_project")
    ci_gap = await _ci_gap(db, commit_sha, project_policy.ci_contract_of(project))
    task = dict(row)
    submitted = (
        task["branch"] == BRANCH
        and task["submission_sha"] == commit_sha
        and task["submission_generation"] > 0
    )
    approved = (
        submitted
        and not task["archived"]
        # Pair approval without a PR returns the task to running for report_done.
        and task["status"]
        in {"review", "running", "ci_check", "needs_decision", "completed"}
        and review_approved_for_current_submission(task)
    )
    reason = ci_gap
    if not reason and not approved:
        reason = "awaiting_verdict" if submitted else "awaiting_submission"
    return ReceiptVerification(True, not ci_gap, submitted, approved, reason)


async def verify_network_receipt(
    db: aiosqlite.Connection,
    *,
    order_id: int,
    receipt_id: str,
    head_sha: str,
    commit_sha: str,
) -> ReceiptVerification:
    """Prove the order's immutable file on GitHub, then read Hub's independent gates."""
    try:
        expected = _contents(order_id, receipt_id, head_sha)
        async with asyncio.timeout(45):
            await _verify_git(GitHubForge(), head_sha, _sha(commit_sha), expected)
    except _Unverified as exc:
        return ReceiptVerification(reason=str(exc))
    except (TimeoutError, OSError, KeyError, TypeError, AttributeError, ValueError):
        return ReceiptVerification(reason="github_evidence_unavailable")
    return await recheck_network_receipt(
        db, commit_sha=commit_sha, commit_verified=True
    )
