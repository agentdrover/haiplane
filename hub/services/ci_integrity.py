"""Does the diff change how CI gets or passes its results? (#1688, variant G)

Even a verified CI run (#1687) executes the BRANCH's code: a branch that edits
``ci.yml``, a script CI calls, ``conftest.py``, the pytest configuration or a
test with a pytest hook can make any diff green. The hub did not look at
whether a diff touches the inputs of CI. This module does, and nothing more:

* a deterministic scan (no model) of the added lines of ``base...sha`` against
  the ONE list of rules below;
* when there are signs, the reviewer is asked one question and answers
  ``explained`` or ``suspicious`` (``MachineReviewSubmit.ci_integrity``);
* the owner gets a short note instead of YAML. Automation (the autopilot, the
  steward, a self-approval) never decides a submission that has signs.

The threat model and the residual risk are in docs/agent-context/invariants.md:
test code of a branch is branch code, and a determined author can forge results
with ordinary code that shows none of these signs. This scan catches the
explicit tricks; the rest is the review of the diff.

Where each question is asked. The diff is read from the forge/clone, so it is
asked BEFORE any write transaction (``ensure_scan``) and stored per
(task, commit). Everything that decides afterwards reads the database only
(``standing`` / ``gap_of``), in particular the verdict transaction rechecks
through ``ci_trust.recheck_in_transaction`` - there is no second mechanism.

Why each rule exists
--------------------
Path rules (a file that CI trusts):

``ci_workflow``   ``.github/**``: workflows and actions are the CI program; the
                  report step and its inputs live there.
``ci_script``     the scripts CI runs (``CI_SCRIPTS``): a changed budget,
                  secret-scan or reporter script decides what CI reports. The
                  list is kept equal to what ci.yml, the actions and
                  ci-report.yml call; tests/test_ci_integrity.py fails when a
                  called script is missing from it.
``ci_makefile``   ``Makefile``: ``make check``/``security`` is what CI mirrors.
                  The whole file, not only the ``security`` target: the diff
                  carries no target boundaries and a narrow match would be a
                  guess.
``conftest``      ``conftest.py`` at any level: pytest imports it before any
                  test and it can rewrite results of the whole run.
``pytest_config`` the ``tool.pytest`` tables of the root ``pyproject.toml``
                  (compared as data, base against head), ANY change to a
                  nested ``pyproject.toml``, and ``pytest.ini``, ``.pytest.ini``,
                  ``pytest.toml``, ``.pytest.toml``, ``tox.ini``, ``setup.cfg`` at
                  any depth: pytest picks the config nearest to the tests it
                  runs (``pytest tests/`` finds ``tests/pytest.ini``), and
                  plugins, addopts and markers change what a run means.
``lockfile``      ``uv.lock``: the resolved environment CI installs.

Line rules (added lines of test files and ``conftest.py`` only):

``pytest_report_hook``  ``pytest_runtest_makereport``, ``pytest_runtest_logreport``,
                  ``pytest_sessionfinish``, ``pytest_terminal_summary``,
                  ``pytest_collection_modifyitems``: the hooks through which a
                  test run can change its own outcome.
``token_read``    ``os.environ`` / ``os.getenv`` of a name with ``HAIPLANE_`` or
                  ``TOKEN``: a test has no business with the hub or CI token.
``report_file``   a name of a CI report file (``hub-ci-results*``,
                  ``mutation-report.json``, ``red-test-baseline.json``,
                  ``audit.json``): the files the reporter forwards as proof.
                  The line shows a name, not a write: that is the reviewer's
                  question.
``hard_exit``     ``os._exit``: ends the run without the reporter noticing.
``detached``      ``start_new_session`` / ``daemon=True``: a process that
                  outlives the test and is not waited for.

A diff that could not be obtained is a sign too (``diff_not_received``): the
answer to "does it touch CI" is "unknown", never "no".
"""

from __future__ import annotations

