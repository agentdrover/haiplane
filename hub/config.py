from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("OPENCLAW_HUB_HOME", Path.home()))

REPO_NAME = os.environ.get("OPENCLAW_HUB_REPO", "")
WORKSPACE_REPO_LINK = Path(
    os.environ.get("OPENCLAW_WORKSPACE_REPO", str(HOME / ".openclaw" / "workspace" / "repo"))
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

TRANSCRIPTS_DIR = Path(
    os.environ.get("OPENCLAW_TRANSCRIPTS_DIR", str(HOME / ".openclaw" / "transcripts"))
)

HUB_DB_PATH = Path(
    os.environ.get(
        "OPENCLAW_HUB_DB", str(HOME / ".local" / "state" / "openclaw-hub" / "hub.db")
    )
)
HUB_HOST = os.environ.get("OPENCLAW_HUB_HOST", "0.0.0.0")
HUB_PORT = int(os.environ.get("OPENCLAW_HUB_PORT", "8080"))

MAX_REVIEW_CYCLES = int(os.environ.get("OPENCLAW_MAX_REVIEW_CYCLES", "3"))
MAX_CI_FIX_CYCLES = int(os.environ.get("OPENCLAW_MAX_CI_FIX_CYCLES", "3"))
REVIEW_RUNTIME = os.environ.get("OPENCLAW_REVIEW_RUNTIME", "openrouter")
REVIEW_AGENT = os.environ.get("OPENCLAW_REVIEW_AGENT", "code-reviewer")

ARBITER_RUNTIME = os.environ.get("OPENCLAW_ARBITER_RUNTIME", "openrouter")
ARBITER_AGENT = os.environ.get("OPENCLAW_ARBITER_AGENT", "architect-analyst")

STALE_THRESHOLD_MINUTES = int(os.environ.get("OPENCLAW_STALE_MINUTES", "30"))

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
HUB_COOKIE_NAME = os.environ.get("OPENCLAW_HUB_COOKIE", "openclaw_hub_session")
# 30 days by default — Hub is an internal tool, long-lived sessions are fine.
HUB_COOKIE_MAX_AGE = int(os.environ.get("OPENCLAW_HUB_COOKIE_MAX_AGE", str(30 * 24 * 3600)))


def parse_tokens(raw: str) -> dict[str, str]:
    """Parse OPENCLAW_HUB_TOKENS into a {token: username} mapping.

    Format: "name1:token1,name2:token2" (whitespace tolerated).
    Malformed entries are skipped silently — the env should be considered
    operator-controlled, not user input.
    """
    out: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        name, token = chunk.split(":", 1)
        name = name.strip()
        token = token.strip()
        if name and token:
            out[token] = name
    return out


HUB_TOKENS: dict[str, str] = parse_tokens(HUB_TOKENS_RAW)
