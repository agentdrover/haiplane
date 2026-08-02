"""The brief enumerates call sites, so nobody has to remember to (#601).

The diffs here are produced by real git against real files, not hand-written:
the parser's whole job is to read what git emits, and a fixture I wrote by
hand would only prove I can match my own format.
"""

from __future__ import annotations

import subprocess
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
