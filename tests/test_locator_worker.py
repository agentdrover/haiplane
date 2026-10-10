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


async def _resolve(
    files: dict, acs: list, git: FakeGit | None = None, fresh: bool = True
) -> list[dict]:
    if fresh:  # one fake sha names different files from test to test
        test_existence.clear_locator_cache()
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


# ---- round 4: the cost of a request is bounded -----------------------------------------


async def test_deep_paths_cost_a_bounded_number_of_git_calls():
    deep = "/".join(f"d{i}" for i in range(12))
    files = {f"{deep}/t{i}.py": "def test_a():\n    pass\n" for i in range(50)}
    git = FakeGit(files)
    acs = [_ac("test_a", f"{deep}/t{i}.py", f"AC-{i}") for i in range(50)]

    res = await _resolve(files, acs, git)

    assert all(r["status"] == RESOLVABLE for r in res), res[:2]
    # one tree listing, two calls (size, content) per existing file, a few refs
    assert git.calls <= 2 * 50 + 10, git.calls


async def test_the_number_of_file_reads_per_request_is_capped():
    files = {f"tests/t{i}.py": "def test_a():\n    pass\n" for i in range(100)}
    git = FakeGit(files)
    acs = [_ac("test_a", f"tests/t{i}.py", f"AC-{i}") for i in range(100)]

    res = await _resolve(files, acs, git)

    assert git.calls <= 2 * test_existence.MAX_READS + 10, git.calls
    assert any(r["status"] == UNKNOWN and "file reads" in r["reason"] for r in res)
    assert all(r["status"] != MISSING for r in res)


def _logging_worker(tmp_path: Path, log: Path, seconds: float) -> Path:
    return _script(
        tmp_path,
        "import json, sys, time\n"
        f"log = {str(log)!r}\n"
        "open(log, 'a').write('start %f\\n' % time.time())\n"
        "sys.stdin.read()\n"
        f"time.sleep({seconds})\n"
        "open(log, 'a').write('end %f\\n' % time.time())\n"
        "print(json.dumps({'results': {'0': ['resolvable', 'ok']}}))\n",
    )


async def test_only_a_few_workers_run_at_once(monkeypatch, tmp_path: Path):
    log = tmp_path / "events"
    monkeypatch.setattr(
        test_existence, "_WORKER_FILE", _logging_worker(tmp_path, log, 0.6)
    )

    async def one(i: int):
        files = {_PATH: f"def test_{i}():\n    pass\n"}
        return await _resolve(files, [_ac(f"test_{i}")])

    await asyncio.gather(*(one(i) for i in range(6)))

    events = sorted(
        (float(line.split()[1]), 1 if line.startswith("start") else -1)
        for line in log.read_text().splitlines()
    )
    running = peak = 0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    assert len(events) == 12
    assert peak <= test_existence.MAX_WORKERS, peak


async def test_a_repeated_request_is_answered_from_the_cache(monkeypatch):
    runs = []
    real = test_existence.analyse_in_worker

    async def counting(request):
        runs.append(1)
        return await real(request)

    monkeypatch.setattr(test_existence, "analyse_in_worker", counting)
    files = {_PATH: "def test_a():\n    pass\n"}
    git = FakeGit(files)
    first = await _resolve(files, [_ac("test_a")], git)
    calls_after_first = git.calls
    second = await _resolve(files, [_ac("test_a")], git, fresh=False)

    assert first == second
    assert len(runs) == 1
    assert git.calls - calls_after_first <= 2  # only the ref is resolved again


async def test_identical_concurrent_requests_share_one_computation(monkeypatch):
    runs = []
    real = test_existence.analyse_in_worker

    async def counting(request):
        runs.append(1)
        await asyncio.sleep(0.3)
        return await real(request)

    monkeypatch.setattr(test_existence, "analyse_in_worker", counting)
    files = {_PATH: "def test_a():\n    pass\n"}

    results = await asyncio.gather(
        *(
            _resolve(files, [_ac("test_a")], FakeGit(files), fresh=False)
            for _ in range(5)
        )
    )

    assert len(runs) == 1
    assert all(r == results[0] for r in results)


async def test_a_failed_worker_is_not_cached(monkeypatch, tmp_path: Path):
    files = {_PATH: "def test_a():\n    pass\n"}
    git = FakeGit(files)
    good = test_existence._WORKER_FILE
    monkeypatch.setattr(
        test_existence, "_WORKER_FILE", _script(tmp_path, "import sys\nsys.exit(3)\n")
    )
    failed = await _resolve(files, [_ac("test_a")], git)
    assert failed[0]["status"] == UNKNOWN
    monkeypatch.setattr(test_existence, "_WORKER_FILE", good)

    again = await _resolve(files, [_ac("test_a")], git, fresh=False)

    assert again[0]["status"] == RESOLVABLE, again


def test_a_shared_conftest_is_parsed_once_per_request(monkeypatch):
    from hub.services import locator_worker

    calls = []
    real = locator_worker._conftest_issue

    def counting(path, text, budget):
        calls.append(path)
        return real(path, text, budget)

    monkeypatch.setattr(locator_worker, "_conftest_issue", counting)
    conftest = "x = 1\n" * 38000  # ~230 KB
    request = {
        "files": {f"tests/t{i}.py": "def test_a():\n    pass\n" for i in range(20)},
        "aux": {
            (f"{d}/{n}" if d else n): {"state": "missing", "text": ""}
            for d in ("tests", "")
            for n in (*locator_worker.CONFIG_NAMES, "conftest.py")
        },
        "locators": [
            {"key": str(i), "nodeid": f"tests/t{i}.py::test_a", "runner": "pytest"}
            for i in range(20)
        ],
    }
    request["aux"]["tests/conftest.py"] = {"state": "present", "text": conftest}
    out = locator_worker.analyse(request)["results"]
    assert all(v[0] == RESOLVABLE for v in out.values()), out
    assert calls == ["tests/conftest.py"]


async def test_twenty_files_under_one_big_conftest_are_all_resolvable():
    conftest = "x = 1\n" * 38000
    files = {f"tests/t{i}.py": "def test_a():\n    pass\n" for i in range(20)}
    files["tests/conftest.py"] = conftest
    acs = [_ac("test_a", f"tests/t{i}.py", f"AC-{i}") for i in range(20)]
    res = await _resolve(files, acs)
    assert all(r["status"] == RESOLVABLE for r in res), res[:2]
