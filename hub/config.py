from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("OPENCLAW_HUB_HOME", Path.home()))

REPO_NAME = os.environ.get("OPENCLAW_HUB_REPO", "")
WORKSPACE_REPO_LINK = Path(
    os.environ.get(
        "OPENCLAW_WORKSPACE_REPO", str(HOME / ".openclaw" / "workspace" / "repo")
    )
)

DISPATCH_JOBS_DIR = HOME / ".local" / "state" / "openclaw-dev-dispatch" / "jobs"
DISPATCH_LOGS_DIR = HOME / ".local" / "state" / "openclaw-dev-dispatch" / "logs"
DISPATCH_BIN = os.environ.get(
    "OPENCLAW_DISPATCH_BIN", str(HOME / ".local" / "bin" / "oc-dev-dispatch")
)

N4L_BIN = os.environ.get("OPENCLAW_N4L_BIN", str(HOME / ".local" / "bin" / "n4l"))
N4L_SPACE_ID = os.environ.get("OPENCLAW_N4L_SPACE", "")

GH_BIN = os.environ.get("GH_BIN", "gh")
VAST_JOB_BIN = os.environ.get(
    "OPENCLAW_VAST_JOB_BIN", str(HOME / ".local" / "bin" / "vast-openclaw")
)
# Vast.ai is opt-in. Disabled by default — set OPENCLAW_VAST_ENABLED=1 to turn on.
VAST_ENABLED = os.environ.get("OPENCLAW_VAST_ENABLED", "0") == "1"

TRANSCRIPTS_DIR = Path(
    os.environ.get("OPENCLAW_TRANSCRIPTS_DIR", str(HOME / ".openclaw" / "transcripts"))
)

HUB_DB_PATH = Path(
    os.environ.get(
        "OPENCLAW_HUB_DB", str(HOME / ".local" / "state" / "openclaw-hub" / "hub.db")
    )
)
HUB_HOST = os.environ.get("OPENCLAW_HUB_HOST", "127.0.0.1")
HUB_PORT = int(os.environ.get("OPENCLAW_HUB_PORT", "8080"))

MAX_REVIEW_CYCLES = int(os.environ.get("OPENCLAW_MAX_REVIEW_CYCLES", "3"))
# Universal Review Gate (#318): 'forbid' (default) rejects review verdicts
# from the agent principal that implemented the task (assigned_agent or
# claimed_by); 'allow' is the explicit solo-mode opt-out.
REVIEW_SELF_APPROVE = os.environ.get("OPENCLAW_REVIEW_SELF_APPROVE", "forbid")
# Projects V2 (#345): 'propose' (default) — agent-created projects start as
# pending and need human activation; 'direct' is the solo-mode opt-out.
ALLOW_AGENT_PROJECTS = os.environ.get("OPENCLAW_ALLOW_AGENT_PROJECTS", "propose")
# Agent Practices (#382): 'warn' (default) — a missing machine-review report
# only warns in the review panel; 'require' blocks the human verdict until a
# current report exists. Applies only where policy says a review is needed.
MACHINE_REVIEW_MODE = os.environ.get("OPENCLAW_MACHINE_REVIEW", "warn")
MAX_CI_FIX_CYCLES = int(os.environ.get("OPENCLAW_MAX_CI_FIX_CYCLES", "3"))
REVIEW_RUNTIME = os.environ.get("OPENCLAW_REVIEW_RUNTIME", "openrouter")
REVIEW_AGENT = os.environ.get("OPENCLAW_REVIEW_AGENT", "code-reviewer")

ARBITER_RUNTIME = os.environ.get("OPENCLAW_ARBITER_RUNTIME", "openrouter")
ARBITER_AGENT = os.environ.get("OPENCLAW_ARBITER_AGENT", "architect-analyst")
# At-most-once arbiter dispatch (#421): if the marker sits in 'dispatching'
# (submit started, job id never recorded — a crash window) past this grace, the
# task fails safe to needs_decision rather than risk a duplicate paid dispatch.
ARBITER_DISPATCH_GRACE_MINUTES = int(
    os.environ.get("OPENCLAW_ARBITER_DISPATCH_GRACE_MINUTES", "15")
)