import ast
import json
import logging
import posixpath
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub import repository_ci_integrity as store
from hub.db import write_transaction
from hub.models import (
    CI_INTEGRITY_EXPLAINED,
    CI_INTEGRITY_NOTE_LIMIT,
    CI_INTEGRITY_SUSPICIOUS,
    FindingLocator,
    MachineFinding,
    MachineReviewSubmit,
    ReviewSeverity,
)

log = logging.getLogger("hub")

# --- the one list --------------------------------------------------------

#: Scripts the CI calls (see the module docstring). A superset is fine; a
#: script that CI calls and this list lacks is a hole, and a test fails on it.
CI_SCRIPTS: frozenset[str] = frozenset(
    {
        "scripts/ci_report_to_hub.py",
        "scripts/ci_report_audit_to_hub.py",
        "scripts/ci_trusted_report.py",
        "scripts/mutation_changed.py",
        # #1767: a plugin loaded by the command of each test part
        # (``-p scripts.pytest_shard``); changing it can hide tests.
        "scripts/pytest_shard.py",
        "scripts/red_test_baseline.py",
        "scripts/complexity_budget.py",
        "scripts/file_size_budget.py",
        "scripts/mcp_catalog_budget.py",
        "scripts/secret_scan.py",
        "scripts/surface_parity.py",
        "deploy/review-drain.sh",
        "deploy/remote-deploy.sh",
        "deploy/docker/compose_smoke.sh",
    }
)

PYTEST_CONFIG_FILES: frozenset[str] = frozenset(
    {"pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg"}
)

RULE_WORKFLOW = "ci_workflow"
RULE_SCRIPT = "ci_script"
RULE_MAKEFILE = "ci_makefile"
RULE_CONFTEST = "conftest"
RULE_PYTEST_CONFIG = "pytest_config"
RULE_LOCKFILE = "lockfile"
RULE_HOOK = "pytest_report_hook"
RULE_ENV_READ = "token_read"
RULE_ENV_COMPUTED = "env_key_computed"
RULE_REPORT_FILE = "report_file"
RULE_EXIT = "hard_exit"
RULE_DETACHED = "detached"
RULE_UNREAD = "text_not_read"

#: Wording of each rule for the reviewer and the owner (Russian: both read it).
RULE_TEXT: dict[str, str] = {
    RULE_WORKFLOW: "входы CI (.github)",
    RULE_SCRIPT: "скрипт, который вызывает CI",
    RULE_MAKEFILE: "Makefile (цели проверок)",
    RULE_CONFTEST: "conftest.py",
    RULE_PYTEST_CONFIG: "настройка pytest",
    RULE_LOCKFILE: "uv.lock",
    RULE_HOOK: "хук pytest, меняющий итог прогона",
    RULE_ENV_READ: "чтение переменной с токеном",
    RULE_ENV_COMPUTED: "ключ переменной окружения вычисляется",
    RULE_REPORT_FILE: "имя файла отчёта CI",
    RULE_EXIT: "os._exit",
    RULE_DETACHED: "процесс без ожидания (start_new_session/daemon)",
    RULE_UNREAD: "текст файла не прочитан (бинарный дифф, не разобран или не получен)",
}

_LINE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        RULE_HOOK,
        re.compile(
            r"\bpytest_(?:runtest_makereport|runtest_logreport|sessionfinish"
            r"|terminal_summary|collection_modifyitems)\b"
        ),
    ),
    (
        RULE_ENV_READ,
        re.compile(
            r"(?:\bos\.environ\b|\bgetenv\s*\(|\benviron\s*[\[.(])"
            r"[^\n]*(?:HAIPLANE_|TOKEN)"
        ),
    ),
    (
        RULE_REPORT_FILE,
        re.compile(
            r"hub-ci-results|mutation-report\.json|red-test-baseline\.json|audit\.json"
        ),
    ),
    (RULE_EXIT, re.compile(r"\bos\._exit\b")),
    (RULE_DETACHED, re.compile(r"\bstart_new_session\b|\bdaemon\s*=\s*True\b")),
)

