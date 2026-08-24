from __future__ import annotations

PRODUCT_NAME = "Haiplane"
PRODUCT_TITLE = "Haiplane Hub"
PACKAGE_NAME = "haiplane-hub"
MCP_SERVER_NAME = "haiplane-hub"
PUBLIC_DOMAIN = "haiplane.com"
ENV_PREFIX = "HAIPLANE_"
GIT_BASE_BRANCH_KEY = "haiplane.baseBranch"
GIT_RELEASE_BRANCH_KEY = "haiplane.releaseBranch"
COOKIE_NAME = "haiplane_hub_session"
CSRF_COOKIE_NAME = "haiplane_csrf"
SEEDED_CI = "haiplane-ci.yml"
SEEDED_STALE = "haiplane-stale.yml"
GITHUB_OWNER = "agentdrover"
GITHUB_REPO = "haiplane"


def github_slug() -> str:
    return f"{require_github_owner()}/{GITHUB_REPO}"


def ci_report_action() -> str:
    return f"{github_slug()}/.github/actions/hub-ci-report@main"


def require_github_owner() -> str:
    if not GITHUB_OWNER:
        raise ValueError(
            "GITHUB_OWNER is empty; refuse to emit a GitHub slug until the "
            "new account is written into hub.brand"
        )
    return GITHUB_OWNER
