from __future__ import annotations

import os
from pathlib import Path
from typing import overload

from hub import brand


@overload
def env_get(suffix: str) -> str | None: ...
@overload
def env_get(suffix: str, default: str) -> str: ...


def env_get(suffix: str, default: str | None = None) -> str | None:
    value = os.environ.get(brand.ENV_PREFIX + suffix)
    if value:
        return value
    return default


HOME = Path(env_get("HUB_HOME", str(Path.home())))

REPO_NAME = env_get("HUB_REPO", "")
WORKSPACE_REPO_LINK = Path(
    env_get("WORKSPACE_REPO", str(HOME / ".haiplane" / "workspace" / "repo"))
)

DISPATCH_JOBS_DIR = HOME / ".local" / "state" / "haiplane-dev-dispatch" / "jobs"
DISPATCH_LOGS_DIR = HOME / ".local" / "state" / "haiplane-dev-dispatch" / "logs"
DISPATCH_BIN = env_get("DISPATCH_BIN", str(HOME / ".local" / "bin" / "hp-dev-dispatch"))

N4L_BIN = env_get("N4L_BIN", str(HOME / ".local" / "bin" / "n4l"))
N4L_SPACE_ID = env_get("N4L_SPACE", "")

GH_BIN = os.environ.get("GH_BIN", "gh")
VAST_JOB_BIN = env_get("VAST_JOB_BIN", str(HOME / ".local" / "bin" / "vast-haiplane"))
# Vast.ai is opt-in. Disabled by default — set HAIPLANE_VAST_ENABLED=1 to turn on.
VAST_ENABLED = env_get("VAST_ENABLED", "0") == "1"

TRANSCRIPTS_DIR = Path(
    env_get("TRANSCRIPTS_DIR", str(HOME / ".haiplane" / "transcripts"))
)

HUB_DB_PATH = Path(
    env_get("HUB_DB", str(HOME / ".local" / "state" / "haiplane-hub" / "hub.db"))
)
HUB_HOST = env_get("HUB_HOST", "127.0.0.1")
HUB_PORT = int(env_get("HUB_PORT", "8080"))

