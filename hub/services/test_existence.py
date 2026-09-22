"""Static existence check for AC test locators (#506).

Complements #505: given a verifiable_by=test AC with a valid pytest locator,
check whether that test actually exists via ``pytest --collect-only`` — no test
body runs, no side effects. The result (resolvable / missing / unknown) makes a
broken AC↔test binding a visible defect in the review brief instead of a silent
hole. Best-effort: when the workspace or pytest is unavailable the status is
``unknown``, never a false ``missing``.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from typing import Any

from hub.process_kill import kill_process_group
from hub.services.test_locator import PYTEST, parse_test_locator, runner_of

log = logging.getLogger("hub")

RESOLVABLE = "resolvable"
MISSING = "missing"
UNKNOWN = "unknown"
UNPARSEABLE = "unparseable"

# The whole status vocabulary of this calculation, named once so a reader of
# the answer can check it decomposed ALL of it and not just the statuses it
# happened to think of. Anyone adding a status here has to look at who
# consumes it (#1158): a consumer that folds unnamed statuses into its own
# default silently turns a new "could not look" into an accusation.
LOCATOR_STATUSES: tuple[str, ...] = (RESOLVABLE, MISSING, UNKNOWN, UNPARSEABLE)

# How a locator was resolved. The two are not equally strong and the brief says
# which one answered: collection proves pytest can actually run the test;
# reading the file proves only that a function by that name is written there.
BY_COLLECTION = "test found by collection"
BY_SOURCE = "test found in the file, read without running it"
# Why an answer could not be given. Never merged into BY_SOURCE's silence:
# "this runner is not one I can look into" is a fact about the hub, and a
# reader who cannot tell it from "the test is not there" is being accused
# on the hub's behalf (#1203).
NO_RESOLVER = "no way to look inside a {runner} test file"
# ``missing`` answers two different questions and the reason is the only thing
# that tells them apart, so both wordings are named rather than spelled inline.
# NO_VALID_LOCATOR means nothing resolvable was NAMED — the hub never got as
# far as looking. NOT_COLLECTED means a locator was named, the hub looked, and
# the test is not there. Reading the first as the second accuses an author of a
# missing test they never claimed to have written (#1158).
NO_VALID_LOCATOR = "no valid test locator in test_ref"
NOT_COLLECTED = "locator does not match any collected test"

_COLLECT_TIMEOUT = 90


async def collect_test_nodeids(repo_path: str | None) -> set[str] | None:
    """Collect existing pytest nodeids in ``repo_path`` (best-effort, #506).

    Runs ``pytest --collect-only -q`` — collection only, no test body executes.
    Returns the set of nodeids, or ``None`` when collection could not run (no
    workspace, pytest missing, timeout, non-zero exit) so callers report
    ``unknown`` rather than a false ``missing``.
    """
    if not repo_path:
        return None
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own session, so the timeout path can signal the whole group (#544).
            start_new_session=True,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_COLLECT_TIMEOUT)
    except (OSError, TimeoutError, asyncio.TimeoutError):
        # wait_for only cancels the await — the child keeps running and would
        # accumulate orphaned collectors on every brief read (#506). Kill the
        # group, not the pid: we spawn `uv`, which runs pytest as a child, so
        # killing `uv` alone leaves the collector running (#544).
        await kill_process_group(proc)
        log.warning("AC locator collect-only failed in %s", repo_path)
        return None
    if proc.returncode != 0:
        # Collection errors (e.g. import failure) mean we cannot trust the set.
        return None
    nodeids: set[str] = set()
    for raw in out.decode(errors="replace").splitlines():
        line = raw.strip()
        if "::" in line and line.split("::", 1)[0].endswith(".py"):
            nodeids.add(line)
    return nodeids or None


def _verifiable_by(ac: Any) -> str:
    vb = getattr(ac, "verifiable_by", None)
    # Обещали str, а при отсутствующем поле отдавали None: сравнение с "test"
    # молча давало False, а .lower() у вызывающего дал бы AttributeError.
    return str(getattr(vb, "value", vb) or "")


def _base_nodeids(collected: set[str]) -> set[str]:
    """Parametrized nodeids reduced to their bare function form (#506).

    pytest only emits the parametrized ids (``…::test_x[case]``) and never the
    bare ``…::test_x``, but a bare locator is the documented common form and
    runs every case. Without this, a perfectly valid AC binding is reported as
    ``missing`` — the false-missing the module promises never to produce.
    """
    return {c.split("[", 1)[0] for c in collected if "[" in c}


def _wanted_name(nodeid: str) -> str:
    """The test's own name, read the way its runner writes names (#1203).

    For pytest that is the last ``::`` segment without its ``[param]`` suffix.
    For a runner whose test names are free text it is everything after the
    FIRST ``::``, verbatim: a name may legitimately contain ``::`` or square
    brackets, and pytest's trimming would quietly hunt for a different test.
    """
    if runner_of(nodeid) == PYTEST:
        return nodeid.split("::")[-1].split("[", 1)[0]
    return nodeid.split("::", 1)[1] if "::" in nodeid else nodeid


def _defines_test(tree: ast.AST, name: str) -> int | None:
    """Line where ``name`` is defined as a function anywhere in ``tree``.

    Walks the whole tree rather than the module's top level, which is what
    makes ``Class::method`` nodeids resolve without the resolver having to know
    the class shape. It answers existence and nothing more — a name found here
    says a function is written, not that pytest would collect it.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
        ):
            return node.lineno
    return None


def locator_files(acs: Any) -> list[str]:
    """The distinct files the test-AC locators name, for a caller to fetch."""
    files: list[str] = []
    for ac in acs:
        if _verifiable_by(ac) != "test":
            continue
        parsed = parse_test_locator(getattr(ac, "test_ref", None))
        if parsed and parsed[0] not in files:
            files.append(parsed[0])
    return files


def resolve_locator_absent_file(nodeid: str) -> tuple[str, str]:
    """The submission contains no such file — a fact, not a failure to look."""
    rel = nodeid.split("::", 1)[0]
    return MISSING, f"the submission contains no file {rel}"


# A vitest test declaration: it("name"), test('name'), it.only(`name`) and
# the table forms, where an argument list sits between the token and the name:
# it.each([1, 2])("name"). That optional group allows one level of nesting, so
# a table of objects or a call inside it does not hide the name behind it.
#
# The quote style is captured and back-referenced, so the OTHER two quote
# characters are ordinary text inside a name — which they are, in real suites.
_VITEST_DECL = re.compile(
    r"""\b(?:it|test)(?:\.\w+)*\s*"""
    r"""(?:\((?:[^()]|\([^()]*\))*\)\s*)?"""
    r"""\(\s*(?P<q>['"`])(?P<name>(?:\\.|(?!(?P=q)).)*)(?P=q)""",
    re.DOTALL,
)


def _unescape(raw: str) -> str:
    return re.sub(r"\\(.)", r"\1", raw)


def _declares_vitest_test(text: str, name: str) -> int | None:
    """Line where a vitest test called ``name`` is declared, or ``None``.

    The counterpart of :func:`_defines_test`, and deliberately no stronger: it
    answers existence by reading the file, exactly what ``BY_SOURCE`` claims.
    Nesting inside describe blocks needs no handling — the locator names the
    test's own name, which is the string this call takes.
    """
    for m in _VITEST_DECL.finditer(text):
        if _unescape(m.group("name")) == name:
            return text.count("\n", 0, m.start()) + 1
    return None


def _resolve_python_source(text: str, rel: str, name: str) -> tuple[str, str]:
    try:
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError) as exc:
        # Distinct from missing on purpose: "I could not read this" and "the
        # test is not there" are different facts, and a blanket unknown for
        # both is what taught reviewers to skip this block.
        return UNPARSEABLE, f"could not parse {rel}: {type(exc).__name__}"
    line = _defines_test(tree, name)
    if line is None:
        return MISSING, f"{rel} defines no test named {name}"
    return RESOLVABLE, f"{BY_SOURCE}: {rel}:{line}"


