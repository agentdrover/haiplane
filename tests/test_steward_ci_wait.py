"""Судья стюарда не стартует, пока отчёт CI по сдаче ещё в пути (#1695).

Прод 14.10: #1691 и #1692 — escalate по ci_pinned_sha absent, пока шёл прогон
PR; через 2–15 минут отчёт стал verified, а суждение на (task, generation,
kind) одно (#1022). Три пути старта судьи — заказ, старт размещённого заказа и
разбор просроченного неначатого заказа — проходят один страж
(``review_wait_gate``); тесты ставят настоящие строки ci_run_reports и
ci_report_attempts, а не подменяют читатель.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services import steward_shadow as sh
from hub.services.steward_ci_wait import EVENT_JUDGED_WITHOUT_CI, ci_wait_view
from hub.services.steward_dispatch import (
    EVENT_DEFERRED,
    RUN_OPEN,
    open_run,
    order_due_runs,
    order_run,
    runs_today,
    sweep_steward_runs,
)
from tests.test_steward_dispatch import (
    _due_task,
    _events,
    _run_row,
    _start_env,
    _submitted_minutes_ago,
)

SHA = "a" * 40
CREATED = ({"agent": {"id": "agent-1"}, "run": {"id": "run-1"}}, None)


@pytest.fixture(autouse=True)
def shadow_mode(monkeypatch):
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 20)
    monkeypatch.setattr(config, "STEWARD_RUN_DEADLINE_MIN", 30)
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 120)


async def _ci(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    trust: str,
    reason: str = "",
    final: bool = False,
    key: str = "ci:7:1",
    attempts: tuple[tuple[str, str, bool], ...] = (("7", "1", False),),
    sha: str = SHA,
    checks: str = "{}",
) -> None:
    """Проекция по коммиту и строки попыток — то, что оставляет проверка CI."""
    await db.execute("DELETE FROM ci_run_reports WHERE task_id=?", (task_id,))
    await db.execute("DELETE FROM ci_report_attempts WHERE task_id=?", (task_id,))
    await db.execute(
        "INSERT INTO ci_run_reports (task_id, head_sha, checks, validation_status, "
        "trust, trust_reason, attempt_key) VALUES (?, ?, ?, 'pass', ?, ?, ?)",
        (task_id, sha, checks, trust, reason, key),
    )
    for run_id, attempt, is_final in attempts:
        await db.execute(
            "INSERT INTO ci_report_attempts (task_id, head_sha, run_id, run_attempt, "
            "trust, trust_reason, trust_final) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                sha,
                run_id,
                attempt,
                trust,
                reason,
                1 if (is_final or final) else 0,
            ),
        )
    await db.commit()


async def _incomplete(db: aiosqlite.Connection, task_id: int) -> None:
    await _ci(db, task_id, trust="unverified", reason="current_attempt_incomplete")


async def _verified(db: aiosqlite.Connection, task_id: int) -> None:
    await _ci(db, task_id, trust="verified", attempts=(("7", "1", True),))


async def _ready_task(db: aiosqlite.Connection, slug: str, minutes: int = 3) -> int:
    """Сдача судьи: отчёт машинного ревью есть, сдана `minutes` минут назад."""
    project_id, task_id = await _due_task(db, slug)
    await db.execute(
        "UPDATE projects SET repo=? WHERE id=?", (f"agentdrover/{slug}", project_id)
    )
    await repo.update_task(db, task_id, submission_model="claude-opus-5")
    await db.commit()
    # Ревьюер не из семейства судьи, иначе старт откажет по другой причине.
    await db.execute(
        "INSERT INTO machine_reviews "
        "(task_id, submission_generation, model, submitted_by) "
        "VALUES (?, 1, 'grok-4.6', 'rev-agent')",
        (task_id,),
    )
    await db.commit()
    await _submitted_minutes_ago(db, task_id, minutes)
    return task_id


async def _steward_rows(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE task_id=?", (task_id,))
    return [dict(r) for r in rows]


async def _placed_order(db: aiosqlite.Connection, task_id: int) -> dict:
    """Заказ, размещённый мимо стража (как до этой задачи или до CI)."""
    run = await order_run(db, task_id, 1)
    assert run is not None
    return run


async def test_judge_waits_for_verified_ci_on_order_and_start(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-1: unverified, trust_final=0, окно не вышло — ни заказа, ни старта."""
    _start_env(monkeypatch)
    ordering = await _ready_task(db, "ci-wait-order")
    starting = await _ready_task(db, "ci-wait-start")
    await _incomplete(db, ordering)
    await _incomplete(db, starting)
    order = await _placed_order(db, starting)

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=CREATED),
    ) as provider:
        assert await order_due_runs(db) == 0
        assert await order_due_runs(db) == 0
        assert await sh.start_run(db, order) is False
        assert provider.await_count == 0, "провайдер позван, пока CI не подтверждён"

    assert await _steward_rows(db, ordering) == [], "заказ размещён при неполном CI"
    held = await _run_row(db, order["id"])
    assert held["status"] == RUN_OPEN and held["agent_id"] == ""
    assert await _events(db, EVENT_JUDGED_WITHOUT_CI) == []

    await _verified(db, ordering)
    await _verified(db, starting)
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=CREATED),
    ) as provider:
        assert await order_due_runs(db) == 1, "verified не вернул заказ в ближайший тик"
        assert await sh.start_due_runs(db) == 2
        assert provider.await_count == 2
    assert (await _run_row(db, order["id"]))["agent_id"] == "agent-1"
    assert await _events(db, EVENT_JUDGED_WITHOUT_CI) == []