# --- states --------------------------------------------------------------

CLEAN = "clean"
SIGNS = "signs"
UNREADABLE = "unreadable"
#: Never stored: no row for the commit means the diff was not looked at yet.
UNSCANNED = "unscanned"

#: The rule name for "the diff could not be obtained".
DIFF_NOT_RECEIVED = "diff_not_received"

MAX_SIGNS = 50

QUESTION = (
    "Меняет ли дифф то, как CI получает или передаёт результаты? Если да — "
    "зачем и законно ли это для задачи."
)
ANSWER_SHAPE = (
    '"ci_integrity": {"verdict": "explained|suspicious", "explanation_ru": '
    f'"≤{CI_INTEGRITY_NOTE_LIMIT} знаков: что меняется, зачем, риск '
    '(низкий/высокий)"}'
)
NO_ANSWER_NOTE = "ревьюер не ответил"


@dataclass(frozen=True)
class Sign:
    path: str
    line: int
    rule: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "rule": self.rule}


@dataclass
class _FileDiff:
    old: str = ""
    new: str = ""
    deleted: bool = False
    #: git printed «Binary files differ» / a binary patch instead of text
    binary: bool = False
    #: the block has an ``index a..b`` line (the content changed)
    content_changed: bool = False
    #: rename / mode-only blocks legitimately carry no hunk
    moved: bool = False
    #: (new_start, new_len) of each hunk
    hunks: list[tuple[int, int]] = field(default_factory=list)
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def paths(self) -> list[str]:
        return [p for p in dict.fromkeys((self.old, self.new)) if p]


# --- parsing the diff ----------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of a path (octal bytes, \\\\, \\")."""
    if len(path) < 2 or path[0] != '"' or path[-1] != '"':
        return path
    raw, out, i = path[1:-1], bytearray(), 0
    simple = {"t": 9, "n": 10, '"': 34, "\\": 92}
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and raw[i + 1 : i + 4].isdigit() and len(raw[i + 1 : i + 4]) == 3:
            out.append(int(raw[i + 1 : i + 4], 8))
            i += 4
        elif ch == "\\" and i + 1 < len(raw) and raw[i + 1] in simple:
            out.append(simple[raw[i + 1]])
            i += 2
        else:
            out.extend(ch.encode("utf-8"))
            i += 1
    return out.decode("utf-8", "replace")


def _split_header(rest: str) -> tuple[str, str]:
    """``a/x b/y`` of a ``diff --git`` line. Equal halves win over a guess."""
    if rest.startswith("a/"):
        body, start = rest[2:], 0
        while (i := body.find(" b/", start)) >= 0:
            if body[:i] == body[i + 3 :]:
                return body[:i], body[i + 3 :]
            start = i + 1
        if (i := body.find(" b/")) >= 0:
            return body[:i], body[i + 3 :]
    if rest.startswith('"a/'):
        mid = rest.find('" "b/')
        if mid > 0:
            return _unquote(rest[: mid + 1])[2:], _unquote(rest[mid + 2 :])[2:]
    return rest, rest


def _side(text: str, prefix: str) -> str:
    name = _unquote(text.split("\t")[0])
    return name[len(prefix) :] if name.startswith(prefix) else ""


def _header_line(cur: _FileDiff, line: str) -> None:
    """One line between ``diff --git`` and the first hunk."""
    if line.startswith("rename from "):
        cur.moved = True
        cur.old = _unquote(line[len("rename from ") :])
    elif line.startswith("rename to "):
        cur.new = _unquote(line[len("rename to ") :])
    elif line.startswith("deleted file mode"):
        cur.deleted = True
    elif line.startswith(("old mode ", "copy from ")):
        cur.moved = True
    elif line.startswith("index "):
        cur.content_changed = True
    elif line.startswith(("Binary files ", "GIT binary patch")):
        cur.binary = True
    elif line.startswith("--- ") and line[4:] != "/dev/null":
        cur.old = _side(line[4:], "a/") or cur.old
    elif line.startswith("+++ ") and line[4:] != "/dev/null":
        cur.new = _side(line[4:], "b/") or cur.new


