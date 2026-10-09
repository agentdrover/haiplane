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
# test_x in some other class. "Found" and "absent" are both claims, and each is
# made only for what the tree PROVES:
#
# * a name bound once, unconditionally, by a def/class is a definition; bound
#   twice, under an if/try/with, by an assignment or an import, or reachable
#   through a star import, it is opaque — which binding wins is decided by
#   running the module;
# * a class is clean only if nothing can rewrite its members: no metaclass, no
#   decorator but pytest.mark.*, and every base is a clean class written in
#   this same file;
# * a function is a test only with no decorator but pytest.mark.* (and
#   staticmethod/classmethod); anything else may replace or hide it.
#
# The tree is hostile input, so the walk is bounded: inheritance and nesting
# beyond ``_MAX_DEPTH`` are opaque, and the callers catch what the parser or
# ``ast.unparse`` can still raise.

_FOUND, _ABSENT, _OPAQUE = "found", "absent", "opaque"
_MAX_DEPTH = 40
_SAFE_PLAIN_DECORATORS = {"staticmethod", "classmethod"}


@dataclass
class _Binding:
    kind: str  # "def" | "class" | "other"
    node: ast.AST | None
    conditional: bool


@dataclass
class _Scope:
    """The names a module or class body binds, every binding kept."""

    names: dict[str, list[_Binding]] = field(default_factory=dict)
    star: bool = False

    def add(self, name: str, kind: str, node: ast.AST | None, conditional: bool):
        self.names.setdefault(name, []).append(_Binding(kind, node, conditional))


def _blocks(node: ast.stmt):
    """The nested statement lists of a compound statement that open no scope."""
    if isinstance(node, ast.If | ast.For | ast.AsyncFor | ast.While):
        yield node.body
        yield node.orelse
    elif isinstance(node, ast.With | ast.AsyncWith):
        yield node.body
    elif isinstance(node, ast.Try | ast.TryStar):
        yield node.body
        for handler in node.handlers:
            yield handler.body
        yield node.orelse
        yield node.finalbody
    elif isinstance(node, ast.Match):
        for case in node.cases:
            yield case.body


def _targets(node: ast.stmt) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, ast.AnnAssign | ast.AugAssign):
        return [node.target]
    if isinstance(node, ast.For | ast.AsyncFor):
        return [node.target]
    return []


