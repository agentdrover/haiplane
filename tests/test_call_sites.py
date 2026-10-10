"""The brief enumerates call sites, so nobody has to remember to (#601).

The diffs here are produced by real git against real files, not hand-written:
the parser's whole job is to read what git emits, and a fixture I wrote by
hand would only prove I can match my own format.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

from hub.services import call_sites


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "HOME": str(repo),
        },
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny project: a module with a helper, a caller, and a test."""
    root = tmp_path / "proj"
    (root / "hub").mkdir(parents=True)
    (root / "tests").mkdir()

    (root / "hub" / "core.py").write_text(
        "def guard(value):\n"
        "    return bool(value)\n"
        "\n"
        "\n"
        "def unrelated():\n"
        "    return 1\n"
    )
    (root / "hub" / "writer.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def write(value):\n"
        "    if guard(value):\n"
        "        return value\n"
        "    return None\n"
    )
    (root / "hub" / "bulk.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def write_many(values):\n"
        "    return [v for v in values if guard(v)]\n"
    )
    (root / "tests" / "test_core.py").write_text(
        "from hub.core import guard\n\n\ndef test_guard():\n    assert guard(1)\n"
    )

    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _diff(repo: Path) -> str:
    _git(repo, "add", "-A")
    return _git(repo, "diff", "-U0", "--cached", "HEAD").stdout


# ---- AC-1: the other call sites are listed, and marked touched or not ----


def test_the_brief_lists_untouched_call_sites(repo: Path):
    """The shape of every finding this task was opened about: the author
    changes a helper and updates one of its callers, and the other caller —
    in a module they were not reading — keeps the old behaviour."""
    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n"
        "    return bool(value) and value != 0\n"
        "\n"
        "\n"
        "def unrelated():\n"
        "    return 1\n"
    )
    (repo / "hub" / "writer.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def write(value):\n"
        "    if guard(value):\n"
        "        return str(value)\n"
        "    return None\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    assert report.analysed, report.reason
    guard_report = next(s for s in report.symbols if s.symbol == "guard")
    assert guard_report.state == call_sites.UNTOUCHED_SITES

    by_file = {s.file: s for s in guard_report.sites}
    assert by_file["hub/writer.py"].touched is True
    assert by_file["hub/bulk.py"].touched is False, (
        "the site the author never opened is the one that has to stand out"
    )
    assert by_file["hub/bulk.py"].caller == "write_many"
    assert "does not touch" in report.summary(), report.summary()


def test_two_call_sites_in_one_file_are_judged_separately(repo: Path):
    """#532 round 1, the case this tool exists for and would have missed.

    Arming ran on one branch of clone_repo and not on the other — both in
    git_ops.py. Judging "touched" per FILE marks both as covered and reports
    full coverage over the very defect being hunted. It is a property of the
    call site, not of the module it sits in.
    """
    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n    return bool(value) and value != 0\n"
    )
    # Two calls in ONE module: the author updates the first and never scrolls
    # down to the second.
    (repo / "hub" / "writer.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def write_one(value):\n"
        "    return str(value) if guard(value) else None\n"
        "\n"
        "\n"
        "def write_existing(value):\n"
        "    if guard(value):\n"
        "        return value\n"
        "    return None\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "both sites exist")

    # Now change the helper and only the first call site.
    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n    return value not in (None, 0, '')\n"
    )
    (repo / "hub" / "writer.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def write_one(value):\n"
        "    return repr(value) if guard(value) else None\n"
        "\n"
        "\n"
        "def write_existing(value):\n"
        "    if guard(value):\n"
        "        return value\n"
        "    return None\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    guard_report = next(s for s in report.symbols if s.symbol == "guard")
    untouched = [(s.file, s.caller) for s in guard_report.sites if not s.touched]
    assert ("hub/writer.py", "write_existing") in untouched, (
        "the second call site in the same file must stand on its own"
    )
    assert guard_report.state == call_sites.UNTOUCHED_SITES
    # Four sites in this fixture: bulk.write_many, writer.write_one,
    # writer.write_existing, tests.test_guard. Only write_one was edited.
    assert "3 of 4 call sites" in guard_report.statement(), (
        f"the total has to be visible: {guard_report.statement()!r}"
    )


def test_the_summary_names_what_it_could_not_read(repo: Path):
    """Cross-language analysis is out of scope, and silence about it is not:
    a diff that also changes a shell script must not read as fully analysed.
    """
    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n    return bool(value) or False\n"
    )
    (repo / "deploy.sh").write_text("#!/bin/sh\necho deploying\n")

    report = call_sites.analyse(str(repo), _diff(repo))

    assert report.other_languages == 1
    assert "not analysed at all" in report.summary(), report.summary()


