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


async def require_operation_scope(
    db: Any, identity: Any, row: Any, *, source: str | None = None
) -> None:
    """Re-check the CURRENT binding of the operation's object (review, major 4).

    ``scope_ref`` is a snapshot of where the object lived when the operation was
    recorded. An epic moved to a closed project still carries the old slug in
    that column, so a replay would keep serving a result the caller may no
    longer see (T3 §4.12, resource-level rights are rechecked on every read).
    The resource id is stored next to the snapshot for exactly this check.

    Deleted object policy: the current binding cannot be established at all, so
    the read fails closed with 410 ``operation_resource_gone`` instead of
    handing out a snapshot whose access can no longer be proven. The key and
    the outbox rows stay for audit until their own retention removes them.
    """
    if identity is None:
        return  # trusted in-process service callers
    resource_id = row["resource_id"] if "resource_id" in row.keys() else None
    if resource_id is None:
        # Keys written before the resource id existed (and non-create
        # operations in T11c) still have only the saved scope.
        await require_scope(db, identity, row["scope_ref"], source=source)
        return
    if await repo.get_task(db, int(resource_id)) is None:
        raise error(410, "operation_resource_gone")
    scope = await task_scope(db, int(resource_id))
    await require_scope(db, identity, scope, source=source)


async def task_scope(db: Any, task_id: int) -> str | None:
    # Routing may substitute default for an inactive project; authorization
    # must retain the actual binding (#1644).
    project = await repo.resolve_bound_project(db, task_id)
    return f"project:{project['slug']}" if project else "project:default"
