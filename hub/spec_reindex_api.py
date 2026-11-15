"""Маршрут переиндексации спецификации (#2223).

Вынесен из ``hub/app.py``: у того бюджет размера (#1669) без запаса. Маршрут -
тонкая обёртка над ``hub.services.spec_reindex.reindex_project``; ту же команду
зовут CLI (``oc-hub spec reindex``) и MCP (``hub_reindex_spec``) через этот путь.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from hub.models import SpecIndexResult
from hub.services import spec_reindex

router = APIRouter()

_REFUSED_HINT = (
    "Fix the named spec files on the default branch and run the command again: "
    "the previous index is kept whole"
)
_UNREADABLE_HINT = (
    "Git did not answer; this says nothing about the spec files. "
    "Check the project workspace and run the command again"
)


def _conn(request: Request):
    return getattr(request.state, "db", None) or request.app.state.db


def _refusal(result: SpecIndexResult) -> HTTPException:
    """409 при отказе (файл назван), 503 при неизвестности (git не ответил)."""
    refused = result.state == "refused"
    return HTTPException(
        status_code=409 if refused else 503,
        detail={
            "reason": "spec_reindex_refused" if refused else "spec_reindex_unreadable",
            "message": result.detail,
            "issues": [issue.model_dump() for issue in result.issues],
            "hint": _REFUSED_HINT if refused else _UNREADABLE_HINT,
            "actor_hint": "agent",
        },
    )


@router.post("/api/projects/{project_id}/spec/reindex", response_model=SpecIndexResult)
async def api_reindex_spec(project_id: int, request: Request):
    """Перечитать спецификацию проекта по ref его базовой ветки и заменить индекс.

    Одна команда для человека и агента. Ошибка разбора или дубль id отказывают
    её с именами файлов; прежний индекс остаётся целиком. Блоки слитого коммита
    без одобренного снимка пропущены и названы в ``skipped``.
    """
    try:
        result = await spec_reindex.reindex_project(_conn(request), project_id)
    except LookupError as exc:
        raise HTTPException(404, "project not found") from exc
    if result.state != "indexed":
        raise _refusal(result)
    return result