def test_a_function_passed_as_a_callback_is_a_call_site(repo: Path):
    """Found by running the tool on its own branch: it reported its own
    analyse() as "called only from tests" while hub/app.py runs it through
    asyncio.to_thread — passed as a reference, not called, so the Call node
    belongs to to_thread and the walk missed it. A callback is a call site in
    the practical sense; a tool that lies about its own wiring is dismissed
    on day one."""
    (repo / "hub" / "sched.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def submit(fn, value):\n"
        "    return fn(value)\n"
        "\n"
        "\n"
        "def kickoff(values):\n"
        "    return [submit(guard, v) for v in values]\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "callback wiring exists")

    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n    return bool(value) and value != 0\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    guard_report = next(s for s in report.symbols if s.symbol == "guard")
    callers = {(s.file, s.caller) for s in guard_report.sites}
    assert ("hub/sched.py", "kickoff") in callers, (
        "a function handed over as an argument is still a place it is used"
    )
    assert guard_report.state != call_sites.ONLY_TESTS, (
        "product wiring through a callback must not read as tests-only"
    )


def test_a_decorated_function_names_its_decorators(repo: Path):
    """Also from dogfooding: api_review_brief (@app.get) and needs_attention
    (@property) both reported as "nothing calls it". Routes and properties are
    invoked by machinery an AST walk cannot see, and a flat false alarm is
    what gets a section scrolled past — the recorded risk of this task."""
    (repo / "hub" / "api.py").write_text(
        "def route(fn):\n"
        "    return fn\n"
        "\n"
        "\n"
        "@route\n"
        "def api_endpoint():\n"
        "    return 42\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    endpoint = next(s for s in report.symbols if s.symbol == "api_endpoint")
    assert endpoint.state == call_sites.NO_CALLERS
    assert endpoint.decorators == ["route"]
    assert "carries @route" in endpoint.statement(), endpoint.statement()
    assert "cannot see" in endpoint.statement(), (
        "the honest wording is the point: not 'dead code', but 'wired in a "
        "way this walk cannot see'"
    )


# ---- AC-2: written, and nothing calls it ----


def test_a_symbol_called_only_from_tests_is_named(repo: Path):
    """#534 in miniature: a guard with 194 correct lines whose only callers
    were its own tests. Folding that into "no callers" would lose the one
    signal that mattered — the tests prove it works, nothing runs it."""
    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n"
        "    return bool(value)\n"
        "\n"
        "\n"
        "def check_all_projects():\n"
        "    return ['checked']\n"
    )
    (repo / "tests" / "test_check.py").write_text(
        "from hub.core import check_all_projects\n"
        "\n"
        "\n"
        "def test_it():\n"
        "    assert check_all_projects()\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    checker = next(s for s in report.symbols if s.symbol == "check_all_projects")
    assert checker.state == call_sites.ONLY_TESTS, (
        "called only from tests is its own state, not a kind of no-callers"
    )
    assert [s.file for s in checker.sites] == ["tests/test_check.py"]


def test_a_symbol_with_no_callers_at_all_is_named(repo: Path):
    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n"
        "    return bool(value)\n"
        "\n"
        "\n"
        "def nothing_calls_me():\n"
        "    return 'dead on arrival'\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    orphan = next(s for s in report.symbols if s.symbol == "nothing_calls_me")
    assert orphan.state == call_sites.NO_CALLERS
    assert orphan.sites == []
    assert call_sites.DYNAMIC_CALLS_NOTE in report.note


# ---- AC-3: full coverage is said, not implied by silence ----


def test_full_coverage_is_stated_not_implied(repo: Path):
    """A section that says nothing when everything is fine cannot be told
    apart from a section that failed to run."""
    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n    return value is not None\n"
    )
    (repo / "hub" / "writer.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def write(value):\n"
        "    return value if guard(value) else None\n"
    )
    (repo / "hub" / "bulk.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def write_many(values):\n"
        "    return [v for v in values if guard(v) is True]\n"
    )
    (repo / "tests" / "test_core.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def test_guard():\n"
        "    assert guard(1) is True\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    guard_report = next(s for s in report.symbols if s.symbol == "guard")
    assert guard_report.state == call_sites.ALL_TOUCHED, [
        (s.file, s.touched) for s in guard_report.sites
    ]
    assert "touches every one of its" in guard_report.statement(), (
        f"full coverage must be stated in words: {guard_report.statement()!r}"
    )
    assert report.summary(), "the section never stays silent"


# ---- AC-4: what could not be read is named, not skipped ----


def test_unparsable_files_are_reported_not_skipped(repo: Path):
    (repo / "hub" / "broken.py").write_text("def oops(:\n    pass\n")
    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n    return bool(value) or False\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    assert report.analysed, "one unreadable file must not sink the whole section"
    assert "hub/broken.py" in report.unparsed, (
        "a file nobody could parse is not a file with no calls in it"
    )


# ---- the trap from #598: an empty walk is not an empty answer ----


def test_an_empty_call_index_is_not_analysed(tmp_path: Path, monkeypatch):
    """If the walk finds no calls anywhere, it failed. Reporting "no callers"
    for every symbol would turn a broken analysis into a clean bill of health
    — the same mistake as reading "could not check" as "no drift" (#534)."""
    monkeypatch.setattr(
        call_sites, "build_call_index", lambda root, subdirs=("hub", "tests"): ({}, [])
    )

    report = call_sites.analyse(
        str(tmp_path), "+++ b/hub/core.py\n@@ -1,2 +1,2 @@\n+x\n"
    )

    assert not report.analysed
    assert report.status == call_sites.UNKNOWN
    assert "not analysed" in report.summary()
    assert report.reason, "an unknown without a reason is a shrug, not an answer"


def test_a_diff_with_no_hunks_is_not_analysed(repo: Path):
    report = call_sites.analyse(str(repo), "")

    assert not report.analysed
    assert "not analysed" in report.summary()


# ---- the section reaches the reviewer, with real content ----


async def test_the_brief_shows_the_untouched_site(repo: Path, client, monkeypatch):
    """End to end through the endpoint a reviewer calls: a real repository, a
    real diff, and the untouched call site named in the response."""
    from unittest.mock import AsyncMock

    from hub import app as hub_app
    from hub.integrations.registry import plugins

    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n    return bool(value) and value != 0\n"
    )
    (repo / "hub" / "writer.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def write(value):\n"
        "    return str(value) if guard(value) else None\n"
    )
    diff = _diff(repo)

    created = await client.post("/api/tasks", json={"title": "Section"})
    task_id = created.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: wire it"},
    )
    started = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert started.status_code == 200, started.text

    monkeypatch.setattr(
        hub_app.services,
        "project_git_context",
        AsyncMock(return_value={"repo": str(repo), "base_branch": "develop"}),
    )
    monkeypatch.setattr(
        plugins.git_ops, "branch_diff", AsyncMock(return_value=diff), raising=False
    )

    section = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()[
        "call_sites"
    ]

    assert section["status"] == "analysed", section
    guard_entry = next(e for e in section["entries"] if e["symbol"] == "guard")
    assert any("hub/bulk.py" in u for u in guard_entry["untouched"]), (
        "the call site nobody opened must be named in the brief itself"
    )
    assert guard_entry["statement"], "each entry states its finding in words"
    assert section["note"], "the blind spots are named in the section itself"