def _scope_of(body: list[ast.stmt]) -> _Scope:
    scope = _Scope()
    stack: list[tuple[list[ast.stmt], bool]] = [(body, False)]
    while stack:
        stmts, conditional = stack.pop()
        for node in stmts:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                scope.add(node.name, "def", node, conditional)
            elif isinstance(node, ast.ClassDef):
                scope.add(node.name, "class", node, conditional)
            elif isinstance(node, ast.Import | ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        scope.star = True
                    else:
                        name = (alias.asname or alias.name).split(".")[0]
                        scope.add(name, "other", node, conditional)
            for target in _targets(node):
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        scope.add(sub.id, "other", node, conditional)
            for block in _blocks(node):
                stack.append((block, True))
    return scope


def _decorator_parts(dec: ast.expr) -> list[str]:
    node = dec.func if isinstance(dec, ast.Call) else dec
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return parts


def _is_mark(dec: ast.expr) -> bool:
    """``@pytest.mark.x`` / ``@pytest.mark.x(...)``: metadata, not a rewrite."""
    return _decorator_parts(dec)[-2:] == ["mark", "pytest"]


def _function_issue(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    for dec in fn.decorator_list:
        parts = _decorator_parts(dec)
        if parts[:1] == ["fixture"]:
            return f"{fn.name} is a pytest fixture, which is not a test"
        if _is_mark(dec) or (len(parts) == 1 and parts[0] in _SAFE_PLAIN_DECORATORS):
            continue
        return (
            f"{fn.name} carries the decorator @{ast.unparse(dec)}, which can "
            "replace or hide the test"
        )
    return ""


def _class_issue(
    cls: ast.ClassDef, module: _Scope, depth: int = 0, trail: tuple[str, ...] = ()
) -> str:
    """Why this class cannot be trusted to hold only what its body says; ``""`` if clean."""
    if depth > _MAX_DEPTH:
        return f"{cls.name} sits under more than {_MAX_DEPTH} levels of inheritance"
    if cls.keywords:
        return f"{cls.name} has a metaclass or class keywords"
    for dec in cls.decorator_list:
        if not _is_mark(dec):
            return (
                f"{cls.name} carries the decorator @{ast.unparse(dec)}, which can "
                "rewrite its members"
            )
    if "__test__" in _scope_of(cls.body).names:
        return f"{cls.name} sets __test__, which can hide any test from pytest"
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id == "object":
            continue
        local = _local_class(base, module)
        if local is None:
            return (
                f"{cls.name} inherits {ast.unparse(base)}, which is not a class "
                "written once in this file"
            )
        if local.name in trail or local is cls:
            return f"{cls.name} has a cyclic base {local.name}"
        issue = _class_issue(local, module, depth + 1, (*trail, cls.name))
        if issue:
            return issue
    return ""


def _local_class(base: ast.expr, module: _Scope) -> ast.ClassDef | None:
    if not isinstance(base, ast.Name):
        return None
    bindings = module.names.get(base.id, [])
    if (
        len(bindings) == 1
        and bindings[0].kind == "class"
        and not bindings[0].conditional
    ):
        node = bindings[0].node
        return node if isinstance(node, ast.ClassDef) else None
    return None


def _lookup(scope: _Scope, name: str, owner: str) -> tuple[str, Any]:
    """``(_FOUND, node)`` | ``(_ABSENT, why)`` | ``(_OPAQUE, why)`` in one body."""
    bindings = scope.names.get(name, [])
    if not bindings:
        if scope.star:
            return _OPAQUE, f"{owner} has a star import that may bring {name} in"
        return _ABSENT, f"{owner} defines no {name}"
    if len(bindings) == 1 and bindings[0].kind in ("def", "class"):
        if bindings[0].conditional:
            return _OPAQUE, f"{name} in {owner} is defined only under a condition"
        return _FOUND, bindings[0].node
    return _OPAQUE, (
        f"{name} is bound {len(bindings)} time(s) in {owner}, by an assignment, "
        "an import or a repeated definition, so which binding wins is not static"
    )


def _member(
    container: ast.Module | ast.ClassDef,
    name: str,
    module: _Scope,
    depth: int = 0,
    trail: tuple[str, ...] = (),
) -> tuple[str, Any]:
    scope = _scope_of(container.body)
    owner = "the module" if isinstance(container, ast.Module) else container.name
    if "__test__" in scope.names:
        return _OPAQUE, f"{owner} sets __test__, which can hide any test from pytest"
    verdict, detail = _lookup(scope, name, owner)
    if verdict != _ABSENT or isinstance(container, ast.Module):
        return verdict, detail
    if depth > _MAX_DEPTH:
        return (
            _OPAQUE,
            f"{owner} sits under more than {_MAX_DEPTH} levels of inheritance",
        )
    issue = _class_issue(container, module) if depth == 0 else ""
    if issue:
        return _OPAQUE, f"{issue}, so the absence of {name} is not established"
    for base in container.bases:
        if isinstance(base, ast.Name) and base.id == "object":
            continue
        local = _local_class(base, module)
        if local is None or local.name in trail or local is container:
            return _OPAQUE, (
                f"{container.name} inherits {ast.unparse(base)}, which is not a "
                f"class written once in this file, so the absence of {name} is "
                "not established"
            )
        verdict, detail = _member(
            local, name, module, depth + 1, (*trail, container.name)
        )
        if verdict != _ABSENT:
            return verdict, detail
    return _ABSENT, f"{container.name} defines no {name}"


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
        if isinstance(container, ast.ClassDef):
            issue = _class_issue(container, module)
            if issue:
                return _OPAQUE, issue
        if index == len(segments) - 1:
            if isinstance(detail, ast.ClassDef):
                issue = _class_issue(detail, module)
            else:
                issue = _function_issue(detail)
            return (_OPAQUE, issue) if issue else (_FOUND, detail)
        if not isinstance(detail, ast.ClassDef):
            return _ABSENT, f"{segment} is a function, so it has no members"
        container = detail
    return _ABSENT, "the locator names no test"


def _resolve_python_source(
    text: str, rel: str, nodeid: str, *, config_issue: str = ""
) -> tuple[str, str]:
    try:
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError) as exc:
        # Distinct from missing on purpose: "I could not read this" and "the
        # test is not there" are different facts, and a blanket unknown for
        # both is what taught reviewers to skip this block.
        return UNPARSEABLE, f"could not parse {rel}: {type(exc).__name__}"
    except (RecursionError, MemoryError) as exc:
        return UNKNOWN, f"{rel} is too deeply nested to read: {type(exc).__name__}"
    segments, has_param = _pytest_path(nodeid)
    try:
        verdict, detail = _resolve_pytest_path(tree, segments)
    except (RecursionError, MemoryError) as exc:
        return UNKNOWN, f"{rel} is too deeply nested to read: {type(exc).__name__}"
    if verdict == _OPAQUE:
        return UNKNOWN, f"{rel}: {detail}"
    if verdict == _ABSENT:
        return MISSING, f"{rel}: {detail}"
    if config_issue:
        return UNKNOWN, f"{rel}: {config_issue}"
    suffix = f" ({PARAM_NOT_CHECKED})" if has_param else ""
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
    text: str, rel: str, nodeid: str, *, config_issue: str = ""
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
    text: str | None, nodeid: str, *, config_issue: str = "", unread: str = ""
) -> tuple[str, str]:
    """``(status, reason)`` from the file's own text — no imports, no runner (#764).

    ``text`` is the file as of the ref the caller resolved; ``None`` means it
    could not be read (``unread`` says why), which is ``unknown`` and never
    ``missing``. ``config_issue`` is a reason pytest's collection options make
    a positive answer unreliable.

    Reading is per runner (#1203). It used to parse every file with ``ast``,
    so a TypeScript test came back ``unparseable`` — true in the letter and
    useless in fact, because the file parses perfectly well for the tool that
    owns it.
    """
    rel = nodeid.split("::", 1)[0]
    if text is None:
        return UNKNOWN, unread or f"could not read {rel} at the resolved ref"
    runner = runner_of(nodeid)
    resolver = SOURCE_RESOLVERS.get(runner)
    if resolver is None:
        return UNKNOWN, NO_RESOLVER.format(runner=runner or "unknown")
    return resolver(text, rel, nodeid, config_issue=config_issue)


