"""Суточный потолок стюарда: значения, группы, ожидание до reset_at (#1915).

Основные сценарии AC-1 и AC-4 живут в tests/test_steward_dispatch.py (их test_ref
названы в карточке); здесь — остальное, что держит ту же границу.
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import config
from hub.services.steward_dispatch import EVENT_REFUSED, KIND_DOR, order_due_runs
from tests.test_steward_dispatch import _events, _project, _submitted_task


@pytest.fixture(autouse=True)
def shadow_mode(monkeypatch):
    from hub.services import steward_ci_wait

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 20)

    async def _not_waiting(_db, _task):
        return steward_ci_wait.NOT_WAITING

    monkeypatch.setattr(steward_ci_wait, "ci_wait_view", _not_waiting)


async def test_the_advisor_cap_defaults_to_the_shared_value(monkeypatch):
    from hub.services import steward_cap

    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 7)
    monkeypatch.setattr(config, "STEWARD_ADVISOR_DAILY_CAP", "")
    assert steward_cap.cap_of("advisor") == 7
    monkeypatch.setattr(config, "STEWARD_ADVISOR_DAILY_CAP", "not-a-number")
    assert steward_cap.cap_of("advisor") == 7, "нечитаемое — это «не задано»"


def test_the_cap_groups_name_the_dispatcher_kinds():
    from hub.services import steward_cap
    from hub.services.steward_dispatch import KIND_ADVISOR, KIND_VERDICT

    assert KIND_VERDICT in steward_cap.kinds_of("verdict_dor")
    assert KIND_DOR in steward_cap.kinds_of("verdict_dor")
    assert KIND_ADVISOR in steward_cap.kinds_of("advisor")
    assert steward_cap.group_of(KIND_ADVISOR) == "advisor"


async def test_a_capped_generation_waits_for_reset_or_for_a_raised_cap(
    db: aiosqlite.Connection, monkeypatch
):
    """#1915: диспетчер суждений не заказывает до reset_at и не держит поднятый потолок."""
    from hub.services import steward_cap
    from tests.steward_cap_support import BEFORE_MIDNIGHT, set_clock

    set_clock(monkeypatch, BEFORE_MIDNIGHT)
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 0)
    project_id = await _project(db, "cap-wait", steward=True)
    task_id = await _submitted_task(db, project_id)

    assert await order_due_runs(db) == 0  # первый отказ: событие и alert
    assert await steward_cap.cap_handoff(db, task_id, 1) is not None
    from hub.services import steward_dispatch

    tried: list[int] = []
    real_order = steward_dispatch.order_run

    async def _counting(*args, **kwargs):
        tried.append(args[1])
        return await real_order(*args, **kwargs)

    monkeypatch.setattr(steward_dispatch, "order_run", _counting)
    assert await order_due_runs(db) == 0  # передана человеку: заказа не пробует
    assert tried == []
    assert len(await _events(db, EVENT_REFUSED)) == 1

    # Владелец поднял потолок среди суток: передача снимается сама.
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 5)
    assert await steward_cap.cap_handoff(db, task_id, 1) is None
    assert await order_due_runs(db) == 1


async def test_a_raise_that_fits_exactly_returns_every_capped_generation(
    db: aiosqlite.Connection, monkeypatch
):
    """Потолок N, N отказов, подъём ровно на N: все заказаны и ни одна не у человека.

    Заказанное после отказа поколение возвращается стюарду насовсем, хотя
    после последнего заказа used == cap и потолок снова «исчерпан».
    """
    from hub.services import steward_cap
    from tests.steward_cap_support import BEFORE_MIDNIGHT, set_clock

    set_clock(monkeypatch, BEFORE_MIDNIGHT)
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 0)
    project_id = await _project(db, "cap-exact", steward=True)
    tasks = [await _submitted_task(db, project_id) for _ in range(3)]
    assert await order_due_runs(db) == 0
    for task_id in tasks:
        assert await steward_cap.cap_handoff(db, task_id, 1) is not None

    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 3)
    assert await order_due_runs(db) == 3
    assert await steward_cap.used_today(db, project_id, "verdict_dor") == 3
    for task_id in tasks:
        assert await steward_cap.cap_handoff(db, task_id, 1) is None, task_id


def test_the_dor_alert_does_not_promise_a_human_verdict():
    from hub.services import steward_cap

    assert "вердикт за человеком" in steward_cap.alert_text(
        "verdict", 1, "д", "2026-10-19"
    )
    assert "вердикт" not in steward_cap.alert_text("dor", 0, "д", "2026-10-19")