async def test_window_expiry_starts_judge_with_one_feed_line(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-2: окно вышло — судим как прежде, в ленте ровно одна строка."""
    _start_env(monkeypatch)
    task_id = await _ready_task(db, "ci-wait-window", minutes=130)
    await _incomplete(db, task_id)

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=CREATED),
    ) as provider:
        assert await order_due_runs(db) == 1
        assert await order_due_runs(db) == 0
        assert await sh.start_due_runs(db) == 1
        assert await sh.start_due_runs(db) == 0
        assert provider.await_count == 1

    lines = await _events(db, EVENT_JUDGED_WITHOUT_CI)
    assert len(lines) == 1, "строка в ленте одна на поколение, на обоих путях"
    payload = json.loads(lines[0]["payload"])
    assert payload["generation"] == 1
    assert "ждали 130 мин" in payload["detail"]
    assert "current_attempt_incomplete" in payload["detail"]


@pytest.mark.parametrize(
    ("case", "kwargs", "waits"),
    [
        ("verified_green", {"trust": "verified", "attempts": (("7", "1", True),)}, 0),
        (
            "verified_red",
            {
                "trust": "verified",
                "attempts": (("7", "1", True),),
                "checks": json.dumps({"tests": "fail"}),
            },
            0,
        ),
        (
            "unverified_final_current",
            {"trust": "unverified", "reason": "artifact_missing", "final": True},
            0,
        ),
        (
            "final_only_on_past_attempt",
            {
                "trust": "unverified",
                "reason": "current_attempt_incomplete",
                "key": "ci:7:1",
                "attempts": (("6", "1", True), ("7", "1", False)),
            },
            1,
        ),
        (
            "key_without_attempt_row",
            {
                "trust": "unverified",
                "reason": "newer_attempt_without_report",
                "key": "ci:9:1",
                "attempts": (("7", "1", True),),
            },
            1,
        ),
        (
            "unverified_not_final_any_reason",
            {"trust": "unverified", "reason": "forge_unavailable"},
            1,
        ),
        (
            "verified_report_of_another_sha",
            {"trust": "verified", "attempts": (("7", "1", True),), "sha": "b" * 40},
            1,
        ),
    ],
)
async def test_ci_wait_decision_table(
    db: aiosqlite.Connection, case: str, kwargs: dict, waits: int
):
    """AC-3: когда читатель ждёт, а когда нет (по trust и trust_final текущей попытки)."""
    task_id = await _ready_task(db, f"ci-table-{case}".replace("_", "-"))
    await _ci(db, task_id, **kwargs)
    task = dict(await repo.get_task(db, task_id))

    view = await ci_wait_view(db, task)

    assert view.waiting is bool(waits), (case, view)


async def test_ci_wait_decision_table_no_report_waits_until_the_window_ends(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-3, строка «отчёта нет»: ждём до окна, потом судим (неизвестность)."""
    task_id = await _ready_task(db, "ci-table-no-report", minutes=5)
    task = dict(await repo.get_task(db, task_id))
    assert (await ci_wait_view(db, task)).waiting is True
    assert await order_due_runs(db) == 0, (
        "отчёта CI нет, окно идёт — заказа быть не должно"
    )

    await db.execute("DELETE FROM submissions WHERE task_id=?", (task_id,))
    await _submitted_minutes_ago(db, task_id, 121)
    assert await order_due_runs(db) == 1, "окно вышло — судим как прежде"


async def test_overdue_order_not_closed_while_waiting_for_ci(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-4: просроченный неначатый заказ не закрывается never_started, пока ждём CI."""
    _start_env(monkeypatch)
    task_id = await _ready_task(db, "ci-wait-overdue")
    await _incomplete(db, task_id)
    run = await _placed_order(db, task_id)
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now', '-45 minutes') WHERE id=?",
        (run["id"],),
    )
    await db.commit()

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=CREATED),
    ) as provider:
        await sweep_steward_runs(db)
        await sweep_steward_runs(db)
        assert provider.await_count == 0

    row = await _run_row(db, run["id"])
    assert row["status"] == RUN_OPEN, row["closed_reason"]
    fresh = await fetchall(
        db,
        "SELECT 1 FROM steward_runs WHERE id=? AND deadline_at > datetime('now')",
        (run["id"],),
    )
    assert list(fresh), "окно старта не сдвинуто: слот закроется never_started"
    assert await open_run(db, task_id, 1) is not None

    await _verified(db, task_id)
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=CREATED),
    ) as provider:
        await sweep_steward_runs(db)
        assert provider.await_count == 1, "CI подтверждён — слот должен стартовать"


async def test_waiting_keeps_at_most_once_and_buys_nothing(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-5: пока ждём — ни провайдера, ни квоты; после verified суждение одно."""
    _start_env(monkeypatch)
    task_id = await _ready_task(db, "ci-wait-once")
    project = await repo.resolve_project_for_task(db, task_id)
    await _incomplete(db, task_id)
    reviews_before = await fetchall(db, "SELECT id FROM review_dispatches")

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=CREATED),
    ) as provider:
        for _ in range(3):
            assert await order_due_runs(db) == 0
            assert await sh.start_due_runs(db) == 0
        assert provider.await_count == 0
        assert await runs_today(db, int(project["id"])) == 0
        assert await _steward_rows(db, task_id) == []

        await _verified(db, task_id)
        for _ in range(3):
            await order_due_runs(db)
            await sh.start_due_runs(db)
        assert provider.await_count == 1

    assert len(await _steward_rows(db, task_id)) == 1, "суждение поколения одно"
    assert await fetchall(db, "SELECT id FROM review_dispatches") == reviews_before, (
        "поздний verified не должен создавать заказ машинного ревью"
    )
    assert len(await _events(db, EVENT_DEFERRED)) == 1
