"""#1242 / #1199: наблюдённый отказ конфига не должен навсегда глушить переспрос."""

from __future__ import annotations

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import config
from hub.integrations import cursor_cloud
from hub.services.review_dispatch import sweep_review_dispatches
from tests.test_review_dispatch import (
    _Sequence,
    _age_dispatches,
    _alerts_of,
    _no_local_path,
    _rows_of,
    _submitted,
    _wire,
)


@pytest.fixture
def monkeypatch_mod(monkeypatch):
    return monkeypatch


async def test_observed_missing_config_is_not_blindness_1199(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """Алерт переспроса пишется до вызова; missing-config возвращает False без строки.

    Следующий свип видит asked > retried и называет это слепотой #1199 — даже
    когда отказ наблюдён (нет токена), а не слепой. После возврата конфига
    переспрос должен снова позвать провайдера.
    """
    provider = _Sequence(
        [
            ({"agent": {"id": "bc-dead"}, "run": {"id": "run-dead"}}, None),
            ({"agent": {"id": "bc-fresh"}, "run": {"id": "run-fresh"}}, None),
        ]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)

    task_id = await _submitted(
        client, db, "ask-again-missing-config", policy={"review": "dispatch"}
    )
    assert len(provider.calls) == 1
    await _age_dispatches(db)

    async def _errored(agent_id, run_id):
        if agent_id == "bc-dead":
            return {"id": run_id, "status": "ERROR"}
        return {"id": run_id, "status": "RUNNING"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    await sweep_review_dispatches(db)
    assert any("отчёт НЕ сдан" in a for a in await _alerts_of(db, task_id))

    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "")
    await sweep_review_dispatches(db)
    mid = await _alerts_of(db, task_id)
    assert any("Переспрос ревью" in a for a in mid), mid
    assert any("конфигурации" in a for a in mid), mid
    assert len(await _rows_of(db, task_id)) == 1
    assert len(provider.calls) == 1

    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "reviewer-token")
    await sweep_review_dispatches(db)
    alerts = await _alerts_of(db, task_id)
    assert not any("вслепую" in a for a in alerts), alerts
    assert len(provider.calls) == 2, (
        "наблюдённый отказ конфига не слепота #1199: после возврата "
        "токена переспрос обязан позвать провайдера снова"
    )
