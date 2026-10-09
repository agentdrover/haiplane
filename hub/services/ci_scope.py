"""Project binding of the CI key (#1644).

A ``ci_runner`` key can write what the hub's gates believe: the CI report of a
commit (#546) and the deploy callback (#495). Before this module the key was
valid for every project, so the key of one repository could green the tasks of
another and overwrite a stored report. ``api_keys.scopes`` now binds a key to
projects; both entrances call :func:`enforce_ci_project_scope` BEFORE any write.

- A key with scopes may speak only for those projects.
- A key whose scopes are damaged is refused (fail closed), never read as
  unrestricted.
- A key without scopes (legacy) keeps working, and the hub records one event a
  UTC day per key: the transition mode is visible, not closed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from hub.db import write_transaction

log = logging.getLogger(__name__)

EVENT_UNSCOPED_KEY = "ci_key_unscoped"
REASON_REPORT = "ci_report_out_of_scope"
REASON_DEPLOY = "ci_deploy_out_of_scope"
REASON_DAMAGED = "ci_key_scope_damaged"
REASON_INACTIVE = "ci_key_scope_project_inactive"


class CIScopeRefused(Exception):
    """The key may not speak for this project."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


async def enforce_ci_project_scope(
    db: Any, identity: Any, project_slug: str | None, *, entrance: str
) -> None:
    """Raise :class:`CIScopeRefused` or return; the only write is the event."""
    out_reason = REASON_DEPLOY if entrance == "deploy" else REASON_REPORT
    if getattr(identity, "scopes_damaged", False):
        raise CIScopeRefused(
            REASON_DAMAGED,
            "scopes ключа повреждены: ключ отклонён. Выпустите новый ключ.",
        )
    scopes = getattr(identity, "scopes", None)
    if scopes:
        await _require_active_projects(db, scopes)
        if project_slug is None:
            raise CIScopeRefused(
                out_reason,
                "ключ привязан к проекту: укажите project, входящий в его scope.",
            )
        if project_slug not in scopes:
            raise CIScopeRefused(
                out_reason,
                f"ключ не привязан к проекту {project_slug!r}; "
                f"его проекты: {', '.join(scopes)}.",
            )
        return
    await _flag_unscoped_key(db, identity)


async def _require_active_projects(db: Any, scopes: tuple[str, ...]) -> None:
    """Every project of the scope must exist and be active, or the key stops."""
    for slug in scopes:
        rows = await db.execute_fetchall(
            "SELECT status FROM projects WHERE slug = ?", (slug,)
        )
        if not rows or rows[0][0] != "active":
            raise CIScopeRefused(
                REASON_INACTIVE,
                f"проект {slug!r} из scope ключа не найден или не активен: "
                "ключ остановлен до выпуска нового.",
            )


async def _flag_unscoped_key(db: Any, identity: Any) -> None:
    """One event per key per UTC day; best effort, never blocks the report."""
    key_id = getattr(identity, "api_key_id", None)
    if key_id is None:
        return  # env token or session: no key to flag
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload = json.dumps(
        {"api_key_id": int(key_id), "day": day, "principal": identity.username},
        ensure_ascii=False,
    )
    try:
        # One statement: the existence check and the insert cannot interleave
        # with a parallel callback; the dedup survives a restart (it is in the
        # table, not in memory).
        # Inside the caller's write transaction when there is one (no early
        # commit); on its own otherwise.
        async with write_transaction(db):
            await db.execute(
                "INSERT INTO events (kind, actor, payload) "
                "SELECT ?, ?, ? WHERE NOT EXISTS ("
                "SELECT 1 FROM events WHERE kind = ? "
                "AND json_extract(payload, '$.api_key_id') = ? "
                "AND json_extract(payload, '$.day') = ?)",
                (
                    EVENT_UNSCOPED_KEY,
                    identity.username,
                    payload,
                    EVENT_UNSCOPED_KEY,
                    int(key_id),
                    day,
                ),
            )
    except Exception:  # noqa: BLE001 - a missed flag must not stop CI
        log.warning("could not record the unscoped CI key event", exc_info=True)
