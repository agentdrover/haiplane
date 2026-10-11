"""Floor for dependencies pinned up to close a security advisory (#1284).

A lock refresh that silently rolls a patched package back below its fix
should fail here, not surface later as a reopened Dependabot alert.
"""

import tomllib
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

# CVE-2026-63374 (critical, IDNA 2003 host encoding in TLSStream) and
# CVE-2026-64847 (moderate, process-pool workers hanging on unread stderr)
# are both fixed in anyio 4.14.2.
ANYIO_PATCHED_RELEASE = Version("4.14.2")


def test_anyio_is_at_or_above_the_patched_release() -> None:
    installed = Version(version("anyio"))
    assert installed >= ANYIO_PATCHED_RELEASE, (
        f"anyio {installed} is below {ANYIO_PATCHED_RELEASE}, "
        "the release that fixes CVE-2026-63374 and CVE-2026-64847"
    )


# #1619: pip-audit named these three. pyjwt is a runtime dependency (via mcp)
# and prod installs without uv.lock, so its floor must also be a requirement
# of the project; urllib3 and multidict are dev-only and held by the lock.
FIXED_RELEASES = {
    "pyjwt": Version("2.15.0"),
    "urllib3": Version("2.8.0"),
    "multidict": Version("6.9.1"),
}


def test_vulnerable_dependencies_are_above_their_fixed_versions() -> None:
    for name, fixed in FIXED_RELEASES.items():
        installed = Version(version(name))
        assert installed >= fixed, (
            f"{name} {installed} is below {fixed}, the release that fixes its advisories"
        )


def test_pyjwt_floor_is_a_project_requirement() -> None:
    # Prod runs `pip install -e` without the lock: an already installed
    # pyjwt satisfies mcp's loose bound, so only our own floor upgrades it.
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text()
    )
    requirements = [Requirement(r) for r in pyproject["project"]["dependencies"]]
    pyjwt = [r for r in requirements if canonicalize_name(r.name) == "pyjwt"]
    assert pyjwt, "pyjwt must be a direct requirement of the project"
    assert any(
        not r.specifier.contains("2.14.9", prereleases=True)
        and r.specifier.contains("2.15.0")
        for r in pyjwt
    ), f"pyjwt floor in pyproject must be >=2.15.0, got {[str(r) for r in pyjwt]}"
