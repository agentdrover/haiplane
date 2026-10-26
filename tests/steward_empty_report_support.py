"""Сцена пустого машинного отчёта для самостоятельного approve стюарда (#2082).

Сцена настоящая (проект, задача в review, отчёт, заказ судьи, approve и concur
советника на хеше пакета); подменён только ответ биллинга провайдера
(``cursor_cloud.get_usage``): доступен, ниже порога, расходится с отчётом,
недоступен.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.integrations import cursor_cloud
from tests.steward_double_clean_support import (
    OTHER,
    SHA,
    Case,
    build_case,
    judge_and_advise,
    policy_of,
)

PROVEN = 250_000
DOWN = "down"


async def add_report(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    raw_count: int = 0,
    tokens_spent: int | None = PROVEN,
    generation: int = 1,
) -> int:
    """Отчёт ревью без находок; ``raw_count`` - сколько кандидатов он видел."""
    review_id = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill="multi-agent-review",
        harness_version=1,
        agent_count=3,
        tokens_spent=tokens_spent,
        duration_ms=1000,
        orchestrator="cursor",
        model="grok-4.6",
        raw_count=raw_count,
        findings_confirmed="[]",
        findings_rejected=json.dumps(
            [{"title": f"кандидат {i}"} for i in range(raw_count)]
        ),
        unresolved="[]",
        incomplete=False,
        submitted_by="reviewer",
        self_reviewed=False,
        principal_id=OTHER,
    )
    await db.execute(
        "UPDATE machine_reviews SET reviewed_sha=?, pin_verified=1 WHERE id=?",
        (SHA, review_id),
    )
    await db.commit()
    return review_id


async def settle_dispatch(
    db: aiosqlite.Connection,
    task_id: int,
    monkeypatch,
    *,
    usage: int | str | None,
    generation: int = 1,
    report_id: int | None = None,
) -> list[tuple[Any, ...]]:
    """Завершённый заказ хаба и подставленный ответ биллинга; журнал обращений к нему.

    ``report_id`` - отчёт, который этот заказ породил (привязка #1946).
    """
    dispatch_id = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id="bc-proof",
        run_id="run-proof",
        model="grok-4.6",
    )
    await repo.set_review_dispatch_status(db, dispatch_id, "done")
    if report_id is not None:
        await db.execute(
            "UPDATE machine_reviews SET dispatch_id=? WHERE id=?",
            (dispatch_id, report_id),
        )
    await db.commit()
    calls: list[tuple[Any, ...]] = []

    async def _usage(agent_id, run_id=None):
        calls.append((agent_id, run_id))
        if usage == DOWN:
            raise RuntimeError("provider unavailable")
        return None if usage is None else {"totalUsage": {"totalTokens": usage}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    return calls


async def empty_case(
    db: aiosqlite.Connection,
    monkeypatch,
    slug: str,
    *,
    risk: str = "R1",
    raw_count: int = 0,
    tokens_spent: int | None = PROVEN,
    usage: int | str | None = PROVEN,
) -> tuple[Case, list[tuple[Any, ...]]]:
    """Сдача с отчётом ``raw_count`` и ответом биллинга ``usage``, готовая к применению."""
    case = await build_case(
        db,
        monkeypatch,
        slug,
        risk=risk,
        policy=policy_of(ordinary=None, clean=None),
        pair=False,
        order=False,
    )
    report = await add_report(
        db, case.task_id, raw_count=raw_count, tokens_spent=tokens_spent
    )
    calls = await settle_dispatch(
        db, case.task_id, monkeypatch, usage=usage, report_id=report
    )
    await judge_and_advise(db, case.task_id)
    return case, calls
