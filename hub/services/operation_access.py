"""Resource-level access shared by replay and operation status reads."""

from typing import Any

from hub import repository as repo
from hub.idempotency import error


async def require_scope(
    db: Any, identity: Any, scope_ref: str | None, *, source: str | None = None
) -> None:
    if identity is None:
        return  # trusted in-process service callers
    if not identity.has_permission("tasks.create"):
        raise error(403, "operation_forbidden")
    if source is not None and source != "agent" and not identity.is_human:
        raise error(403, "operation_forbidden")
    if identity.scopes_damaged:
        raise error(403, "operation_forbidden")
    if not identity.scopes:
        return
    slug = (scope_ref or "").removeprefix("project:")
    if slug not in identity.scopes:
        raise error(403, "operation_forbidden")
    project = await repo.get_project_by_slug(db, slug)
    if not project or project["status"] != "active" or project["archived"]:
        raise error(403, "operation_forbidden")


async def task_scope(db: Any, task_id: int) -> str | None:
    # Routing may substitute default for an inactive project; authorization
    # must retain the actual binding (#1644).
    project = await repo.resolve_bound_project(db, task_id)
    return f"project:{project['slug']}" if project else "project:default"