MAX_REVIEW_CYCLES = int(env_get("MAX_REVIEW_CYCLES", "3"))
# Universal Review Gate (#318): 'forbid' (default) rejects review verdicts
# from the agent principal that implemented the task (assigned_agent or
# claimed_by); 'allow' is the explicit solo-mode opt-out.
REVIEW_SELF_APPROVE = env_get("REVIEW_SELF_APPROVE", "forbid")
# Projects V2 (#345): 'propose' (default) — agent-created projects start as
# pending and need human activation; 'direct' is the solo-mode opt-out.
ALLOW_AGENT_PROJECTS = env_get("ALLOW_AGENT_PROJECTS", "propose")
# Agent Practices (#382): 'warn' (default) — a missing machine-review report
# only warns in the review panel; 'require' blocks the human verdict until a
# current report exists. Applies only where policy says a review is needed.
MACHINE_REVIEW_MODE = env_get("MACHINE_REVIEW", "warn")
# Verifiable SDD (#501/#505): 'off' (default) — refine does not enforce that a
# verifiable_by=test AC carries a resolvable pytest test locator; 'require'
# rejects such AC at refine time. Default off so existing refine flows and the
# hub's own tasks (which predate locators) keep working; opt in per project.
SDD_AC_LOCATOR = env_get("SDD_AC_LOCATOR", "off")
# Verifiable SDD (#508): 'warn' (default) — a red/absent AC test only shows in
# the review brief; 'require' blocks an APPROVED verdict until every current
# verifiable_by=test AC is green. Only APPROVED is gated — CHANGES_REQUESTED
# always passes so a reviewer can always reject (lesson from #382).
SDD_AC_TESTS = env_get("SDD_AC_TESTS", "warn")
# Verifiable SDD (#510): 'warn' (default) — a failed validation_commands run only
# warns; 'require' blocks completion until the current validation run is green.
SDD_VALIDATION = env_get("SDD_VALIDATION", "warn")
# Commit scope (#361): 'warn' (default) — files dirty at commit time that fall
# outside the task's declared affected_areas are named in a task update and
# committed anyway; 'require' stops the git tail and escalates to
# needs_decision instead. 'off' disables the check. Headless tasks share the
# main clone for their whole run, so this is the only attribution the hub has.
COMMIT_SCOPE_GATE = env_get("COMMIT_SCOPE", "warn")
# Declared surfaces vs the actual diff (#550): 'warn' (default) — files the
# branch changes that no declared area covers are named in a task update and
# the submission proceeds; 'require' refuses the submission; 'off' disables
# the check. Compared against the branch diff, not against a prediction: on
# submit the hub has the truth, so there are no name-matching heuristics and
# no false positives by construction.
SDD_SURFACES = env_get("SDD_SURFACES", "warn")
# Deterministic submit rules (#855): the cheap layer that runs BEFORE the paid
# reviewer, on the diff the submission already resolved (#583) — no extra git
# call, no tokens. 'warn' (default) reports; 'require' refuses a submission
# that changes code without touching a single test; 'off' disables it. The
# measurement behind it (#854, 30 days): the paid reviewer confirmed
# test-coverage / test-adequacy / missing-test-hides-defect findings at 124k
# tokens each, and 61% of its raw findings were rejected — two thirds of the
# budget spent on noise a rule cannot produce.
SUBMIT_RULES = env_get("SUBMIT_RULES", "warn")
# #911: does a resubmission have to say what became of the findings it was sent
# back over? Default warn, like every gate that asks the author for something
# new: a rule that starts by refusing work teaches people to route around it.
FINDING_OUTCOME = env_get("FINDING_OUTCOME", "warn")
# Auto-approval of low-risk drafts (#584): 'off' (default) — every draft waits
# for a human, today's behavior in full; 'r0' / 'r1' — a DoR-passed draft
# whose DERIVED risk class (#582) is at or below the named class is approved
# by the hub itself, with the reason written into the feed. This single
# setting is the switch the task demands: flipping it back to 'off' restores
# the human gate completely. 'r2' is deliberately NOT accepted here — that
# band opens only with #585, after measured reviewer agreement.
AUTO_APPROVE_MAX_CLASS = env_get("AUTO_APPROVE_MAX_CLASS", "off")
# Review-round token budget (#745): a machine-review report that spent more
# than this escalates the verdict to the human instead of auto-approving —
# a round that did not converge normally is itself a risk signal. Matches
# the multi-agent-review skill's per-round bar. 0 disables the trigger.
REVIEW_TOKEN_BUDGET = int(env_get("REVIEW_TOKEN_BUDGET", "300000"))
# Proven-empty review (#769): raw_count=0 may still auto-approve when the
# provider's own usage numbers prove the reviewer actually worked. This is
# the minimum billed tokens for a hub-dispatched run to count as proof;
# below it (or without a settled dispatch at all) the #750 rule stands and
# the verdict stays with the human. 0 disables the proven-empty path.
EMPTY_REVIEW_MIN_USAGE = int(env_get("EMPTY_REVIEW_MIN_USAGE", "200000"))
# Class ceiling for the proven-empty path (#835): usage proves the reviewer
# WORKED, never that it was ABLE to find — and the cross-model reviewer runs
# on the free tier of its provider. So an empty report may stand in for a
# review only where the cost of a miss matches that reviewer: at or below
# this class. Above it the human keeps the verdict even when the emptiness
# is proven. An unknown value reads as the strictest band, not as "off" —
# a typo in a drop-in must not silently disable a safeguard.
PROVEN_EMPTY_MAX_CLASS = env_get("PROVEN_EMPTY_MAX_CLASS", "r1")
# Steward mode (#1073, epic #994): the global kill-switch for the steward
# contour. off — the dispatcher orders nothing and today's human route stands
# everywhere; shadow — runs are ordered and judged, but nothing they say
# changes a task (#997); act — the judgement may be applied (#998). An
# unrecognised value reads as `off`, the same way AUTO_APPROVE_MAX_CLASS
# treats a typo: a mistyped drop-in must not switch a contour ON.
STEWARD_MODE = env_get("STEWARD_MODE", "off")
# Runs per project per UTC day (#1073, owner's decision 28.08.2026). The cap
# is about predictability rather than money: hitting it escalates with the
# code `daily_cap` and the task goes down today's human route — it never
# means "checked and clean".
STEWARD_DAILY_CAP = int(env_get("STEWARD_DAILY_CAP", "20"))
# Minutes a steward run may hold its slot (#1073). review:client is a
# human-owned slot with no deadline of its own, so a hung cloud agent would
# otherwise never escalate — it would just sit there looking ordered.
STEWARD_RUN_DEADLINE_MIN = int(env_get("STEWARD_RUN_DEADLINE_MIN", "30"))
# The model the steward runs on (#994 §4): a third family, distinct from the
# implementer's and from the reviewer's. Declared on the order so the
# diversity rule has something to check before the run starts.
STEWARD_MODEL = env_get("STEWARD_MODEL", "gpt-5.3-codex")
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
# Review profiles (#807). The lite profile reviews the branch diff in one
# pass; deep is the multi-agent harness.
#
# This number is NOT a run budget any more (#893). As one it was measured and
# failed: eight lite runs billed 777k-1.97M tokens against a stated 40k, and
# the ceiling reached the reviewer only as a sentence in a prompt. What it
# still bounds is the rules text the hub itself puts into that prompt
# (review_dispatch.rules_char_cap) — hub-written input, genuinely ours to
# size. The unit that tracks cost is the RUN: none billed under 777k, lite
# averages 1.38M, deep 3.85M.
REVIEW_LITE_TOKEN_BUDGET = int(env_get("REVIEW_LITE_TOKEN_BUDGET", "40000"))
MAX_CI_FIX_CYCLES = int(env_get("MAX_CI_FIX_CYCLES", "3"))
# Seconds after CI start / first missing-run probe before "no run for this
# SHA" is a named fact rather than a wait. Same window the poller already
# used (#1041). Project-level policy is a sibling, not this change.
CI_GRACE_PERIOD = 180
REVIEW_RUNTIME = env_get("REVIEW_RUNTIME", "openrouter")
REVIEW_AGENT = env_get("REVIEW_AGENT", "code-reviewer")

