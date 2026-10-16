"""#1767: the suite runs as three parts; together they are the suite, once.

The plugin is ``scripts/pytest_shard.py``. These tests execute a REAL isolated
suite in a temporary directory (pytester) and never run the repository's own
suite recursively: for ``tests/`` itself only ``--collect-only`` of the three
parts is run.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import pytest_shard

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ["-p", "scripts.pytest_shard", "-p", "no:cacheprovider"]
PARTS = (1, 2, 3)
FILES = 20
TESTS_PER_FILE = 3

# Written into the isolated suite: every process (the controller and each xdist
# worker) records the collection it ended up with, after the plugin has run.
_RECORDER = """
import os
from pathlib import Path


def pytest_collection_finish(session):
    who = os.environ.get("PYTEST_XDIST_WORKER", "controller")
    out = Path(os.environ["COLLECTION_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    (out / (who + ".txt")).write_text(
        "\\n".join(sorted(i.nodeid for i in session.items)), encoding="utf-8"
    )
"""


def _make_suite(pytester: pytest.Pytester) -> set[str]:
    expected: set[str] = set()
    for n in range(FILES):
        name = f"test_f{n:02d}.py"
        body = "".join(f"def test_{k}():\n    pass\n\n" for k in range(TESTS_PER_FILE))
        pytester.makepyfile(**{name[:-3]: body})
        expected |= {f"{name}::test_{k}" for k in range(TESTS_PER_FILE)}
    pytester.makeconftest(_RECORDER)
    return expected


# This suite itself runs under xdist in CI: the inner runs must not inherit the
# outer worker's identity (the conftest of an inner run tells controller from
# worker by PYTEST_XDIST_WORKER, and xdist reads the others).
_OUTER_XDIST = (
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_XDIST_TESTRUNUID",
)


def _env(monkeypatch: pytest.MonkeyPatch, spec: str | None, extra: Path | None) -> None:
    for name in _OUTER_XDIST:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(pytest_shard.ENV_VAR, raising=False)
    if spec is not None:
        monkeypatch.setenv(pytest_shard.ENV_VAR, spec)
    # The subprocess must find `scripts.pytest_shard` the way `uv run pytest`
    # does from the repository root: it is a plain import of the root.
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    if extra is not None:
        monkeypatch.setenv("COLLECTION_DIR", str(extra))


def _executed(output: str) -> list[str]:
    return re.findall(r"PASSED\s+(\S+::\S+)", output)


def _collected(output: str) -> list[str]:
    return [ln.strip() for ln in output.splitlines() if "::" in ln and ln[:1] != " "]


def _pytest_bin() -> str:
    # The console script, like `uv run pytest`: its sys.path[0] is the venv's
    # bin directory, NOT the working directory, so a plugin that only imports
    # thanks to the cwd would fail here exactly as it would in CI.
    return str(Path(sys.executable).parent / "pytest")


def _collect_tests_dir(spec: str | None) -> subprocess.Popen[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k != pytest_shard.ENV_VAR and k not in _OUTER_XDIST
    }
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTHONPATH", None)
    args = [_pytest_bin(), "--collect-only", "-q", "-p", "no:cacheprovider"]
    if spec is not None:
        env[pytest_shard.ENV_VAR] = spec
        args = [*args, "-p", "scripts.pytest_shard"]
    return subprocess.Popen(  # nosec B603
        args,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_three_shards_cover_a_suite_exactly_once_under_xdist(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-1: three parts with -n 2 are the suite, once; same collection per worker."""
    expected = _make_suite(pytester)
    assert len(expected) == FILES * TESTS_PER_FILE

    executed: dict[int, list[str]] = {}
    for part in PARTS:
        record = tmp_path / f"collection-{part}"
        _env(monkeypatch, f"{part}/3", record)
        result = pytester.runpytest_subprocess("-v", "-n", "2", *PLUGIN, timeout=120)
        out = result.stdout.str() + result.stderr.str()
        assert result.ret == 0, f"part {part}\n{out}"
        executed[part] = _executed(out)
        assert executed[part], f"part {part} is empty\n{out}"

        # Every worker of the part got the SAME collection, and it is the part.
        workers = {p.stem: p.read_text() for p in record.glob("gw*.txt")}
        assert set(workers) == {"gw0", "gw1"}, (part, sorted(workers))
        assert workers["gw0"] == workers["gw1"], f"part {part}: workers disagree"
        assert set(workers["gw0"].splitlines()) == set(executed[part]), part

    everything = [n for part in PARTS for n in executed[part]]
    assert len(everything) == len(set(everything)), "a test ran in two parts"
    assert set(everything) == expected, expected.symmetric_difference(everything)

    # The real tests/: only collection, the three parts against the whole.
    runs = {spec: _collect_tests_dir(spec) for spec in (None, "1/3", "2/3", "3/3")}
    listings = {}
    for spec, proc in runs.items():
        out, _ = proc.communicate(timeout=300)
        assert proc.returncode == 0, f"{spec}\n{out[-2000:]}"
        listings[spec] = _collected(out)
    whole = listings.pop(None)
    assert len(whole) > 1000, "the collection of tests/ looks empty"
    parts = [n for lst in listings.values() for n in lst]
    assert all(listings.values()), "a part of tests/ is empty"
    assert len(parts) == len(set(parts)), "a test of tests/ is in two parts"
    assert sorted(parts) == sorted(whole), "the parts are not the whole tests/"


_BAD_SPECS = ["0/3", "4/3", "abc", "1/0", "1/3/2", "", None]
_SPOIL_FOR_WORKERS = """
import os
import pytest


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        os.environ["HAIPLANE_TEST_SHARD"] = "abc"
"""


def _run(pytester: pytest.Pytester, *extra: str) -> tuple[int, str]:
    result = pytester.runpytest_subprocess("-v", *extra, *PLUGIN, timeout=120)
    return int(result.ret), result.stdout.str() + result.stderr.str()


def test_a_bad_or_empty_shard_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a wrong spec is a session error with the reason, an empty part is 5."""
    pytester.makepyfile(test_one="def test_a():\n    pass\n")

    # A wrong or missing spec: usage error (4) with the reason, no test runs,
    # serial and under xdist.
    for spec in _BAD_SPECS:
        for extra in ([], ["-n", "2"]):
            _env(monkeypatch, spec, None)
            ret, out = _run(pytester, *extra)
            label = f"{spec!r} {extra}\n{out}"
            assert ret == 4, label
            assert "pytest_shard" in out, label
            assert f"{pytest_shard.ENV_VAR}={spec!r}" in out or "is not set" in out, (
                label
            )
            assert "PASSED" not in out, label

    # A part that keeps no file: no tests collected (5), serial and under xdist.
    owner = pytest_shard.shard_of("test_one.py", 3)
    empty = next(p for p in PARTS if p != owner)
    for extra in ([], ["-n", "2"]):
        _env(monkeypatch, f"{empty}/3", None)
        ret, out = _run(pytester, *extra)
        assert ret == 5, f"{extra}\n{out}"
        assert "PASSED" not in out, out

    # Every xdist worker checks the spec itself: the controller sees a valid
    # spec, the environment its workers inherit is spoiled afterwards, and the
    # run still ends red with the reason.
    pytester.makeconftest(_SPOIL_FOR_WORKERS)
    _env(monkeypatch, f"{owner}/3", None)
    ret, out = _run(pytester, "-n", "2")
    assert ret != 0, out
    assert "pytest_shard" in out and "'abc'" in out, out
    assert "PASSED" not in out, out


def test_the_spec_parser_and_the_split_are_pure() -> None:
    assert pytest_shard.parse_spec("2/3") == (2, 3)
    assert pytest_shard.parse_spec(" 1/1 ") == (1, 1)
    for bad in ("0/3", "4/3", "x", "1/0", "-1/3", "1/3/2", "", None):
        with pytest.raises(pytest_shard.ShardSpecError):
            pytest_shard.parse_spec(bad)
    assert pytest_shard.shard_of("tests/a.py", 3) == pytest_shard.shard_of(
        "tests/a.py", 3
    )
    assert {pytest_shard.shard_of(f"tests/f{i}.py", 3) for i in range(60)} == {1, 2, 3}