# Where a test declaration BEGINS, without any claim to read its name. The
# gap between this count and the names actually extracted is the reader's own
# blind spot, and it is the only thing that separates "the test is not here"
# from "I could not read how this file declares its tests" (#1203).
_VITEST_DECL_START = re.compile(r"\b(?:it|test)(?:\.\w+)*\s*[(`]")


def _resolve_vitest_source(text: str, rel: str, name: str) -> tuple[str, str]:
    """Existence by reading, and a refusal that never poses as an absence.

    ``ast`` makes "not found" trustworthy for Python: a real parser saw the
    whole file. Here the reader is a regular expression, and a form it cannot
    follow — a tagged template, a table built by a call, a name held in a
    variable — looks exactly like a test that was never written. Reporting
    that as ``missing`` is the false accusation this whole change exists to
    remove, so the reader counts what it could not follow and says so.
    """
    line = _declares_vitest_test(text, name)
    if line is not None:
        return RESOLVABLE, f"{BY_SOURCE}: {rel}:{line}"
    started = len(_VITEST_DECL_START.findall(text))
    named = sum(1 for _ in _VITEST_DECL.finditer(text))
    if started > named:
        # Erring towards unknown on a false start inside a string or comment
        # is the safe direction: it withholds an answer instead of inventing
        # one against the author.
        return UNKNOWN, (
            f"{rel} declares {started - named} test(s) in a form this reader "
            f"cannot follow, so the absence of {name} is not established"
        )
    return MISSING, f"{rel} defines no test named {name}"


