"""Floor for dependencies pinned up to close a security advisory (#1284).

A lock refresh that silently rolls a patched package back below its fix
should fail here, not surface later as a reopened Dependabot alert.
"""

from importlib.metadata import version

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