STALE_THRESHOLD_MINUTES = int(os.environ.get("OPENCLAW_STALE_MINUTES", "30"))
# Stale watchdog (#319): silent dead-end statuses get their own, longer
# thresholds — review and human answers move slower than execution.
STALE_REVIEW_MINUTES = int(os.environ.get("OPENCLAW_STALE_REVIEW_MINUTES", "120"))
STALE_CLAIMED_MINUTES = int(os.environ.get("OPENCLAW_STALE_CLAIMED_MINUTES", "240"))
STALE_NEEDS_INFO_MINUTES = int(
    os.environ.get("OPENCLAW_STALE_NEEDS_INFO_MINUTES", "480")
)

# Bounded recovery (#417): a headless dispatch/review job that stays missing
# past the grace escalates to needs_decision; a claim held past the lease is
# auto-released back to open. Both decisions read persisted timestamps so a
# restart never resets them.
MISSING_JOB_GRACE_MINUTES = int(
    os.environ.get("OPENCLAW_MISSING_JOB_GRACE_MINUTES", "5")
)
CLAIM_LEASE_MINUTES = int(os.environ.get("OPENCLAW_CLAIM_LEASE_MINUTES", "240"))

# Machine-owned deadlines (#418): a backstop, deliberately generous so normal
# work never trips them — the stale watchdog (#393) alerts long before. When a
# machine-owned instance sits past its deadline the watchdog transitions it to
# needs_decision, so no combination stays stuck without an owner.
DEADLINE_CI_CHECK_MINUTES = int(
    os.environ.get("OPENCLAW_DEADLINE_CI_CHECK_MINUTES", "180")
)
DEADLINE_FIX_REQUESTED_MINUTES = int(
    os.environ.get("OPENCLAW_DEADLINE_FIX_REQUESTED_MINUTES", "180")
)
DEADLINE_PENDING_REPORT_MINUTES = int(
    os.environ.get("OPENCLAW_DEADLINE_PENDING_REPORT_MINUTES", "120")
)
DEADLINE_RUNNING_MINUTES = int(
    os.environ.get("OPENCLAW_DEADLINE_RUNNING_MINUTES", "360")
)
DEADLINE_REVIEW_MINUTES = int(
    os.environ.get("OPENCLAW_DEADLINE_REVIEW_MINUTES", "180")
)

# Pair mode: base branch for safe branch creation (default develop per repo-rules).
PAIR_BASE_BRANCH = os.environ.get("OPENCLAW_PAIR_BASE_BRANCH", "develop")

# ---------------------------------------------------------------------------
# Authentication (multi-user)
# ---------------------------------------------------------------------------
#
# Tokens are configured as a comma-separated list of "name:token" pairs in
# OPENCLAW_HUB_TOKENS, e.g.: "alice:s3cret,bob:hunter2".
#
# Behaviour:
# - If the list is empty, Hub runs in single-user open mode (no auth, identity
#   defaults to "anonymous"). This preserves the previous behaviour for
#   existing deploys and unit tests.
# - If at least one token is configured, every /api/*, /tasks/*, /partials/*
#   and /mcp/* request must authenticate via Bearer header or session cookie.
# - OPENCLAW_HUB_AUTH_DISABLED=1 force-disables the gate even when tokens are
#   configured. Useful for one-off debugging; never enable in production.
HUB_TOKENS_RAW = os.environ.get("OPENCLAW_HUB_TOKENS", "")
HUB_AUTH_DISABLED = os.environ.get("OPENCLAW_HUB_AUTH_DISABLED", "0") == "1"
HUB_ALLOW_UNAUTH_NETWORK = (
    os.environ.get("OPENCLAW_HUB_ALLOW_UNAUTHENTICATED_NETWORK", "0") == "1"
)
HUB_ALLOWED_HOSTS_RAW = os.environ.get("OPENCLAW_HUB_ALLOWED_HOSTS", "")
HUB_COOKIE_NAME = os.environ.get("OPENCLAW_HUB_COOKIE", "openclaw_hub_session")
# 30 days by default — Hub is an internal tool, long-lived sessions are fine.
HUB_COOKIE_MAX_AGE = int(
    os.environ.get("OPENCLAW_HUB_COOKIE_MAX_AGE", str(30 * 24 * 3600))
)
HUB_COOKIE_SECURE = os.environ.get("OPENCLAW_HUB_COOKIE_SECURE", "0") == "1"


HUB_BOOTSTRAP_TOKEN = os.environ.get("OPENCLAW_HUB_BOOTSTRAP_ADMIN_TOKEN", "")