# ---- the analyser on its own class of defect ----


def test_it_would_have_caught_the_defect_it_was_written_for(repo: Path):
    """Straight from #596: a rule applied on four write paths and missing on a
    fifth in another module. The enumeration has to put that fifth path in
    front of the reviewer without anyone thinking to look for it."""
    (repo / "hub" / "core.py").write_text("def guard(value):\n    return bool(value)\n")
    (repo / "hub" / "writer.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def write(value):\n"
        "    if not guard(value):\n"
        "        raise ValueError('refused')\n"
        "    return value\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))
    guard_report = next(s for s in report.symbols if s.symbol == "guard")
    untouched = [s.file for s in guard_report.sites if not s.touched]

    assert "hub/bulk.py" in untouched, (
        "the fifth path has to appear by itself; that is the whole point"
    )


# ---- #1254: only_tests is removable, and removing it charges nothing ----


async def test_a_registry_call_is_removable_and_charges_nothing(
    repo: Path, client, monkeypatch
):
    """AC-4 (#1254): the analyser's known false positive can be cleared.

    A tool registered by NAME (the MCP case: hub_submit_machine_review came
    out only_tests on 11.09.2026) is invisible to a static walk. The reviewer
    clears it by naming the call path; the clearing is accepted, and the
    task's readiness, dor_passed and ability to be approved are read from the
    task itself before and after — none of them moves.
    """
    from unittest.mock import AsyncMock

    from hub import app as hub_app
    from hub.integrations.registry import plugins

    (repo / "hub" / "registry.py").write_text(
        "import importlib\n"
        "\n"
        "\n"
        "def call_tool(name, *args):\n"
        "    return getattr(importlib.import_module('hub.tools'), name)(*args)\n"
    )
    (repo / "hub" / "tools.py").write_text(
        "def hub_submit_probe(value):\n    return value\n"
    )
    (repo / "tests" / "test_tools.py").write_text(
        "from hub.tools import hub_submit_probe\n"
        "\n"
        "\n"
        "def test_probe():\n"
        "    assert hub_submit_probe(1) == 1\n"
    )
    diff = _diff(repo)
    report = call_sites.analyse(str(repo), diff)
    probe = next(s for s in report.symbols if s.symbol == "hub_submit_probe")
    assert probe.state == call_sites.ONLY_TESTS, "the false positive must be real"

    task_id = (
        await client.post("/api/tasks", json={"title": "Probe", "source": "agent"})
    ).json()["id"]
    for url, payload in (
        (f"/api/tasks/{task_id}/approve", {"force": True}),
        (f"/api/tasks/{task_id}/claim", {"agent": "dev"}),
        (
            f"/api/tasks/{task_id}/updates",
            {"agent": "dev", "kind": "status", "content": "Plan: register it"},
        ),
        (f"/api/tasks/{task_id}/pair-start", {"assigned_agent": "dev"}),
        (f"/api/tasks/{task_id}/submit-review", {"agent": "dev"}),
    ):
        resp = await client.post(url, json=payload)
        assert resp.status_code == 200, f"{url}: {resp.text}"

    monkeypatch.setattr(
        hub_app.services,
        "project_git_context",
        AsyncMock(return_value={"repo": str(repo), "base_branch": "develop"}),
    )
    monkeypatch.setattr(
        plugins.git_ops, "branch_diff", AsyncMock(return_value=diff), raising=False
    )

    async def _charge() -> tuple:
        # Readiness first: reading it stores the score on the task row.
        readiness = (await client.get(f"/api/tasks/{task_id}/readiness")).json()
        task = (await client.get(f"/api/tasks/{task_id}")).json()
        return (
            task["readiness_score"],
            task["dor_passed"],
            task["status"],
            readiness.get("score"),
            readiness.get("dor_passed"),
        )

    before = await _charge()
    named = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()[
        "call_sites"
    ]
    assert named["only_tests_state"] == "no_report", named
    assert [o["outcome"] for o in named["only_tests"]] == ["pending"]

    path = "hub/registry.py:call_tool -> getattr(hub.tools, name)"
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "agent_count": 1,
            "tokens_spent": 1000,
            "model": "grok-4.6",
            "raw_count": 1,
            "findings_confirmed": [],
            "findings_rejected": [
                {"title": "hub_submit_probe", "category": "only_tests", "reason": path}
            ],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "cursor-cloud-reviewer",
        },
    )
    assert resp.status_code == 200, resp.text

    cleared = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()[
        "call_sites"
    ]
    assert cleared["only_tests"] == [
        {"symbol": "hub_submit_probe", "outcome": "cleared", "call_path": path}
    ], "the clearing is accepted with its named path"
    assert await _charge() == before, "the clearing must charge nothing"

    verdict = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert verdict.status_code == 200, verdict.text


