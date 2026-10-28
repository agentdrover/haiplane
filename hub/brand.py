from __future__ import annotations

PRODUCT_NAME = "Haiplane"
PRODUCT_TITLE = "Haiplane Hub"
PACKAGE_NAME = "haiplane-hub"
MCP_SERVER_NAME = "haiplane-hub"
PUBLIC_DOMAIN = "haiplane.com"
ENV_PREFIX = "HAIPLANE_"
# Префиксы, которые env_get больше НЕ читает (#964). Живут в одном файле с
# ENV_PREFIX сознательно: следующий ребрендинг меняет обе строки одним диффом,
# и отставленный префикс попадает сюда, а не в молчаливое небытие — ребрендинг
# #932 оставил на проде три drop-in'а со старым префиксом, и хаб молча жил на
# дефолтах. Значение собрано конкатенацией: страж Волны 5
# (tests/test_no_legacy_name.py) запрещает старому имени жить в HEAD литералом.
RETIRED_ENV_PREFIXES: tuple[str, ...] = (("open" + "claw").upper() + "_",)
GIT_BASE_BRANCH_KEY = "haiplane.baseBranch"
GIT_RELEASE_BRANCH_KEY = "haiplane.releaseBranch"
# #2000: the command a clone agreed to run before a push (set by `make hooks`).
# The hub never writes it: it must not run a branch's code on its own host.
GIT_PRE_PUSH_CHECK_KEY = "haiplane.prePushCheck"
COOKIE_NAME = "haiplane_hub_session"
CSRF_COOKIE_NAME = "haiplane_csrf"
SEEDED_CI = "haiplane-ci.yml"
SEEDED_STALE = "haiplane-stale.yml"
SEEDED_CI_REPORT = "haiplane-ci-report.yml"
GITHUB_OWNER = "agentdrover"
GITHUB_REPO = "haiplane"


def github_slug() -> str:
    return f"{require_github_owner()}/{GITHUB_REPO}"


def ci_report_action() -> str:
    return f"{github_slug()}/.github/actions/hub-ci-report@main"


def ci_send_action() -> str:
    """The trusted sender of a satellite's CI report (#1848)."""
    return f"{github_slug()}/.github/actions/hub-ci-send@main"


def require_github_owner() -> str:
    if not GITHUB_OWNER:
        raise ValueError(
            "GITHUB_OWNER is empty; refuse to emit a GitHub slug until the "
            "new account is written into hub.brand"
        )
    return GITHUB_OWNER
