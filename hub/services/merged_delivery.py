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

    def merge_identity(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "repository": self.repository,
            "base": self.base,
            "number": self.number,
            "branch": self.branch,
            "head": self.head,
        }


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
        if await repo.reconciled_merge_recorded(db, int(task["id"]), number):
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
    except github_merged_delivery.MergeEvidenceUnavailable as exc:
        return False, f"merge_commit_unverified: {exc}"
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


async def completion_gap(db: aiosqlite.Connection, snapshot: dict[str, Any]) -> str:
    """Explain what changed after a proved reconciliation, while holding the lock."""
    proof = snapshot.get("_reconciled_merge")
    if proof is None:
        return ""
    pin, standing = proof
    live = await repo.get_task(db, pin.task_id)
    if live is None or await _pin(db, dict(live)) != pin:
        return "условия текущей сдачи изменились"
    if not await _ci_holds(db, pin, standing):
        return "результат CI изменился"
    return ""


async def note_completion_wait(
    db: aiosqlite.Connection, task: dict[str, Any], reason: str
) -> None:
    """Record the wait in the same transaction as the failed recheck."""
    text = (
        f"PR #{task.get('pr_number')} влит. Завершение ждёт повторной сверки: {reason}."
    )
    updates = await repo.get_task_updates(db, int(task["id"]))
    if not updates or updates[-1]["content"] != text:
        await repo.add_task_update(db, int(task["id"]), "hub", "status", text)


async def _reconciled_rows(db: aiosqlite.Connection, task_id: int, pr_number: int):
    return list(
        await fetchall(
            db,
            "SELECT id, merge_sha, reconciliation FROM pipeline_merges "
            "WHERE task_id=? AND pr_number=? AND reconciliation<>''",
            (task_id, pr_number),
        )
    )


async def _insert(
    db: aiosqlite.Connection,
    pin: _Pin,
    facts: github_merged_delivery.MergedPullRequest,
    attempt: str,
) -> tuple[bool, str]:
    prior = await _reconciled_rows(db, pin.task_id, pin.number)
    evidence = {"pin": asdict(pin), "github": asdict(facts), "ci_attempt": attempt}
    if prior:
        changed = await _renew(db, pin, facts, evidence, prior)
        if changed is None:
            return False, "merged_outside_gate: prior_reconciliation_conflicts"
        if not changed:
            return True, f"{RECONCILED}: {facts.merge_sha}"
    elif not await _append(db, pin, facts, evidence):
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
        ("Мерж вне гейта повторно сверён" if prior else "Подтверждён мерж вне гейта")
        + f": {pin.repository}#{pin.number}, "
        f"{facts.merge_sha} в {pin.base}, GitHub {facts.merged_at} ({facts.merged_by}). "
        f"Сдача {pin.generation}, head {pin.head}, подтверждённый CI {attempt}.",
    )
    return True, f"{RECONCILED}: {facts.merge_sha}"


async def _renew(
    db: aiosqlite.Connection,
    pin: _Pin,
    facts: github_merged_delivery.MergedPullRequest,
    evidence: dict[str, Any],
    prior: list[aiosqlite.Row],
) -> bool | None:
    """Re-attest the same immutable merge under current approval and CI."""
    if len(prior) != 1 or prior[0]["merge_sha"] != facts.merge_sha:
        return None
    existing = json.loads(prior[0]["reconciliation"])
    if any(
        existing["pin"].get(key) != value for key, value in pin.merge_identity().items()
    ):
        return None
    if (
        existing["pin"] == evidence["pin"]
        and existing["ci_attempt"] == evidence["ci_attempt"]
    ):
        return False
    evidence["first_attestation"] = existing.get("first_attestation", existing)
    await db.execute(
        "UPDATE pipeline_merges SET reconciliation=? WHERE id=?",
        (json.dumps(evidence), prior[0]["id"]),
    )
    return True


async def _append(
    db: aiosqlite.Connection,
    pin: _Pin,
    facts: github_merged_delivery.MergedPullRequest,
    evidence: dict[str, Any],
) -> bool:
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
    return bool(cursor.rowcount)
