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
# Verifiable SDD (#501/#505): 'off' (default) — refine does not enforce that a
# verifiable_by=test AC carries a resolvable pytest test locator; 'require'
# rejects such AC at refine time. Default off so existing refine flows and the
# hub's own tasks (which predate locators) keep working; opt in per project.
SDD_AC_LOCATOR = os.environ.get("OPENCLAW_SDD_AC_LOCATOR", "off")
# Verifiable SDD (#508): 'warn' (default) — a red/absent AC test only shows in
# the review brief; 'require' blocks an APPROVED verdict until every current
# verifiable_by=test AC is green. Only APPROVED is gated — CHANGES_REQUESTED
# always passes so a reviewer can always reject (lesson from #382).
SDD_AC_TESTS = os.environ.get("OPENCLAW_SDD_AC_TESTS", "warn")
# Verifiable SDD (#510): 'warn' (default) — a failed validation_commands run only
# warns; 'require' blocks completion until the current validation run is green.
SDD_VALIDATION = os.environ.get("OPENCLAW_SDD_VALIDATION", "warn")
# Commit scope (#361): 'warn' (default) — files dirty at commit time that fall
# outside the task's declared affected_areas are named in a task update and
# committed anyway; 'require' stops the git tail and escalates to
# needs_decision instead. 'off' disables the check. Headless tasks share the
# main clone for their whole run, so this is the only attribution the hub has.
COMMIT_SCOPE_GATE = os.environ.get("OPENCLAW_COMMIT_SCOPE", "warn")
# Declared surfaces vs the actual diff (#550): 'warn' (default) — files the
# branch changes that no declared area covers are named in a task update and
# the submission proceeds; 'require' refuses the submission; 'off' disables
# the check. Compared against the branch diff, not against a prediction: on
# submit the hub has the truth, so there are no name-matching heuristics and
# no false positives by construction.
SDD_SURFACES = os.environ.get("OPENCLAW_SDD_SURFACES", "warn")
# Auto-approval of low-risk drafts (#584): 'off' (default) — every draft waits
# for a human, today's behavior in full; 'r0' / 'r1' — a DoR-passed draft
# whose DERIVED risk class (#582) is at or below the named class is approved
# by the hub itself, with the reason written into the feed. This single
# setting is the switch the task demands: flipping it back to 'off' restores
# the human gate completely. 'r2' is deliberately NOT accepted here — that
# band opens only with #585, after measured reviewer agreement.
AUTO_APPROVE_MAX_CLASS = os.environ.get("OPENCLAW_AUTO_APPROVE_MAX_CLASS", "off")
# Review-round token budget (#745): a machine-review report that spent more
# than this escalates the verdict to the human instead of auto-approving —
# a round that did not converge normally is itself a risk signal. Matches
# the multi-agent-review skill's per-round bar. 0 disables the trigger.
REVIEW_TOKEN_BUDGET = int(os.environ.get("OPENCLAW_REVIEW_TOKEN_BUDGET", "300000"))
# Proven-empty review (#769): raw_count=0 may still auto-approve when the
# provider's own usage numbers prove the reviewer actually worked. This is
# the minimum billed tokens for a hub-dispatched run to count as proof;
# below it (or without a settled dispatch at all) the #750 rule stands and
# the verdict stays with the human. 0 disables the proven-empty path.
EMPTY_REVIEW_MIN_USAGE = int(
    os.environ.get("OPENCLAW_EMPTY_REVIEW_MIN_USAGE", "200000")
)
# Cursor Cloud Agents API (#756): the server-side executor for cross-model
# reviews. Empty key = the integration is off and every client method
# degrades to None without a network call. The key comes from the Cursor
# Dashboard → API Keys and lives in a chmod-600 systemd drop-in on the VM.
CURSOR_API_KEY = os.environ.get("CURSOR_API_KEY", "")
CURSOR_API_URL = os.environ.get("CURSOR_API_URL", "https://api.cursor.com")
# Cross-model review dispatch (#757). CURSOR_REVIEW_MODEL overrides the
# reviewer-model choice; empty = pick from the built-in preference list, the
# first whose family differs from the implementer's declared model (#758).
# The hub MCP token the cloud reviewer authenticates with comes from its own
# drop-in — a dedicated agent principal (cursor-cloud-reviewer), revocable
# without touching the implementers' tokens.
CURSOR_REVIEW_MODEL = os.environ.get("CURSOR_REVIEW_MODEL", "")
CURSOR_REVIEWER_HUB_TOKEN = os.environ.get("CURSOR_REVIEWER_HUB_TOKEN", "")
CURSOR_REVIEW_GRACE_MINUTES = int(os.environ.get("CURSOR_REVIEW_GRACE_MINUTES", "15"))
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
# Unrefined-draft watchdog (#751): a draft older than this without a passed
# DoR gets one feed alert naming what is missing — approval of such a draft
# mechanically fails (422 dor_failed), and until this watchdog the author
# learned that only when the owner hit the button.
UNREFINED_DRAFT_MINUTES = int(os.environ.get("OPENCLAW_UNREFINED_DRAFT_MINUTES", "240"))
STALE_CLAIMED_MINUTES = int(os.environ.get("OPENCLAW_STALE_CLAIMED_MINUTES", "240"))
STALE_NEEDS_INFO_MINUTES = int(
    os.environ.get("OPENCLAW_STALE_NEEDS_INFO_MINUTES", "480")
)
# Machine-owned dead-end statuses (#393): visible via stale alerts until the
# durable deadline transitions from F2 land. F1 only alerts — status unchanged.
STALE_CI_CHECK_MINUTES = int(os.environ.get("OPENCLAW_STALE_CI_CHECK_MINUTES", "60"))
STALE_FIX_REQUESTED_MINUTES = int(
    os.environ.get("OPENCLAW_STALE_FIX_REQUESTED_MINUTES", "60")
)
STALE_PENDING_REPORT_MINUTES = int(
    os.environ.get("OPENCLAW_STALE_PENDING_REPORT_MINUTES", "30")
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
DEADLINE_REVIEW_MINUTES = int(os.environ.get("OPENCLAW_DEADLINE_REVIEW_MINUTES", "180"))

# Agent session registry (#771): presence is a derived fact, not a stored flag.
# A session counts as online while its last heartbeat is younger than the TTL;
# past it the registry says offline and names the age, because an agent that
# died without saying goodbye must not keep looking alive. Retention mirrors the
# events feed: the registry is a directory of who is around, not an archive.
SESSION_TTL_MINUTES = int(os.environ.get("OPENCLAW_SESSION_TTL_MINUTES", "10"))
SESSION_RETENTION_DAYS = int(os.environ.get("OPENCLAW_SESSION_RETENTION_DAYS", "14"))

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

    __slots__ = (
        "username",
        "role",
        "principal_id",
        "permissions",
        "auth_source",
        "api_key_id",
    )

    def __init__(
        self,
        username: str,
        role: str = "human",
        principal_id: int | None = None,
        permissions: frozenset[str] | None = None,
        auth_source: str | None = None,
        api_key_id: int | None = None,
    ) -> None:
        self.username = username
        self.role = role
        self.principal_id = principal_id
        self.permissions = permissions or frozenset()
        self.auth_source = auth_source
        self.api_key_id = api_key_id

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