# ---- #1652: the raw index is built once per clean commit ----


@pytest.fixture(autouse=True)
def _isolated_index_cache():
    call_sites.clear_index_cache()
    yield
    call_sites.clear_index_cache()


@pytest.fixture
def committed(repo: Path) -> tuple[Path, str, str]:
    """HEAD clean; two diffs against the base, as real git emits them."""
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n"
        "    return bool(value) and value != 0\n"
        "\n"
        "\n"
        "def unrelated():\n"
        "    return 1\n"
    )
    _git(repo, "commit", "-qam", "core")
    mid = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "hub" / "writer.py").write_text(
        "from hub.core import guard\n"
        "\n"
        "\n"
        "def write(value):\n"
        "    if guard(value):\n"
        "        return str(value)\n"
        "    return None\n"
    )
    _git(repo, "commit", "-qam", "writer")
    core_diff = _git(repo, "diff", "-U0", base, mid).stdout
    writer_diff = _git(repo, "diff", "-U0", mid, "HEAD").stdout
    assert core_diff and writer_diff
    return repo, core_diff, writer_diff


def _uncached(monkeypatch, root: Path, diff: str):
    """The report the way it was computed before the cache existed."""
    with monkeypatch.context() as m:
        m.setattr(call_sites, "cached_call_index", call_sites.build_call_index)
        return call_sites.analyse(str(root), diff)


