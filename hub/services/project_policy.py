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

from hub import config
from hub import repository as repo

log = logging.getLogger(__name__)


def base_branch_of(project) -> str:
    """The integration branch of an already-loaded project row (#475).

    One reader for the question every gate asks — "which branch does work
    land on here?" — because the answer differs per project: the hub itself
    lives on ``develop``, calc-kids on ``master``, spike-bo on ``main``. Each
    gate that answered it with a literal answered it for one project only.

    The fallback is ``config.PAIR_BASE_BRANCH`` and it applies in exactly one
    case: the project declares no branch at all (missing column, NULL, empty
    or whitespace). A project that DOES declare one is never overridden — a
    fallback that can win over a declared value is not a fallback, it is a
    second source of truth, which is the defect this function removes.

    Takes a row or a mapping, and tolerates both being absent: gates run on
    projects that may not be resolvable, and a lookup failure must degrade to
    the configured default rather than raise inside a gate.
    """
    declared = ""
    if project is not None:
        try:
            declared = str(project["default_branch"] or "").strip()
        except (KeyError, IndexError, TypeError):
            declared = ""
    return declared or config.PAIR_BASE_BRANCH


async def base_branch_for_task(db: aiosqlite.Connection, task_id: int) -> str:
    """The integration branch of the project this task belongs to (#475)."""
    try:
        project = await repo.resolve_project_for_task(db, task_id)
    except Exception:  # noqa: BLE001 - degradation is the contract
        log.warning("could not resolve project for task #%s", task_id)
        return config.PAIR_BASE_BRANCH
    return base_branch_of(project)


# Recognised key of the release base in ``default_branch_policy`` (#812/#475).
# The UI has advertised ``{"release_base": "main"}`` since the policy column
# existed; until #475 nothing read it, so a project whose default_branch is
# already ``main`` (spike-bo) would have had a release PR opened from main
# into main. Declared per project, falling back to the configured branch.
RELEASE_BASE_KEY = "release_base"


def release_base_of(project) -> str:
    """Where this project's releases land; ``config.RELEASE_BRANCH`` by default."""
    try:
        policy = json.loads(project["default_branch_policy"] or "{}")
    except (ValueError, KeyError, IndexError, TypeError):
        policy = {}
    declared = ""
    if isinstance(policy, dict):
        declared = str(policy.get(RELEASE_BASE_KEY) or "").strip()
    return declared or config.RELEASE_BRANCH


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


# Recognised values of the `review` key (#805). Anything else — a typo, a
# value from a future version, an empty string — reads as OFF: an unreadable
# policy must never spend tokens, exactly as it must never grant approval.
REVIEW_OFF = "off"
REVIEW_DISPATCH = "dispatch"


def review_dispatch_enabled(policy: dict) -> bool:
    """Whether the hub calls a reviewer for this project's submissions.

    Two ways to say yes, and they mean different things:

    * ``review='dispatch'`` — call the reviewer, leave the verdict to the
      human. This is the mode the hub's own project needs: the gate keeps
      its owner, but the owner finally has something to read (#804).
    * ``verdict='auto'`` — the autopilot decides the verdict, and it decides
      it BY READING THE REPORT (#745). A project asking for an auto verdict
      is asking for the review that feeds it, so this keeps dispatching for
      calc-kids and spike-bo without touching their stored policy.

    Note what this function is NOT: it does not weaken the default-project
    lock (#743). That lock refuses 'auto' on the dor and verdict gates, and
    dispatching a reviewer removes no human from anywhere — it hands the
    human evidence. The two are opposite in direction.
    """
    if not isinstance(policy, dict):
        return False
    if policy.get("verdict") == "auto":
        return True
    return policy.get("review") == REVIEW_DISPATCH


# Recognised values of the `release` key (#812). Default is manual, and it is
# the default on purpose: releasing takes what is in develop as a whole,
# including other sessions' work, so turning it on is a decision about the
# project rather than a convenience for whoever delivered last.
RELEASE_MANUAL = "manual"
RELEASE_AUTO = "auto"


def release_auto_enabled(policy: dict) -> bool:
    """Whether the hub carries develop into main for this project by itself.

    Anything that is not exactly 'auto' — a typo, a missing key, a value from
    a future version — reads as manual. An unreadable policy must never ship
    code, the same way it never grants approval (#743).
    """
    if not isinstance(policy, dict):
        return False
    return policy.get("release") == RELEASE_AUTO


async def release_policy_for_task(db: aiosqlite.Connection, task_id: int) -> str:
    """``auto`` or ``manual`` for the project this task belongs to."""
    policy = await gate_policy_for_task(db, task_id)
    return RELEASE_AUTO if release_auto_enabled(policy) else RELEASE_MANUAL


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
