"""Стенд для тестов автоперезапуска CI на свежей базе (#1956).

Настоящими остаются база (файл SQLite), поллер, гейт доставки и проверка
доверия к отчёту CI (``ci_trust`` с ``FakeGitHub``). Подменён только форж: он
отвечает по таблицам, которые заполняет тест, и считает вызовы платного POST.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import aiosqlite

from hub import poller
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopDispatch, NoopGitOps
from hub.integrations.protocols import (
    BaseFreshness,
    BaseFreshnessState,
    BranchUpdateOutcome,
    CIProbeOutcome,
    CIProbeResult,
    CIRunRequestOutcome,
    CIRunRequestResult,
)
from hub.integrations.registry import plugins
from hub.models import TaskCreate, TaskReviewVerdict, TaskUpdateCreate
from hub.services import ci_report, orchestration
from tests.ci_trust_support import FakeGitHub, attach_fake_github

PR = 77
APPROVED = "approved0commit"
TIP = "tip0after0gate0update"
RUN = "5001"
POLICY_KEY = "delivery_fresh_base_rerun"


def behind(head: str) -> BaseFreshness:
    return BaseFreshness(BaseFreshnessState.behind, head_sha=head, behind_by=2)


def current(head: str) -> BaseFreshness:
    return BaseFreshness(BaseFreshnessState.current, head_sha=head, behind_by=0)


async def set_policy(db: aiosqlite.Connection, **keys) -> None:
    """Слить ключи в gate_policy проекта default (None - удалить ключ)."""
    from hub.db import seed_default_project

    await seed_default_project(db)
    row = await db.execute_fetchall(
        "SELECT id, gate_policy FROM projects WHERE slug='default'"
    )
    policy = json.loads(dict(row[0])["gate_policy"] or "{}")
    for key, value in keys.items():
        if value is None:
            policy.pop(key, None)
        else:
            policy[key] = value
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE id=?",
        (json.dumps(policy), dict(row[0])["id"]),
    )
    await db.commit()


class Rig:
    """Задача, дошедшая до красного CI на голове, которую сделал гейт."""

    def __init__(self, db: aiosqlite.Connection, monkeypatch, task_id: int) -> None:
        self.db = db
        self.task_id = task_id
        self.monkeypatch = monkeypatch
        g = NoopGitOps()
        self.git = g
        self.fake: FakeGitHub = attach_fake_github(g)
        g.check_pr_ci = AsyncMock(
            return_value=CIProbeResult(CIProbeOutcome.passed, "ok")
        )
        g.merge_pr = AsyncMock(return_value=True)
        g.merge_commit_sha = AsyncMock(return_value="gate0merge0sha")
        g.pull_main = AsyncMock(return_value=True)
        g.delete_branch = AsyncMock(return_value=True)
        g.fetch_base = AsyncMock(return_value=(True, ""))
        g.head_sha = AsyncMock(return_value=APPROVED)
        g.pr_head_sha = AsyncMock(return_value=TIP)
        g.pr_base_freshness = AsyncMock(return_value=behind(APPROVED))
        g.update_pr_branch = AsyncMock(
            return_value=(BranchUpdateOutcome.updated, "GitHub принял")
        )
        g.branch_diff = AsyncMock(return_value="@@ -1,0 +2 @@\n+авторская строка\n")
        self.post_results: list[CIRunRequestResult] = []
        self.next_attempt_status = "in_progress"
        g.rerun_failed_jobs = AsyncMock(side_effect=self._rerun)
        self.jobs: dict[tuple[str, str], list[dict] | None] = {}
        g.ci_attempt_jobs = AsyncMock(side_effect=self._jobs)
        plugins.git_ops = g
        plugins.dispatch = NoopDispatch()
        monkeypatch.setattr(
            orchestration,
            "project_git_context",
            AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
        )

    # -- форж ---------------------------------------------------------------

    async def _rerun(self, run_id, repo=None, gh_repo=None, forge=""):
        result = (
            self.post_results.pop(0)
            if self.post_results
            else CIRunRequestResult(CIRunRequestOutcome.requested, "rerun_requested")
        )
        if result.outcome is CIRunRequestOutcome.requested:
            self.start_attempt("2", status=self.next_attempt_status)
        return result

    async def _jobs(self, run_id, attempt, repo=None, gh_repo=None, forge=""):
        return self.jobs.get((str(run_id), str(attempt)), [])

    def start_attempt(
        self, attempt: str, *, status="in_progress", conclusion="failure"
    ):
        return self.fake.attempt(
            self.task_id,
            TIP,
            run_id=RUN,
            attempt=attempt,
            status=status,
            conclusion=conclusion,
            event="pull_request",
        )

    def red_first_attempt(self) -> None:
        self.start_attempt("1", status="completed", conclusion="failure")
        self.git.check_pr_ci = AsyncMock(
            return_value=CIProbeResult(
                CIProbeOutcome.failed, "checks_failed", details=f"run {RUN}"
            )
        )

    async def report_attempt(self, attempt: str, *, conclusion="success", **kw):
        """Отчёт попытки, как его пошлёт доверенный репортёр; принят хабом."""
        sent = self.fake.attempt(
            self.task_id,
            TIP,
            run_id=RUN,
            attempt=attempt,
            status="completed",
            conclusion=conclusion,
            event="pull_request",
            **kw,
        )
        await ci_report.accept_ci_run_report(self.db, self.task_id, **sent)

    # -- ход доставки -------------------------------------------------------

    async def sweep(self) -> None:
        await poller._sweep_pair_delivery(self.db)

    async def task(self) -> dict:
        return dict(await repo.get_task(self.db, self.task_id))

    async def feed(self) -> str:
        rows = await repo.get_task_updates(self.db, self.task_id)
        return "\n".join(dict(u)["content"] or "" for u in rows)

    async def rows(self) -> list[dict]:
        got = await self.db.execute_fetchall(
            "SELECT * FROM delivery_reruns WHERE task_id=?", (self.task_id,)
        )
        return [dict(r) for r in got]

    async def row(self) -> dict:
        (only,) = await self.rows()
        return only

    async def needs_decision(self) -> bool:
        return (await self.task())["status"] == "needs_decision"

    async def age_request(self, minutes: int) -> None:
        await self.db.execute(
            "UPDATE delivery_reruns SET requested_at=datetime('now', ?)",
            (f"-{minutes} minutes",),
        )
        await self.db.commit()

    @property
    def posts(self) -> int:
        return self.git.rerun_failed_jobs.await_count


async def approved_task(db: aiosqlite.Connection, pr_number: int = PR) -> int:
    tv = await services.create_task(db, TaskCreate(title="Deliver me"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await repo.update_task(db, tv.id, pr_number=pr_number)
    await db.commit()
    await services.submit_for_review(db, tv.id)
    await repo.update_task(db, tv.id, submission_sha=APPROVED)
    await db.commit()
    await services.record_review_verdict(
        db, tv.id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )
    return tv.id


async def rig_after_base_update(db, monkeypatch, *, policy="on") -> Rig:
    """Гейт слил базу в ветку (событие и, при политике on, долговечная запись)."""
    await set_policy(db, **{POLICY_KEY: policy})
    task_id = await approved_task(db)
    rig = Rig(db, monkeypatch, task_id)
    await rig.sweep()  # отставшая ветка -> update-branch
    assert rig.git.update_pr_branch.await_count == 1
    assert (await rig.task())["status"] == "running"
    rig.git.head_sha = AsyncMock(return_value=TIP)
    rig.git.pr_base_freshness = AsyncMock(return_value=current(TIP))
    return rig


async def report_done(db, task_id: int) -> None:
    await services.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
    )
