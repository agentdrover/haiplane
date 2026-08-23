"""Application version helper."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from hub import brand


def get_app_version() -> str:
    """Return the installed package version or the pyproject fallback."""
    for name in (brand.PACKAGE_NAME, brand.PACKAGE_NAME_LEGACY):
        try:
            return version(name)
        except PackageNotFoundError:
            continue
    return "0.1.0"
