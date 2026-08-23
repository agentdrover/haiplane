from importlib.metadata import PackageNotFoundError

from hub import brand
from hub import version as hub_version
from hub.config import env_get


def test_product_title() -> None:
    assert brand.PRODUCT_TITLE == "Haiplane Hub"
    assert brand.PACKAGE_NAME == "haiplane-hub"
    assert brand.PACKAGE_NAME_LEGACY == "openclaw-hub"
    assert brand.MCP_SERVER_NAME == "haiplane-hub"
    assert brand.PUBLIC_DOMAIN == "haiplane.com"
    assert brand.ENV_PREFIX == "HAIPLANE_"
    assert brand.ENV_PREFIX_LEGACY == "OPENCLAW_"
    assert brand.COOKIE_NAME == "haiplane_hub_session"
    assert brand.COOKIE_NAME_LEGACY == "openclaw_hub_session"
    assert brand.CSRF_COOKIE_NAME == "haiplane_csrf"
    assert brand.CSRF_COOKIE_NAME_LEGACY == "openclaw_csrf"
    assert brand.GITHUB_REPO == "haiplane"
    assert brand.GITHUB_SLUG_LEGACY == "mrPDA/openclaw-hub-standalone"
    assert brand.CI_REPORT_ACTION_LEGACY == (
        "mrPDA/openclaw-hub-standalone/.github/actions/hub-ci-report@main"
    )


def test_env_get_prefers_haiplane(monkeypatch) -> None:
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://haiplane.com")
    monkeypatch.setenv("OPENCLAW_HUB_URL", "https://agenthai.ru")
    assert env_get("HUB_URL") == "https://haiplane.com"


def test_env_get_falls_back_to_openclaw(monkeypatch) -> None:
    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    monkeypatch.setenv("OPENCLAW_HUB_URL", "https://agenthai.ru")
    assert env_get("HUB_URL") == "https://agenthai.ru"


def test_env_get_treats_empty_haiplane_as_missing(monkeypatch) -> None:
    monkeypatch.setenv("HAIPLANE_HUB_URL", "")
    monkeypatch.setenv("OPENCLAW_HUB_URL", "https://agenthai.ru")
    assert env_get("HUB_URL") == "https://agenthai.ru"


def test_env_get_default(monkeypatch) -> None:
    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    monkeypatch.delenv("OPENCLAW_HUB_URL", raising=False)
    assert env_get("HUB_URL", "http://127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_github_slug_uses_legacy_when_owner_empty(monkeypatch) -> None:
    monkeypatch.setattr(brand, "GITHUB_OWNER", "")
    assert brand.github_slug() == brand.GITHUB_SLUG_LEGACY
    assert brand.ci_report_action() == brand.CI_REPORT_ACTION_LEGACY


def test_github_slug_uses_new_when_owner_set(monkeypatch) -> None:
    monkeypatch.setattr(brand, "GITHUB_OWNER", "agentdrover")
    assert brand.github_slug() == "agentdrover/haiplane"
    assert brand.require_github_owner() == "agentdrover"


def test_require_github_owner_raises_while_empty(monkeypatch) -> None:
    monkeypatch.setattr(brand, "GITHUB_OWNER", "")
    try:
        brand.require_github_owner()
    except ValueError:
        return
    raise AssertionError("require_github_owner must refuse an empty owner")


def test_get_app_version_prefers_new_distribution(monkeypatch) -> None:
    def fake_version(name: str) -> str:
        if name == brand.PACKAGE_NAME:
            return "9.9.9"
        raise PackageNotFoundError(name)

    monkeypatch.setattr(hub_version, "version", fake_version)
    assert hub_version.get_app_version() == "9.9.9"


def test_get_app_version_falls_back_to_legacy_distribution(monkeypatch) -> None:
    def fake_version(name: str) -> str:
        if name == brand.PACKAGE_NAME_LEGACY:
            return "8.8.8"
        raise PackageNotFoundError(name)

    monkeypatch.setattr(hub_version, "version", fake_version)
    assert hub_version.get_app_version() == "8.8.8"


def test_get_app_version_defaults_when_neither_installed(monkeypatch) -> None:
    def fake_version(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(hub_version, "version", fake_version)
    assert hub_version.get_app_version() == "0.1.0"