class TokenIdentity:
    """Authenticated identity resolved from a token or DB principal.

    For env-token identities: role is 'human', 'agent', or 'admin';
    principal_id and permissions are empty.
    For DB-backed identities: principal_id is set, permissions are populated
    from role_permissions, and role is the effective legacy role.
    """

    __slots__ = ("username", "role", "principal_id", "permissions")

    def __init__(
        self,
        username: str,
        role: str = "human",
        principal_id: int | None = None,
        permissions: frozenset[str] | None = None,
    ) -> None:
        self.username = username
        self.role = role
        self.principal_id = principal_id
        self.permissions = permissions or frozenset()

    def __repr__(self) -> str:
        return f"TokenIdentity({self.username!r}, role={self.role!r}, pid={self.principal_id})"

    @property
    def is_admin(self) -> bool:
        if self.role in ("admin", "super_admin"):
            return True
        return "admin.read" in self.permissions

    @property
    def is_human(self) -> bool:
        if self.role in ("human", "admin", "super_admin"):
            return True
        return "tasks.human_gate" in self.permissions

    @property
    def is_agent(self) -> bool:
        if self.role == "agent":
            return True
        return not self.is_human and bool(self.principal_id)

    def has_permission(self, perm: str) -> bool:
        if self.role == "super_admin":
            return True
        if self.permissions:
            return perm in self.permissions
        if self.role == "admin":
            return True
        if self.role == "human":
            return perm in _HUMAN_DEFAULT_PERMS
        return perm in _AGENT_DEFAULT_PERMS


_HUMAN_DEFAULT_PERMS = frozenset(
    {
        "tasks.read",
        "tasks.create",
        "tasks.refine",
        "tasks.update",
        "tasks.human_gate",
        "tasks.decision",
        "tasks.archive",
        "tasks.delete",
        "integrations.vast.manage",
    }
)
_AGENT_DEFAULT_PERMS = frozenset(
    {
        "tasks.read",
        "tasks.create",
        "tasks.refine",
        "tasks.update",
        "tasks.agent_report",
    }
)


VALID_ROLES = frozenset({"human", "agent", "admin"})


def parse_tokens(raw: str) -> dict[str, TokenIdentity]:
    """Parse OPENCLAW_HUB_TOKENS into a {token: TokenIdentity} mapping.

    Format: "name:token[:role],name2:token2[:role2]"
    Role is optional — defaults to ``human``. Valid roles: human, agent, admin.
    Whitespace tolerated. Malformed entries are skipped silently.
    """
    out: dict[str, TokenIdentity] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        parts = chunk.split(":")
        if len(parts) == 2:
            name, token = parts[0].strip(), parts[1].strip()
            role = "human"
        elif len(parts) >= 3:
            name, token, role = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if role not in VALID_ROLES:
                role = "human"
        else:
            continue
        if name and token:
            out[token] = TokenIdentity(username=name, role=role)
    return out


HUB_TOKENS: dict[str, TokenIdentity] = parse_tokens(HUB_TOKENS_RAW)


def parse_allowed_hosts(raw: str) -> frozenset[str]:
    """Parse comma-separated Host allowlist values.

    Values may include ports. A host entry without a port matches any port for
    that hostname; a host:port entry must match exactly.
    """
    return frozenset(
        item.lower().rstrip(".")
        for item in (part.strip() for part in (raw or "").split(","))
        if item
    )


HUB_ALLOWED_HOSTS: frozenset[str] = parse_allowed_hosts(HUB_ALLOWED_HOSTS_RAW)


def _is_loopback(host: str) -> bool:
    """Check if the given host string is a loopback address."""
    return host in ("127.0.0.1", "localhost", "::1")


def validate_network_auth() -> None:
    """Reject non-loopback binds when auth is open, unless explicitly overridden.

    Called at startup before the server begins accepting connections.
    Raises RuntimeError if the configuration is unsafe.
    """
    if _is_loopback(HUB_HOST):
        return
    has_tokens = bool(HUB_TOKENS) and not HUB_AUTH_DISABLED
    if has_tokens:
        return
    if HUB_ALLOW_UNAUTH_NETWORK:
        return
    raise RuntimeError(
        f"Refusing to bind to {HUB_HOST!r} without authentication. "
        f"Either set OPENCLAW_HUB_TOKENS, bind to 127.0.0.1, or set "
        f"OPENCLAW_HUB_ALLOW_UNAUTHENTICATED_NETWORK=1 to override."
    )