class _CountingBuilder:
    def __init__(self, monkeypatch):
        self.calls = 0
        self._real = call_sites.build_call_index
        monkeypatch.setattr(call_sites, "build_call_index", self)

    def __call__(self, root, subdirs=call_sites.INDEX_SUBDIRS):
        self.calls += 1
        return self._real(root, subdirs)


def test_call_index_is_reused_for_the_same_clean_head(committed, monkeypatch):
    root, core_diff, writer_diff = committed
    expected = [_uncached(monkeypatch, root, d) for d in (core_diff, writer_diff)]
    builder = _CountingBuilder(monkeypatch)

    got = [call_sites.analyse(str(root), d) for d in (core_diff, writer_diff)]

    assert builder.calls == 1, "two diffs on one clean HEAD share one raw index"
    assert got == expected

    # What an analyse hands out is a copy: mutating it leaves the cache alone.
    index, unparsed = call_sites.cached_call_index(str(root))
    assert builder.calls == 1
    index["guard"][0].line = 99999
    index["injected"] = []
    unparsed.append("injected.py")
    again_index, again_unparsed = call_sites.cached_call_index(str(root))
    assert "injected" not in again_index
    assert again_index["guard"][0].line != 99999
    assert "injected.py" not in again_unparsed
    assert builder.calls == 1