def parse_diff(diff: str) -> list[_FileDiff]:
    """File blocks of a unified diff with old/new paths, hunks and +/- lines."""
    files: list[_FileDiff] = []
    cur: _FileDiff | None = None
    new_no = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            old, new = _split_header(line[len("diff --git ") :])
            cur = _FileDiff(old=old, new=new)
            files.append(cur)
        elif cur is None:
            continue
        elif (m := _HUNK.match(line)) is not None:
            new_no = int(m.group(1))
            cur.hunks.append((new_no, int(m.group(2) or 1)))
        elif not cur.hunks:
            _header_line(cur, line)
        elif line.startswith("+"):
            cur.added.append((new_no, line[1:]))
            new_no += 1
        elif line.startswith("-"):
            cur.removed.append(line[1:])
        elif line.startswith(" "):
            new_no += 1
    return files


# --- the rules -----------------------------------------------------------


def _is_test_file(path: str) -> bool:
    base = posixpath.basename(path)
    return (
        base == "conftest.py"
        or base.startswith("test_")
        or base.endswith("_test.py")
        or path.startswith(("tests/", "test/"))
        or "/tests/" in path
    )


def _path_rule(path: str) -> str | None:
    base = posixpath.basename(path)
    if path.startswith(".github/"):
        return RULE_WORKFLOW
    if path in CI_SCRIPTS:
        return RULE_SCRIPT
    if path == "Makefile":
        return RULE_MAKEFILE
    if base == "conftest.py":
        return RULE_CONFTEST
    if base in PYTEST_CONFIG_FILES:  # at ANY depth, like conftest.py
        return RULE_PYTEST_CONFIG
    if path == "uv.lock":
        return RULE_LOCKFILE
    return None


def _toml_pytest(text: str | None) -> tuple[bool, Any]:
    """(readable, the ``tool.pytest`` subtree) of a pyproject text; "" = no file."""
    if text is None:
        return False, None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False, None
    tool = data.get("tool")
    return True, tool.get("pytest") if isinstance(tool, dict) else None


def pyproject_changes_pytest(head: str | None, base: str | None) -> bool:
    """Does the ``tool.pytest`` subtree differ between base and head? Unread = yes.

    Semantic, not textual: ``[tool."pytest".ini_options]``, dotted and quoted
    keys all parse to the same subtree, so no spelling hides a change.
    """
    head_ok, head_tree = _toml_pytest(head)
    base_ok, base_tree = _toml_pytest(base)
    return not (head_ok and base_ok) or head_tree != base_tree


def _is_environ(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    ) or (isinstance(node, ast.Name) and node.id == "environ")


