"""Where else is this function called, and does the diff touch those places (#601).

Five returns in one session had the same shape: the mechanism was written
correctly and the place where it should switch on was not wired. #519 — the
API normalised, the MCP tool echoed its input. #596 — four write paths guarded,
a fifth in another module wrote straight to the repository. #534 — a drift
guard with no caller at all. #532 twice — arming that only ran on a path
nobody takes, then arming whose caller had no caller.

None of them was a reasoning error, and CI was green for all five: a path that
never executes breaks no test. The skill asks the author to enumerate call
sites; an instruction addressed to memory fails the same way the hook nobody
remembered to activate did. So this computes the enumeration and puts it in
front of the reviewer.

WHAT IT DOES NOT SEE, said out loud because a half-blind check read as
complete is worse than none: calls made through the plugin registry, getattr,
or any other name resolved at run time are invisible to an AST walk. So is a
call in a language this does not parse. Matching is by symbol name, so two
methods sharing a name across classes land in one bucket — noise this first,
advisory version accepts and names rather than hides.

AN EMPTY RESULT IS NOT AN ANSWER. If the walk found no calls anywhere, the
walk failed — a rename of an import alias would otherwise turn the section
green while proving nothing (#598). That case reports "not analysed", the same
distinction between clean and unknown the drift guard makes (#534).
"""

from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger("hub.call_sites")

ANALYSED = "analysed"
UNKNOWN = "unknown"

# What the enumeration says about one changed symbol.
ALL_TOUCHED = "all_touched"
UNTOUCHED_SITES = "untouched_sites"
ONLY_TESTS = "only_tests"
NO_CALLERS = "no_callers"

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

DYNAMIC_CALLS_NOTE = (
    "Calls resolved at run time — through the plugin registry, getattr, or a "
    "string name — are invisible here. A symbol reported as having no callers "
    "may still be reached that way."
)


@dataclass
class CallSite:
    file: str
    line: int
    caller: str
    touched: bool
    # Span of the function the call sits in. "Touched" is judged over this
    # span, not over the call's own line and not over the whole file — see
    # _is_touched.
    caller_start: int = 0
    caller_end: int = 0


@dataclass
class SymbolReport:
    symbol: str
    defined_in: str
    state: str
    sites: list[CallSite] = field(default_factory=list)
    # Decorator names on the definition. A route or a property has no direct
    # caller by design — the framework invokes it — and a flat "nothing calls
    # it" there is the false alarm that gets a section scrolled past.
    decorators: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return self.state in (UNTOUCHED_SITES, NO_CALLERS, ONLY_TESTS)

    def statement(self) -> str:
        """Say what was found in words, including when nothing is wrong.

        A symbol that is fully covered has to SAY so. Leaving it out would
        make "checked and fine" look exactly like "never examined" — the
        distinction the whole section exists to draw (AC-3).
        """
        if self.state == ALL_TOUCHED:
            return (
                f"{self.symbol}: this diff touches every one of its "
                f"{len(self.sites)} call sites"
            )
        if self.state == NO_CALLERS:
            if self.decorators:
                decs = ", ".join(f"@{d}" for d in self.decorators)
                return (
                    f"{self.symbol}: no direct caller found, but it carries "
                    f"{decs} — decorators register or invoke functions in "
                    "ways this walk cannot see"
                )
            return f"{self.symbol}: nothing calls it — no caller found anywhere"
        if self.state == ONLY_TESTS:
            return (
                f"{self.symbol}: called only from tests — the tests exercise it, "
                "nothing in the product does"
            )
        untouched = [s for s in self.sites if not s.touched]
        where = ", ".join(f"{s.file}:{s.line} ({s.caller})" for s in untouched[:5])
        return (
            f"{self.symbol}: {len(untouched)} of {len(self.sites)} call sites "
            f"this diff leaves alone — {where}"
        )


