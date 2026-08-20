"""One reader for a project's gate policy, resolved from a task (#760).

The policy lives on the project and every consumer reaches it the same way —
by walking the hierarchy with ``resolve_project_for_task`` (#747). Before this
module each consumer inlined its own ``json.loads`` with its own failure
handling; three copies of a rule is how the copies start to disagree.

Every failure mode here — no project, unreadable column, a policy that is not
an object — returns the empty policy, which means "nothing delegated". A
policy that cannot be read must never read as permission.
"""

from __future__ import annotations

import json
import logging

import aiosqlite

from hub import repository as repo

log = logging.getLogger(__name__)


async def gate_policy_for_task(db: aiosqlite.Connection, task_id: int) -> dict:
    """The project's gate policy for this task; ``{}`` when unknown."""
    try:
        project = await repo.resolve_project_for_task(db, task_id)
    except Exception:  # noqa: BLE001 - degradation is the contract
        log.warning("could not resolve project for task #%s", task_id)
        return {}
    if project is None:
        return {}
    return gate_policy_of(project)


def gate_policy_of(project) -> dict:
    """The gate policy of an already-loaded project row; ``{}`` when unknown."""
    try:
        policy = json.loads(project["gate_policy"] or "{}")
    except (ValueError, KeyError, TypeError):
        return {}
    return policy if isinstance(policy, dict) else {}


async def risk_map_for_task(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, str] | None:
    """The project's path map for risk derivation, or None when it has none.

    None and ``{}`` mean the same thing to the derivation, but None is the
    honest word for "this project never described its paths".
    """
    policy = await gate_policy_for_task(db, task_id)
    raw = policy.get("risk_map")
    if not isinstance(raw, dict) or not raw:
        return None
    return {str(k): str(v) for k, v in raw.items()}
