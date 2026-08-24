"""Application version helper."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from hub import brand


def get_app_version() -> str:
    """Return the installed package version or the pyproject fallback."""
    try:
        return version(brand.PACKAGE_NAME)
    except PackageNotFoundError:
        return "0.1.0"