ARBITER_RUNTIME = env_get("ARBITER_RUNTIME", "openrouter")
ARBITER_AGENT = env_get("ARBITER_AGENT", "architect-analyst")
# At-most-once arbiter dispatch (#421): if the marker sits in 'dispatching'
# (submit started, job id never recorded — a crash window) past this grace, the
# task fails safe to needs_decision rather than risk a duplicate paid dispatch.
ARBITER_DISPATCH_GRACE_MINUTES = int(env_get("ARBITER_DISPATCH_GRACE_MINUTES", "15"))

STALE_THRESHOLD_MINUTES = int(env_get("STALE_MINUTES", "30"))
# Stale watchdog (#319): silent dead-end statuses get their own, longer
# thresholds — review and human answers move slower than execution.
STALE_REVIEW_MINUTES = int(env_get("STALE_REVIEW_MINUTES", "120"))
# Unrefined-draft watchdog (#751): a draft older than this without a passed
# DoR gets one feed alert naming what is missing — approval of such a draft
# mechanically fails (422 dor_failed), and until this watchdog the author
# learned that only when the owner hit the button.
UNREFINED_DRAFT_MINUTES = int(env_get("UNREFINED_DRAFT_MINUTES", "240"))
# Delivery reconciliation (#897): how often the poller compares "completed"
# against "the PR is still open", and how far back it looks. On a timer because
# every candidate costs a call to GitHub; bounded in time because history from
# before the delivery gate existed was merged by hand and is not news.
DELIVERY_SCAN_MINUTES = int(env_get("DELIVERY_SCAN_MINUTES", "15"))
DELIVERY_SCAN_LOOKBACK_DAYS = int(env_get("DELIVERY_SCAN_LOOKBACK_DAYS", "30"))
STALE_CLAIMED_MINUTES = int(env_get("STALE_CLAIMED_MINUTES", "240"))
STALE_NEEDS_INFO_MINUTES = int(env_get("STALE_NEEDS_INFO_MINUTES", "480"))
# Machine-owned dead-end statuses (#393): visible via stale alerts until the
# durable deadline transitions from F2 land. F1 only alerts — status unchanged.
STALE_CI_CHECK_MINUTES = int(env_get("STALE_CI_CHECK_MINUTES", "60"))
STALE_FIX_REQUESTED_MINUTES = int(env_get("STALE_FIX_REQUESTED_MINUTES", "60"))
STALE_PENDING_REPORT_MINUTES = int(env_get("STALE_PENDING_REPORT_MINUTES", "30"))

