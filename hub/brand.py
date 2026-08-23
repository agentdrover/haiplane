from __future__ import annotations

PRODUCT_NAME = "Haiplane"
PRODUCT_TITLE = "Haiplane Hub"
PACKAGE_NAME = "haiplane-hub"
PACKAGE_NAME_LEGACY = "openclaw-hub"
MCP_SERVER_NAME = "haiplane-hub"
PUBLIC_DOMAIN = "haiplane.com"
FORMER_TITLE = "OpenClaw Hub"
ENV_PREFIX = "HAIPLANE_"
ENV_PREFIX_LEGACY = "OPENCLAW_"
GIT_BASE_BRANCH_KEY = "haiplane.baseBranch"
GIT_RELEASE_BRANCH_KEY = "haiplane.releaseBranch"
GIT_BASE_BRANCH_KEY_LEGACY = "openclaw.baseBranch"
GIT_RELEASE_BRANCH_KEY_LEGACY = "openclaw.releaseBranch"
COOKIE_NAME = "haiplane_hub_session"
COOKIE_NAME_LEGACY = "openclaw_hub_session"
CSRF_COOKIE_NAME = "haiplane_csrf"
CSRF_COOKIE_NAME_LEGACY = "openclaw_csrf"
SEEDED_CI = "haiplane-ci.yml"
SEEDED_STALE = "haiplane-stale.yml"
SEEDED_CI_LEGACY = "openclaw-ci.yml"
SEEDED_STALE_LEGACY = "openclaw-stale.yml"
GITHUB_OWNER = (
    ""  # "agentdrover" once agentdrover/haiplane exists; do not default to mrPDA
)
GITHUB_REPO = "haiplane"
GITHUB_OWNER_LEGACY = "mrPDA"
GITHUB_REPO_LEGACY = "openclaw-hub-standalone"
GITHUB_SLUG_LEGACY = f"{GITHUB_OWNER_LEGACY}/{GITHUB_REPO_LEGACY}"
CI_REPORT_ACTION_LEGACY = f"{GITHUB_SLUG_LEGACY}/.github/actions/hub-ci-report@main"


def github_slug() -> str:
    if not GITHUB_OWNER:
        return GITHUB_SLUG_LEGACY
    return f"{GITHUB_OWNER}/{GITHUB_REPO}"


def ci_report_action() -> str:
    if not GITHUB_OWNER:
        return CI_REPORT_ACTION_LEGACY
    return f"{github_slug()}/.github/actions/hub-ci-report@main"


def require_github_owner() -> str:
    if not GITHUB_OWNER:
        raise ValueError(
            "GITHUB_OWNER is empty; refuse to emit a GitHub slug until the "
            "new account is written into hub.brand"
        )
    return GITHUB_OWNER
