"""Static existence check for AC test locators (#506).

Complements #505: given a verifiable_by=test AC with a valid pytest locator,
check whether that test is written, by READING the file as of a git ref — no
checkout, no import, no process. A task branch is whatever an agent pushed, so
collecting its tests (``pytest --collect-only`` imports conftest.py and every
plugin) would run that agent's code on the hub host with the hub's environment;
#1650 removed that path. The result (resolvable / missing / unknown) makes a
broken AC↔test binding a visible defect in the review brief instead of a silent
hole. Best-effort: when the ref or the file cannot be read, or the file's
shape is one a static reader cannot judge, the status is ``unknown``, never a
false ``missing``.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

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

# How a locator was resolved: by reading the file, which proves a definition is
# WRITTEN there, not that pytest would run it.
BY_SOURCE = "test found in the file, read without running it"
# Why an answer could not be given. Never merged into BY_SOURCE's silence:
# "this runner is not one I can look into" is a fact about the hub, and a
# reader who cannot tell it from "the test is not there" is being accused
# on the hub's behalf (#1203).
NO_RESOLVER = "no way to look inside a {runner} test file"
# ``missing`` answers two different questions and the reason is the only thing
# that tells them apart. NO_VALID_LOCATOR means nothing resolvable was NAMED —
# the hub never got as far as looking. Any other ``missing`` reason names the
# part of the definition path the file does not contain: a locator was named,
# the hub read the file, and the test is not there. Reading the first as the
# second accuses an author of a missing test they never claimed to have
# written (#1158).
NO_VALID_LOCATOR = "no valid test locator in test_ref"
PARAM_NOT_CHECKED = "параметр не проверен"
PYTEST_CONFIGURED = "python_* заданы в настройках: сбор pytest не воспроизведён"


def _verifiable_by(ac: Any) -> str:
    vb = getattr(ac, "verifiable_by", None)
    # Обещали str, а при отсутствующем поле отдавали None: сравнение с "test"
    # молча давало False, а .lower() у вызывающего дал бы AttributeError.
    return str(getattr(vb, "value", vb) or "")


def _wanted_name(nodeid: str) -> str:
    """A vitest test's own name: everything after the FIRST ``::``, verbatim.

    A name may legitimately contain ``::`` or square brackets, and pytest's
    trimming would quietly hunt for a different test (#1203).
    """
    return nodeid.split("::", 1)[1] if "::" in nodeid else nodeid


# --- pytest: the full definition path, read from the syntax tree -------------
#
# ``Class::method`` is resolved along the path the nodeid names — module, then
# class, then member — and never by hunting for the last segment anywhere in
# the file: ``MissingClass::test_x`` must not be satisfied by a function called
# test_x in some other class. Whatever cannot be followed without executing
# code (a base class imported from elsewhere, an assignment or import that
# might bring the name in, ``__test__``, a class decorator or metaclass that
# can rewrite members, a star import) is ``unknown`` with the reason, because
# "absent" is only ever claimed for what the tree proves absent.

_FOUND, _ABSENT, _OPAQUE = "found", "absent", "opaque"


@dataclass
class _Scope:
    """The names a module or class body binds, by how they are bound."""

    defs: dict[str, ast.AST] = field(default_factory=dict)
    bound: set[str] = field(default_factory=set)
    star: bool = False


def _statements(body: list[ast.stmt]):
    """Statements of a body, looking inside the blocks that do not open a scope."""
    for node in body:
        yield node
        if isinstance(node, ast.If | ast.For | ast.AsyncFor | ast.While):
            yield from _statements(node.body)
            yield from _statements(node.orelse)
        elif isinstance(node, ast.With | ast.AsyncWith):
            yield from _statements(node.body)
        elif isinstance(node, ast.Try | ast.TryStar):
            yield from _statements(node.body)
            for handler in node.handlers:
                yield from _statements(handler.body)
            yield from _statements(node.orelse)
            yield from _statements(node.finalbody)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from _statements(case.body)


def _targets(node: ast.stmt) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, ast.AnnAssign | ast.AugAssign):
        return [node.target]
    return []


def _scope_of(body: list[ast.stmt]) -> _Scope:
    scope = _Scope()
    for node in _statements(body):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            scope.defs[node.name] = node
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    scope.star = True
                else:
                    scope.bound.add((alias.asname or alias.name).split(".")[0])
        for target in _targets(node):
            for name in ast.walk(target):
                if isinstance(name, ast.Name):
                    scope.bound.add(name.id)
    return scope


def _is_mark_decorator(dec: ast.expr) -> bool:
    """``@pytest.mark.x`` / ``@pytest.mark.x(...)``: metadata, not a rewrite."""
    node = dec.func if isinstance(dec, ast.Call) else dec
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return parts[-2:] == ["mark", "pytest"]


def _member(
    container: ast.Module | ast.ClassDef,
    name: str,
    module: _Scope,
    seen: frozenset[str] = frozenset(),
) -> tuple[str, Any]:
    """``(_FOUND, node)`` | ``(_ABSENT, why)`` | ``(_OPAQUE, why)``."""
    scope = _scope_of(container.body)
    owner = "the module" if isinstance(container, ast.Module) else container.name
    if "__test__" in scope.bound:
        return _OPAQUE, f"{owner} sets __test__, which can hide any test from pytest"
    hit = scope.defs.get(name)
    if hit is not None and name not in scope.bound:
        return _FOUND, hit
    if name in scope.bound:
        return _OPAQUE, (
            f"{name} is bound in {owner} by an assignment or an import, "
            "which a static reader cannot tell from a test"
        )
    if scope.star:
        return _OPAQUE, f"{owner} has a star import that may bring {name} in"
    if isinstance(container, ast.Module):
        return _ABSENT, f"{owner} defines no {name}"
    return _inherited(container, name, module, seen)


def _inherited(
    cls: ast.ClassDef, name: str, module: _Scope, seen: frozenset[str]
) -> tuple[str, Any]:
    if cls.keywords or any(not _is_mark_decorator(d) for d in cls.decorator_list):
        return _OPAQUE, (
            f"{cls.name} has a metaclass or a class decorator that can rewrite "
            f"its members, so the absence of {name} is not established"
        )
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id == "object":
            continue
        base_name = base.id if isinstance(base, ast.Name) else ""
        local = module.defs.get(base_name)
        if not isinstance(local, ast.ClassDef) or base_name in module.bound:
            shown = ast.unparse(base)
            return _OPAQUE, (
                f"{cls.name} inherits {shown}, which is not a class written in "
                f"this file, so the absence of {name} is not established"
            )
        if local.name in seen or local is cls:
            return _OPAQUE, f"{cls.name} has a cyclic base {local.name}"
        verdict, detail = _member(local, name, module, seen | {cls.name})
        if verdict != _ABSENT:
            return verdict, detail
    return _ABSENT, f"{cls.name} defines no {name}"


def _pytest_path(nodeid: str) -> tuple[list[str], bool]:
    """``(segments, has_param)`` of a pytest nodeid without its file part."""
    rest = nodeid.split("::", 1)[1] if "::" in nodeid else ""
    head, bracket, _ = rest.partition("[")
    return [seg for seg in head.split("::") if seg], bool(bracket)


def _resolve_pytest_path(tree: ast.Module, segments: list[str]) -> tuple[str, Any]:
    module = _scope_of(tree.body)
    container: ast.Module | ast.ClassDef = tree
    for index, segment in enumerate(segments):
        verdict, detail = _member(container, segment, module)
        if verdict != _FOUND:
            return verdict, detail
        if index == len(segments) - 1:
            return _FOUND, detail
        if not isinstance(detail, ast.ClassDef):
            return _ABSENT, f"{segment} is a function, so it has no members"
        container = detail
    return _ABSENT, "the locator names no test"


def _resolve_python_source(
    text: str, rel: str, nodeid: str, *, configured: bool = False
) -> tuple[str, str]:
    try:
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError) as exc:
        # Distinct from missing on purpose: "I could not read this" and "the
        # test is not there" are different facts, and a blanket unknown for
        # both is what taught reviewers to skip this block.
        return UNPARSEABLE, f"could not parse {rel}: {type(exc).__name__}"
    segments, has_param = _pytest_path(nodeid)
    verdict, detail = _resolve_pytest_path(tree, segments)
    if verdict == _OPAQUE:
        return UNKNOWN, f"{rel}: {detail}"
    if verdict == _ABSENT:
        return MISSING, f"{rel}: {detail}"
    notes = [PARAM_NOT_CHECKED] if has_param else []
    if configured:
        notes.append(PYTEST_CONFIGURED)
    suffix = f" ({'; '.join(notes)})" if notes else ""
    return RESOLVABLE, f"{BY_SOURCE}: {rel}:{detail.lineno}{suffix}"


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

    The counterpart of :func:`_resolve_python_source`, and deliberately no stronger: it
    answers existence by reading the file, exactly what ``BY_SOURCE`` claims.
    Nesting inside describe blocks needs no handling — the locator names the
    test's own name, which is the string this call takes.
    """
    for m in _VITEST_DECL.finditer(text):
        if _unescape(m.group("name")) == name:
            return text.count("\n", 0, m.start()) + 1
    return None


# Where a test declaration BEGINS, without any claim to read its name. The
# gap between this count and the names actually extracted is the reader's own
# blind spot, and it is the only thing that separates "the test is not here"
# from "I could not read how this file declares its tests" (#1203).
_VITEST_DECL_START = re.compile(r"\b(?:it|test)(?:\.\w+)*\s*[(`]")


def _resolve_vitest_source(
    text: str, rel: str, nodeid: str, *, configured: bool = False
) -> tuple[str, str]:
    """Existence by reading, and a refusal that never poses as an absence.

    ``ast`` makes "not found" trustworthy for Python: a real parser saw the
    whole file. Here the reader is a regular expression, and a form it cannot
    follow — a tagged template, a table built by a call, a name held in a
    variable — looks exactly like a test that was never written. Reporting
    that as ``missing`` is the false accusation this whole change exists to
    remove, so the reader counts what it could not follow and says so.
    """
    name = _wanted_name(nodeid)
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


def resolve_locator_in_source(
    text: str | None, nodeid: str, *, configured: bool = False
) -> tuple[str, str]:
    """``(status, reason)`` from the file's own text — no imports, no runner (#764).

    ``text`` is the file as of the ref the caller resolved; ``None`` means it
    could not be read, which is ``unknown`` and never ``missing``. ``configured``
    says the project sets pytest's ``python_*`` options, which this reader does
    not reproduce.

    Reading is per runner (#1203). It used to parse every file with ``ast``,
    so a TypeScript test came back ``unparseable`` — true in the letter and
    useless in fact, because the file parses perfectly well for the tool that
    owns it.
    """
    rel = nodeid.split("::", 1)[0]
    if text is None:
        return UNKNOWN, f"could not read {rel} at the resolved ref"
    runner = runner_of(nodeid)
    resolver = SOURCE_RESOLVERS.get(runner)
    if resolver is None:
        return UNKNOWN, NO_RESOLVER.format(runner=runner or "unknown")
    return resolver(text, rel, nodeid, configured=configured)


def resolve_ac_locators(
    acs: Any,
    sources: dict[str, str | None] | None = None,
    absent_files: set[str] | None = None,
    *,
    ref_label: str = "",
    pytest_configured: bool = False,
) -> list[dict]:
    """Resolution status for each verifiable_by=test AC (#506, #764, #1650).

    ``sources`` is the text of each file named by a locator, as of one resolved
    ref, read with git and parsed without importing anything. A file the caller
    could not read maps to ``None`` and stays ``unknown``, because "could not
    check" must never be reported as "checked and clean". ``absent_files`` are
    the files the ref demonstrably does not contain. Non-test AC are skipped —
    they need no test. ``ref_label`` is appended to every reason so a reader
    knows WHICH code the answer is about.
    """
    resolutions: list[dict] = []
    for ac in acs:
        if _verifiable_by(ac) != "test":
            continue
        locator = getattr(ac, "test_ref", None)
        parsed = parse_test_locator(locator)
        if parsed is None:
            status, reason = MISSING, NO_VALID_LOCATOR
        elif parsed[0] in (absent_files or set()):
            status, reason = resolve_locator_absent_file(parsed[1])
        else:
            status, reason = resolve_locator_in_source(
                (sources or {}).get(parsed[0]),
                parsed[1],
                configured=pytest_configured and runner_of(parsed[1]) == PYTEST,
            )
        if ref_label and parsed is not None:
            reason = f"{reason} [ref: {ref_label}]"
        resolutions.append(
            {
                "ac_id": getattr(ac, "id", "?"),
                "locator": locator,
                "status": status,
                "reason": reason,
            }
        )
    return resolutions


# --- reading the files: one ref, resolved once, through git only --------------

_PYTEST_CONFIG_FILES = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")
_PYTEST_PYTHON_OPTIONS = re.compile(r"^\s*python_(?:files|classes|functions)\s*=", re.M)


@dataclass
class LocatorEvidence:
    """What the locator files said at ONE ref (#1650).

    ``ref_label`` names the ref and where it came from (``submission_sha``,
    the task branch, the project base); empty when nothing could be resolved,
    in which case every source is ``None`` and ``why`` says why.
    """

    sources: dict[str, str | None] = field(default_factory=dict)
    absent: set[str] = field(default_factory=set)
    ref_label: str = ""
    why: str = ""
    pytest_configured: bool = False


async def _pick_ref(
    git: Any, repo: str, submission_sha: str, branch: str, base: str
) -> tuple[str, str, str]:
    """``(ref, label, why)`` — the first of submission_sha → branch → base.

    Only the first one that is NAMED is tried: a pinned commit that cannot be
    read is not a reason to answer about the branch, which is different code
    and would be reported as if it were the submission. A branch or a base is
    resolved to the sha of ``origin/<name>`` once, and every read after that
    goes to the sha.
    """
    if submission_sha:
        return submission_sha, f"submission_sha {submission_sha[:10]}", ""
    for kind, name in (("task branch", branch), ("project base", base)):
        if not name:
            continue
        sha = await git.head_sha(repo, name)
        if sha:
            return sha, f"{kind} {name} @ {sha[:10]}", ""
        # A local-only ref (no origin): the name itself, as before.
        return name, f"{kind} {name}", ""
    return "", "", "no submission_sha, branch or base to read from"


async def read_locator_evidence(
    git: Any,
    repo: str | None,
    files: list[str],
    *,
    submission_sha: str = "",
    branch: str = "",
    base: str = "",
) -> LocatorEvidence:
    """Read the locator files at one ref, never running anything (#1650).

    Everything goes through ``git show`` / ``git ls-tree``: no checkout, no
    import, no environment handed over. Failure is ``unknown`` for every file,
    never ``missing``.
    """
    if not files:
        return LocatorEvidence()
    unread = dict.fromkeys(files)
    if not repo:
        return LocatorEvidence(unread, why="project has no workspace")
    ref, label, why = await _pick_ref(
        git, repo, (submission_sha or "").strip(), (branch or "").strip(), base or ""
    )
    if not ref:
        return LocatorEvidence(unread, why=why)
    try:
        tree = await git.files_at_ref(repo, ref)
    except Exception:  # noqa: BLE001 - the caller must assemble regardless
        log.warning("locator tree listing failed at %s", label)
        tree = None
    if tree is None:
        return LocatorEvidence(unread, why=f"could not list the tree of {label}")
    absent = {p for p in files if p not in tree}
    wanted = [p for p in files if p not in absent]
    configs = (
        [c for c in _PYTEST_CONFIG_FILES if c in tree]
        if any(runner_of(p) == PYTEST for p in wanted)
        else []
    )
    sources: dict[str, str | None] = dict(unread)
    for path in wanted:
        sources[path] = await _read(git, repo, ref, path)
    configured = False
    for config_file in configs:
        text = await _read(git, repo, ref, config_file) or ""
        configured = configured or bool(_PYTEST_PYTHON_OPTIONS.search(text))
    return LocatorEvidence(sources, absent, label, "", configured)


async def _read(git: Any, repo: str, ref: str, path: str) -> str | None:
    try:
        return await git.file_at_ref(repo, ref, path)
    except Exception:  # noqa: BLE001 - one unreadable file is one unknown
        log.warning("locator source read failed: %s", path)
        return None


async def resolve_locators_at_ref(
    git: Any,
    repo: str | None,
    acs: Any,
    *,
    submission_sha: str = "",
    branch: str = "",
    base: str = "",
) -> list[dict]:
    """Resolve every test-AC locator of ``acs``: read at one ref, then parse.

    The single door for callers (review brief, epic approve): reading goes
    through git, parsing runs off the event loop.
    """
    evidence = await read_locator_evidence(
        git,
        repo,
        locator_files(acs),
        submission_sha=submission_sha,
        branch=branch,
        base=base,
    )
    return await asyncio.to_thread(
        resolve_ac_locators,
        acs,
        evidence.sources,
        evidence.absent,
        ref_label=evidence.ref_label,
        pytest_configured=evidence.pytest_configured,
    )