@dataclass
class CallSiteReport:
    status: str
    reason: str = ""
    symbols: list[SymbolReport] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    note: str = DYNAMIC_CALLS_NOTE
    # Files in the diff this cannot read at all — a shell script, a template.
    # Listing each would be noise (cross-language analysis is out of scope),
    # but staying silent lets a partial analysis read as a complete one.
    other_languages: int = 0

    @property
    def analysed(self) -> bool:
        return self.status == ANALYSED

    def summary(self) -> str:
        """One line for a reader, and it never stays silent about coverage.

        Silence is indistinguishable from an analysis that did not run, which
        is the failure this whole section exists to remove (AC-3).
        """
        if not self.analysed:
            return f"call sites: not analysed — {self.reason}"
        if not self.symbols:
            return "call sites: the diff changes no Python function in hub/"
        flagged = [s for s in self.symbols if s.needs_attention]
        if not flagged:
            head = (
                f"call sites: every call site of all {len(self.symbols)} changed "
                "symbols is touched by this diff"
            )
        else:
            head = (
                f"call sites: {len(flagged)} of {len(self.symbols)} changed symbols "
                "have call sites this diff does not touch"
            )
        if self.other_languages:
            head += (
                f"; {self.other_languages} non-Python files in this diff were "
                "not analysed at all"
            )
        return head


def changed_line_ranges(diff: str) -> dict[str, set[int]]:
    """New-side line numbers touched per file, from ``git diff -U0``."""
    out: dict[str, set[int]] = {}
    current: str | None = None
    for line in (diff or "").splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :].strip()
            out.setdefault(current, set())
            continue
        if line.startswith("+++ /dev/null"):
            current = None
            continue
        m = _HUNK.match(line)
        if m and current:
            start = int(m.group(1))
            count = int(m.group(2) or 1)
            out[current].update(range(start, start + max(count, 1)))
    return {f: lines for f, lines in out.items() if lines}