# The human queue's ladder (#1020): a day, three days, a week. Every other
# deadline in this file watches a machine, which escalates on its own; the
# statuses where work actually waits — draft, needs_info, needs_decision,
# client review — had no clock at all, so their only detector was a person
# noticing on the board, which is the very resource the queue is short of.
# Three rungs in a week is the whole budget: a reminder that speaks on every
# pass becomes the background nobody reads, which is how the single lifetime
# alert it replaces already failed.
HUMAN_QUEUE_LADDER_MINUTES: tuple[tuple[int, str], ...] = (
    (int(env_get("HUMAN_QUEUE_RUNG_1_MINUTES", "1440")), "24h"),
    (int(env_get("HUMAN_QUEUE_RUNG_2_MINUTES", "4320")), "72h"),
    (int(env_get("HUMAN_QUEUE_RUNG_3_MINUTES", "10080")), "168h"),
)

# Bounded recovery (#417): a headless dispatch/review job that stays missing
# past the grace escalates to needs_decision; a claim held past the lease is
# auto-released back to open. Both decisions read persisted timestamps so a
# restart never resets them.
MISSING_JOB_GRACE_MINUTES = int(env_get("MISSING_JOB_GRACE_MINUTES", "5"))
CLAIM_LEASE_MINUTES = int(env_get("CLAIM_LEASE_MINUTES", "240"))

# Machine-owned deadlines (#418): a backstop, deliberately generous so normal
# work never trips them — the stale watchdog (#393) alerts long before. When a
# machine-owned instance sits past its deadline the watchdog transitions it to
# needs_decision, so no combination stays stuck without an owner.
DEADLINE_CI_CHECK_MINUTES = int(env_get("DEADLINE_CI_CHECK_MINUTES", "180"))
DEADLINE_FIX_REQUESTED_MINUTES = int(env_get("DEADLINE_FIX_REQUESTED_MINUTES", "180"))
DEADLINE_PENDING_REPORT_MINUTES = int(env_get("DEADLINE_PENDING_REPORT_MINUTES", "120"))
DEADLINE_RUNNING_MINUTES = int(env_get("DEADLINE_RUNNING_MINUTES", "360"))
DEADLINE_REVIEW_MINUTES = int(env_get("DEADLINE_REVIEW_MINUTES", "180"))

# Agent session registry (#771): presence is a derived fact, not a stored flag.
# A session counts as online while its last heartbeat is younger than the TTL;
# past it the registry says offline and names the age, because an agent that
# died without saying goodbye must not keep looking alive. Retention mirrors the
# events feed: the registry is a directory of who is around, not an archive.
SESSION_TTL_MINUTES = int(env_get("SESSION_TTL_MINUTES", "10"))
SESSION_RETENTION_DAYS = int(env_get("SESSION_RETENTION_DAYS", "14"))

