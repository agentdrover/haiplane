"""Split one pytest run into N parts by test FILE (#1767).

CI runs the full suite as parallel parts (``.github/workflows/ci.yml``,
job ``pytest``; the number of parts is the matrix there). The split lives here, in a plugin loaded only by the command of
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
* A part does not COLLECT the test files of the other parts (#1987):
  ``pytest_ignore_collect`` skips them before their modules are imported, which
  is most of the cost of a part's start (every xdist worker repeats it). It does
  not stop an import that a test of the part makes itself (a test file importing
  a sibling), and it never runs for a file named on the command line: such a file
  is imported and ``pytest_collection_modifyitems`` drops its items, so the part
  ends with code 5 as before. ``pytest_collection_modifyitems`` is unchanged and
  stays the filter for everything the hook did not skip.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

_matches_python_files: Callable[[Path, Iterable[str]], bool] | None
try:  # pytest's own match of a file against ``python_files`` (same rules, #1987)
    from _pytest.python import path_matches_patterns

    _matches_python_files = path_matches_patterns
except ImportError:  # pragma: no cover - a pytest without it: the hook stays off
    _matches_python_files = None

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


def _in_other_part(rel_path: str, spec: tuple[int, int]) -> bool:
    """Whether the file belongs to a part other than ``spec``: THE one rule.

    Both hooks ask this, with the same kind of path (relative to the rootdir,
    with ``/``), so they cannot assign a file differently.
    """
    index, total = spec
    return shard_of(rel_path, total) != index


def _lexical_rel_path(path: Path, rootpath: Path) -> str | None:
    """``path`` relative to ``rootpath`` as the nodeid spells it, or None.

    Lexical on purpose: no ``resolve()``, pytest keeps the path of a symlink. A
    file outside the rootdir has its own nodeid rules, so there is no answer.
    """
    try:
        return path.relative_to(rootpath).as_posix()
    except ValueError:
        return None


def pytest_configure(config: pytest.Config) -> None:
    # Runs on the controller and in every xdist worker: each one validates the
    # spec itself, so none of them can drift into running a different split.
    try:
        config.stash[_STASH_KEY] = parse_spec(os.environ.get(ENV_VAR))
    except ShardSpecError as exc:
        raise pytest.UsageError(f"pytest_shard: {exc}") from exc


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Skip a test file of another part before its module is imported (#1987).

    True only for a file that pytest would collect as a test module
    (``python_files``, pytest's own matcher) and that another part owns. Any
    doubt - no spec, no matcher, a file outside the rootdir, not a file - is None:
    the file is collected as before and ``pytest_collection_modifyitems`` decides.
    """
    spec = config.stash.get(_STASH_KEY, None)
    if spec is None or _matches_python_files is None:
        return None
    if collection_path.suffix != ".py" or not collection_path.is_file():
        return None
    if not _matches_python_files(collection_path, config.getini("python_files")):
        return None
    rel = _lexical_rel_path(collection_path, config.rootpath)
    if rel is None:
        return None
    return True if _in_other_part(rel, spec) else None


def pytest_collection_modifyitems(config: pytest.Config, items: list[Any]) -> None:
    spec = config.stash.get(_STASH_KEY, None)
    if spec is None:  # configure was skipped: fail, never run everything
        raise pytest.UsageError("pytest_shard: the shard spec was never read")
    kept = [i for i in items if not _in_other_part(_file_of(i), spec)]
    dropped = [i for i in items if _in_other_part(_file_of(i), spec)]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
