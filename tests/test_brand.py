import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from hub import brand
from hub import version as hub_version
from hub.config import env_get

# Старый бренд, собранный конкатенацией: негативные тесты обязаны его
# упоминать, а страж (tests/test_no_legacy_name.py) не должен ловить их сами.
_LEGACY = "open" + "claw"
_LEGACY_PREFIX = _LEGACY.upper() + "_"


def test_product_title() -> None:
    assert brand.PRODUCT_TITLE == "Haiplane Hub"
    assert brand.PACKAGE_NAME == "haiplane-hub"
    assert brand.MCP_SERVER_NAME == "haiplane-hub"
    assert brand.PUBLIC_DOMAIN == "haiplane.com"
    assert brand.ENV_PREFIX == "HAIPLANE_"
    assert brand.COOKIE_NAME == "haiplane_hub_session"
    assert brand.CSRF_COOKIE_NAME == "haiplane_csrf"
    assert brand.GITHUB_REPO == "haiplane"
    assert brand.GITHUB_OWNER == "agentdrover"


def test_brand_carries_no_legacy_constants() -> None:
    """Wave 5: ни одной *_LEGACY-константы и ни одного старого значения."""
    for name in dir(brand):
        if name.startswith("_"):
            continue  # dunder-атрибуты модуля несут путь чекаута
        assert "LEGACY" not in name.upper(), name
        value = getattr(brand, name)
        if isinstance(value, str):
            assert _LEGACY not in value.lower(), f"{name}={value!r}"


def test_env_get_reads_canonical_prefix(monkeypatch) -> None:
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://haiplane.com")
    assert env_get("HUB_URL") == "https://haiplane.com"


def test_env_get_ignores_legacy_prefix(monkeypatch) -> None:
    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    monkeypatch.setenv(_LEGACY_PREFIX + "HUB_URL", "https://agenthai.ru")
    assert env_get("HUB_URL") is None


def test_env_get_treats_empty_as_missing(monkeypatch) -> None:
    monkeypatch.setenv("HAIPLANE_HUB_URL", "")
    assert env_get("HUB_URL", "http://127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_env_get_default(monkeypatch) -> None:
    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    assert env_get("HUB_URL", "http://127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_github_slug_refuses_empty_owner(monkeypatch) -> None:
    monkeypatch.setattr(brand, "GITHUB_OWNER", "")
    with pytest.raises(ValueError):
        brand.github_slug()
    with pytest.raises(ValueError):
        brand.ci_report_action()


def test_github_slug_uses_owner(monkeypatch) -> None:
    monkeypatch.setattr(brand, "GITHUB_OWNER", "agentdrover")
    assert brand.github_slug() == "agentdrover/haiplane"
    assert brand.ci_report_action() == (
        "agentdrover/haiplane/.github/actions/hub-ci-report@main"
    )
    assert brand.require_github_owner() == "agentdrover"


def test_require_github_owner_raises_while_empty(monkeypatch) -> None:
    monkeypatch.setattr(brand, "GITHUB_OWNER", "")
    try:
        brand.require_github_owner()
    except ValueError:
        return
    raise AssertionError("require_github_owner must refuse an empty owner")


def test_get_app_version_reads_canonical_distribution(monkeypatch) -> None:
    def fake_version(name: str) -> str:
        if name == brand.PACKAGE_NAME:
            return "9.9.9"
        raise PackageNotFoundError(name)

    monkeypatch.setattr(hub_version, "version", fake_version)
    assert hub_version.get_app_version() == "9.9.9"


def test_get_app_version_defaults_when_not_installed(monkeypatch) -> None:
    def fake_version(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(hub_version, "version", fake_version)
    assert hub_version.get_app_version() == "0.1.0"


# ---------------------------------------------------------------------------
# Import-time env reads. ``hub.config`` and ``hub.cli`` bind URL/token/path at
# import, so monkeypatching the environment after import proves nothing —
# these tests spawn a fresh interpreter with a controlled environment instead.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    """Process env with every hub-prefixed variable removed, plus extra."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("HAIPLANE_", _LEGACY_PREFIX))
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
    """Hub state / workspace / transcripts / binaries defaults are Haiplane."""
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
    assert _LEGACY not in db
    assert workspace.endswith(os.path.join(".haiplane", "workspace", "repo"))
    assert "." + _LEGACY not in workspace
    assert transcripts.endswith(os.path.join(".haiplane", "transcripts"))
    assert "." + _LEGACY not in transcripts
    assert "hp-dev-dispatch" in dispatch_bin
    assert "vast-haiplane" in vast_bin


def test_cli_reads_haiplane_url() -> None:
    url = _module_attr(
        "hub.cli",
        "HUB_URL",
        {
            "HAIPLANE_HUB_URL": "https://haiplane.com",
            _LEGACY_PREFIX + "HUB_URL": "https://agenthai.ru",
        },
    )
    assert url.strip() == "https://haiplane.com"


def test_legacy_only_tokens_do_not_authenticate(tmp_path: Path) -> None:
    """Wave 5: токены только под старым префиксом больше не читаются.

    Хаб, у которого нет HAIPLANE_HUB_TOKENS, работает в открытом режиме —
    анонимный запрос отвечает 200, то есть legacy-переменная не загрузилась
    и никак не влияет на аутентификацию.
    """
    code = (
        "from fastapi.testclient import TestClient\n"
        "from hub.app import app\n"
        "with TestClient(app) as client:\n"
        "    anon = client.get('/api/tasks')\n"
        "print(anon.status_code)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_clean_env(
            {
                _LEGACY_PREFIX + "HUB_TOKENS": "legacy:tok-legacy:human",
                "HAIPLANE_HUB_DB": str(tmp_path / "hub.db"),
            }
        ),
        cwd=str(_REPO_ROOT),
        check=True,
    )
    assert proc.stdout.strip() == "200", proc.stdout + proc.stderr
