"""#1666: a hanging test must fail fast and by name; CI steps carry time limits."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parent.parent

_HANGING = """
import time

def test_fast():
    pass

def test_hangs_forever():
    time.sleep(30)
"""


def test_a_hanging_test_fails_with_its_nodeid(pytester) -> None:
    """AC-1: timeout=1 turns a 30 s sleep into a failure naming the test.

    Run twice: serial, and under xdist (-n 2), the way CI runs the suite.
    This proves the PLUGIN behaviour with an explicit inner config; that the
    project itself sets timeout=300 / signal is proven by AC-2 below.
    """
    pytester.makepyfile(test_hang=_HANGING)
    pytester.makeini("[pytest]\ntimeout = 1\ntimeout_method = signal\n")
    for extra in ([], ["-n", "2"]):
        result = pytester.runpytest_subprocess(*extra, timeout=60)
        out = result.stdout.str() + result.stderr.str()
        label = f"{extra}\n{out}"
        assert result.ret != 0, label
        assert "Timeout (>1.0s)" in out, label
        assert "test_hang.py::test_hangs_forever" in out, label
        assert "time.sleep(30)" in out, label  # the stack points at the hang
        result.assert_outcomes(passed=1, failed=1)


def test_ci_and_pytest_have_explicit_time_limits() -> None:
    """AC-2: exact limits in pyproject, uv.lock and ci.yml."""
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())
    ini = py["tool"]["pytest"]["ini_options"]
    assert ini["timeout"] == 300
    assert ini["timeout_method"] == "signal"
    assert any(d.startswith("pytest-timeout") for d in py["dependency-groups"]["dev"])

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    assert "pytest-timeout" in {p["name"] for p in lock["package"]}

    wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = wf["jobs"]["test"]
    assert job["timeout-minutes"] == 90
    steps = {s.get("name"): s for s in job["steps"]}
    assert steps["Test"]["timeout-minutes"] == 25
    assert steps["Report AC tests and validation to Hub"]["timeout-minutes"] == 20
