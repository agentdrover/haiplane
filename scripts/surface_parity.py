#!/usr/bin/env python3
"""Warn about contract surfaces that usually change together (#833).

``consistency`` is the densest family of confirmed review findings in this
repository: a change touches a contract in several places — REST, CLI, MCP, a
template, the model — the author edits some of them, and the reviewer finds the
rest one cycle later. In #810 the miss surfaced after approval and cost a
re-submission. That gap is visible in the diff itself, before review, which is
the entire idea here: lay the changed files over a map of surfaces that travel
together and name the ones this diff left behind.

    uv run python scripts/surface_parity.py                    # before submit
    uv run python scripts/surface_parity.py --base origin/main # release branch
    uv run python scripts/surface_parity.py --json             # machine-readable

Warning only, by design: the exit code is 0 even when something is named. The
family map is a heuristic, and a check that fails a build on a guess is ignored
from its first false positive onward. First we measure how often it is right
(#833 outcome metric); tightening or blocking comes after that, if at all.

Files that fall into no family are listed and counted rather than dropped —
same rule as ``findings_unaccounted`` (#519) and ``no_completion_tasks``
(#810): silence about what was not counted reads as "everything is covered".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFAULT_BASE = "origin/develop"


@dataclass(frozen=True)
class Family:
    """Surfaces that publish one contract and are expected to move together.

    ``surfaces`` are repo-relative glob patterns. ``why`` names the evidence
    the family rests on — a family nobody can trace back to a real finding is
    a guess, and guesses are what make the output ignorable.
    """

    name: str
    why: str
    surfaces: tuple[str, ...]


# Every family below is grounded in a confirmed finding or in the High-Risk
# Couplings section of docs/agent-context/change-map.md. Deliberately absent:
# "MCP docstring ↔ docs/agent-context/mcp-catalog-budget.json". The coupling is
# real but inverted — under #829 ordinary delivery fits below the ceiling and
# must NOT touch the budget file, so asking for it on every MCP change would
# manufacture a false positive each time. That pair already has its own CI
# check (scripts/mcp_catalog_budget.py), which measures instead of guessing.
FAMILIES: tuple[Family, ...] = (
    Family(
        name="hub contract",
        why=(
            "one contract is published three times over — REST, CLI, MCP — "
            "plus the model behind them (#819; change-map: enum and status "
            "changes affect API, CLI, MCP and persistence together)"
        ),
        surfaces=(
            "hub/app.py",
            "hub/cli.py",
            "hub/mcp_server.py",
            "hub/models.py",
        ),
    ),
    Family(
        name="practice metrics",
        why=(
            "the number and the page explaining it are computed in one file "
            "and read in another (#810: the median moved to completed_at "
            "while /metrics kept explaining the old estimate)"
        ),
        surfaces=(
            "hub/services/orchestration.py",
            "hub/templates/metrics.html",
        ),
    ),
    Family(
        name="schema and storage",
        why=(
            "change-map: a task row field means the migration in hub/db.py "
            "and the serialization paths that read it"
        ),
        surfaces=(
            "hub/db.py",
            "hub/repository.py",
            "hub/models.py",
        ),
    ),
)


@dataclass(frozen=True)
class SurfaceGap:
    """One family this diff entered but did not cover."""

    family: str
    why: str
    touched: tuple[str, ...]
    missing: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "family": self.family,
            "why": self.why,
            "touched": list(self.touched),
            "missing": list(self.missing),
        }


@dataclass(frozen=True)
class ParityReport:
    """What the diff covered, what it missed, and what was never mapped."""

    changed: tuple[str, ...]
    gaps: tuple[SurfaceGap, ...] = ()
    unmapped: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "changed_count": len(self.changed),
            "gaps": [gap.as_dict() for gap in self.gaps],
            "unmapped": list(self.unmapped),
            "unmapped_count": len(self.unmapped),
        }


def _matches(path: str, pattern: str) -> bool:
    """Glob match anchored at the repo root, with ``*`` never crossing ``/``.

    Both sides are made absolute so that ``docs/hub/app.py`` cannot satisfy the
    ``hub/app.py`` surface — PurePosixPath.match otherwise matches from the
    right and would treat a namesake in another directory as the real thing.
    """
    return PurePosixPath("/" + path).match("/" + pattern)


def classify(
    paths: Iterable[str], families: Sequence[Family] = FAMILIES
) -> ParityReport:
    """Lay changed paths over the family map. Pure: no git, no filesystem."""
    changed = tuple(dict.fromkeys(paths))
    gaps: list[SurfaceGap] = []
    mapped: set[str] = set()

    for family in families:
        touched: list[str] = []
        missing: list[str] = []
        for surface in family.surfaces:
            hits = [path for path in changed if _matches(path, surface)]
            if hits:
                touched.append(surface)
                mapped.update(hits)
            else:
                missing.append(surface)
        if touched and missing:
            gaps.append(
                SurfaceGap(
                    family=family.name,
                    why=family.why,
                    touched=tuple(touched),
                    missing=tuple(missing),
                )
            )

    unmapped = tuple(path for path in changed if path not in mapped)
    return ParityReport(changed=changed, gaps=tuple(gaps), unmapped=unmapped)


def _run_git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def changed_paths(
    base: str = DEFAULT_BASE,
    *,
    run: Callable[[list[str]], str] = _run_git,
) -> list[str]:
    """Files this branch changed against ``base``, working tree included.

    Uncommitted and untracked files count on purpose: the check is meant to be
    run just before submitting, when the last edits are often still unstaged,
    and a surface added as a brand-new file is exactly the case worth catching.
    """
    merge_base = run(["merge-base", base, "HEAD"]).strip()
    paths = [
        *run(["diff", "--name-only", merge_base]).splitlines(),
        *run(["ls-files", "--others", "--exclude-standard"]).splitlines(),
    ]
    return list(dict.fromkeys(path.strip() for path in paths if path.strip()))


def format_report(report: ParityReport, *, base: str) -> str:
    """Human-readable result for a terminal or a CI log."""
    lines = [
        f"Surface parity (#833): {len(report.changed)} changed files "
        f"against {base}. Warning only — this never fails the build."
    ]

    if report.gaps:
        lines.append("")
        lines.append("Surfaces usually changed together but missing here:")
        for gap in report.gaps:
            lines.append(f"  {gap.family} — {gap.why}")
            lines.append(f"    changed: {', '.join(gap.touched)}")
            lines.append(f"    missing: {', '.join(gap.missing)}")
            lines.append(
                "    → either change them too, or say in the submission why "
                "this one does not need them."
            )
    else:
        lines.append("No family was entered and left half-covered.")

    lines.append("")
    lines.append(f"Files in no family ({len(report.unmapped)}):")
    if report.unmapped:
        lines.extend(f"  {path}" for path in report.unmapped)
    else:
        lines.append("  none")

    return "\n".join(lines)


def _paths_from(source: str) -> list[str]:
    if source == "-":
        text = sys.stdin.read()
    else:
        text = Path(source).read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=f"branch the diff is measured against (default: {DEFAULT_BASE})",
    )
    parser.add_argument(
        "--paths-from",
        metavar="FILE",
        help="read changed paths from FILE ('-' for stdin) instead of git",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.paths_from:
        paths = _paths_from(args.paths_from)
    else:
        try:
            paths = changed_paths(args.base)
        except (subprocess.CalledProcessError, OSError) as exc:
            # No merge base (shallow clone, unknown branch) means no diff to
            # judge — say so and stay out of the way. Anything else here would
            # turn an advisory into an obstacle.
            print(
                f"Surface parity (#833): cannot diff against {args.base} "
                f"({exc}); nothing checked.",
                file=sys.stderr,
            )
            return 0

    report = classify(paths)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(format_report(report, base=args.base))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
