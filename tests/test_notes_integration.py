from __future__ import annotations

import pytest

from hub.integrations import notes
from hub.integrations.notes import NotesIntegration


@pytest.mark.asyncio
async def test_save_decision_sends_notes_decision_payload(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_n4l(tool: str, payload=None, timeout: float = 15):
        calls.append((tool, payload or {}))
        return {"id": "decision-page"}

    monkeypatch.setattr(notes, "N4L_SPACE_ID", "space-1")
    monkeypatch.setattr(notes, "_n4l", fake_n4l)

    result = await NotesIntegration().save_decision(
        task_id=42,
        action="accept",
        summary="Review findings are cosmetic.",
        context="Reviewer noted non-blocking style issues.",
    )

    assert result == {"id": "decision-page"}
    assert calls == [
        (
            "notes_decision_save",
            {
                "space_id": "space-1",
                "title": "Task #42: decision=accept",
                "decision": "accept: Review findings are cosmetic.",
                "why": "Reviewer noted non-blocking style issues.",
                "task_id": "task-42",
                "tags": ["hub-decision", "task-42"],
            },
        )
    ]