# One resolver per runner the locator registry accepts. The pairing is not
# decoration: a shape accepted upstream with no entry here is exactly the
# state where the hub reports a real test as absent, so the two registries are
# checked against each other by test (#1203).
SOURCE_RESOLVERS = {
    PYTEST: _resolve_python_source,
    "vitest": _resolve_vitest_source,
}


def resolve_locator_in_source(text: str | None, nodeid: str) -> tuple[str, str]:
    """``(status, reason)`` from the file's own text — no imports, no runner (#764).

    The fallback for when collection cannot run: the project's dependencies
    are not installed, the task's tree was retired, or no tree ever held this
    branch. ``text`` is the file as of the submitted commit; ``None`` means it
    could not be read, which is ``unknown`` and never ``missing``.

    Reading is per runner (#1203). It used to parse every file with ``ast``,
    so a TypeScript test came back ``unparseable`` — true in the letter and
    useless in fact, because the file parses perfectly well for the tool that
    owns it.
    """
    rel = nodeid.split("::", 1)[0]
    if text is None:
        return UNKNOWN, f"could not read {rel} at the submitted commit"
    runner = runner_of(nodeid)
    resolver = SOURCE_RESOLVERS.get(runner)
    if resolver is None:
        return UNKNOWN, NO_RESOLVER.format(runner=runner or "unknown")
    return resolver(text, rel, _wanted_name(nodeid))


def needs_source_reading(acs: Any, collected: set[str] | None) -> bool:
    """Whether resolving ``acs`` will need file text as well as ``collected``.

    The caller used to read files only when collection failed outright, which
    starved every locator collection cannot speak for: pytest collected its
    own tests, so no text was fetched, and the resolver — right to refuse to
    judge a vitest locator by a pytest collection — then had nothing to read
    and answered "could not read at the submitted commit". Nobody had tried.

    The predicate lives beside the resolver on purpose. The defect was not
    that either side was wrong on its own; it was that the rule for when text
    is needed existed twice and the two copies disagreed (#1203).
    """
    if collected is None:
        return True
    return any(
        runner_of(getattr(ac, "test_ref", None)) != PYTEST
        for ac in acs
        if _verifiable_by(ac) == "test"
        and parse_test_locator(getattr(ac, "test_ref", None))
    )


def resolve_ac_locators(
    acs: Any,
    collected: set[str] | None,
    sources: dict[str, str | None] | None = None,
    absent_files: set[str] | None = None,
) -> list[dict]:
    """Resolution status for each verifiable_by=test AC (#506, #764).

    ``collected`` is the set of existing nodeids, or ``None`` when collection
    could not run — and that is where ``sources`` takes over: the text of each
    file named by a locator, as of the submitted commit, read with git and
    parsed with ast, needing none of the project's dependencies. A file the
    caller could not read maps to ``None`` and stays ``unknown``, because
    "could not check" must never be reported as "checked and clean". Non-test
    AC are skipped — they need no test.
    """
    resolutions: list[dict] = []
    bases = _base_nodeids(collected) if collected else set()
    for ac in acs:
        if _verifiable_by(ac) != "test":
            continue
        locator = getattr(ac, "test_ref", None)
        parsed = parse_test_locator(locator)
        # ``collected`` came from pytest and speaks only for pytest, so it is
        # usable for a pytest locator and for nothing else. Judging a locator
        # of another runner against it reported a test that plainly exists as
        # absent — measured on #1202 — so a foreign one takes the
        # no-collection route whether or not pytest ran (#1203).
        usable = collected if runner_of(locator) == PYTEST else None
        if parsed is None:
            status, reason = MISSING, NO_VALID_LOCATOR
        elif usable is None and parsed[0] in (absent_files or set()):
            status, reason = resolve_locator_absent_file(parsed[1])
        elif usable is None:
            status, reason = resolve_locator_in_source(
                (sources or {}).get(parsed[0]), parsed[1]
            )
        elif parsed[1] in usable or parsed[1] in bases:
            status, reason = RESOLVABLE, BY_COLLECTION
        else:
            status, reason = MISSING, NOT_COLLECTED
        resolutions.append(
            {
                "ac_id": getattr(ac, "id", "?"),
                "locator": locator,
                "status": status,
                "reason": reason,
            }
        )
    return resolutions
