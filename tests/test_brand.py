import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

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
    assert brand.GITHUB_OWNER == "agentdrover"
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


# ---------------------------------------------------------------------------
# Import-time env precedence (Task 4). ``hub.config`` and ``hub.cli`` bind
# URL/token/path at import, so monkeypatching the environment after import
# proves nothing — these tests spawn a fresh interpreter with a controlled
# environment instead.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    """Process env with every HAIPLANE_/OPENCLAW_ variable removed, plus extra."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("HAIPLANE_", "OPENCLAW_"))
    }
    env["PYTHONPATH"] = str(_REPO_ROOT)
    env.update(extra)
    return env


def _module_attr(module: str, attr: str, env: dict[str, str]) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", f"from {module} import {attr}; print({attr})"],
        capture_output=True,
        text=True,
        env=_clean_env(env),
        cwd=str(_REPO_ROOT),
        check=True,
    )
    return proc.stdout.strip()


def test_config_defaults_are_haiplane_family() -> None:
    """Wave 4-code: hub state / workspace / transcripts defaults are Haiplane.

    Dispatch and vast defaults are deliberately asserted as STILL legacy —
    they move only in Wave 4-dispatch (Task 9), after the external producer
    writes the new catalog or the new binary names resolve.
    """
    code = (
        "import hub.config as c\n"
        "print(c.HUB_DB_PATH)\n"
        "print(c.WORKSPACE_REPO_LINK)\n"
        "print(c.TRANSCRIPTS_DIR)\n"
        "print(c.DISPATCH_BIN)\n"
        "print(c.VAST_JOB_BIN)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_clean_env({}),
        cwd=str(_REPO_ROOT),
        check=True,
    )
    db, workspace, transcripts, dispatch_bin, vast_bin = (
        proc.stdout.strip().splitlines()
    )
    assert "haiplane-hub" in db
    assert "openclaw-hub" not in db
    assert workspace.endswith(os.path.join(".haiplane", "workspace", "repo"))
    assert ".openclaw" not in workspace
    assert transcripts.endswith(os.path.join(".haiplane", "transcripts"))
    assert ".openclaw" not in transcripts
    assert "oc-dev-dispatch" in dispatch_bin, "dispatch binary moves in Task 9 only"
    assert "vast-openclaw" in vast_bin, "vast binary moves in Task 9 only"


def test_cli_prefers_haiplane_url() -> None:
    url = _module_attr(
        "hub.cli",
        "HUB_URL",
        {
            "HAIPLANE_HUB_URL": "https://haiplane.com",
            "OPENCLAW_HUB_URL": "https://agenthai.ru",
        },
    )
    assert url.strip() == "https://haiplane.com"


def test_legacy_only_tokens_authenticate(tmp_path: Path) -> None:
    """A process configured only with OPENCLAW_HUB_TOKENS still authenticates.

    Both assertions matter: 401 without a token proves the legacy tokens were
    actually loaded (open mode would answer 200 to anyone and mask a broken
    fallback), 200 with the token proves the legacy value authenticates.
    """
    code = (
        "from fastapi.testclient import TestClient\n"
        "from hub.app import app\n"
        "with TestClient(app) as client:\n"
        "    anon = client.get('/api/tasks')\n"
        "    authed = client.get(\n"
        "        '/api/tasks', headers={'Authorization': 'Bearer tok-legacy'}\n"
        "    )\n"
        "print(anon.status_code, authed.status_code)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_clean_env(
            {
                "OPENCLAW_HUB_TOKENS": "legacy:tok-legacy:human",
                "OPENCLAW_HUB_DB": str(tmp_path / "hub.db"),
            }
        ),
        cwd=str(_REPO_ROOT),
        check=True,
    )
    assert proc.stdout.strip() == "401 200", proc.stdout + proc.stderr
