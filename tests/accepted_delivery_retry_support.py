"""Стенд автоповтора доставки принятой задачи (#1999).

Настоящими остаются база, гейт доставки (``deliver_on_disposition`` ->
``merge_before_completion``) и свип ``scan_completed_deliveries``. Подменён форж
(``tests.delivery_rerun_support.Rig``): ответы по таблицам теста, счёт мержей.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from hub import repository as repo
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.services import lifecycle
from tests.delivery_rerun_support import (
    APPROVED,
    TIP,
    Rig,
    approved_task,
    behind,
    current,
)

PASSED = CIProbeResult(CIProbeOutcome.passed, "checks_passed")
PENDING = CIProbeResult(CIProbeOutcome.pending, "checks_running")
FAILED = CIProbeResult(CIProbeOutcome.failed, "checks_failed", details="run 7001")
TABLE = "accepted_delivery_retries"


class RetryRig(Rig):
    """Принятая (completed) задача с одобренной сдачей и открытым PR."""

    async def retry_rows(self) -> list[dict]:
        got = await self.db.execute_fetchall(
            f"SELECT * FROM {TABLE} WHERE task_id=?", (self.task_id,)
        )
        return [dict(r) for r in got]

    async def retry_row(self) -> dict:
        (only,) = await self.retry_rows()
        return only

    async def deliver(self, via: str = "decide_accept"):
        return await lifecycle.deliver_on_disposition(
            self.db, self.task_id, "deliver", via=via
        )

    def head_moved_to(self, sha: str, *, behind_base: bool = False) -> None:
        """Голова PR стоит на ``sha``; база уже в ней (или ещё нет)."""
        self.git.head_sha = AsyncMock(return_value=sha)
        self.git.pr_head_sha = AsyncMock(return_value=sha)
        self.git.pr_base_freshness = AsyncMock(
            return_value=behind(sha) if behind_base else current(sha)
        )

    def ci(self, probe: CIProbeResult) -> None:
        self.git.check_pr_ci = AsyncMock(return_value=probe)

    async def events(self, kind: str) -> list[dict]:
        got = await self.db.execute_fetchall(
            "SELECT * FROM events WHERE task_id=? AND kind=?", (self.task_id, kind)
        )
        return [dict(r) for r in got]


async def accepted_task(
    db, monkeypatch, *, status: str = "completed", task_id: int | None = None
) -> RetryRig:
    """``task_id`` - заранее одобренная задача: ``approved_task`` зовёт сдачу через
    форж, и после первого стенда это уже форж СТЕНДА, а не пустышка."""
    task_id = task_id or await approved_task(db)
    rig = RetryRig(db, monkeypatch, task_id)
    await repo.update_task(db, task_id, status=status)
    await db.commit()
    return rig


async def armed_rig(db, monkeypatch, *, task_id: int | None = None) -> RetryRig:
    """Решение принять с доставкой получило base_updated: гейт слил базу."""
    rig = await accepted_task(db, monkeypatch, task_id=task_id)
    ok, reason, _ = await rig.deliver()
    assert not ok and reason.startswith("base_updated"), reason
    assert rig.git.update_pr_branch.await_count == 1
    rig.head_moved_to(TIP)  # дальше голова стоит на слитой базе
    rig.ci(PASSED)
    return rig


__all__ = [
    "APPROVED",
    "FAILED",
    "PASSED",
    "PENDING",
    "TIP",
    "RetryRig",
    "approved_task",
]
