"""Static reader of AC test locators, run in a bounded child process (#1650).

The files this reads are whatever an agent pushed, so nothing here is trusted
to terminate: a class graph can fan out exponentially, a regular expression can
backtrack, an expression can nest past the parser. The hub therefore runs this
module in a separate interpreter with hard CPU and memory limits (see
``test_existence.analyse_in_worker``); the code below adds a second line — an
operation budget shared by the whole request — and answers ``unknown`` rather
than guess when the budget runs out.

Standard library only, and no import of the ``hub`` package: the worker is
started as a plain script, because importing ``hub.services`` would load the
whole hub into the sandbox and spend most of its CPU budget before reading a
byte. Input arrives on stdin as JSON (the file texts, read by the hub through
git at one commit), output leaves on stdout as JSON.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any

RESOLVABLE = "resolvable"
MISSING = "missing"
UNKNOWN = "unknown"
UNPARSEABLE = "unparseable"

BY_SOURCE = "test found in the file, read without running it"
PARAM_NOT_CHECKED = "параметр не проверен"
OVER_BUDGET = "анализ превысил бюджет"
NO_RESOLVER = "no way to look inside a {runner} test file"

OPS_LIMIT = 1_500_000
_MAX_DEPTH = 40
_MAX_REASON = 400
_SAFE_PLAIN_DECORATORS = {"staticmethod", "classmethod"}
_FOUND, _ABSENT, _OPAQUE = "found", "absent", "opaque"


class BudgetExceeded(Exception):
    """The request used more operations than it was given."""


class Budget:
    def __init__(self, limit: int | None = None):
        self.left = OPS_LIMIT if limit is None else limit

    def spend(self, n: int = 1) -> None:
        self.left -= n
        if self.left < 0:
            raise BudgetExceeded


def _short(text: str) -> str:
    return text if len(text) <= _MAX_REASON else text[:_MAX_REASON] + "…"


# --- pytest: the full definition path, read from the syntax tree -------------
#
# ``Class::method`` is resolved along the path the nodeid names — module, then
# class, then member — and never by hunting for the last segment anywhere in
# the file. "Found" and "absent" are both claims, made only for what the tree
# PROVES:
#
# * a name is a definition only if it is bound ONCE, unconditionally, by a
#   def/class and by nothing else in its scope (no assignment, import, del,
#   walrus, with/for/except target, match capture, global, star import);
# * a class is clean only if nothing can rewrite its members: no metaclass, no
#   decorator but pytest.mark.*, no __init_subclass__/__class_getitem__, and
#   every base is a clean class written in this same file;
# * a function is a test only with no decorator but pytest.mark.*.
#
# Everything is computed once per file and memoised, so a class graph costs
# its size, not the number of paths through it.


@dataclass
class _Binding:
    kind: str  # "def" | "class" | "other"
    node: ast.AST | None
    conditional: bool


@dataclass
class _Scope:
    names: dict[str, list[_Binding]] = field(default_factory=dict)
    star: bool = False

    def add(self, name: str, kind: str, node: ast.AST | None, conditional: bool):
        self.names.setdefault(name, []).append(_Binding(kind, node, conditional))


_BLOCK_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


def _blocks(node: ast.stmt):
    """The nested statement lists of a compound statement that open no scope."""
    if isinstance(
        node, ast.If | ast.For | ast.AsyncFor | ast.While | ast.With | ast.AsyncWith
    ):
        yield node.body
        yield getattr(node, "orelse", [])
    elif isinstance(node, ast.Try | ast.TryStar):
        yield node.body
        for handler in node.handlers:
            yield handler.body
        yield node.orelse
        yield node.finalbody
    elif isinstance(node, ast.Match):
        for case in node.cases:
            yield case.body


def _expressions(node: ast.stmt):
    """Expression children of a statement, without its nested statement blocks."""
    for name, value in ast.iter_fields(node):
        if name in _BLOCK_FIELDS:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, ast.expr | ast.withitem | ast.keyword | ast.arguments):
                yield item


def _stored_names(target: ast.AST, budget: Budget):
    for sub in ast.walk(target):
        budget.spend()
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store | ast.Del):
            yield sub.id


def _pattern_names(pattern: ast.AST, budget: Budget):
    for sub in ast.walk(pattern):
        budget.spend()
        name = getattr(sub, "name", None) or getattr(sub, "rest", None)
        if isinstance(name, str) and isinstance(
            sub, ast.MatchAs | ast.MatchStar | ast.MatchMapping
        ):
            yield name


def _walrus_names(expr: ast.AST, budget: Budget):
    """Names bound by ``:=`` in an expression, comprehensions included."""
    stack = [expr]
    while stack:
        node = stack.pop()
        budget.spend()
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.NamedExpr):
            yield node.target.id
        stack.extend(ast.iter_child_nodes(node))


def _target_names(node: ast.stmt, budget: Budget):
    """Names bound by the targets of an assignment-like statement."""
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.For | ast.AsyncFor):
        targets = [node.target]
    elif isinstance(node, ast.Delete):
        targets = list(node.targets)
    elif isinstance(node, ast.With | ast.AsyncWith):
        targets = [i.optional_vars for i in node.items if i.optional_vars is not None]
    else:
        targets = []
    for target in targets:
        yield from _stored_names(target, budget)


def _declared_names(node: ast.stmt, budget: Budget):
    """Names bound by an except clause, a match capture or a global statement."""
    if isinstance(node, ast.Try | ast.TryStar):
        for handler in node.handlers:
            if handler.name:
                yield handler.name
    elif isinstance(node, ast.Match):
        for case in node.cases:
            yield from _pattern_names(case.pattern, budget)
    elif isinstance(node, ast.Global | ast.Nonlocal):
        yield from node.names


def _bound_names(node: ast.stmt, budget: Budget):
    """Every name this statement binds in its own scope, apart from def/class."""
    yield from _target_names(node, budget)
    yield from _declared_names(node, budget)
    for expr in _expressions(node):
        yield from _walrus_names(expr, budget)


def _scope_of(body: list[ast.stmt], budget: Budget) -> _Scope:
    scope = _Scope()
    stack: list[tuple[list[ast.stmt], bool]] = [(body, False)]
    while stack:
        stmts, conditional = stack.pop()
        for node in stmts:
            budget.spend()
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
            for name in _bound_names(node, budget):
                scope.add(name, "other", node, conditional)
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
            f"{fn.name} carries the decorator @{_unparse(dec)}, which can "
            "replace or hide the test"
        )
    return ""


def _unparse(node: ast.AST) -> str:
    try:
        return _short(ast.unparse(node))
    except (RecursionError, MemoryError):
        return "<too deep to show>"


class _PyFile:
    """One parsed file: scopes, class checks and member lookups, each computed once."""

    def __init__(self, text: str, rel: str, budget: Budget):
        self.budget = budget
        self._scopes: dict[int, _Scope] = {}
        self.tree = ast.parse(text, filename=rel)
        self.module = self._scope(self.tree)
        for node in ast.walk(self.tree):
            budget.spend()
            if isinstance(node, ast.Global):
                for name in node.names:
                    self.module.add(name, "other", node, True)
        self._issues: dict[int, str] = {}
        self._active: set[Any] = set()
        self._members: dict[tuple[int, str], tuple[str, Any]] = {}

    def _scope(self, container: ast.Module | ast.ClassDef) -> _Scope:
        cached = self._scopes.get(id(container))
        if cached is None:
            cached = self._scopes[id(container)] = _scope_of(
                container.body, self.budget
            )
        return cached

    def local_class(self, base: ast.expr) -> ast.ClassDef | None:
        if not isinstance(base, ast.Name):
            return None
        bindings = self.module.names.get(base.id, [])
        if (
            len(bindings) == 1
            and bindings[0].kind == "class"
            and not bindings[0].conditional
            and not self.module.star
        ):
            node = bindings[0].node
            return node if isinstance(node, ast.ClassDef) else None
        return None

    def class_issue(self, cls: ast.ClassDef, depth: int = 0) -> str:
        """Why this class cannot be trusted to hold only what its body says; ``""`` if clean."""
        key = id(cls)
        if key in self._issues:
            return self._issues[key]
        if ("issue", key) in self._active:
            return f"{cls.name} has a cyclic base"
        if depth > _MAX_DEPTH:
            return f"{cls.name} sits under more than {_MAX_DEPTH} levels of inheritance"
        self._active.add(("issue", key))
        try:
            issue = self._class_issue(cls, depth)
        finally:
            self._active.discard(("issue", key))
        self._issues[key] = issue
        return issue

    def _class_issue(self, cls: ast.ClassDef, depth: int) -> str:
        self.budget.spend()
        if cls.keywords:
            return f"{cls.name} has a metaclass or class keywords"
        for dec in cls.decorator_list:
            if not _is_mark(dec):
                return (
                    f"{cls.name} carries the decorator @{_unparse(dec)}, which "
                    "can rewrite its members"
                )
        names = self._scope(cls).names
        for hook in ("__test__", "__init_subclass__", "__class_getitem__"):
            if hook in names:
                return (
                    f"{cls.name} defines {hook}, which can change how it is collected"
                )
        for base in cls.bases:
            if isinstance(base, ast.Name) and base.id == "object":
                continue
            local = self.local_class(base)
            if local is None:
                return (
                    f"{cls.name} inherits {_unparse(base)}, which is not a class "
                    "written once in this file"
                )
            issue = self.class_issue(local, depth + 1)
            if issue:
                return issue
        return ""

    def lookup(self, scope: _Scope, name: str, owner: str) -> tuple[str, Any]:
        bindings = scope.names.get(name, [])
        if not bindings:
            if scope.star:
                return _OPAQUE, f"{owner} has a star import that may bring {name} in"
            return _ABSENT, f"{owner} defines no {name}"
        if len(bindings) == 1 and bindings[0].kind in ("def", "class"):
            if bindings[0].conditional:
                return _OPAQUE, f"{name} in {owner} is defined only under a condition"
            if scope.star:
                return _OPAQUE, f"a star import in {owner} may rebind {name}"
            return _FOUND, bindings[0].node
        return _OPAQUE, (
            f"{name} is bound {len(bindings)} time(s) in {owner}, by an assignment, "
            "an import, a del or a repeated definition, so which binding wins is "
            "not static"
        )

    def member(
        self, container: ast.Module | ast.ClassDef, name: str, depth: int = 0
    ) -> tuple[str, Any]:
        key = (id(container), name)
        if key in self._members:
            return self._members[key]
        if ("member", key) in self._active:
            return _OPAQUE, f"cyclic inheritance while looking for {name}"
        self._active.add(("member", key))
        try:
            result = self._member(container, name, depth)
        finally:
            self._active.discard(("member", key))
        self._members[key] = result
        return result

    def _member(
        self, container: ast.Module | ast.ClassDef, name: str, depth: int
    ) -> tuple[str, Any]:
        self.budget.spend()
        scope = self._scope(container)
        owner = "the module" if isinstance(container, ast.Module) else container.name
        if "__test__" in scope.names:
            return (
                _OPAQUE,
                f"{owner} sets __test__, which can hide any test from pytest",
            )
        verdict, detail = self.lookup(scope, name, owner)
        if verdict != _ABSENT or isinstance(container, ast.Module):
            return verdict, detail
        if depth > _MAX_DEPTH:
            return (
                _OPAQUE,
                f"{owner} sits under more than {_MAX_DEPTH} levels of inheritance",
            )
        issue = self.class_issue(container)
        if issue:
            return _OPAQUE, f"{issue}, so the absence of {name} is not established"
        for base in container.bases:
            if isinstance(base, ast.Name) and base.id == "object":
                continue
            local = self.local_class(base)
            if local is None:
                return (
                    _OPAQUE,
                    f"{container.name} inherits a class not written once here",
                )
            verdict, detail = self.member(local, name, depth + 1)
            if verdict != _ABSENT:
                return verdict, detail
        return _ABSENT, f"{container.name} defines no {name}"

    def resolve(self, segments: list[str]) -> tuple[str, Any]:
        container: ast.Module | ast.ClassDef = self.tree
        for index, segment in enumerate(segments):
            verdict, detail = self.member(container, segment)
            if verdict != _FOUND:
                return verdict, detail
            if isinstance(container, ast.ClassDef):
                issue = self.class_issue(container)
                if issue:
                    return _OPAQUE, issue
            if index == len(segments) - 1:
                if isinstance(detail, ast.ClassDef):
                    issue = self.class_issue(detail)
                else:
                    issue = _function_issue(detail)
                return (_OPAQUE, issue) if issue else (_FOUND, detail)
            if not isinstance(detail, ast.ClassDef):
                return _ABSENT, f"{segment} is a function, so it has no members"
            container = detail
        return _ABSENT, "the locator names no test"


def _pytest_path(nodeid: str) -> tuple[list[str], bool]:
    """``(segments, has_param)`` of a pytest nodeid without its file part."""
    rest = nodeid.split("::", 1)[1] if "::" in nodeid else ""
    head, bracket, _ = rest.partition("[")
    return [seg for seg in head.split("::") if seg], bool(bracket)


def resolve_python(
    parsed: _PyFile | BaseException, rel: str, nodeid: str, config_issue: str = ""
) -> tuple[str, str]:
    """``(status, reason)`` for a pytest nodeid against an already parsed file."""
    if isinstance(parsed, SyntaxError | ValueError):
        # Distinct from missing on purpose: "I could not read this" and "the
        # test is not there" are different facts.
        return UNPARSEABLE, f"could not parse {rel}: {type(parsed).__name__}"
    if isinstance(parsed, RecursionError | MemoryError):
        return UNKNOWN, f"{rel} is too deeply nested to read: {type(parsed).__name__}"
    if isinstance(parsed, BaseException):
        raise parsed
    segments, has_param = _pytest_path(nodeid)
    try:
        verdict, detail = parsed.resolve(segments)
    except (RecursionError, MemoryError) as exc:
        return UNKNOWN, f"{rel} is too deeply nested to read: {type(exc).__name__}"
    if verdict == _OPAQUE:
        return UNKNOWN, _short(f"{rel}: {detail}")
    if verdict == _ABSENT:
        return MISSING, _short(f"{rel}: {detail}")
    if config_issue:
        return UNKNOWN, _short(f"{rel}: {config_issue}")
    suffix = f" ({PARAM_NOT_CHECKED})" if has_param else ""
    return RESOLVABLE, f"{BY_SOURCE}: {rel}:{detail.lineno}{suffix}"


def parse_python(text: str, rel: str, budget: Budget) -> _PyFile | BaseException:
    """Parse once; a parse failure is returned (not raised) so callers can reuse it."""
    try:
        return _PyFile(text, rel, budget)
    except BudgetExceeded:
        raise
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return exc


# --- vitest: a linear scanner, no regular expression with nested quantifiers --

_VITEST_HEAD = re.compile(r"\b(?:it|test)(?:\.\w+)*")
_SCAN_LIMIT = 2000
_QUOTES = "'\"`"


def _skip_ws(text: str, i: int, budget: Budget) -> int:
    n = len(text)
    start = i
    while i < n and text[i].isspace() and i - start < _SCAN_LIMIT:
        i += 1
    budget.spend(i - start + 1)
    return i


def _string_at(text: str, i: int, budget: Budget) -> tuple[str, int] | None:
    """The quoted literal starting at ``i`` as ``(raw, index_after)``, or ``None``."""
    quote = text[i]
    k = i + 1
    end = min(len(text), i + _SCAN_LIMIT)
    while k < end:
        char = text[k]
        if char == "\\":
            k += 2
        elif char == quote:
            budget.spend(k - i)
            return text[i + 1 : k], k + 1
        else:
            k += 1
    budget.spend(end - i)
    return None


def _balanced_close(text: str, i: int, budget: Budget) -> int | None:
    """Index after the ``)`` closing the ``(`` at ``i``, within the scan limit."""
    depth = 0
    end = min(len(text), i + _SCAN_LIMIT)
    for k in range(i, end):
        char = text[k]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                budget.spend(k - i)
                return k + 1
    budget.spend(end - i)
    return None


def _unescape(raw: str) -> str:
    return re.sub(r"\\(.)", r"\1", raw)


def _declared_name(text: str, head_end: int, budget: Budget) -> tuple[bool, str | None]:
    """``(started, name)`` for a declaration whose head ends at ``head_end``.

    ``started`` says a declaration BEGINS here (an opening paren or backtick),
    ``name`` is its literal name when the reader could follow it.
    """
    j = _skip_ws(text, head_end, budget)
    if j >= len(text) or text[j] not in "(`":
        return False, None
    if text[j] == "`":
        return True, None
    k = _skip_ws(text, j + 1, budget)
    if k < len(text) and text[k] in _QUOTES:
        literal = _string_at(text, k, budget)
        return True, None if literal is None else literal[0]
    close = _balanced_close(text, j, budget)  # the it.each([...]) argument list
    if close is None:
        return True, None
    k = _skip_ws(text, close, budget)
    if k >= len(text) or text[k] != "(":
        return True, None
    k = _skip_ws(text, k + 1, budget)
    if k < len(text) and text[k] in _QUOTES:
        literal = _string_at(text, k, budget)
        return True, None if literal is None else literal[0]
    return True, None


def resolve_vitest(
    text: str, rel: str, nodeid: str, budget: Budget, config_issue: str = ""
) -> tuple[str, str]:
    """Existence by reading, and a refusal that never poses as an absence.

    The reader is a scanner, not a parser: a form it cannot follow — a tagged
    template, a table built by a call, a name held in a variable — looks
    exactly like a test that was never written, so it counts what it could not
    follow and answers ``unknown`` instead of ``missing`` when that count is
    not zero.
    """
    name = nodeid.split("::", 1)[1] if "::" in nodeid else nodeid
    started = named = 0
    for head in _VITEST_HEAD.finditer(text):
        budget.spend()
        begun, literal = _declared_name(text, head.end(), budget)
        started += begun
        if literal is None:
            continue
        named += 1
        if _unescape(literal) == name:
            line = text.count("\n", 0, head.start()) + 1
            return RESOLVABLE, f"{BY_SOURCE}: {rel}:{line}"
    if started > named:
        return UNKNOWN, (
            f"{rel} declares {started - named} test(s) in a form this reader "
            f"cannot follow, so the absence of {name} is not established"
        )
    return MISSING, f"{rel} defines no test named {name}"


# --- pytest's own configuration, read as data ---------------------------------

_DEFAULT_PYTHON_OPTIONS = {
    "python_files": ["test_*.py", "*_test.py"],
    "python_classes": ["Test"],
    "python_functions": ["test"],
}
_INI_SECTIONS = {
    "pytest.ini": "pytest",
    "tox.ini": "pytest",
    "setup.cfg": "tool:pytest",
}
CONFIG_NAMES = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
_COLLECT_HOOKS = (
    "pytest_collect",
    "pytest_generate_tests",
    "pytest_pycollect_makeitem",
)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    return str(value).split()


def _options_issue(options: dict[str, Any], where: str) -> str:
    for key, default in _DEFAULT_PYTHON_OPTIONS.items():
        if key in options and sorted(_as_list(options[key])) != sorted(default):
            return f"{where} sets {key}, so pytest's own collection rules differ"
    if any(k in str(options.get("addopts", "")) for k in _DEFAULT_PYTHON_OPTIONS):
        return f"{where} passes python_* options through addopts"
    return ""


def _config_file_issue(name: str, path: str, text: str) -> str:
    import configparser
    import tomllib

    try:
        if name == "pyproject.toml":
            data = tomllib.loads(text)
            options = dict(
                data.get("tool", {}).get("pytest", {}).get("ini_options", {})
            )
        else:
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.read_string(text)
            section = _INI_SECTIONS[name]
            options = dict(parser[section]) if parser.has_section(section) else {}
    except Exception:  # noqa: BLE001 - any failure to parse is "unreadable"
        return f"{path} could not be parsed, so pytest's python_* options are unknown"
    return _options_issue(options, path)


def _conftest_issue(path: str, text: str, budget: Budget) -> str:
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return f"{path} could not be parsed, so its collection hooks are unknown"
    for node in ast.walk(tree):
        budget.spend()
        names = []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names = [node.name]
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
        if any(n.startswith(_COLLECT_HOOKS) for n in names):
            return (
                f"{path} defines a pytest collection hook, which can add or hide tests"
            )
    return ""


def ancestors(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]


def config_issue_for(path: str, aux: dict[str, dict], budget: Budget) -> str:
    """Why pytest's collection at ``path`` cannot be trusted; ``""`` if it can.

    Looks at pytest's configuration files and conftest.py in EVERY directory
    from the file's own up to the root. A file the hub could not read is an
    issue by itself — it may well set python_* — and so is a conftest.py that
    defines a collection hook.
    """
    for directory in ancestors(path):
        for name in (*CONFIG_NAMES, "conftest.py"):
            full = f"{directory}/{name}" if directory else name
            info = aux.get(full)
            state = (info or {}).get("state")
            if state == "missing":
                continue
            if state != "present":
                return f"{full} could not be read, so pytest's collection rules are unknown"
            text = (info or {}).get("text") or ""
            if name == "conftest.py":
                issue = _conftest_issue(full, text, budget)
            else:
                issue = _config_file_issue(name, full, text)
            if issue:
                return issue
    return ""


# --- the request ---------------------------------------------------------------


def analyse(request: dict[str, Any], budget: Budget | None = None) -> dict[str, Any]:
    """Resolve every locator of ``request``; budget exhaustion makes them ``unknown``.

    Request: ``files`` {path: text}, ``aux`` {path: {state, text}} (pytest
    configuration and conftest files around the locator files) and
    ``locators`` [{key, nodeid, runner}]. Response: ``results`` {key: [status,
    reason]}. Each file is parsed once, whatever the number of locators on it.
    """
    budget = budget or Budget()
    files: dict[str, str] = request.get("files") or {}
    aux: dict[str, dict] | None = request.get("aux")
    fixed_issue = str(request.get("config_issue") or "")
    parsed: dict[str, Any] = {}
    issues: dict[str, str] = {}
    results: dict[str, list[str]] = {}
    exhausted = False
    for item in request.get("locators") or []:
        key, nodeid, runner = item["key"], item["nodeid"], item["runner"]
        if exhausted:
            results[key] = [UNKNOWN, OVER_BUDGET]
            continue
        rel = nodeid.split("::", 1)[0]
        try:
            if runner == "pytest":
                if rel not in parsed:
                    parsed[rel] = parse_python(files[rel], rel, budget)
                if rel not in issues:
                    issues[rel] = fixed_issue or (
                        config_issue_for(rel, aux, budget) if aux is not None else ""
                    )
                status, reason = resolve_python(parsed[rel], rel, nodeid, issues[rel])
            elif runner == "vitest":
                status, reason = resolve_vitest(files[rel], rel, nodeid, budget)
            else:
                status, reason = UNKNOWN, NO_RESOLVER.format(runner=runner or "unknown")
        except BudgetExceeded:
            exhausted = True
            status, reason = UNKNOWN, OVER_BUDGET
        results[key] = [status, reason]
    return {"results": results}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        response = analyse(request)
    except Exception as exc:  # noqa: BLE001 - the parent reads any failure as unknown
        response = {"error": type(exc).__name__}
    sys.stdout.write(json.dumps(response))
    return 0


if __name__ == "__main__":
    sys.exit(main())