def resolve_ac_locators(
    acs: Any,
    sources: dict[str, str | None] | None = None,
    absent_files: set[str] | None = None,
    *,
    ref_label: str = "",
    pytest_config_issue: str = "",
    unread: dict[str, str] | None = None,
) -> list[dict]:
    """Resolution status for each verifiable_by=test AC (#506, #764, #1650).

    ``sources`` is the text of each file named by a locator, as of one resolved
    ref, read with git and parsed without importing anything. A file the caller
    could not read maps to ``None`` (``unread`` says why) and stays ``unknown``,
    because "could not check" must never be reported as "checked and clean".
    ``absent_files`` are the files the ref demonstrably does not contain.
    Non-test AC are skipped — they need no test. ``ref_label`` is appended to
    every reason so a reader knows WHICH code the answer is about.
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
                config_issue=(
                    pytest_config_issue if runner_of(parsed[1]) == PYTEST else ""
                ),
                unread=(unread or {}).get(parsed[0], ""),
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


# --- reading the files: one commit sha, through git only -----------------------
#
# The files are hostile input: the reader asks for a size before a byte is
# kept, refuses what is over the limits, and the callers parse off the event
# loop. A separate process with rlimits was considered and not taken: the
# parser's memory is bounded by the file size cap, its recursion by the
# interpreter's guards (turned into ``unknown`` here), and a process boundary
# would buy a kill switch for CPU only, at the price of a second interpreter
# per brief read.

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
_DEFAULT_PYTHON_OPTIONS = {
    "python_files": ["test_*.py", "*_test.py"],
    "python_classes": ["Test"],
    "python_functions": ["test"],
}
# (file, section) pairs pytest reads its ini-options from.
_INI_SECTIONS = (
    ("pytest.ini", "pytest"),
    ("tox.ini", "pytest"),
    ("setup.cfg", "tool:pytest"),
)


@dataclass
class LocatorEvidence:
    """What the locator files said at ONE commit (#1650).

    ``ref_label`` names the commit and where it came from (``submission_sha``,
    the task branch, the project base); empty when nothing could be resolved,
    in which case every source is ``None`` and ``why`` says why. ``unread``
    names the reason per file that could not be used, and ``pytest_config_issue``
    is a reason a positive pytest answer cannot be given.
    """

    sources: dict[str, str | None] = field(default_factory=dict)
    absent: set[str] = field(default_factory=set)
    ref_label: str = ""
    why: str = ""
    pytest_config_issue: str = ""
    unread: dict[str, str] = field(default_factory=dict)


async def _commit_of(git: Any, repo: str, name: str) -> str:
    """The commit sha ``name`` points at, remote-first; ``""`` when it cannot be told."""
    sha = await git.head_sha(repo, name)
    if sha:
        return sha
    state, detail = await git.resolve_ref(name, repo)
    return detail if state == "resolved" and _SHA.fullmatch(detail or "") else ""


_SHA = re.compile(r"[0-9a-f]{40}")


async def _pick_ref(
    git: Any, repo: str, submission_sha: str, branch: str, base: str
) -> tuple[str, str, str]:
    """``(commit_sha, label, why)`` — the first of submission_sha → branch → base.

    Only the first one that is NAMED is tried: a pinned commit that cannot be
    resolved is not a reason to answer about the branch, which is different
    code and would be reported as if it were the submission. Whatever is named
    — sha, remote branch or local ref — is resolved to a commit sha ONCE, and
    every read after that goes to the sha, so a branch that moves between two
    reads cannot make one answer out of two commits.
    """
    for kind, name in (
        ("submission_sha", submission_sha),
        ("task branch", branch),
        ("project base", base),
    ):
        if not name:
            continue
        sha = await _commit_of(git, repo, name)
        if not sha:
            return "", "", f"could not resolve {kind} {name} to a commit"
        shown = sha[:10] if kind == "submission_sha" else f"{name} @ {sha[:10]}"
        return sha, f"{kind} {shown}", ""
    return "", "", "no submission_sha, branch or base to read from"


async def _read_one(git: Any, repo: str, sha: str, path: str) -> dict[str, Any]:
    try:
        return await git.read_file_at_ref(repo, sha, path, limit_chars=MAX_FILE_BYTES)
    except Exception:  # noqa: BLE001 - one unreadable file is one unknown
        log.warning("locator source read failed: %s", path)
        return {"state": "unreadable", "reason": "the read raised"}


def _toml_options(text: str) -> dict[str, Any]:
    import tomllib

    data = tomllib.loads(text)
    return dict(data.get("tool", {}).get("pytest", {}).get("ini_options", {}) or {})


def _ini_options(text: str, section: str) -> dict[str, Any]:
    import configparser

    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(text)
    return dict(parser[section]) if parser.has_section(section) else {}


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    return str(value).split()


def _python_options_issue(options: dict[str, Any], where: str) -> str:
    for key, default in _DEFAULT_PYTHON_OPTIONS.items():
        if key in options and sorted(_as_list(options[key])) != sorted(default):
            return f"{where} sets {key}, so pytest's own collection rules differ"
    if any(k in str(options.get("addopts", "")) for k in _DEFAULT_PYTHON_OPTIONS):
        return f"{where} passes python_* options through addopts"
    return ""


async def _pytest_config_issue(git: Any, repo: str, sha: str) -> str:
    """Why pytest's collection options cannot be trusted at ``sha``; ``""`` if they can.

    Read as DATA: tomllib for pyproject.toml, configparser for the ini files.
    "no config" and "config I could not read" are different answers — the
    second is an issue, because an unreadable file may well set python_*.
    """
    for name in ("pyproject.toml", *(n for n, _ in _INI_SECTIONS)):
        info = await _read_one(git, repo, sha, name)
        state = info.get("state")
        if state == "missing":
            continue
        if state != "present" or info.get("truncated"):
            return f"{name} could not be read, so pytest's python_* options are unknown"
        text = info.get("content", "")
        try:
            if name == "pyproject.toml":
                options = _toml_options(text)
            else:
                section = dict(_INI_SECTIONS)[name]
                options = _ini_options(text, section)
        except Exception:  # noqa: BLE001 - any parse failure is "unreadable"
            return (
                f"{name} could not be parsed, so pytest's python_* options are unknown"
            )
        issue = _python_options_issue(options, name)
        if issue:
            return issue
    return ""


async def read_locator_evidence(
    git: Any,
    repo: str | None,
    files: list[str],
    *,
    submission_sha: str = "",
    branch: str = "",
    base: str = "",
) -> LocatorEvidence:
    """Read the locator files at one commit, never running anything (#1650).

    Everything goes through git by commit sha: no checkout, no import, no
    environment handed over. Failure is ``unknown`` for the file, never
    ``missing``; a file over ``MAX_FILE_BYTES`` or past ``MAX_TOTAL_BYTES`` for
    the request is ``unknown`` too, and is never parsed.
    """
    if not files:
        return LocatorEvidence()
    unread = dict.fromkeys(files)
    if not repo:
        return LocatorEvidence(unread, why="project has no workspace")
    sha, label, why = await _pick_ref(
        git, repo, (submission_sha or "").strip(), (branch or "").strip(), base or ""
    )
    if not sha:
        return LocatorEvidence(unread, why=why)
    evidence = LocatorEvidence(dict(unread), ref_label=label)
    spent = 0
    for path in files:
        info = await _read_one(git, repo, sha, path)
        state = info.get("state")
        size = int(info.get("size") or 0)
        if state == "missing":
            evidence.absent.add(path)
        elif state != "present":
            evidence.unread[path] = f"could not read {path} at {label}"
        elif size > MAX_FILE_BYTES or info.get("truncated"):
            evidence.unread[path] = (
                f"{path} is {size} bytes, over the {MAX_FILE_BYTES} byte limit "
                "for static reading"
            )
        elif spent + size > MAX_TOTAL_BYTES:
            evidence.unread[path] = (
                f"{path} would take this request past the {MAX_TOTAL_BYTES} byte "
                "limit for static reading"
            )
        else:
            spent += size
            evidence.sources[path] = info.get("content", "")
    if any(runner_of(p) == PYTEST for p in files if p not in evidence.absent):
        evidence.pytest_config_issue = await _pytest_config_issue(git, repo, sha)
    return evidence


async def resolve_locators_at_ref(
    git: Any,
    repo: str | None,
    acs: Any,
    *,
    submission_sha: str = "",
    branch: str = "",
    base: str = "",
) -> list[dict]:
    """Resolve every test-AC locator of ``acs``: read at one commit, then parse.

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
        pytest_config_issue=evidence.pytest_config_issue,
        unread=evidence.unread,
    )