def _is_env_getter(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "getenv"
    if not isinstance(node, ast.Attribute):
        return False
    if node.attr == "getenv":
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return node.attr == "get" and _is_environ(node.value)


_SECRET_NAME = re.compile(r"HAIPLANE_|TOKEN")


_KEY_ARGS = ("key", "name")


def _env_key(node: ast.Subscript | ast.Call) -> tuple[bool, ast.expr | None]:
    """(is an environment read, its key expression) of a subscript or call."""
    if isinstance(node, ast.Subscript):
        return (True, node.slice) if _is_environ(node.value) else (False, None)
    if not _is_env_getter(node.func):
        return False, None
    if node.args:
        return True, node.args[0]
    named = [k.value for k in node.keywords if k.arg in _KEY_ARGS]
    return True, named[0] if named else None


def env_reads(source: str, added: set[int]) -> list[tuple[int, str]] | None:
    """``(line, rule)`` of environment reads touching ``added`` lines; None = no parse.

    Found in the syntax tree, so ``os.environ[`` / ``"HAIPLANE_..."`` / ``]``
    spread over lines and ``key=`` / ``name=`` keyword forms are seen. A
    constant key naming ``HAIPLANE_``/``TOKEN`` is ``token_read``; a key the
    scan cannot evaluate (a variable, an expression, a splat) is
    ``env_key_computed``: it cannot be proven harmless.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Subscript, ast.Call)):
            continue
        is_read, key = _env_key(node)
        if not is_read:
            continue
        last = getattr(node, "end_lineno", None) or node.lineno
        if not any(node.lineno <= n <= last for n in added):
            continue
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            if _SECRET_NAME.search(key.value):
                found.append((node.lineno, RULE_ENV_READ))
        else:
            found.append((node.lineno, RULE_ENV_COMPUTED))
    return found


def scan_diff(
    diff: str,
    head: Mapping[str, str | None] | None = None,
    base: Mapping[str, str | None] | None = None,
) -> list[Sign]:
    """Signs in a unified diff (``-U0`` is enough). Deterministic, no model.

    ``head`` / ``base`` map a path to its text at the head / base commit, for
    the files that need more than the diff: ``pyproject.toml`` (semantic
    compare) and the changed ``.py`` test files (syntax tree). A missing key or
    a None value means «not read», which is a sign, never «clean». Both sides
    of a rename and a deletion count.
    """
    head, base = head or {}, base or {}
    signs: list[Sign] = []
    seen: set[tuple[str, int, str]] = set()

    def add(path: str, line: int, rule: str) -> None:
        key = (path, line, rule)
        if key not in seen:
            seen.add(key)
            signs.append(Sign(path, line, rule))

    for fd in parse_diff(diff):
        for path in fd.paths():
            rule = _path_rule(path)
            if rule:
                add(path, 0, rule)
        nested = [p for p in fd.paths() if p.endswith("/pyproject.toml")]
        if nested:  # not the root one: not compared, any change is a sign
            add(nested[0], 0, RULE_PYTEST_CONFIG)
        if "pyproject.toml" in fd.paths() and (
            fd.deleted
            or pyproject_changes_pytest(
                head.get("pyproject.toml"), base.get("pyproject.toml")
            )
        ):
            add("pyproject.toml", fd.hunks[0][0] if fd.hunks else 0, RULE_PYTEST_CONFIG)
        if not any(_is_test_file(p) for p in fd.paths()):
            continue
        path = fd.new or fd.old
        if fd.binary or (not fd.hunks and fd.content_changed and not fd.moved):
            add(path, 0, RULE_UNREAD)
        _scan_lines(fd, path, head.get(path), add)
    return signs[:MAX_SIGNS]


def _scan_lines(fd: _FileDiff, path: str, text: str | None, add: Any) -> None:
    """The line rules on the ADDED lines of one test file; the syntax tree for env reads."""
    reads: list[tuple[int, str]] | None = None
    if path.endswith(".py") and fd.added and not fd.deleted:
        reads = env_reads(text, {n for n, _ in fd.added}) if text is not None else None
        if reads is None:
            add(
                path, 0, RULE_UNREAD
            )  # unparseable or unread: a sign, regex as fallback
        for line, rule in reads or []:
            add(path, line, rule)
    for no, line_text in fd.added:
        for rule, pattern in _LINE_RULES:
            if rule == RULE_ENV_READ and reads is not None:
                continue
            if pattern.search(line_text):
                add(path, no, rule)


# --- the stored scan and what it means -----------------------------------


@dataclass(frozen=True)
class Scan:
    state: str
    signs: tuple[Sign, ...] = ()
    reason: str = ""


def _from_row(row: dict[str, Any]) -> Scan:
    try:
        raw = json.loads(row.get("signs") or "[]")
    except ValueError:
        raw = []
    signs = tuple(
        Sign(str(s.get("path", "")), int(s.get("line") or 0), str(s.get("rule", "")))
        for s in raw
        if isinstance(s, dict)
    )
    return Scan(str(row["state"]), signs, str(row.get("reason") or ""))


async def _read_text(git: Any, repo_path: str, ref: str, path: str) -> str | None:
    """Text of ``path`` at an exact commit: "" when the file is absent, None when unread."""
    got = await git.read_file_at_ref(repo_path, ref, path, limit_chars=400_000)
    if got.get("state") == "missing":
        return ""
    if got.get("state") != "present" or got.get("truncated"):
        return None
    return str(got.get("content") or "")


async def _scan_now(db: aiosqlite.Connection, task_id: int, sha: str) -> Scan:
    """Read the diff of the pinned commit and scan it. NETWORK: never inside a write transaction."""
    from hub.integrations.registry import plugins
    from hub.services.orchestration import project_git_context

    try:
        ctx = await project_git_context(db, task_id)
        workspace = (ctx.get("repo") or "").strip()
        base = (ctx.get("base_branch") or config.PAIR_BASE_BRANCH).strip()
        if not workspace or not base:
            return Scan(UNREADABLE, reason="клон проекта на хабе не настроен")
        git = plugins.git_ops
        material = await git.integrity_read(workspace, base, sha)
        if material is None:
            return Scan(UNREADABLE, reason="git не отдал дифф сдачи")
        if (material.get("head_sha") or "").lower() != sha.lower():
            return Scan(UNREADABLE, reason="дифф относится к другому коммиту")
        diff = material["diff"]
        head: dict[str, str | None] = {}
        base_texts: dict[str, str | None] = {}
        for fd in parse_diff(diff):
            path = fd.new or fd.old
            if fd.deleted:
                continue
            if path == "pyproject.toml":
                head[path] = await _read_text(git, workspace, sha, path)
                base_texts[path] = await _read_text(
                    git, workspace, material["base_sha"], path
                )
            elif _is_test_file(path) and path.endswith(".py") and fd.added:
                head[path] = await _read_text(git, workspace, sha, path)
    except Exception as exc:  # noqa: BLE001 - an unreadable diff is the answer
        log.warning(
            "ci integrity: could not read the diff of task #%s: %s", task_id, exc
        )
        return Scan(UNREADABLE, reason="дифф не получен: ошибка чтения")
    signs = scan_diff(diff, head, base_texts)
    return Scan(SIGNS if signs else CLEAN, tuple(signs))


async def _stored(db: aiosqlite.Connection, task_id: int, sha: str) -> Scan | None:
    row = await store.get_ci_integrity_scan(db, task_id, sha)
    return _from_row(row) if row else None


async def ensure_scan(db: aiosqlite.Connection, task_id: int) -> Scan:
    """The scan of the commit the task is submitted at; made now when missing.

    Asks the clone (network) BEFORE its own short write, so call it outside a
    write transaction: inside one it only reads what is stored and otherwise
    answers ``unscanned``. An unreadable result is retried on the next call.
    """
    task = dict(await repo.get_task(db, task_id) or {})
    sha = (task.get("submission_sha") or "").strip()
    if not sha:
        return Scan(UNREADABLE, reason="коммит сдачи не закреплён")
    stored = await _stored(db, task_id, sha)
    if stored is not None and stored.state != UNREADABLE:
        return stored
    if db.in_transaction:
        log.warning(
            "ci integrity: scan of task #%s asked inside a transaction", task_id
        )
        return stored or Scan(UNSCANNED)
    scan = await _scan_now(db, task_id, sha)
    async with write_transaction(db):
        await store.save_ci_integrity_scan(
            db,
            task_id,
            sha,
            scan.state,
            json.dumps([s.as_dict() for s in scan.signs]),
            scan.reason,
        )
    return scan


@dataclass(frozen=True)
class Standing:
    """What the hub knows now about the CI integrity of the current submission."""

    scan: Scan
    answer: str = ""
    explanation: str = ""

    @property
    def clean(self) -> bool:
        return self.scan.state == CLEAN

    @property
    def has_signs(self) -> bool:
        """Anything but a proven-clean diff: unread and unscanned count."""
        return self.scan.state != CLEAN

    @property
    def gap(self) -> str | None:
        """Why automation may not decide this submission, or None."""
        state = self.scan.state
        if state == CLEAN:
            return None
        if state == UNSCANNED:
            return "целостность CI не проверялась: дифф сдачи не просканирован"
        if state == UNREADABLE:
            return f"целостность CI не проверена: дифф не получен ({self.scan.reason})"
        if self.answer == CI_INTEGRITY_SUSPICIOUS:
            return "дифф меняет входы CI, ревьюер подозревает подделку"
        if self.answer == CI_INTEGRITY_EXPLAINED:
            return "дифф меняет входы CI: справка владельцу, автоматика не решает"
        return f"дифф меняет входы CI, {NO_ANSWER_NOTE}"

    @property
    def reason(self) -> str:
        """Why automation stops and what the owner reads: the gap, then the note."""
        return f"{self.gap}. Справка: {self.note}" if self.gap else ""

    @property
    def note(self) -> str:
        """The owner's note, at most 600 characters."""
        if self.scan.state == CLEAN:
            return ""
        if self.scan.state in (UNREADABLE, UNSCANNED):
            return self.gap or ""
        if self.explanation:
            return self.explanation[:CI_INTEGRITY_NOTE_LIMIT]
        names = ", ".join(
            sorted({RULE_TEXT.get(s.rule, s.rule) for s in self.scan.signs})
        )
        return f"{NO_ANSWER_NOTE.capitalize()}. Признаки: {names}."[
            :CI_INTEGRITY_NOTE_LIMIT
        ]


async def standing_of(db: aiosqlite.Connection, task: dict[str, Any]) -> Standing:
    """Database-only: the stored scan of the task's commit and the reviewer's answer."""
    sha = (task.get("submission_sha") or "").strip()
    if not sha:
        return Standing(Scan(UNREADABLE, reason="коммит сдачи не закреплён"))
    scan = await _stored(db, int(task["id"]), sha) or Scan(UNSCANNED)
    if scan.state != SIGNS:
        return Standing(scan)
    answer = await store.get_ci_integrity_answer(
        db, int(task["id"]), int(task.get("submission_generation") or 0)
    )
    if answer is None:
        return Standing(scan)
    return Standing(
        scan,
        answer["ci_integrity_verdict"],
        answer["ci_integrity_explanation"],
    )


async def gap_of(db: aiosqlite.Connection, task: dict[str, Any]) -> str | None:
    """Database-only refusal text for automation, or None (the recheck uses it)."""
    return (await standing_of(db, task)).gap


FINDING_CATEGORY = "ci_integrity"


def with_suspicion_finding(body: MachineReviewSubmit) -> MachineReviewSubmit:
    """``suspicious`` is a confirmed high finding with the reviewer's own text.

    The finding then travels the road of any finding: the autopilot refuses a
    report with confirmed findings, the verdict goes CHANGES_REQUESTED-way. Added
    once (a report that already carries one is left alone), and the same for
    every path a report arrives by, because they all end in this model.
    """
    answer = body.ci_integrity
    if answer is None or answer.verdict != CI_INTEGRITY_SUSPICIOUS:
        return body
    if any(f.category == FINDING_CATEGORY for f in body.findings_confirmed):
        return body
    finding = MachineFinding(
        title="Дифф подозрительно меняет входы CI",
        severity=ReviewSeverity.high,
        category=FINDING_CATEGORY,
        locator=FindingLocator.none,
        detail=answer.explanation_ru or "ревьюер ответил suspicious без пояснения",
    )
    return body.model_copy(
        update={"findings_confirmed": [*body.findings_confirmed, finding]}
    )


async def scan_for_automated_approval(state: Any) -> None:
    """Verdict step: read the diff of an automated APPROVED before the write lock.

    The transaction that records the verdict only rereads the stored scan
    (``lifecycle._automation_ci_gate``, through ``ci_trust.recheck_in_transaction``).
    ``state`` is the verdict context; it is duck-typed to keep this module free
    of the lifecycle import.
    """
    if is_automation(state) and state.body.verdict.value == "approved":
        await ensure_scan(state.db, state.task_id)


def is_automation(state: Any) -> bool:
    """Who is not a human reading the note: steward, autopilot, an agent approving its own work.

    A self-approval (``REVIEW_SELF_APPROVE=allow``) by an agent token is
    automation for THIS check; a human caller stays free to approve.
    """
    from hub.services.auto_verdict import _POLICY_ACTOR

    return bool(
        state.applied_by_steward
        or state.body.agent == _POLICY_ACTOR
        or (state.self_approved and state.agent_caller)
    )


async def verdict_gate(state: Any) -> None:
    """In the verdict transaction: an automated APPROVED of signs is refused (422).

    Reads the database only; run through ``ci_trust.recheck_in_transaction``.
    """
    if not is_automation(state) or state.body.verdict.value != "approved":
        return
    task = dict(await repo.get_task(state.db, state.task_id) or {})
    if gap := await gap_of(state.db, task):
        raise HTTPException(422, f"ci_integrity: {gap}")


def stored_answer(body: MachineReviewSubmit) -> tuple[str, str]:
    """(verdict, explanation) as stored; ("", "") when the reviewer did not answer."""
    answer = body.ci_integrity
    return (answer.verdict, answer.explanation_ru) if answer else ("", "")


# --- surfaces: reviewer order, brief ------------------------------------


def sign_lines(signs: tuple[Sign, ...] | list[Sign]) -> list[str]:
    """``path:line — правило`` per sign, as the reviewer and the owner read it."""
    return [
        f"{s.path}{':' + str(s.line) if s.line else ''} — "
        f"{RULE_TEXT.get(s.rule, s.rule)} [{s.rule}]"
        for s in signs
    ]


def section_for(standing: Standing) -> str:
    """The section of the reviewer's order; empty when the diff shows no signs."""
    if standing.clean:
        return ""
    if standing.scan.state in (UNREADABLE, UNSCANNED):
        listing = f"- {DIFF_NOT_RECEIVED}: {standing.gap}"
    else:
        listing = "\n".join(f"- {line}" for line in sign_lines(standing.scan.signs))
    return (
        "## Целостность CI (механическая проверка диффа)\n"
        "Хаб нашёл признаки, что дифф трогает входы CI:\n"
        f"{listing}\n\n"
        f"Вопрос: {QUESTION}\n"
        f"Ответь в отчёте (и в текстовом блоке тоже) ключом верхнего уровня {ANSWER_SHAPE}. «explained» — изменение законно "
        "для задачи; «suspicious» — похоже на подделку результатов (тогда это "
        "находка high). Отчёт без этого поля читается как «ревьюер не ответил»: "
        "автоматика вердикт не вынесет."
    )


async def order_section(db: aiosqlite.Connection, task_id: int) -> str:
    """The reviewer-order section for this submission; "" when the diff is clean."""
    await ensure_scan(db, task_id)
    task = dict(await repo.get_task(db, task_id) or {})
    section = section_for(await standing_of(db, task))
    return f"{section}\n\n" if section else ""


async def brief_state(db: aiosqlite.Connection, task_id: int) -> dict[str, Any] | None:
    """The brief's block: None when there are no signs (the brief reads as before)."""
    await ensure_scan(db, task_id)
    task = dict(await repo.get_task(db, task_id) or {})
    standing = await standing_of(db, task)
    if standing.clean:
        return None
    return {
        "state": standing.scan.state,
        "signs": [s.as_dict() for s in standing.scan.signs],
        "reason": standing.scan.reason,
        "question": QUESTION,
        "answer": standing.answer,
        "note": standing.note,
        "gap": standing.gap or "",
    }
