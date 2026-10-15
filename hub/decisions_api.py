"""Маршрут GET /api/decisions (#1698).

Вынесен из ``hub/app.py`` по той же причине, что и verdict_evidence_api: у
app.py бюджет размера без запаса, а маршрут — тонкая обёртка над
``hub.services.decisions``. Права те же, что у /api/dashboard: отдельного
permission нет, доступ решает middleware (allowlist steward, chat-pair,
ci_reader/ci_auditor; watcher читает любой GET /api/*).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from hub.services import dashboard, decisions

router = APIRouter()


@router.get("/api/decisions")
async def api_decisions(
    request: Request,
    project: str | None = None,
    limit: int = Query(default=decisions.DEFAULT_LIMIT, ge=1, le=decisions.MAX_LIMIT),
) -> dict[str, Any]:
    """Записи ленты задач kind=decision с происхождением (не «решения владельца»)."""
    conn = getattr(request.state, "db", None) or request.app.state.db
    project_id = await dashboard.project_id_for(conn, project)
    rows = await decisions.list_decisions(conn, project_id=project_id, limit=limit)
    return {"decisions": rows, "source": "hub"}


@router.get("/api/integrations/notes")
async def api_notes_gone() -> JSONResponse:
    """Связка через CLI удалена; ответ фиксирован, без плагина и внешних вызовов."""
    return JSONResponse(
        status_code=410,
        content={"detail": ("связка через CLI удалена; handoff — служба n4l (#1505)")},
    )