# Worktrees of finished tasks (#1033). Measured on production 2026-08-28: 189
# directories under .hub-worktrees, some from tasks closed a week earlier. They
# are not only disk — since #989 the hub decides whether to NAME a worktree to
# an agent by whether the directory exists, so an old tree is a live path to a
# forgotten branch.
#
# Three days, not zero: the tree is needed precisely AFTER delivery. Twice on
# 28.08 machine-review findings arrived after the verdict had merged (#1011,
# #1025), and the fix for the second was sitting in its worktree when this was
# written. Removal at delivery would have cut off exactly the case it exists
# for; three days covers every delay observed so far.
WORKTREE_RETENTION_DAYS = int(env_get("WORKTREE_RETENTION_DAYS", "3"))
# How many trees one pass may retire. The first pass on production faces a
# backlog of 189, and a poller tick should not spend minutes in git.
WORKTREE_RETENTION_BATCH = int(env_get("WORKTREE_RETENTION_BATCH", "20"))

# Agent messages (#773): a channel without limits becomes a dump nobody reads,
# and one that blocks work becomes a toll booth. Both defaults are generous
# enough that ordinary coordination never meets them, and a refusal always says
# which limit it hit rather than failing shapelessly.
MESSAGE_MAX_CHARS = int(env_get("MESSAGE_MAX_CHARS", "4000"))

# #824: how much of a submission's diff the gate renders before it says it
# stopped. A cap the reader is not told about is worse than no diff at all —
# it looks like the whole change and is a fragment of it.
DIFF_MAX_LINES = int(env_get("DIFF_MAX_LINES", "2000"))
DIFF_MAX_BYTES = int(env_get("DIFF_MAX_BYTES", str(400 * 1024)))
MESSAGE_RATE_PER_MINUTE = int(env_get("MESSAGE_RATE_PER_MINUTE", "30"))
MESSAGE_RETENTION_DAYS = int(env_get("MESSAGE_RETENTION_DAYS", "14"))

# MCP usage telemetry (#780, epic #776): the Agent API cannot be trimmed by
# taste. What every knob below protects is the same property — the record is
# metadata about a call, never its content. Retention is longer than the
# longest report window on purpose: a 90-day report drawn from a 90-day
# horizon is always missing its own oldest edge while looking complete.
MCP_TELEMETRY_ENABLED = env_get("MCP_TELEMETRY", "1") != "0"
MCP_TELEMETRY_RETENTION_DAYS = int(env_get("MCP_TELEMETRY_RETENTION_DAYS", "120"))
MCP_TELEMETRY_MAX_WINDOW_DAYS = int(env_get("MCP_TELEMETRY_MAX_WINDOW_DAYS", "90"))
# Which Agent API surface answered the call. One value today; feature B (#778)
# splits it into core/extension profiles, and the reports built here are what
# decides where the line falls.
MCP_PROFILE = env_get("MCP_PROFILE", "v1")

# Release branch (#812): where develop is carried when the project releases by
# policy. Separate from PAIR_BASE_BRANCH so "where work lands" and "what is in
# production" stay two different questions.
RELEASE_BRANCH = env_get("RELEASE_BRANCH", "main")

# Pair mode: base branch for safe branch creation (default develop per repo-rules).
PAIR_BASE_BRANCH = env_get("PAIR_BASE_BRANCH", "develop")

