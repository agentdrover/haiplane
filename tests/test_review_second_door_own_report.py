"""#1982: заглушка во второй двери со СВОИМ отчётом идёт общим путём закрытия.

Отчёт облачного ревьюера пришёл раньше строки заказа, поздняя привязка (#1946)
догнала, ``owe_second_door`` перевёл строку в ``second_door``. Раньше такая
строка закрывалась ``done`` напрямую: без поиска агента по метке и без
сверки расхода. Теперь её закрывает тот же писатель, что и обычный заказ.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.services import review_dispatch as dispatcher
from hub.services.review_dispatch import sweep_review_dispatches
from tests.review_dispatch_support import (
    _a_report_from,
    _DispatchRecorder,
    _local_dispatches,
    _local_principal,
    _pinned_setup,
    _reporting_stub,
    _stub_reviewer,
    _submitted,
    _wire,
)

_BILL = {"totalUsage": {"totalTokens": 424242}}


class _Provider:
    """Подставной провайдер: что ответит список агентов и счёт."""

    def __init__(self, monkeypatch, *, seen=None, usage=None) -> None:
        self.finds = 0
        self.usages = 0
        self.seen = seen or cursor_cloud.Reconciliation("bc-found", "r-9", True)
        self.usage = usage
        monkeypatch.setattr(cursor_cloud, "find_agent_by_name", self._find)
        monkeypatch.setattr(cursor_cloud, "get_usage", self._get_usage)

    async def _find(self, name: str, pages: int = 3):
        self.finds += 1
        return self.seen

    async def _get_usage(self, agent_id: str, run_id: str | None = None):
        self.usages += 1
        return self.usage


async def _owed_with_own_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path, slug: str
) -> tuple[int, int]:
    """Заглушка second_door без агента + собственный отчёт облачного ревьюера."""
    _wire(monkeypatch, _DispatchRecorder({"agent": {"id": "bc-x"}, "run": {"id": "r"}}))
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    task_id = await _submitted(
        client,
        db,
        slug,
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await db.execute(
        "UPDATE review_dispatches SET agent_id='', run_id='', "
        "status='second_door', run_status='ERROR', "
        "created_at=datetime('now', '-90 minutes') WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
    await _a_report_from(db, task_id, cloud_pid, "cloud-reviewer")
    return task_id, cloud_pid


async def _row(db: aiosqlite.Connection, task_id: int) -> dict[str, Any]:
    rows = await db.execute_fetchall(
        "SELECT * FROM review_dispatches WHERE task_id=? AND channel='cloud'",
        (task_id,),
    )
    return dict(rows[0])


async def _alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [
        dict(r)["content"]
        for r in await repo.get_task_updates(db, task_id)
        if dict(r)["kind"] == "alert"
    ]


async def test_own_report_settles_through_the_cost_path(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-1: агент восстановлен, расход на заказе и на отчёте, второй двери нет."""
    task_id, _ = await _owed_with_own_report(client, db, monkeypatch, tmp_path, "own-a")
    provider = _Provider(monkeypatch, usage=_BILL)

    await sweep_review_dispatches(db)
    await dispatcher.wait_for_local_runs()
    await db.commit()

    row = await _row(db, task_id)
    assert row["agent_id"] == "bc-found", "агент восстановлен по метке"
    assert row["status"] == "done"
    assert row["provider_tokens"] == 424242, "расход записан на заказе"
    (report,) = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert dict(report)["provider_tokens"] == 424242, "и на отчёте"
    assert await _local_dispatches(db, task_id) == [], "второй прогон не куплен"

    finds = provider.finds
    await sweep_review_dispatches(db)
    await dispatcher.wait_for_local_runs()
    await db.commit()
    assert await _local_dispatches(db, task_id) == []
    assert provider.finds == finds, "закрытая строка не разбирается повторно"


async def test_confirmed_absence_names_the_bill_unavailable_and_closes(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Подтверждённое отсутствие агента: явная недоступность счёта, done."""
    task_id, _ = await _owed_with_own_report(client, db, monkeypatch, tmp_path, "own-b")
    _Provider(monkeypatch, seen=cursor_cloud.Reconciliation("", "", True))

    await sweep_review_dispatches(db)
    await dispatcher.wait_for_local_runs()
    await db.commit()

    assert (await _row(db, task_id))["status"] == "done"
    assert any(
        "Счёт прогона по заказу" in a and "недоступен" in a
        for a in await _alerts(db, task_id)
    )
    assert await _local_dispatches(db, task_id) == []


async def test_a_failed_listing_keeps_the_debt_for_a_retry(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Сбой списка (не ответ): строка остаётся, следующий проход повторяет."""
    task_id, _ = await _owed_with_own_report(client, db, monkeypatch, tmp_path, "own-c")
    # отчёт свежий: потолок 60 минут не вышел
    await db.execute("UPDATE machine_reviews SET created_at=datetime('now')")
    await db.commit()
    provider = _Provider(monkeypatch, seen=cursor_cloud.Reconciliation("", "", False))

    await sweep_review_dispatches(db)
    await dispatcher.wait_for_local_runs()
    await db.commit()
    assert (await _row(db, task_id))["status"] == "second_door"
    assert await _local_dispatches(db, task_id) == []

    provider.seen = cursor_cloud.Reconciliation("bc-later", "r-2", True)
    provider.usage = _BILL
    await sweep_review_dispatches(db)
    await db.commit()
    row = await _row(db, task_id)
    assert row["status"] == "done" and row["agent_id"] == "bc-later"
    assert provider.finds == 2


async def test_unknown_bill_stays_open_for_the_deferred_recount(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Расход не назван: заказ закрыт, счёт помечен открытым для сверки позже."""
    task_id, _ = await _owed_with_own_report(client, db, monkeypatch, tmp_path, "own-d")
    _Provider(monkeypatch, usage=None)

    await sweep_review_dispatches(db)
    await db.commit()

    row = await _row(db, task_id)
    assert row["status"] == "done"
    assert row["provider_tokens"] is None, "угаданный ноль не пишется"
    assert row["usage_scope"] == dispatcher.USAGE_OPEN


async def test_a_report_landing_during_the_opening_of_the_replacement_is_billed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-1 (вторая половина): отчёт во время открытия замены, агента у заглушки нет."""
    _wire(monkeypatch, _DispatchRecorder({"agent": {"id": "bc-x"}, "run": {"id": "r"}}))
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    task_id = await _submitted(
        client,
        db,
        "own-race",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await db.execute(
        "UPDATE review_dispatches SET agent_id='', run_id='', "
        "status='second_door', run_status='ERROR' WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
    provider = _Provider(monkeypatch, usage=_BILL)
    real_prepare = dispatcher.prepare_review_order
    landed: list[int] = []

    async def _report_lands_during_prep(*args, **kwargs):
        if not landed:
            landed.append(
                await _a_report_from(db, task_id, cloud_pid, "cloud-reviewer")
            )
        return await real_prepare(*args, **kwargs)

    monkeypatch.setattr(dispatcher, "prepare_review_order", _report_lands_during_prep)

    await sweep_review_dispatches(db)
    await dispatcher.wait_for_local_runs()
    await db.commit()

    assert landed, "предпосылка: отчёт доехал в окно подготовки замены"
    assert await _local_dispatches(db, task_id) == []
    row = await _row(db, task_id)
    assert row["status"] == "done"
    assert row["agent_id"] == "bc-found" and row["provider_tokens"] == 424242
    assert provider.finds >= 1
