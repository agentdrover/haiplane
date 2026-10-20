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