# ---------------------------------------------------------------------------
# Authentication (multi-user)
# ---------------------------------------------------------------------------
#
# Tokens are configured as a comma-separated list of "name:token" pairs in
# HAIPLANE_HUB_TOKENS, e.g.: "alice:s3cret,bob:hunter2".
#
# Behaviour:
# - If the list is empty, Hub runs in single-user open mode (no auth, identity
#   defaults to "anonymous"). This preserves the previous behaviour for
#   existing deploys and unit tests.
# - If at least one token is configured, every /api/*, /tasks/*, /partials/*
#   and /mcp/* request must authenticate via Bearer header or session cookie.
# - HAIPLANE_HUB_AUTH_DISABLED=1 force-disables the gate even when tokens are
#   configured. Useful for one-off debugging; never enable in production.
HUB_TOKENS_RAW = env_get("HUB_TOKENS", "")
HUB_AUTH_DISABLED = env_get("HUB_AUTH_DISABLED", "0") == "1"
HUB_ALLOW_UNAUTH_NETWORK = env_get("HUB_ALLOW_UNAUTHENTICATED_NETWORK", "0") == "1"
HUB_ALLOWED_HOSTS_RAW = env_get("HUB_ALLOWED_HOSTS", "")
# Session cookie. An operator override via HAIPLANE_HUB_COOKIE names the
# ONLY cookie the hub reads; without one the default is brand.COOKIE_NAME.
_cookie_explicit = env_get("HUB_COOKIE")
HUB_COOKIE_NAME_EXPLICIT = bool(_cookie_explicit)
HUB_COOKIE_NAME = _cookie_explicit or brand.COOKIE_NAME
# 30 days by default — Hub is an internal tool, long-lived sessions are fine.
HUB_COOKIE_MAX_AGE = int(env_get("HUB_COOKIE_MAX_AGE", str(30 * 24 * 3600)))
HUB_COOKIE_SECURE = env_get("HUB_COOKIE_SECURE", "0") == "1"


HUB_BOOTSTRAP_TOKEN = env_get("HUB_BOOTSTRAP_ADMIN_TOKEN", "")

# ---------------------------------------------------------------------------
# Chat-pair (#961): a code pasted into a chat, exchanged for a short session
# ---------------------------------------------------------------------------
#
# The code lives in somebody else's transcript forever, so what it can buy is
# bounded on every axis: minutes to spend it, hours to use what it bought, and
# a permission set that is a constant here rather than a copy of the issuer's
# rights — the same principal is often ``admin`` in production.
CHAT_PAIR_CODE_SECONDS = int(env_get("CHAT_PAIR_CODE_SECONDS", "300"))
CHAT_PAIR_TTL_SECONDS = int(env_get("CHAT_PAIR_TTL_SECONDS", "7200"))
CHAT_PAIR_REDEEM_MAX = int(env_get("CHAT_PAIR_REDEEM_MAX", "10"))
CHAT_PAIR_REDEEM_WINDOW_SECONDS = int(env_get("CHAT_PAIR_REDEEM_WINDOW_SECONDS", "300"))
# How long a spent code stays in the table before the reaper drops it: long
# enough to answer "was this code used?", short enough not to be an archive.
CHAT_PAIR_SPENT_RETENTION_HOURS = int(env_get("CHAT_PAIR_SPENT_RETENTION_HOURS", "24"))

# Fixed, not derived. Post a task, read it back, sharpen it — that is the whole
# job of this channel. Notably absent: tasks.human_gate, tasks.decision,
# tasks.archive, tasks.delete, admin.*, integrations.vast.manage and
# tasks.agent_report.
CHAT_PAIR_PERMS: frozenset[str] = frozenset(
    {
        "tasks.read",
        "tasks.create",
        "tasks.refine",
        "tasks.update",
    }
)

# Acting principal for kind=implementer (#980). HAIPLANE_CHAT_PAIR_AGENT.
CHAT_PAIR_AGENT = env_get("CHAT_PAIR_AGENT", "cloud")

# Deliberately not _AGENT_DEFAULT_PERMS: that set includes tasks.create, which
# would let a leaked implementer code post new work. No tasks.human_gate —
# that flag makes is_human True and would reopen human-only routes.
CHAT_PAIR_IMPLEMENTER_PERMS: frozenset[str] = frozenset(
    {
        "tasks.read",
        "tasks.update",
        "tasks.agent_report",
    }
)

# Reviewer (#1084): the cloud reviewer gets no MCP from Cursor, so the header
# carrying its token never reaches the run — it redeems a one-time code over
# plain HTTPS instead. Two routes, so the narrowest set that opens them; the
# real gate is the deny-by-default allowlist in hub/auth.py, exactly as the
# module docstring of hub/services/chat_pair.py says.
#
# Deliberately NOT a trimmed CHAT_PAIR_IMPLEMENTER_PERMS: that set carries
# tasks.update and tasks.agent_report, and the implementer allowlist opens
# claim, pair-start and submit-review. A reviewer holding those would be the
# checked party voting on itself — the defect #728 closed on the human path.
CHAT_PAIR_REVIEWER_PERMS: frozenset[str] = frozenset({"tasks.read"})

