"""Часы хаба для тестов суточного потолка стюарда (#1915).

Граница UTC-суток ставится часами ``steward_cap.utcnow``; тесты подменяют их
фиксированным моментом, а не спят и не зависят от того, когда запущен набор.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hub.services import steward_cap

BEFORE_MIDNIGHT = "2026-10-19T23:58:00+00:00"
AFTER_MIDNIGHT = "2026-10-20T00:02:00+00:00"


def set_clock(monkeypatch, moment: str) -> None:
    fixed = datetime.fromisoformat(moment).astimezone(UTC)
    monkeypatch.setattr(steward_cap, "utcnow", lambda: fixed)


async def hand_to_the_human_by_the_cap(
    db, monkeypatch, task_id: int, project_id: int, generation: int = 1
) -> None:
    """Активный cap-handoff: отказ ``daily_cap`` по заказу вердикта, потолок исчерпан.

    Часы уходят на пару минут вперёд, чтобы отказ был позже уже созданных
    заказов сдачи (иначе заказ после отказа снимает передачу).
    """
    from datetime import timedelta

    from hub import config

    later = steward_cap.utcnow() + timedelta(minutes=2)
    monkeypatch.setattr(steward_cap, "utcnow", lambda: later)
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 0)
    await steward_cap.record_refusal(
        db, task_id, project_id, generation, "verdict", "потолок"
    )
    await db.commit()
