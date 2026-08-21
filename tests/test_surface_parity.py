"""Surface parity check (#833): the missed surface is visible before review.

The classification is exercised on lists of paths, never on a live git tree:
what has to hold is the map's behaviour, and a test that first builds a
repository to observe it would be measuring git instead.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "surface_parity.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from surface_parity import (  # noqa: E402
    DEFAULT_BASE,
    Family,
    changed_paths,
    classify,
    format_report,
)

HUB_CONTRACT = ("hub/app.py", "hub/cli.py", "hub/mcp_server.py", "hub/models.py")


def _gap(report, family: str):
    return next((gap for gap in report.gaps if gap.family == family), None)


def test_missing_companion_surfaces_are_named():
    """AC-1: REST touched, CLI and MCP silent — both are named, with the family."""
    report = classify(["hub/app.py"])

    gap = _gap(report, "hub contract")
    assert gap is not None
    assert set(gap.missing) == {"hub/cli.py", "hub/mcp_server.py", "hub/models.py"}
    assert gap.touched == ("hub/app.py",)

    text = format_report(report, base=DEFAULT_BASE)
    assert "hub/cli.py" in text
    assert "hub/mcp_server.py" in text
    assert "hub contract" in text


def test_complete_family_reports_nothing():
    """AC-2: the whole family moved together — the check has nothing to say."""
    report = classify(list(HUB_CONTRACT))

    assert _gap(report, "hub contract") is None
    assert report.unmapped == ()


def test_unmapped_files_are_counted_not_dropped():
    """AC-3: a file in no family is listed and counted, never silently ignored."""
    paths = [*HUB_CONTRACT, "hub/web.py", "docs/agent-context/system-map.md"]
    report = classify(paths)

    assert report.unmapped == ("hub/web.py", "docs/agent-context/system-map.md")
    assert report.as_dict()["unmapped_count"] == 2

    text = format_report(report, base=DEFAULT_BASE)
    assert "Files in no family (2)" in text
    assert "hub/web.py" in text


def test_warning_never_fails_the_build():
    """AC-4: run as a process on a diff with a hole — it warns and exits 0."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--paths-from", "-"],
        input="hub/app.py\n",
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert "hub/mcp_server.py" in result.stdout


def test_base_branch_is_a_parameter():
    """AC-5: a branch off main is diffed against main, not against develop."""
    calls: list[list[str]] = []

    def fake_git(args: list[str]) -> str:
        calls.append(args)
        if args[0] == "merge-base":
            return "abc123\n"
        if args[0] == "diff":
            return "hub/app.py\n"
        return ""

    paths = changed_paths("origin/main", run=fake_git)

    assert ["merge-base", "origin/main", "HEAD"] in calls
    assert ["diff", "--name-only", "abc123"] in calls
    assert paths == ["hub/app.py"]
    assert not any(DEFAULT_BASE in arg for call in calls for arg in call)


def test_uncommitted_and_untracked_files_are_part_of_the_diff():
    """The check runs before submit, when the last edits are often unstaged."""

    def fake_git(args: list[str]) -> str:
        if args[0] == "merge-base":
            return "abc123\n"
        if args[0] == "diff":
            return "hub/app.py\n"
        return "hub/cli.py\n"

    assert changed_paths(run=fake_git) == ["hub/app.py", "hub/cli.py"]


def test_namesake_in_another_directory_is_not_the_surface():
    """docs/hub/app.py is not hub/app.py — a right-anchored match would say it is."""
    report = classify(["docs/hub/app.py"])

    assert report.gaps == ()
    assert report.unmapped == ("docs/hub/app.py",)


def test_family_map_is_traceable_to_evidence():
    """A family nobody can trace back to a finding is a guess, and guesses get ignored."""
    from surface_parity import FAMILIES

    assert FAMILIES
    for family in FAMILIES:
        assert family.why.strip(), family.name
        assert len(family.surfaces) >= 2, family.name


def test_only_entered_families_are_reported():
    """An untouched family stays quiet — the check reports holes, not a checklist."""
    families = (
        Family(name="touched", why="evidence", surfaces=("a.py", "b.py")),
        Family(name="untouched", why="evidence", surfaces=("x.py", "y.py")),
    )
    report = classify(["a.py"], families)

    assert [gap.family for gap in report.gaps] == ["touched"]
