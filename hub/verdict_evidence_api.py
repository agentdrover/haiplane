"""Маршрут GET /api/tasks/{id}/verdict-evidence (#1697).

Вынесен из ``hub/app.py``: у того бюджет размера (#1669) без запаса, а маршрут —
одна тонкая обёртка над ``hub.services.verdict_evidence``. Доступ держит
``require_permission``: без него наблюдатель прошёл бы по правилу «любой GET».
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from hub.auth import require_permission
from hub.services import verdict_evidence

router = APIRouter()


@router.get("/api/tasks/{task_id}/verdict-evidence")
async def api_verdict_evidence(
    task_id: int,
    request: Request,
    generation: int | None = None,
    identity=Depends(require_permission("verdict.evidence.read")),
):
    """Суждение стюарда, машинный отчёт и доверие CI ЭТОГО поколения сдачи."""
    conn = getattr(request.state, "db", None) or request.app.state.db
    found = await verdict_evidence.build(conn, task_id, generation)
    if found is None:
        raise HTTPException(404, "task not found")
    return found
