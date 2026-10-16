"""Split one pytest run into N parts by test FILE (#1767).

CI runs the full suite as three parallel parts (``.github/workflows/ci.yml``,
job ``pytest``). The split lives here, in a plugin loaded only by the command of
a part (``-p scripts.pytest_shard``), and NOT in ``tests/conftest.py``: any
``pytest_collection_modifyitems`` in a conftest moves the AC locators under
``tests/`` to "unknown" (``hub/services/locator_worker.py``, ``_conftest_issue``).

Rules:

* ``HAIPLANE_TEST_SHARD=i/N`` keeps the files for which
  ``sha256(path relative to the rootdir) mod N == i - 1``. The assignment is a
  pure function of the path, so every xdist worker of one part collects the same
  items (xdist refuses a worker whose collection differs).
* A wrong spec (not ``i/N``, ``i`` outside 1..N, N below 1) or a missing variable
  while the plugin is loaded is a session error with the reason. A plugin that
  silently ran everything would turn three parts into three full runs; one that
  silently ran nothing would be a green part with no tests.
* A part that keeps no file collects nothing, and pytest ends with code 5
  (no tests collected), which CI treats as a failure.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

import pytest

ENV_VAR = "HAIPLANE_TEST_SHARD"
_SPEC = re.compile(r"^(\d+)/(\d+)$")
_STASH_KEY = pytest.StashKey[tuple[int, int]]()


class ShardSpecError(ValueError):
    """The value of HAIPLANE_TEST_SHARD is missing or is not ``i/N`` in range."""


def parse_spec(raw: str | None) -> tuple[int, int]:
    """(index, total) from ``i/N``; ShardSpecError says why it is not one."""
    if raw is None or not raw.strip():
        raise ShardSpecError(
            f"{ENV_VAR} is not set, but the shard plugin is loaded: "
            "set it to i/N (for example 1/3) or do not load the plugin"
        )
    match = _SPEC.match(raw.strip())
    if match is None:
        raise ShardSpecError(f"{ENV_VAR}={raw!r} is not of the form i/N")
    index, total = int(match.group(1)), int(match.group(2))
    if total < 1 or not 1 <= index <= total:
        raise ShardSpecError(f"{ENV_VAR}={raw!r}: i must be in 1..N and N at least 1")
    return index, total


def shard_of(path: str, total: int) -> int:
    """The part (1..total) a test file belongs to."""
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return int(digest, 16) % total + 1


def _file_of(item: Any) -> str:
    """The path of the item's file relative to the rootdir, with ``/``."""
    return str(item.nodeid).split("::", 1)[0]


def pytest_configure(config: pytest.Config) -> None:
    # Runs on the controller and in every xdist worker: each one validates the
    # spec itself, so none of them can drift into running a different split.
    try:
        config.stash[_STASH_KEY] = parse_spec(os.environ.get(ENV_VAR))
    except ShardSpecError as exc:
        raise pytest.UsageError(f"pytest_shard: {exc}") from exc


def pytest_collection_modifyitems(config: pytest.Config, items: list[Any]) -> None:
    spec = config.stash.get(_STASH_KEY, None)
    if spec is None:  # configure was skipped: fail, never run everything
        raise pytest.UsageError("pytest_shard: the shard spec was never read")
    index, total = spec
    kept = [i for i in items if shard_of(_file_of(i), total) == index]
    dropped = [i for i in items if shard_of(_file_of(i), total) != index]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
