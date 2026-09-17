#!/usr/bin/env python3
"""Mutate only the Python functions this branch changed, and name the survivors (#1270).

Spike #1263: 37 of 234 confirmed review findings in 30 days (15.8%) said one
thing — "this test does not fail when the behaviour is broken" — and in 33 of
them the reviewer named the surviving mutation himself. That is a
deterministic observation the reviewer was paid model prices to make. This
script makes it in CI, before review:

    uv run python scripts/mutation_changed.py --base origin/develop
    uv run python scripts/mutation_changed.py --base origin/develop --json-out m.json

Mutator: cosmic-ray (PyPI, MIT). It is used as a library for two things only —
generating mutation positions for a file and applying one mutation while a test
command runs. Choosing WHICH mutations run is this wrapper's job, because no
mutator narrows a run to "the functions a diff touched":

* a function is changed when a line added or modified against the merge-base
  with ``--base`` falls inside it (innermost ``def`` wins, decorators count);
* a mutation belongs to the innermost function containing its first line, and
  runs only when that function is changed — the rest of the file is untouched;
* each mutant is judged by the tests that reference its file (by dotted module
  name, else by file stem), never by the whole suite.

The baseline is observed, not assumed (#1172): the selected tests run once
unmutated before the series, and a non-zero rc stops everything. A mutant
"killed" by a red suite is killed by nothing, so a red baseline is reported as
"the series did not run" — never as "no survivors".

Warning only, by design: the exit code is 0 whatever is found. Equivalent
mutants exist and a gate built on them would be ignored from the first false
positive. The outcome is revisited on data (task #1270 outcome metric).

cosmic-ray edits the file on disk while a mutant's tests run and restores it
afterwards. The script re-checks every touched file against its snapshot at the
end and restores it if needed — but a SIGKILL mid-run can still leave a mutant
on disk, so run it locally only on a clean tree.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import signal
import subprocess  # nosec B404 - runs git and the repository's own tests
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE = "origin/develop"
DEFAULT_BUDGET_SECONDS = 600
MIN_MUTANT_TIMEOUT = 30.0
MAX_TEST_FILES = 8

# Binary-operator swaps produce eleven mutants per operator, most of which
# (shifts, bitwise, power) no reviewer would ever call a missed case — they
# multiply the run time and the equivalent-mutant noise without adding a
# finding of the T1/T2/T4 kind. Arithmetic swaps stay.
EXCLUDED_OPERATORS = re.compile(
    r"^core/ReplaceBinaryOperator_\w+_(RShift|LShift|BitOr|BitAnd|BitXor|Pow|FloorDiv|Mod)$"
)

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

STATE_RAN = "ran"
STATE_BASELINE_RED = "baseline_red"
STATE_NO_CHANGES = "no_changed_functions"
STATE_NO_TESTS = "no_tests"
STATE_ERROR = "error"


@dataclass(frozen=True)
class Function:
    """A function definition in one file: qualified name and line span."""

    path: str
    name: str
    first: int
    last: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.name}"


@dataclass
class Report:
    """What ran and what it found. ``survivors`` is None when nothing ran."""

    state: str
    base: str
    reason: str = ""
    functions: list[str] = field(default_factory=list)
    untested: list[str] = field(default_factory=list)
    mutants: int = 0
    killed: int = 0
    survived: int = 0
    not_run: int = 0
    survivors: list[dict] | None = None
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "base": self.base,
            "reason": self.reason,
            "functions": self.functions,
            "untested": self.untested,
            "mutants": self.mutants,
            "killed": self.killed,
            "survived": self.survived,
            "not_run": self.not_run,
            "survivors": self.survivors,
            "seconds": round(self.seconds, 1),
        }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def is_mutable_source(path: str) -> bool:
    """Python source that is not itself a test."""
    p = Path(path)
    if p.suffix != ".py" or "tests" in p.parts:
        return False
    return not (p.name.startswith("test_") or p.name == "conftest.py")


def touched_lines(diff_text: str) -> dict[str, set[int]]:
    """``git diff -U0`` → new-side line numbers touched, per file.

    A pure deletion (``+N,0``) touches line N — the line after which the text
    disappeared — so a function that only lost lines still counts as changed.
    """
    result: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = target[2:] if target.startswith("b/") else None
            continue
        match = _HUNK.match(line)
        if match is None or current is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        lines = result.setdefault(current, set())
        lines.update(range(start, start + count) if count else {max(start, 1)})
    return result


def functions_in(path: str, source: str) -> list[Function]:
    """Every function and method in a module, with dotted qualified names."""
    found: list[Function] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                name = f"{prefix}{child.name}"
                found.append(Function(path, name, first, child.end_lineno or first))
                visit(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(ast.parse(source), "")
    return found


def innermost(functions: Iterable[Function], line: int) -> Function | None:
    """The narrowest function whose span contains ``line``."""
    containing = [f for f in functions if f.first <= line <= f.last]
    return min(containing, key=lambda f: f.last - f.first, default=None)


def changed_functions(functions: list[Function], lines: set[int]) -> list[Function]:
    """Functions owning at least one touched line (innermost owner only)."""
    owners = {innermost(functions, line) for line in lines}
    return sorted((f for f in owners if f is not None), key=lambda f: f.first)


def _module_references(path: str, texts: dict[Path, str]) -> list[Path]:
    """Test files that reference a module by dotted name, else by file stem."""
    module = ".".join(Path(path).with_suffix("").parts)
    dotted = re.compile(rf"\b{re.escape(module)}\b")
    by_module = [f for f, text in texts.items() if dotted.search(text)]
    if by_module:
        return by_module
    by_stem = re.compile(rf"\b{re.escape(Path(path).stem)}\b")
    return [f for f, text in texts.items() if by_stem.search(text)]


def tests_for(
    function: Function, texts: dict[Path, str], limit: int = MAX_TEST_FILES
) -> tuple[list[Path], str]:
    """The tests that judge one function's mutants, or why there are none.

    Narrowest first: files referencing the module AND naming the function. A
    function nobody names falls back to the files referencing its module — but
    only while there are at most ``limit`` of them. A core module referenced by
    half the suite would turn every mutant into a full run, so that case is
    reported as not mutated, with the reason, instead of eating the budget.
    """
    module_files = _module_references(function.path, texts)
    if not module_files:
        return [], "ни один тест не ссылается на файл"
    short = function.name.rsplit(".", 1)[-1]
    named = re.compile(rf"\b{re.escape(short)}\b")
    by_name = [f for f in module_files if named.search(texts[f])]
    chosen = by_name or module_files
    if len(chosen) > limit:
        how = "называют функцию" if by_name else "ссылаются на файл"
        return [], f"{len(chosen)} тестовых файлов {how} — шире предела {limit}"
    return chosen, ""


def _test_command(test_files: Iterable[Path], repo: Path) -> str:
    rel = [shlex.quote(str(f.relative_to(repo))) for f in test_files]
    return " ".join(
        [
            shlex.quote(sys.executable),
            "-m",
            "pytest",
            "-x",
            "-q",
            "-p",
            "no:cacheprovider",
            *rel,
        ]
    )


def run_baseline(command: str, repo: Path, timeout: float) -> tuple[int, float, str]:
    """Run the selected tests unmutated. Returns (rc, seconds, output tail).

    A baseline that outlives ``timeout`` is reported as rc -1: it did not end
    green, so nothing may be concluded from mutants.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(  # nosec B603 - the repository's own test command
            shlex.split(command),
            cwd=repo,
            capture_output=True,
            text=True,
            env=_no_bytecode_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, time.monotonic() - started, f"таймаут {timeout:.0f} с"
    return (
        proc.returncode,
        time.monotonic() - started,
        (proc.stdout + proc.stderr)[-2000:],
    )


def _no_bytecode_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _mutations_for(path: str, db_path: Path) -> list:
    """Every cosmic-ray mutation of one file, minus the excluded operators."""
    from cosmic_ray.commands import init
    from cosmic_ray.work_db import WorkDB, use_db

    with use_db(str(db_path), WorkDB.Mode.create) as work_db:
        init([Path(path)], work_db, {})
        return [
            mutation
            for item in work_db.pending_work_items
            for mutation in item.mutations
            if not EXCLUDED_OPERATORS.match(mutation.operator_name)
        ]


@dataclass
class _Target:
    function: Function
    mutations: list
    test_files: list[Path]


def _collect_targets(
    repo: Path, base: str, tests_dir: Path, report: Report, scratch: Path
) -> list[_Target]:
    merge_base = _git(repo, "merge-base", base, "HEAD").strip()
    test_root = repo / tests_dir
    texts = {
        f: f.read_text(encoding="utf-8", errors="replace")
        for f in (sorted(test_root.rglob("test_*.py")) if test_root.is_dir() else [])
    }
    diff = _git(repo, "diff", "-U0", "--no-color", merge_base, "HEAD", "--", "*.py")
    targets: list[_Target] = []
    for index, (path, lines) in enumerate(sorted(touched_lines(diff).items())):
        source_file = repo / path
        if not is_mutable_source(path) or not source_file.is_file():
            continue
        functions = functions_in(path, source_file.read_text(encoding="utf-8"))
        changed = changed_functions(functions, lines)
        if not changed:
            continue
        report.functions.extend(f.key for f in changed)
        mutations: list | None = None
        for function in changed:
            test_files, why_not = tests_for(function, texts)
            if not test_files:
                report.untested.append(f"{function.key}: {why_not}")
                continue
            if mutations is None:
                mutations = _mutations_for(path, scratch / f"session-{index}.sqlite")
            mine = [
                m for m in mutations if innermost(functions, m.start_pos[0]) == function
            ]
            targets.append(_Target(function, mine, test_files))
    return targets


def _run_series(
    repo: Path, targets: list[_Target], timeout: float, budget: float, report: Report
) -> None:
    from cosmic_ray.mutating import mutate_and_test
    from cosmic_ray.work_item import TestOutcome, WorkerOutcome

    deadline = time.monotonic() + budget
    report.survivors = []
    for target in targets:
        command = _test_command(target.test_files, repo)
        for mutation in target.mutations:
            report.mutants += 1
            if time.monotonic() > deadline:
                report.not_run += 1
                continue
            result = mutate_and_test([mutation], command, timeout)
            if result.worker_outcome != WorkerOutcome.NORMAL:
                report.not_run += 1
            elif result.test_outcome == TestOutcome.SURVIVED:
                report.survived += 1
                report.survivors.append(
                    {
                        "file": target.function.path,
                        "function": target.function.name,
                        "line": mutation.start_pos[0],
                        "operator": mutation.operator_name,
                        "diff": (result.diff or "")[-600:],
                    }
                )
            else:
                report.killed += 1


def analyse(
    repo: Path,
    base: str,
    *,
    tests_dir: Path = Path("tests"),
    budget: float = DEFAULT_BUDGET_SECONDS,
) -> Report:
    """Select, baseline, mutate. Never raises for a problem in the work itself."""
    started = time.monotonic()
    report = Report(state=STATE_ERROR, base=base)
    repo = repo.resolve()
    previous_cwd = Path.cwd()
    os.chdir(repo)  # cosmic-ray resolves module paths against the cwd
    try:
        with tempfile.TemporaryDirectory() as scratch:
            targets = _collect_targets(repo, base, tests_dir, report, Path(scratch))
            snapshots = {
                t.function.path: (repo / t.function.path).read_bytes() for t in targets
            }
            try:
                _analyse_targets(repo, targets, report, budget)
            finally:
                for path, content in snapshots.items():
                    if (repo / path).read_bytes() != content:
                        (repo / path).write_bytes(content)
                        report.reason += f" (файл {path} восстановлен после серии)"
    except subprocess.CalledProcessError as exc:
        report.state = STATE_ERROR
        report.reason = f"git {' '.join(exc.cmd[1:])} завершился с rc={exc.returncode}: {exc.stderr.strip()}"
    finally:
        os.chdir(previous_cwd)
        report.seconds = time.monotonic() - started
    return report


def _analyse_targets(
    repo: Path, targets: list[_Target], report: Report, budget: float
) -> None:
    if not report.functions:
        report.state = STATE_NO_CHANGES
        report.reason = (
            "изменённых функций Python относительно базы нет — мутировать нечего"
        )
        return
    if not targets:
        report.state = STATE_NO_TESTS
        report.reason = "ни у одной изменённой функции нет тестов, которыми её судить"
        return
    test_files = sorted({f for t in targets for f in t.test_files})
    command = _test_command(test_files, repo)
    rc, seconds, tail = run_baseline(command, repo, budget)
    if rc != 0:
        report.state = STATE_BASELINE_RED
        report.reason = (
            f"базовый прогон тестов красный (rc={rc}) — серия мутаций не выполнялась, "
            f"выжившие неизвестны. Хвост прогона:\n{tail}"
        )
        return
    timeout = max(MIN_MUTANT_TIMEOUT, 3 * seconds)
    report.state = STATE_RAN
    _run_series(repo, targets, timeout, max(0.0, budget - seconds), report)


def format_report(report: Report) -> str:
    """Human-readable result for a CI log. Warning only."""
    lines = [
        f"Мутации изменённых функций (#1270) против {report.base}: "
        "только предупреждение, сборку не валит."
    ]
    if report.functions:
        lines.append(
            f"Изменённые функции ({len(report.functions)}): {', '.join(report.functions)}"
        )
    if report.untested:
        lines.append(f"Не мутировались ({len(report.untested)}):")
        lines.extend(f"  {entry}" for entry in report.untested)
    if report.state != STATE_RAN:
        lines.append(f"Состояние: {report.state} — {report.reason}")
        return "\n".join(lines)
    lines.append(
        f"Мутантов {report.mutants}: убито {report.killed}, выжило {report.survived}, "
        f"не выполнено {report.not_run}; {report.seconds:.0f} с."
    )
    if report.not_run:
        lines.append(
            f"Не выполнено {report.not_run}: исчерпан бюджет времени или мутацию нельзя "
            "было применить — про них ничего не известно."
        )
    for survivor in report.survivors or []:
        lines.append(
            f"ВЫЖИЛ {survivor['file']}:{survivor['line']} {survivor['function']} — "
            f"{survivor['operator']}"
        )
    if not report.survivors:
        if report.not_run:
            lines.append("Среди выполненных мутантов выживших нет.")
        else:
            lines.append("Выживших нет.")
    if report.reason:
        lines.append(report.reason.strip())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"default: {DEFAULT_BASE}")
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    parser.add_argument("--tests-dir", default="tests", help="relative to --repo")
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=DEFAULT_BUDGET_SECONDS,
        help="stop starting new mutants after this long; the rest are counted as not run",
    )
    parser.add_argument(
        "--json-out", metavar="FILE", help="also write the report as JSON"
    )
    args = parser.parse_args(argv)
    # A CI step timeout sends SIGTERM. Turning it into SystemExit lets the
    # snapshot restore in analyse() run, so no mutant is left on disk.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    report = analyse(
        Path(args.repo),
        args.base,
        tests_dir=Path(args.tests_dir),
        budget=args.budget_seconds,
    )
    print(format_report(report))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
