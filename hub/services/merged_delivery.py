"""Reconcile a GitHub merge observed outside the delivery gate."""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall, write_transaction
from hub.integrations.forge import github_merged_delivery
from hub.services import ci_contract, ci_trust, project_policy

RECONCILED = "reconciled_external_merge"


@dataclass(frozen=True)
class _Pin:
    task_id: int
    project_id: int
    repository: str
    base: str
    number: int
    branch: str
    head: str
    generation: int
    verdict_revision: int
    contract: str


async def _pin(db: aiosqlite.Connection, task: dict[str, Any]) -> _Pin | None:
    from hub.services.orchestration import review_approved_for_current_submission

    if (
        not review_approved_for_current_submission(task)
        or task.get("review_self_approved")
        or task.get("status") not in {"running", "completed"}
        or not task.get("branch")
        or not github_merged_delivery.full_sha(task.get("submission_sha"))
    ):
        return None
    task_id = int(task["id"])
    project = await repo.resolve_bound_project(db, task_id)
    if project is None:
        project = await repo.get_project_by_slug(db, "default")
    if (
        not project
        or project["status"] != "active"
        or project_policy.forge_of(project) != "github"
    ):
        return None
    if not project["repo"] or not project["default_branch"]:
        return None
    return _Pin(
        task_id,
        project["id"],
        project["repo"],
        project["default_branch"],
        int(task["pr_number"]),
        task["branch"],
        task["submission_sha"],
        int(task["submission_generation"]),
        int(task["review_verdict_revision"] or 0),
        await ci_contract.fingerprint_for_task(db, task_id),
    )


async def reconcile(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    *,
    state: str,
    gate_busy: Callable[[], bool],
) -> tuple[bool, str] | None:
    """None leaves the ordinary gate in charge. A known merge has its own outcome."""
    repository, base = ctx.get("gh_repo"), ctx.get("base_branch")
    number = int(task["pr_number"])
    if state != "merged":
        if await _reconciled_rows(db, int(task["id"]), number):
            return False, "ci_unavailable: merged_reconciliation_pr_unconfirmed"
        return None
    if db.in_transaction:
        return (
            False,
            "ci_unavailable: merged_reconciliation_requires_unlocked_connection",
        )
    pin = await _pin(db, task)
    if pin is None or (pin.repository, pin.base) != (repository, base):
        return False, "merged_outside_gate: current_independent_approval_not_proven"
    try:
        facts = await github_merged_delivery.verify(
            repository=pin.repository,
            workspace=ctx.get("repo"),
            number=pin.number,
            base=pin.base,
            branch=pin.branch,
            head=pin.head,
        )
    except github_merged_delivery.UnprovedMerge as exc:
        return False, f"merged_outside_gate: {exc}"
    standing = await ci_trust.refresh_quietly(db, pin.task_id, pin.head)
    if (
        not standing
        or not standing.verified
        or standing.forge_down
        or standing.version is None
    ):
        return False, "ci_unavailable: merged_reconciliation_exact_ci_not_verified"
    # GitHub and SQLite have no shared transaction. Read the latest attempt
    # immediately before taking the lock and compare the local CI version there.
    result = await _record(db, pin, facts, standing, gate_busy)
    if result[1].startswith(RECONCILED):
        task["_reconciled_merge"] = (pin, standing)
    return result


async def _record(
    db: aiosqlite.Connection,
    pin: _Pin,
    facts: github_merged_delivery.MergedPullRequest,
    standing: ci_trust.Standing,
    gate_busy: Callable[[], bool],
) -> tuple[bool, str]:
    async with write_transaction(db):
        task = await repo.get_task(db, pin.task_id)
        if not task or await _pin(db, dict(task)) != pin:
            return (
                False,
                "ci_unavailable: merged_reconciliation_approval_or_project_changed",
            )
        if not await _ci_holds(db, pin, standing):
            return False, "ci_unavailable: merged_reconciliation_exact_ci_not_green"
        if await repo.pipeline_merge_recorded(
            db, pin.task_id, pin.number, gate_only=True
        ):
            return True, "already delivered"
        if gate_busy():
            return False, "gate_merge_in_flight: gate provenance not settled"
        return await _insert(db, pin, facts, standing.attempt_key)


async def _ci_holds(
    db: aiosqlite.Connection, pin: _Pin, standing: ci_trust.Standing
) -> bool:
    if not standing.attempt_key:
        return False
    if await repo.ci_state_version(db, pin.task_id, pin.head) != standing.version:
        return False
    row = await ci_trust.trusted_report(db, pin.task_id, pin.head)
    if not row or row["attempt_key"] != standing.attempt_key:
        return False
    return row["validation_status"] == "pass" and not ci_trust.green_gap(
        dict(row), await ci_contract.contract_for_task(db, pin.task_id)
    )


async def completion_stands(
    db: aiosqlite.Connection, live_task: dict[str, Any], snapshot: dict[str, Any]
) -> bool:
    """Keep the proof current until the caller writes completed."""
    proof = snapshot.get("_reconciled_merge")
    if proof is None:
        return True
    pin, standing = proof
    return await _pin(db, live_task) == pin and await _ci_holds(db, pin, standing)


async def _reconciled_rows(db: aiosqlite.Connection, task_id: int, pr_number: int):
    return await fetchall(
        db,
        "SELECT project_id, merge_sha, reconciliation FROM pipeline_merges "
        "WHERE task_id=? AND pr_number=? AND reconciliation<>''",
        (task_id, pr_number),
    )


async def _insert(
    db: aiosqlite.Connection,
    pin: _Pin,
    facts: github_merged_delivery.MergedPullRequest,
    attempt: str,
) -> tuple[bool, str]:
    prior = await _reconciled_rows(db, pin.task_id, pin.number)
    if prior:
        existing = prior[0]
        if (
            len(prior) == 1
            and existing["merge_sha"] == facts.merge_sha
            and json.loads(existing["reconciliation"])["pin"] == asdict(pin)
        ):
            return True, f"{RECONCILED}: {facts.merge_sha}"
        return False, "merged_outside_gate: prior_reconciliation_conflicts"
    evidence = {"pin": asdict(pin), "github": asdict(facts), "ci_attempt": attempt}
    cursor = await db.execute(
        "INSERT OR IGNORE INTO pipeline_merges "
        "(project_id, task_id, pr_number, merge_sha, merged_at, reconciliation) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            pin.project_id,
            pin.task_id,
            pin.number,
            facts.merge_sha,
            datetime.fromisoformat(facts.merged_at)
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S"),
            json.dumps(evidence),
        ),
    )
    if not cursor.rowcount:
        return False, "merged_outside_gate: merge_already_attributed"
    await repo.insert_event(
        db,
        kind=RECONCILED,
        task_id=pin.task_id,
        project_id=pin.project_id,
        actor="hub",
        payload=evidence,
    )
    await repo.add_task_update(
        db,
        pin.task_id,
        "hub",
        "status",
        f"Подтверждён мерж вне гейта: {pin.repository}#{pin.number}, "
        f"{facts.merge_sha} в {pin.base}, GitHub {facts.merged_at} ({facts.merged_by}). "
        f"Сдача {pin.generation}, head {pin.head}, подтверждённый CI {attempt}.",
    )
    return True, f"{RECONCILED}: {facts.merge_sha}"
