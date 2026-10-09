"""The locator analysis runs in a bounded child process (#1650, round 3).

Two inputs found by review hang a regular-expression or graph walk that runs in
the hub process: a vitest file ``// it('`` followed by backslashes, and a class
graph ``Ci(Ci-1, Ci-2)``. The answer is not to catch such inputs one by one but
to make the analysis unable to hurt the hub: separate interpreter, hard limits,
a deadline, and ``unknown`` for the whole request when any of them trips.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

from hub.services import test_existence
from hub.services.test_existence import (
    MISSING,
    OVER_BUDGET,
    RESOLVABLE,
    UNKNOWN,
    resolve_locators_at_ref,
)
from tests.branch_code_support import FakeGit

_PATH = "tests/test_p.py"
_TS = "web/a.test.ts"


def _ac(node: str, file: str = _PATH, ac_id: str = "AC-1") -> SimpleNamespace:
    return SimpleNamespace(id=ac_id, verifiable_by="test", test_ref=f"{file}::{node}")


async def _resolve(files: dict, acs: list, git: FakeGit | None = None) -> list[dict]:
    git = git or FakeGit(files)
    return await asyncio.wait_for(
        resolve_locators_at_ref(git, "/repo", acs, branch="task-42/work", base="main"),
        timeout=30,
    )


# ---- the two inputs from review ---------------------------------------------------


async def test_a_backtracking_vitest_file_costs_the_worker_and_nothing_else():
    hostile = "// it('" + "\\" * 44 + "\n"
    started = time.monotonic()
    res = await _resolve({_TS: hostile}, [_ac("a name", _TS)])
    assert time.monotonic() - started < 12
    assert res[0]["status"] == UNKNOWN, res


async def test_an_inheritance_graph_that_fans_out_is_answered_quickly():
    classes = "class C0:\n    pass\n\nclass C1(C0):\n    pass\n\n" + "".join(
        f"class C{i}(C{i - 1}, C{i - 2}):\n    pass\n\n" for i in range(2, 36)
    )
    started = time.monotonic()
    res = await _resolve({_PATH: classes}, [_ac("C35::test_x")])
    assert time.monotonic() - started < 12
    # Every class is local and clean and none defines test_x: provably absent,
    # and provable in linear time because each class is examined once.
    assert res[0]["status"] == MISSING, res


# ---- the worker is a real boundary --------------------------------------------------


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "worker.py"
    path.write_text(body)
    return path


async def test_a_worker_that_overruns_is_killed_and_the_request_is_unknown(
    monkeypatch, tmp_path: Path
):
    pid_file = tmp_path / "pid"
    script = _script(
        tmp_path,
        f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(300)\n",
    )
    monkeypatch.setattr(test_existence, "_WORKER_FILE", script)
    monkeypatch.setattr(test_existence, "_WORKER_WALL_SECONDS", 1)

    started = time.monotonic()
    res = await _resolve({_PATH: "def test_a():\n    pass\n"}, [_ac("test_a")])

    assert time.monotonic() - started < 10
    assert res[0]["status"] == UNKNOWN
    assert OVER_BUDGET in res[0]["reason"]
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError("the worker outlived its deadline")


async def test_a_crashing_or_garbling_worker_makes_every_locator_unknown(
    monkeypatch, tmp_path: Path
):
    for body in (
        "import sys\nsys.exit(3)\n",
        "print('not json at all')\n",
        "print('{\"results\": 5}')\n",
        "import sys\nsys.stdout.write('x' * 3_000_000)\n",
    ):
        monkeypatch.setattr(test_existence, "_WORKER_FILE", _script(tmp_path, body))
        res = await _resolve(
            {_PATH: "def test_a():\n    pass\n"},
            [_ac("test_a"), _ac("test_b", ac_id="AC-2")],
        )
        assert [r["status"] for r in res] == [UNKNOWN, UNKNOWN], (body, res)
        assert all(OVER_BUDGET in r["reason"] for r in res)


async def test_the_worker_gets_a_minimal_environment_and_an_empty_cwd(
    monkeypatch, tmp_path: Path
):
    out = tmp_path / "seen.txt"
    script = _script(
        tmp_path,
        "import json, os, sys\n"
        f"open({str(out)!r}, 'w').write(json.dumps([os.getcwd(), dict(os.environ)]))\n"
        "sys.stdin.read()\nprint(json.dumps({'results': {}}))\n",
    )
    monkeypatch.setattr(test_existence, "_WORKER_FILE", script)
    monkeypatch.setenv("SYNTH_HUB_SECRET", "synthetic")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/synthetic.sock")

    await _resolve({_PATH: "def test_a():\n    pass\n"}, [_ac("test_a")])

    import json

    cwd, env = json.loads(out.read_text())
    assert "SYNTH_HUB_SECRET" not in env and "SSH_AUTH_SOCK" not in env
    assert set(env) <= {"PATH", "LANG", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert os.path.realpath(cwd).startswith(os.path.realpath(os.sep))
    assert "task-1650" not in cwd and "repo" not in os.path.basename(cwd)


def test_an_operation_budget_ends_the_analysis_in_unknown(monkeypatch):
    from hub.services import locator_worker
    from hub.services.test_existence import resolve_ac_locators

    monkeypatch.setattr(locator_worker, "OPS_LIMIT", 50)
    source = "".join(f"def test_{i}():\n    pass\n" for i in range(200))
    res = resolve_ac_locators([_ac("test_199")], {_PATH: source})[0]
    assert res["status"] == UNKNOWN
    assert OVER_BUDGET in res["reason"]


# ---- the byte budget is spent BEFORE reading, and a file is parsed once -------------


async def test_the_byte_budget_is_checked_before_a_file_is_read():
    pad = "# pad\n" * 40000  # ~240 KB
    files = {f"tests/test_{i}.py": f"def test_a():\n    pass\n{pad}" for i in range(20)}
    git = FakeGit(files)
    acs = [_ac("test_a", f"tests/test_{i}.py", f"AC-{i}") for i in range(20)]

    res = await _resolve(files, acs, git)

    assert git.bytes_served <= test_existence.MAX_TOTAL_BYTES, git.bytes_served
    assert any(r["status"] == UNKNOWN and "limit" in r["reason"] for r in res)
    assert all(r["status"] != MISSING for r in res)


async def test_many_locators_on_one_big_file_parse_it_once():
    pad = "x = 1\n" * 40000  # ~240 KB of statements
    files = {_PATH: "def test_a():\n    pass\n" + pad}
    acs = [_ac("test_a", ac_id=f"AC-{i}") for i in range(20)]
    started = time.monotonic()
    res = await _resolve(files, acs)
    assert time.monotonic() - started < 8
    assert all(r["status"] == RESOLVABLE for r in res), res
