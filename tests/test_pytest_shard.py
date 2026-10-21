"""#1767: the suite runs as parts; together they are the suite, once.

The plugin is ``scripts/pytest_shard.py``. These tests execute a REAL isolated
suite in a temporary directory (pytester) and never run the repository's own
suite recursively: for ``tests/`` itself only ``--collect-only`` of the parts is
run (their number is the matrix of ci.yml, #1785).
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


def assert_parts_of_tests_dir_are_the_whole(total: int) -> None:
    """The real tests/: only collection, the ``total`` parts against the whole.

    #1785: ``total`` is the number of parts in ci.yml, not a constant here; the
    contract test of the workflow (AC-4) calls this with the matrix length.
    """
    specs = [None, *[f"{i}/{total}" for i in range(1, total + 1)]]
    runs = {spec: _collect_tests_dir(spec) for spec in specs}
    listings = {}
    for spec, proc in runs.items():
        out, _ = proc.communicate(timeout=300)
        assert proc.returncode == 0, f"{spec}\n{out[-2000:]}"
        listings[spec] = _collected(out)
    whole = listings.pop(None)
    assert len(whole) > 1000, "the collection of tests/ looks empty"
    assert len(listings) == total
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


# --- #1987: a part does not collect the files of the other parts -------------

# Every module that gets imported leaves a mark named after itself.
_MARKING_MODULE = """
import os
from pathlib import Path

Path(os.environ["IMPORT_MARKS"], {name!r}.replace("/", "__")).write_text("x")


def test_it():
    pass
"""


def _make_marking_suite(pytester: pytest.Pytester, names: list[str]) -> None:
    for name in names:
        path = pytester.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_MARKING_MODULE.format(name=name), encoding="utf-8")


def _passed(output: str) -> list[str]:
    """Ids of passed tests, serial (``id PASSED``) and xdist (``PASSED id``)."""
    serial = re.findall(r"^(\S+::\S+) PASSED", output, flags=re.MULTILINE)
    return serial or _executed(output)


def _marks(directory: Path) -> set[str]:
    return {p.name.replace("__", "/") for p in directory.iterdir()}


def test_a_part_does_not_collect_the_files_of_other_parts(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-1: only the files of the part are imported, serial and under xdist."""
    names = [f"test_m{n:02d}.py" for n in range(12)]
    _make_marking_suite(pytester, names)
    owned = {p: {n for n in names if pytest_shard.shard_of(n, 3) == p} for p in PARTS}
    assert all(owned.values()), owned  # the check below must be able to see a leak
    for extra in ([], ["-n", "2"]):
        for part in PARTS:
            marks = tmp_path / f"marks-{part}-{len(extra)}"
            marks.mkdir()
            monkeypatch.setenv("IMPORT_MARKS", str(marks))
            _env(monkeypatch, f"{part}/3", None)
            ret, out = _run(pytester, *extra)
            assert ret == 0, f"part {part} {extra}\n{out}"
            assert _marks(marks) == owned[part], (part, extra, out)


def test_the_hook_assigns_files_like_the_nodeid_does(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-4: directories, one basename twice, ``*_test.py``, a symlink, explicit files."""
    names = [
        "sub1/test_same.py",
        "sub2/test_same.py",
        "sub1/deep/test_deep.py",
        "x_test.py",
        "test_top.py",
    ]
    _make_marking_suite(pytester, names)
    for package in ("sub1", "sub2", "sub1/deep"):
        (pytester.path / package / "__init__.py").write_text("", encoding="utf-8")
    (pytester.path / "target_impl.py").write_text(
        _MARKING_MODULE.format(name="test_link.py"), encoding="utf-8"
    )
    (pytester.path / "test_link.py").symlink_to("target_impl.py")
    everything = [*names, "test_link.py"]

    executed: list[str] = []
    for part in PARTS:
        marks = tmp_path / f"marks-{part}"
        marks.mkdir()
        monkeypatch.setenv("IMPORT_MARKS", str(marks))
        _env(monkeypatch, f"{part}/3", None)
        for extra in ([], ["-n", "2"]):
            ret, out = _run(pytester, *extra)
            # An empty part is code 5; the others pass. Never anything else.
            assert ret in (0, 5), f"{part} {extra}\n{out}"
            for nodeid in _passed(out):
                # The hook and the nodeid agree on whose file it is.
                assert pytest_shard.shard_of(nodeid.split("::")[0], 3) == part, nodeid
            if extra == []:
                executed += _passed(out)
    files = sorted(n.split("::")[0] for n in executed)
    assert files == sorted(everything), files  # each file once, none lost

    # A pattern with a directory: pytest's own match decides what is a test file.
    monkeypatch.setenv("IMPORT_MARKS", str(tmp_path / "marks-1"))
    narrowed: list[str] = []
    for part in PARTS:
        _env(monkeypatch, f"{part}/3", None)
        ret, out = _run(pytester, "-o", "python_files=sub1/test_*.py")
        assert ret in (0, 5), out
        narrowed += [n.split("::")[0] for n in _passed(out)]
    assert sorted(narrowed) == ["sub1/test_same.py"], narrowed

    # An explicit file of another part: imported (the hook never sees it), its
    # items dropped, nothing runs, code 5. Same for ``file::test``.
    foreign = next(n for n in everything if pytest_shard.shard_of(n, 3) != 1)
    marks = tmp_path / "marks-explicit"
    marks.mkdir()
    monkeypatch.setenv("IMPORT_MARKS", str(marks))
    _env(monkeypatch, "1/3", None)
    for target in (foreign, f"{foreign}::test_it"):
        for extra in ([], ["-n", "2"]):
            ret, out = _run(pytester, target, *extra)
            assert ret == 5, f"{target} {extra}\n{out}"
            assert "PASSED" not in out, out
    assert foreign in _marks(marks)  # imported, then dropped