def _enclosing(tree: ast.AST) -> list[tuple[str, int, int]]:
    """(name, first line, last line) for every function in a module."""
    spans: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            spans.append((node.name, node.lineno, node.end_lineno or node.lineno))
    return spans


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Human-readable decorator names: ``app.get``, ``property``."""
    names: list[str] = []
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call):
            dec = dec.func
        try:
            names.append(ast.unparse(dec))
        except Exception:  # noqa: BLE001 - display only, never fatal
            continue
    return names


def changed_symbols(
    root: str,
    ranges: dict[str, set[int]],
    *,
    test_dirs: tuple[str, ...] = ("tests",),
) -> list[tuple[str, str, list[str]]]:
    """Functions whose body the diff touches: (symbol, file, decorators).

    Tests are excluded as SUBJECTS: a test function has no callers by design,
    so listing every changed test would fill the section with entries nobody
    can act on — and a section people scroll past is the risk this task
    recorded. Tests still count as call SITES.
    """
    found: list[tuple[str, str, list[str]]] = []
    for rel, lines in sorted(ranges.items()):
        if not rel.endswith(".py") or rel.startswith(test_dirs):
            continue
        try:
            source = open(os.path.join(root, rel), encoding="utf-8").read()
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            end = node.end_lineno or node.lineno
            if any(node.lineno <= n <= end for n in lines):
                found.append((node.name, rel, _decorator_names(node)))
    # Deduplicate while keeping order: a name defined twice in one file is one
    # entry, or the section would repeat itself.
    seen: set[tuple[str, str]] = set()
    unique = []
    for item in found:
        key = (item[0], item[1])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _walk_python(root: str, subdir: str) -> list[str]:
    base = os.path.join(root, subdir)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames if d not in {"__pycache__", ".venv", "node_modules"}
        ]
        for name in filenames:
            if name.endswith(".py"):
                out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(out)


def build_call_index(
    root: str, subdirs: tuple[str, ...] = ("hub", "tests")
) -> tuple[dict[str, list[CallSite]], list[str]]:
    """Every call by name, and the files that could not be parsed.

    Returns names only — matching is nominal. A method and a function sharing a
    name share a bucket; that is noise the report names rather than hides.
    """
    index: dict[str, list[CallSite]] = {}
    unparsed: list[str] = []

    for subdir in subdirs:
        for rel in _walk_python(root, subdir):
            try:
                source = open(os.path.join(root, rel), encoding="utf-8").read()
                tree = ast.parse(source)
            except (OSError, SyntaxError, ValueError) as exc:
                # Never silent: a file nobody could read is not a file with no
                # calls in it (AC-4).
                log.debug("call index: could not parse %s: %s", rel, exc)
                unparsed.append(rel)
                continue

            spans = _enclosing(tree)
            span_of = {name: (start, end) for name, start, end in spans}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                else:
                    continue
                caller = "<module>"
                for fname, start, end in spans:
                    if start <= node.lineno <= end:
                        caller = fname
                start, end = span_of.get(caller, (node.lineno, node.lineno))
                index.setdefault(name, []).append(
                    CallSite(
                        file=rel,
                        line=node.lineno,
                        caller=caller,
                        touched=False,
                        caller_start=start,
                        caller_end=end,
                    )
                )
                # A function handed to another as an argument is a call site
                # in the practical sense — asyncio.to_thread(f, ...),
                # create_task, functools.partial. Dogfooding caught this: the
                # section reported its own analyse() as "called only from
                # tests" while hub/app.py runs it through to_thread. A tool
                # that lies about its own wiring would be dismissed on day one.
                for ref in [*node.args, *(kw.value for kw in node.keywords)]:
                    if isinstance(ref, ast.Name):
                        ref_name = ref.id
                    elif isinstance(ref, ast.Attribute):
                        ref_name = ref.attr
                    else:
                        continue
                    index.setdefault(ref_name, []).append(
                        CallSite(
                            file=rel,
                            line=ref.lineno,
                            caller=caller,
                            touched=False,
                            caller_start=start,
                            caller_end=end,
                        )
                    )
    return index, unparsed


def _is_touched(site: CallSite, changed: set[int]) -> bool:
    """Did this diff put the author in front of THIS call?

    The unit is the enclosing function, and neither of the obvious extremes.
    Per FILE is too loose: two calls of the same helper commonly sit in one
    module, which is exactly #532 round 1 — arming ran on one branch of
    clone_repo and not on the other, both in git_ops.py — and per-file would
    have called that full coverage, missing one of the five cases this tool
    was built from.

    Per LINE is too strict: editing the line after a call leaves the call
    itself unchanged, and the author plainly read it. That would fill the
    section with false alarms, and a section people scroll past is the risk
    this task recorded.
    """
    if not changed:
        return False
    if site.caller_start and site.caller_end:
        return any(site.caller_start <= n <= site.caller_end for n in changed)
    return site.line in changed


def analyse(
    root: str,
    diff: str,
    *,
    test_dirs: tuple[str, ...] = ("tests",),
) -> CallSiteReport:
    """Enumerate the call sites of everything this diff changes."""
    ranges = changed_line_ranges(diff)
    if not ranges:
        return CallSiteReport(UNKNOWN, "the diff named no changed lines")

    index, unparsed = build_call_index(root)
    if not index:
        # The walk itself failed. Reporting "no callers" for every symbol here
        # would be a green section that proves nothing (#598).
        return CallSiteReport(
            UNKNOWN,
            "the call index came out empty — nothing was analysed",
            unparsed=unparsed,
        )

    symbols: list[SymbolReport] = []

    for symbol, defined_in, decorators in changed_symbols(
        root, ranges, test_dirs=test_dirs
    ):
        sites = [
            CallSite(
                file=s.file,
                line=s.line,
                caller=s.caller,
                touched=_is_touched(s, ranges.get(s.file, set())),
                caller_start=s.caller_start,
                caller_end=s.caller_end,
            )
            for s in index.get(symbol, [])
            # A recursive call inside the function itself is not a call site
            # anyone needs to review.
            if not (s.file == defined_in and s.caller == symbol)
        ]

        if not sites:
            state = NO_CALLERS
        elif all(s.file.startswith(test_dirs) for s in sites):
            # Its own state, not a variety of "no callers": #534 shipped a
            # guard whose only callers were its tests, and that is exactly the
            # signal worth showing.
            state = ONLY_TESTS
        elif all(s.touched for s in sites):
            state = ALL_TOUCHED
        else:
            state = UNTOUCHED_SITES

        symbols.append(
            SymbolReport(
                symbol=symbol,
                defined_in=defined_in,
                state=state,
                sites=sites,
                decorators=decorators,
            )
        )

    return CallSiteReport(
        ANALYSED,
        symbols=symbols,
        unparsed=unparsed,
        other_languages=sum(1 for f in ranges if not f.endswith(".py")),
    )


__all__ = [
    "ALL_TOUCHED",
    "ANALYSED",
    "DYNAMIC_CALLS_NOTE",
    "NO_CALLERS",
    "ONLY_TESTS",
    "UNKNOWN",
    "UNTOUCHED_SITES",
    "CallSite",
    "CallSiteReport",
    "SymbolReport",
    "analyse",
    "build_call_index",
    "changed_line_ranges",
    "changed_symbols",
]