# Kinds whose session is bound to ONE task. Written once and read by both the
# issuer and the route guard: when this was the literal "implementer" in five
# places, adding a kind meant finding all five, and the one that got missed
# would be an UNBOUND session — the failure that does not announce itself.
CHAT_PAIR_TASK_BOUND_KINDS: frozenset[str] = frozenset({"implementer", "reviewer"})

# Steward (#1021): closed list of two operations, not a cut-down CHAT_PAIR_PERMS.
# Those four include create/refine/update and would let the steward write the
# statement it then judges. Deny-by-default lives in hub/auth.py; these strings
# are what the two allowed routes ask for.
STEWARD_PERMS: frozenset[str] = frozenset(
    {
        "steward.evidence.read",
        "steward.judgement.write",
    }
)


class TokenIdentity:
    """Authenticated identity resolved from a token or DB principal.

    For env-token identities: role is 'human', 'agent', 'admin', or 'steward';
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
        "chat_pair_kind",
        "chat_pair_task_id",
        "chat_pair_generation",
    )

    def __init__(
        self,
        username: str,
        role: str = "human",
        principal_id: int | None = None,
        permissions: frozenset[str] | None = None,
        auth_source: str | None = None,
        api_key_id: int | None = None,
        chat_pair_kind: str | None = None,
        chat_pair_task_id: int | None = None,
        chat_pair_generation: int | None = None,
    ) -> None:
        self.username = username
        self.role = role
        self.principal_id = principal_id
        self.permissions = permissions or frozenset()
        self.auth_source = auth_source
        self.api_key_id = api_key_id
        self.chat_pair_kind = chat_pair_kind
        self.chat_pair_task_id = chat_pair_task_id
        self.chat_pair_generation = chat_pair_generation

    def __repr__(self) -> str:
        return f"TokenIdentity({self.username!r}, role={self.role!r}, pid={self.principal_id})"

    @property
    def is_admin(self) -> bool:
        if self.role in ("admin", "super_admin"):
            return True
        return "admin.read" in self.permissions

    @property
    def is_steward(self) -> bool:
        return self.role == "steward"

    @property
    def is_human(self) -> bool:
        if self.is_steward:
            return False
        if self.role in ("human", "admin", "super_admin"):
            return True
        return "tasks.human_gate" in self.permissions

    @property
    def is_agent(self) -> bool:
        if self.is_steward:
            return False
        if self.role == "agent":
            return True
        return not self.is_human and bool(self.principal_id)

    def has_permission(self, perm: str) -> bool:
        if self.role == "super_admin":
            return True
        if self.is_steward:
            held = self.permissions if self.permissions else STEWARD_PERMS
            return perm in held
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


VALID_ROLES = frozenset({"human", "agent", "admin", "steward"})


def parse_tokens(raw: str) -> dict[str, TokenIdentity]:
    """Parse HAIPLANE_HUB_TOKENS into a {token: TokenIdentity} mapping.

    Format: "name:token[:role],name2:token2[:role2]"
    Role is optional — defaults to ``human``. Valid roles: human, agent, admin, steward.
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


def stale_env_names() -> list[str]:
    """Имена (никогда не значения) переменных, которые хаб не прочитает (#964).

    Переменная с устаревшим префиксом — это политика, которую оператор считает
    включённой, а код никогда не увидит. Значения сюда не попадают: в окружении
    сервиса лежат и секреты, а сигнал уходит в ленту и в публичный /health.
    """
    return sorted(
        name
        for name in os.environ
        for prefix in brand.RETIRED_ENV_PREFIXES
        if name.startswith(prefix)
    )


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
        f"Either set HAIPLANE_HUB_TOKENS, bind to 127.0.0.1, or set "
        f"HAIPLANE_HUB_ALLOW_UNAUTHENTICATED_NETWORK=1 to override."
    )
