"""Application version helper."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def get_app_version() -> str:
    """Return the installed package version or the pyproject fallback."""
    try:
        return version("openclaw-hub")
    except PackageNotFoundError:
        return "0.1.0"
