"""Общее для тестов «CI стал verified - будим автопилота и повторяем применение» (#2054)."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.services import ci_trust
from hub.services.ci_report import VALIDATION_PASS, settle_ci_run_report
from tests.ci_trust_support import VERIFIED_ROW, upsert_hub_report

SHA = "a" * 40
WAKE_EVENT = "ci_verified_wake"
RETRY_EVENT = "steward_apply_retry"


async def ci_report_arrives(
    db: aiosqlite.Connection, monkeypatch, task_id: int, sha: str = SHA
) -> dict:
    """Пришёл отчёт CI, и GitHub его подтвердил: тот же вход, что у ручки отчёта."""

    async def confirmed(_db, _task_id, _sha, **_kw):
        return ci_trust.Standing(trust=ci_trust.VERIFIED, reason="")

    monkeypatch.setattr(ci_trust, "refresh_quietly", confirmed)
    stored = {"head_sha": sha, "ac_ignored": [], "validation_status": VALIDATION_PASS}
    return await settle_ci_run_report(db, task_id, stored)


async def seed_verified_ci(db: aiosqlite.Connection, task_id: int, sha: str) -> None:
    """В базе лежит отчёт CI закреплённого коммита, подтверждённый GitHub."""
    await upsert_hub_report(
        db,
        task_id=task_id,
        head_sha=sha,
        ac_results="{}",
        validation_status=VALIDATION_PASS,
        validation_log="",
        reason="",
        reported_by="ci",
        **VERIFIED_ROW,
    )
    await db.commit()


async def events_of(db: aiosqlite.Connection, kind: str, task_id: int) -> list[dict]:
    rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind=? AND task_id=? ORDER BY id",
        (kind, task_id),
    )
    return [json.loads(dict(r)["payload"] or "{}") for r in rows]


async def count_events(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await fetchall(
        db, "SELECT COUNT(*) AS n FROM events WHERE task_id=?", (task_id,)
    )
    return int(dict(rows[0])["n"])


async def judgement_of(db: aiosqlite.Connection, task_id: int) -> dict[str, Any]:
    return dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))


async def fail_the_apply(
    db: aiosqlite.Connection, monkeypatch, exc: Exception | None = None, gap: str = ""
) -> None:
    """Поллер применяет approve, а запись вердикта отказывает: сбой применения.

    ``gap`` - как если бы CI закреплённого коммита не был подтверждён (``ci_not_green``
    изнутри транзакции записи); ``exc`` - любое другое исключение той же точки.
    """
    from hub.services import steward_advisor, steward_apply

    with monkeypatch.context() as scoped:
        if exc is not None:

            async def breaks(*_args, **_kwargs):
                raise exc

            scoped.setattr(steward_apply, "ceiling_gate", breaks)
        else:

            async def not_green(*_args, **_kwargs):
                return gap

            scoped.setattr(ci_trust, "pass_gap", not_green)
        assert await steward_advisor.apply_advisor_outcomes(db) == 1