def _new_commit(root: Path):
    (root / "hub" / "extra.py").write_text("def extra():\n    return guard(2)\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "extra")


def _edit_tracked(root: Path):
    (root / "hub" / "bulk.py").write_text("def write_many(values):\n    return []\n")


def _add_untracked(root: Path):
    (root / "hub" / "fresh.py").write_text("def fresh():\n    return guard(3)\n")


def _add_ignored(root: Path):
    with open(root / ".git" / "info" / "exclude", "a") as handle:
        handle.write("hub/hidden.py\n")
    (root / "hub" / "hidden.py").write_text("def hidden():\n    return guard(4)\n")


@pytest.mark.parametrize(
    "change, caches_new_entry",
    [
        pytest.param(_new_commit, True, id="new-commit"),
        pytest.param(_edit_tracked, False, id="modified-tracked"),
        pytest.param(_add_untracked, False, id="untracked"),
        pytest.param(_add_ignored, False, id="ignored"),
    ],
)
def test_call_index_cache_never_serves_a_stale_tree(
    committed, monkeypatch, change, caches_new_entry
):
    root, core_diff, _ = committed
    call_sites.analyse(str(root), core_diff)  # warms the cache
    assert len(call_sites._index_cache) == 1

    change(root)

    got = call_sites.analyse(str(root), core_diff)
    assert got == _uncached(monkeypatch, root, core_diff)
    assert len(call_sites._index_cache) == (2 if caches_new_entry else 1)


def test_call_index_cache_never_serves_a_stale_tree_head_moves_mid_build(
    committed, monkeypatch
):
    root, core_diff, _ = committed
    real = call_sites.build_call_index
    moved = []

    def build_then_move(path, subdirs=call_sites.INDEX_SUBDIRS):
        built = real(path, subdirs)
        if not moved:
            moved.append(True)
            _git(root, "commit", "-q", "--allow-empty", "-m", "moved under the build")
        return built

    monkeypatch.setattr(call_sites, "build_call_index", build_then_move)
    got = call_sites.analyse(str(root), core_diff)

    assert moved, "the builder ran, so the HEAD really moved under it"
    assert got == _uncached(monkeypatch, root, core_diff)
    assert len(call_sites._index_cache) == 0, (
        "a build that straddled a commit is not kept"
    )


def test_call_index_cache_never_serves_a_stale_tree_outside_a_repository(
    committed, tmp_path, monkeypatch
):
    root, core_diff, _ = committed
    plain = tmp_path / "plain"
    plain.mkdir()
    for sub in ("hub", "tests"):
        (plain / sub).mkdir()
        for src in (root / sub).glob("*.py"):
            (plain / sub / src.name).write_text(src.read_text())

    got = call_sites.analyse(str(plain), core_diff)

    assert got == _uncached(monkeypatch, plain, core_diff)
    assert len(call_sites._index_cache) == 0


class _SignalEvent(threading.Event):
    """An Event that says when somebody has started waiting on it."""

    waiting = threading.Event()

    def wait(self, timeout=None):
        type(self).waiting.set()
        return super().wait(timeout)


def _run_thread(target, results: list, errors: list):
    def body():
        try:
            results.append(target())
        except BaseException as exc:  # noqa: BLE001 - the test reads it back
            errors.append(exc)

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def flight_probe(monkeypatch):
    class Flight(call_sites._Flight):
        def __init__(self):
            self.done = _SignalEvent()

    _SignalEvent.waiting = threading.Event()
    monkeypatch.setattr(call_sites, "_Flight", Flight)
    return _SignalEvent


def test_call_index_cache_single_flight_and_lru(committed, monkeypatch, flight_probe):
    root, core_diff, _ = committed
    real = call_sites.build_call_index
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def gated(path, subdirs=call_sites.INDEX_SUBDIRS):
        calls.append(1)
        started.set()
        assert release.wait(10)
        return real(path, subdirs)

    monkeypatch.setattr(call_sites, "build_call_index", gated)

    # Two threads, one key: one build.
    results: list = []
    errors: list = []
    first = _run_thread(
        lambda: call_sites.analyse(str(root), core_diff), results, errors
    )
    assert started.wait(10)
    second = _run_thread(
        lambda: call_sites.analyse(str(root), core_diff), results, errors
    )
    assert flight_probe.waiting.wait(10), "the second call waits for the first build"
    release.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert not errors and len(results) == 2
    assert len(calls) == 1
    assert results[0] == results[1]
    assert call_sites._index_inflight == {}

    # The owner fails: the waiter does not hang, builds for itself, and the
    # in-flight mark is gone for whoever comes next.
    call_sites.clear_index_cache()
    calls.clear()
    started.clear()
    release.clear()
    flight_probe.waiting.clear()
    boom = RuntimeError("owner failed")

    def failing_then_real(path, subdirs=call_sites.INDEX_SUBDIRS):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(10)
            raise boom
        return real(path, subdirs)

    monkeypatch.setattr(call_sites, "build_call_index", failing_then_real)
    results, errors = [], []
    owner = _run_thread(
        lambda: call_sites.analyse(str(root), core_diff), results, errors
    )
    assert started.wait(10)
    waiter_results: list = []
    waiter_errors: list = []
    waiter = _run_thread(
        lambda: call_sites.analyse(str(root), core_diff), waiter_results, waiter_errors
    )
    assert flight_probe.waiting.wait(10)
    release.set()
    owner.join(10)
    waiter.join(10)
    assert not owner.is_alive() and not waiter.is_alive(), "nobody hangs"
    assert errors == [boom]
    assert not waiter_errors and len(waiter_results) == 1
    assert len(calls) == 2, "the waiter rebuilt after the owner failed"
    assert call_sites._index_inflight == {}

    # And a failure with nobody waiting leaves nothing behind either.
    call_sites.clear_index_cache()
    calls.clear()

    def fails_once(path, subdirs=call_sites.INDEX_SUBDIRS):
        calls.append(1)
        if len(calls) == 1:
            raise boom
        return real(path, subdirs)

    monkeypatch.setattr(call_sites, "build_call_index", fails_once)
    with pytest.raises(RuntimeError):
        call_sites.analyse(str(root), core_diff)
    assert call_sites._index_inflight == {} and len(call_sites._index_cache) == 0
    call_sites.analyse(str(root), core_diff)
    assert len(calls) == 2, "the next request builds again"


def test_call_index_cache_keeps_the_eight_most_recent_keys(committed, monkeypatch):
    root, _, _ = committed
    shas = [_git(root, "rev-parse", "HEAD").stdout.strip()]
    for i in range(8):
        _git(root, "commit", "-q", "--allow-empty", "-m", f"empty {i}")
        shas.append(_git(root, "rev-parse", "HEAD").stdout.strip())
    builder = _CountingBuilder(monkeypatch)

    def at(sha: str):
        _git(root, "checkout", "-q", sha)
        call_sites.cached_call_index(str(root))

    def key(sha: str):
        return (os.path.abspath(str(root)), sha, call_sites.INDEX_SUBDIRS)

    for sha in shas[:8]:
        at(sha)
    assert builder.calls == 8 and len(call_sites._index_cache) == 8

    at(shas[0])  # a hit: the first key becomes the most recent
    assert builder.calls == 8
    at(shas[8])  # the ninth key evicts the least recent one
    assert builder.calls == 9
    assert key(shas[1]) not in call_sites._index_cache, "the second key is evicted"
    assert key(shas[0]) in call_sites._index_cache, "the first key stayed"
    assert len(call_sites._index_cache) == 8
