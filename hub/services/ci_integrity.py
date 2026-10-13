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
``pytest_config`` ``[tool.pytest*]`` in ``pyproject.toml`` (and ``pytest.ini``,
                  ``tox.ini``, ``setup.cfg``): plugins, addopts and markers
                  change what a run means.
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

import json
import logging
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

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
    {"pytest.ini", "tox.ini", "setup.cfg", ".pytest.ini"}
)

RULE_WORKFLOW = "ci_workflow"
RULE_SCRIPT = "ci_script"
RULE_MAKEFILE = "ci_makefile"
RULE_CONFTEST = "conftest"
RULE_PYTEST_CONFIG = "pytest_config"
RULE_LOCKFILE = "lockfile"
RULE_HOOK = "pytest_report_hook"
RULE_ENV_READ = "token_read"
RULE_REPORT_FILE = "report_file"
RULE_EXIT = "hard_exit"
RULE_DETACHED = "detached"

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
    RULE_REPORT_FILE: "имя файла отчёта CI",
    RULE_EXIT: "os._exit",
    RULE_DETACHED: "процесс без ожидания (start_new_session/daemon)",
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


def parse_diff(diff: str) -> list[_FileDiff]:
    """File blocks of a unified diff with old/new paths, hunks and +/- lines."""
    files: list[_FileDiff] = []
    cur: _FileDiff | None = None
    in_hunk, new_no = False, 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            old, new = _split_header(line[len("diff --git ") :])
            cur = _FileDiff(old=old, new=new)
            files.append(cur)
            in_hunk = False
            continue
        if cur is None:
            continue
        if not in_hunk:
            if line.startswith("rename from "):
                cur.old = _unquote(line[len("rename from ") :])
            elif line.startswith("rename to "):
                cur.new = _unquote(line[len("rename to ") :])
            elif line.startswith("deleted file mode"):
                cur.deleted = True
            elif line.startswith("--- ") and line[4:] != "/dev/null":
                cur.old = _side(line[4:], "a/") or cur.old
            elif line.startswith("+++ ") and line[4:] != "/dev/null":
                cur.new = _side(line[4:], "b/") or cur.new
            elif (m := _HUNK.match(line)) is not None:
                in_hunk = True
                new_no = int(m.group(1))
                cur.hunks.append((new_no, int(m.group(2) or 1)))
            continue
        if (m := _HUNK.match(line)) is not None:
            new_no = int(m.group(1))
            cur.hunks.append((new_no, int(m.group(2) or 1)))
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
    if path in PYTEST_CONFIG_FILES:
        return RULE_PYTEST_CONFIG
    if path == "uv.lock":
        return RULE_LOCKFILE
    return None


_PYTEST_HEADER = re.compile(r"^\s*\[\[?tool\.pytest[^\]]*\]\]?")
_ANY_HEADER = re.compile(r"^\s*\[")


def pytest_sections(text: str) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of the ``[tool.pytest*]`` tables."""
    ranges: list[tuple[int, int]] = []
    start = 0
    for no, line in enumerate(text.splitlines(), 1):
        if _ANY_HEADER.match(line):
            if start:
                ranges.append((start, no - 1))
                start = 0
            if _PYTEST_HEADER.match(line):
                start = no
    if start:
        ranges.append((start, len(text.splitlines())))
    return ranges


def _pyproject_touches_pytest(fd: _FileDiff, text: str | None) -> bool:
    """Do the changed lines of ``pyproject.toml`` sit in a ``[tool.pytest*]`` table?

    A pure deletion (new length 0) is positioned by the line after which it
    happened, so it counts when that line is inside a table or on its edge. The
    removal of a whole table is seen by its header among the removed lines.
    """
    if fd.deleted:
        return True
    if any(_PYTEST_HEADER.match(t) for t in fd.removed):
        return True
    if any(_PYTEST_HEADER.match(t) for _, t in fd.added):
        return True
    if text is None:
        return True  # unread file: unknown is never "no"
    for start, length in fd.hunks:
        lo, hi = (start, start + length - 1) if length else (start, start + 1)
        if any(lo <= end and hi >= begin for begin, end in pytest_sections(text)):
            return True
    return False


def scan_diff(diff: str, pyproject_text: str | None = None) -> list[Sign]:
    """Signs in a unified diff (``-U0`` is enough). Deterministic, no model.

    Both sides of a rename and a deletion count: moving ``conftest.py`` away or
    deleting a CI script changes CI as much as editing it.
    """
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
        if "pyproject.toml" in fd.paths() and _pyproject_touches_pytest(
            fd, pyproject_text
        ):
            add("pyproject.toml", fd.hunks[0][0] if fd.hunks else 0, RULE_PYTEST_CONFIG)
        if not any(_is_test_file(p) for p in fd.paths()):
            continue
        path = fd.new or fd.old
        for no, text in fd.added:
            for rule, pattern in _LINE_RULES:
                if pattern.search(text):
                    add(path, no, rule)
    return signs[:MAX_SIGNS]


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
        diff = await plugins.git_ops.branch_diff(workspace, base, sha, 0)
        if diff is None:
            return Scan(UNREADABLE, reason="git не отдал дифф сдачи")
        text = None
        if "pyproject.toml" in diff:
            text = await plugins.git_ops.file_at_ref(workspace, sha, "pyproject.toml")
    except Exception as exc:  # noqa: BLE001 - an unreadable diff is the answer
        log.warning(
            "ci integrity: could not read the diff of task #%s: %s", task_id, exc
        )
        return Scan(UNREADABLE, reason="дифф не получен: ошибка чтения")
    signs = scan_diff(diff, text)
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
    from hub.services.auto_verdict import _POLICY_ACTOR

    automated = state.applied_by_steward or state.body.agent == _POLICY_ACTOR
    if automated and state.body.verdict.value == "approved":
        await ensure_scan(state.db, state.task_id)


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
