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

from hub import config, git_policy
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
# The closed set of keys ``default_branch_policy`` may carry (#886). It lives
# next to the reader on purpose: a set kept in the write layer drifts from the
# reader that gives keys their meaning, and the drift is invisible — an
# unrecognised key reads exactly like a key nobody wrote. ``release_base``
# missing is a legitimate state ("this project declared no release branch");
# ``releaseBase`` present is a typo that produces the same fallback while the
# owner looks at their JSON and believes the policy is set. Refusing on write
# is what tells those two apart, and it is the only moment where the person
# who made the typo is still there to fix it.
DEFAULT_BRANCH_POLICY_KEYS: tuple[str, ...] = (RELEASE_BASE_KEY,)


def validate_default_branch_policy(policy: object) -> dict:
    """Return the policy, or raise ``ValueError`` naming the unknown keys.

    The message names both halves — what was written and what exists —
    because it is shown to a human in the project card, not only logged:
    "unknown key" without the allowed list sends the reader to the source.
    """
    if policy is None:
        return {}
    if not isinstance(policy, dict):
        raise ValueError("default_branch_policy must be an object")
    unknown = set(policy) - set(DEFAULT_BRANCH_POLICY_KEYS)
    if unknown:
        raise ValueError(
            f"unknown default_branch_policy keys: {sorted(unknown)}; "
            f"allowed: {', '.join(DEFAULT_BRANCH_POLICY_KEYS)}"
        )
    return policy


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


def _workspace_of(project) -> str:
    """The clone path a project declares; ``""`` when it declares none."""
    if project is None:
        return ""
    try:
        return str(project["workspace_path"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def clone_branch_state(project) -> git_policy.BranchSyncState:
    """Does this project's clone protect the branch the project declares (#887).

    The project card and the API read the SAME function, because a divergence
    visible in one place and not the other is how two answers to one question
    start to disagree — the defect ``base_branch_of`` removed for the branch
    itself.

    Three states, and the third is load-bearing: a project with no workspace,
    or a workspace the hub cannot read, is ``unknown`` with a cause, never
    ``match``. Reading "could not look" as "agrees" is the exact shape of the
    bug this task fixes, and the rule is already settled twice in this code
    base — CIRunReportState (#546) and sha_check (#572).
    """
    return git_policy.branch_sync(_workspace_of(project), base_branch_of(project))


def rearm_clone(project) -> git_policy.HookStatus | None:
    """Rewrite the branch keys in this project's clone; None when there is none.

    The single point where a project's branches reach its clone outside the two
    moments #475 covered (cloning, and hub startup). Between those two, an owner
    changing ``default_branch`` in the UI changed nothing the hook could see
    until the next restart: the card showed the new branch while the clone kept
    refusing pushes from it.

    Not a second way to write the keys — it calls the same
    ``git_policy.activate_quietly`` those two moments call, with the same two
    readers for the branches. Idempotent (git config sets a key to a value it
    may already hold) and it touches no key but these; never raises, because a
    clone the hub cannot reach must not fail the edit that reached the database.
    """
    workspace = _workspace_of(project)
    if not workspace:
        return None
    return git_policy.activate_quietly(
        workspace,
        base_branch=base_branch_of(project),
        release_branch=release_base_of(project),
    )


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


# Recognised key of the CI test command in ``gate_policy`` (#476). The hub
# lays a CI workflow into a provisioned repository, and that workflow reports
# acceptance-test results back — but HOW this repository runs its tests is a
# fact only the project knows. Undeclared means "use the documented default of
# the shared reporting action", never "guess a build command": a wrong guess
# turns a missing CI run into a failing one, and the delivery gate treats red
# as a blocker while it routes silence to a human.
CI_RUNNER_KEY = "ci_runner"


def ci_runner_of(project) -> str:
    """How this project's acceptance tests are run in CI; ``""`` when unsaid."""
    policy = gate_policy_of(project)
    return str(policy.get(CI_RUNNER_KEY) or "").strip()


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
