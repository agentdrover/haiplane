"""Static existence check for AC test locators (#506).

Complements #505: given a verifiable_by=test AC with a valid pytest locator,
check whether that test is written, by READING the file as of one git commit —
no checkout, no import, no pytest. A task branch is whatever an agent pushed,
so collecting its tests (``pytest --collect-only`` imports conftest.py and
every plugin) would run that agent's code on the hub host with the hub's
environment; #1650 removed that path.

The files are hostile input too, so the analysis itself does not run in the hub
process: the texts are read through git within a byte budget and handed to
``hub.services.locator_worker`` in a separate interpreter with hard CPU and
memory limits and a wall-clock deadline. A worker that overruns, crashes or
answers garbage makes every locator of the request ``unknown`` — never
``missing``. The result (resolvable / missing / unknown) makes a broken AC↔test
binding a visible defect in the review brief instead of a silent hole.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import shutil
import sys
import tempfile
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hub.process_kill import kill_process_group
from hub.services import locator_worker
from hub.services.locator_worker import (
    BY_SOURCE,
    MISSING,
    NO_RESOLVER,
    OVER_BUDGET,
    PARAM_NOT_CHECKED,
    RESOLVABLE,
    UNKNOWN,
    UNPARSEABLE,
)
from hub.services.test_locator import PYTEST, parse_test_locator, runner_of

log = logging.getLogger("hub")

__all__ = [
    "BY_SOURCE",
    "MISSING",
    "NO_RESOLVER",
    "OVER_BUDGET",
    "PARAM_NOT_CHECKED",
    "RESOLVABLE",
    "UNKNOWN",
    "UNPARSEABLE",
]

# The whole status vocabulary of this calculation, named once so a reader of
# the answer can check it decomposed ALL of it and not just the statuses it
# happened to think of. Anyone adding a status here has to look at who
# consumes it (#1158): a consumer that folds unnamed statuses into its own
# default silently turns a new "could not look" into an accusation.
LOCATOR_STATUSES: tuple[str, ...] = (RESOLVABLE, MISSING, UNKNOWN, UNPARSEABLE)

# ``missing`` answers two different questions and the reason is the only thing
# that tells them apart. NO_VALID_LOCATOR means nothing resolvable was NAMED —
# the hub never got as far as looking. Any other ``missing`` reason names the
# part of the definition path the file does not contain: a locator was named,
# the hub read the file, and the test is not there. Reading the first as the
# second accuses an author of a missing test they never claimed to have
# written (#1158).
NO_VALID_LOCATOR = "no valid test locator in test_ref"


def _verifiable_by(ac: Any) -> str:
    vb = getattr(ac, "verifiable_by", None)
    # Обещали str, а при отсутствующем поле отдавали None: сравнение с "test"
    # молча давало False, а .lower() у вызывающего дал бы AttributeError.
    return str(getattr(vb, "value", vb) or "")


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


# --- the analysis, in process: for unit tests and callers that hold small text --
#
# Production goes through ``resolve_locators_at_ref`` (and so through the
# worker process). These helpers run the same analysis in the calling process
# under the same operation budget, which is only appropriate for text the
# caller wrote itself.


def _analyse_here(request: dict[str, Any]) -> dict[str, list[str]] | None:
    try:
        return locator_worker.analyse(request)["results"]
    except Exception:  # noqa: BLE001 - nothing may escape as an accusation
        log.warning("in-process locator analysis failed", exc_info=True)
        return None


def resolve_locator_in_source(
    text: str | None, nodeid: str, *, config_issue: str = "", unread: str = ""
) -> tuple[str, str]:
    """``(status, reason)`` from the file's own text — no imports, no runner (#764).

    ``text`` is the file as of the commit the caller resolved; ``None`` means
    it could not be read (``unread`` says why), which is ``unknown`` and never
    ``missing``. ``config_issue`` is a reason pytest's collection options make
    a positive answer unreliable. Reading is per runner (#1203).
    """
    rel = nodeid.split("::", 1)[0]
    if text is None:
        return UNKNOWN, unread or f"could not read {rel} at the resolved ref"
    request = {
        "files": {rel: text},
        "config_issue": config_issue,
        "locators": [{"key": "0", "nodeid": nodeid, "runner": runner_of(nodeid)}],
    }
    results = _analyse_here(request)
    if results is None:
        return UNKNOWN, OVER_BUDGET
    status, reason = results["0"]
    return status, reason


# One resolver per runner the locator registry accepts. The pairing is not
# decoration: a shape accepted upstream with no entry here is exactly the
# state where the hub reports a real test as absent, so the two registries are
# checked against each other by test (#1203).
SOURCE_RESOLVERS = {
    PYTEST: resolve_locator_in_source,
    "vitest": resolve_locator_in_source,
}


def _plan(
    acs: Any,
    sources: dict[str, str | None],
    absent_files: set[str],
    unread: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    """Rows for every test-AC, and the locators that still need the analysis."""
    rows: list[dict] = []
    todo: list[dict] = []
    for ac in acs:
        if _verifiable_by(ac) != "test":
            continue
        locator = getattr(ac, "test_ref", None)
        parsed = parse_test_locator(locator)
        row = {
            "ac_id": getattr(ac, "id", "?"),
            "locator": locator,
            "status": UNKNOWN,
            "reason": "",
        }
        if parsed is None:
            row["status"], row["reason"] = MISSING, NO_VALID_LOCATOR
        elif parsed[0] in absent_files:
            row["status"], row["reason"] = resolve_locator_absent_file(parsed[1])
        elif sources.get(parsed[0]) is None:
            rel = parsed[0]
            row["reason"] = (
                unread.get(rel) or f"could not read {rel} at the resolved ref"
            )
        else:
            row["key"] = str(len(todo))
            todo.append(
                {"key": row["key"], "nodeid": parsed[1], "runner": runner_of(parsed[1])}
            )
        rows.append(row)
    return rows, todo


def _finish(
    rows: list[dict], results: dict[str, list[str]] | None, ref_label: str
) -> list[dict]:
    for row in rows:
        key = row.pop("key", None)
        if key is not None:
            status, reason = (results or {}).get(key) or (UNKNOWN, OVER_BUDGET)
            row["status"], row["reason"] = status, reason
        if ref_label and parse_test_locator(row["locator"]) is not None:
            row["reason"] = f"{row['reason']} [ref: {ref_label}]"
    return rows


def resolve_ac_locators(
    acs: Any,
    sources: dict[str, str | None] | None = None,
    absent_files: set[str] | None = None,
    *,
    ref_label: str = "",
    pytest_config_issue: str = "",
    unread: dict[str, str] | None = None,
) -> list[dict]:
    """Resolution status for each verifiable_by=test AC, in process (#506, #764).

    ``sources`` is the text of each file named by a locator, as of one resolved
    commit. A file the caller could not read maps to ``None`` (``unread`` says
    why) and stays ``unknown``, because "could not check" must never be
    reported as "checked and clean". ``absent_files`` are the files the commit
    demonstrably does not contain. Non-test AC are skipped. ``ref_label`` is
    appended to every reason so a reader knows WHICH code the answer is about.
    """
    sources = sources or {}
    rows, todo = _plan(acs, sources, absent_files or set(), unread or {})
    results = None
    if todo:
        files = {
            p: sources[p]
            for p in (t["nodeid"].split("::", 1)[0] for t in todo)
            if sources.get(p) is not None
        }
        results = _analyse_here(
            {"files": files, "config_issue": pytest_config_issue, "locators": todo}
        )
    return _finish(rows, results, ref_label)


# --- the worker process ----------------------------------------------------------

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_PATH_DEPTH = 12
MAX_READS = 60
_READ_DEADLINE_SECONDS = 30
_CACHE_SIZE = 128
_WORKER_WALL_SECONDS = 8
_WORKER_CPU_SECONDS = 5
_WORKER_ADDRESS_SPACE = 512 * 1024 * 1024
_WORKER_MAX_OUTPUT = 1024 * 1024
_WORKER_FILE = Path(locator_worker.__file__).resolve()

# Runs inside the child before the worker: hard limits first, then the worker
# file itself as a script. The ``hub`` package is deliberately NOT imported —
# ``hub.services`` loads the whole hub and would eat the CPU budget before the
# first byte is read. RLIMIT_AS cannot always be lowered (macOS refuses), which
# is why the wall clock and the CPU limit are separate layers.
_BOOTSTRAP = """
import resource, runpy, sys
for name, value in (
    ("RLIMIT_CPU", {cpu}),
    ("RLIMIT_AS", {address_space}),
    ("RLIMIT_CORE", 0),
):
    try:
        resource.setrlimit(getattr(resource, name), (value, value))
    except (ValueError, OSError):
        pass
runpy.run_path(sys.argv[1], run_name="__main__")
""".format(cpu=_WORKER_CPU_SECONDS, address_space=_WORKER_ADDRESS_SPACE)


async def _exchange(proc: Any, payload: bytes) -> bytes:
    """Feed the request, read the answer up to a cap, reap the worker."""

    async def feed() -> None:
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    feeder = asyncio.create_task(feed())
    try:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > _WORKER_MAX_OUTPUT:
                raise ValueError("worker output over the cap")
            chunks.append(chunk)
        await proc.wait()
        return b"".join(chunks)
    finally:
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await feeder


MAX_WORKERS = 2
_slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _worker_slots() -> asyncio.Semaphore:
    """At most ``MAX_WORKERS`` analysis processes at once, per event loop."""
    loop = asyncio.get_running_loop()
    if loop not in _slots:
        _slots[loop] = asyncio.Semaphore(MAX_WORKERS)
    return _slots[loop]


async def analyse_in_worker(request: dict[str, Any]) -> dict[str, list[str]] | None:
    """Run ``request`` through the locator worker; ``None`` on any failure.

    At most ``MAX_WORKERS`` run at a time: a burst of briefs queues here instead
    of forking a process per request.

    Own interpreter (``-I -B``: no site customisation, no bytecode), an empty
    temporary directory as cwd (never the task's repository), a minimal
    environment, CPU and address-space limits, and a wall-clock deadline after
    which the whole process group is killed. A crash, a timeout, a non-zero
    exit or an answer that is not the expected JSON all give ``None`` — the
    caller turns that into ``unknown`` for every locator of the request.
    """
    async with _worker_slots():
        return await _run_worker(request)


async def _run_worker(request: dict[str, Any]) -> dict[str, list[str]] | None:
    payload = json.dumps(request).encode()
    workdir = tempfile.mkdtemp(prefix="hub-locator-")
    proc = None
    pgid = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            "-c",
            _BOOTSTRAP,
            str(_WORKER_FILE),
            cwd=workdir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            start_new_session=True,
        )
        pgid = proc.pid
        out = await asyncio.wait_for(
            _exchange(proc, payload), timeout=_WORKER_WALL_SECONDS
        )
    except (OSError, TimeoutError, asyncio.TimeoutError, ValueError):
        await kill_process_group(proc, pgid=pgid)
        log.warning("locator worker did not finish within its budget")
        return None
    except asyncio.CancelledError:
        await kill_process_group(proc, pgid=pgid)
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if proc.returncode != 0:
        log.warning("locator worker exited with %s", proc.returncode)
        return None
    try:
        results = json.loads(out)["results"]
        if not isinstance(results, dict):
            raise TypeError
        return {str(k): [str(v[0]), str(v[1])] for k, v in results.items()}
    except (ValueError, KeyError, TypeError, IndexError):
        log.warning("locator worker answered something that is not its JSON")
        return None


# --- reading the files: one commit sha, through git only, within a byte budget --


@dataclass
class LocatorEvidence:
    """What the locator files said at ONE commit (#1650).

    ``ref_label`` names the commit and where it came from (``submission_sha``,
    the task branch, the project base); empty when nothing could be resolved,
    in which case every source is ``None`` and ``why`` says why. ``unread``
    names the reason per file that could not be used. ``aux`` holds the pytest
    configuration and conftest files around the locator files (``None`` = not
    collected), and ``pytest_config_issue`` is a fixed reason a positive pytest
    answer cannot be given.
    """

    sources: dict[str, str | None] = field(default_factory=dict)
    absent: set[str] = field(default_factory=set)
    ref_label: str = ""
    why: str = ""
    pytest_config_issue: str = ""
    unread: dict[str, str] = field(default_factory=dict)
    aux: dict[str, dict] | None = None


async def _commit_of(git: Any, repo: str, name: str) -> str:
    """The commit sha ``name`` points at, remote-first; ``""`` when it cannot be told."""
    sha = await git.head_sha(repo, name)
    if sha:
        return sha
    state, detail = await git.resolve_ref(name, repo)
    return detail if state == "resolved" and _is_sha(detail) else ""


def _is_sha(value: str) -> bool:
    return len(value or "") == 40 and all(c in "0123456789abcdef" for c in value)


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


class _Reader:
    """Reads files at one commit and keeps count of the bytes it was allowed."""

    def __init__(self, git: Any, repo: str, sha: str):
        self.git, self.repo, self.sha = git, repo, sha
        self.spent = 0
        self.reads = 0

    async def _call(self, path: str, limit_chars: int) -> dict[str, Any]:
        try:
            return await self.git.read_file_at_ref(
                self.repo, self.sha, path, limit_chars=limit_chars
            )
        except Exception:  # noqa: BLE001 - one unreadable file is one unknown
            log.warning("locator source read failed: %s", path)
            return {"state": "unreadable"}

    async def read(self, path: str) -> tuple[str, str, str]:
        """``(state, text, why)``; state is present | missing | unreadable | toobig.

        The size is learned BEFORE the content is taken (a zero-length probe
        reads a few bytes at most), so a file over the per-file limit or over
        what is left of the request is never read at all.
        """
        if self.reads >= MAX_READS:
            return (
                "toobig",
                "",
                (
                    f"{path} was not read: this request already used its {MAX_READS} "
                    "file reads"
                ),
            )
        self.reads += 1
        probe = await self._call(path, 0)
        state = probe.get("state")
        if state == "missing":
            return "missing", "", ""
        if state != "present":
            return "unreadable", "", f"could not read {path}"
        size = int(probe.get("size") or 0)
        if size > MAX_FILE_BYTES:
            return (
                "toobig",
                "",
                (
                    f"{path} is {size} bytes, over the {MAX_FILE_BYTES} byte limit "
                    "for static reading"
                ),
            )
        if self.spent + size > MAX_TOTAL_BYTES:
            return (
                "toobig",
                "",
                (
                    f"{path} would take this request past the {MAX_TOTAL_BYTES} byte "
                    "limit for static reading"
                ),
            )
        if not size:
            return "present", "", ""
        info = await self._call(path, size)
        if info.get("state") != "present" or info.get("truncated"):
            return "unreadable", "", f"could not read {path}"
        self.spent += size
        return "present", info.get("content", ""), ""


def _aux_paths(files: list[str]) -> list[str] | None:
    """Config and conftest candidates in every directory above the pytest files."""
    paths: list[str] = []
    for path in files:
        if runner_of(path) != PYTEST:
            continue
        if path.count("/") > MAX_PATH_DEPTH:
            return None
        for directory in locator_worker.ancestors(path):
            for name in (*locator_worker.CONFIG_NAMES, "conftest.py"):
                full = f"{directory}/{name}" if directory else name
                if full not in paths:
                    paths.append(full)
    return paths


async def read_locator_evidence(
    git: Any,
    repo: str | None,
    files: list[str],
    *,
    submission_sha: str = "",
    branch: str = "",
    base: str = "",
    picked: tuple[str, str] | None = None,
) -> LocatorEvidence:
    """Read the locator files at one commit, never running anything (#1650).

    Everything goes through git by commit sha: no checkout, no import, no
    environment handed over. The tree is listed ONCE; which files exist, and
    which pytest configuration and conftest.py files stand above the locator
    files, is read off that list, and only files that exist are read. Failure
    is ``unknown`` for the file, never ``missing``. Every file's size is learned
    before its content is read, a file over ``MAX_FILE_BYTES`` — or past what is
    left of ``MAX_TOTAL_BYTES`` or ``MAX_READS`` for the whole request — is
    ``unknown`` and is not read.
    """
    if not files:
        return LocatorEvidence()
    unread = dict.fromkeys(files)
    if not repo:
        return LocatorEvidence(unread, why="project has no workspace")
    if picked is None:
        sha, label, why = await _pick_ref(
            git,
            repo,
            (submission_sha or "").strip(),
            (branch or "").strip(),
            base or "",
        )
    else:
        (sha, label), why = picked, ""
    if not sha:
        return LocatorEvidence(unread, why=why)
    evidence = LocatorEvidence(dict(unread), ref_label=label)
    try:
        tree = await git.files_at_ref(repo, sha)
    except Exception:  # noqa: BLE001 - a failed listing is "could not look"
        log.warning("locator tree listing failed at %s", label)
        tree = None
    if tree is None:
        reason = f"could not list the tree of {label}"
        evidence.unread = dict.fromkeys(files, reason)
        evidence.pytest_config_issue = reason
        return evidence
    reader = _Reader(git, repo, sha)
    for path in files:
        if path not in tree:
            evidence.absent.add(path)
            continue
        state, text, reason = await reader.read(path)
        if state == "present":
            evidence.sources[path] = text
        else:
            evidence.unread[path] = reason
    aux_paths = _aux_paths([p for p in files if p not in evidence.absent])
    if aux_paths is None:
        evidence.pytest_config_issue = (
            f"a locator path is deeper than {MAX_PATH_DEPTH} directories, so the "
            "pytest configuration around it was not looked at"
        )
        return evidence
    evidence.aux = {}
    for full in aux_paths:
        if full not in tree:
            evidence.aux[full] = {"state": "missing", "text": ""}
            continue
        state, text, _ = await reader.read(full)
        evidence.aux[full] = {"state": state, "text": text}
    return evidence


# Answers are a function of the commit, the locators and the analyser, so they
# are kept (bounded) and identical concurrent requests share one computation.
_cache: OrderedDict[tuple, list[dict]] = OrderedDict()
_inflight: dict[tuple, asyncio.Task] = {}


def clear_locator_cache() -> None:
    """Forget cached answers (tests; a commit sha never changes its content)."""
    _cache.clear()
    _inflight.clear()


async def _compute(
    git: Any,
    repo: str | None,
    acs: Any,
    files: list[str],
    *,
    submission_sha: str,
    branch: str,
    base: str,
    picked: tuple[str, str] | None,
) -> tuple[list[dict], bool]:
    """``(rows, cacheable)``: a failure of the worker or the deadline is not cached."""
    try:
        evidence = await asyncio.wait_for(
            read_locator_evidence(
                git,
                repo,
                files,
                submission_sha=submission_sha,
                branch=branch,
                base=base,
                picked=picked,
            ),
            timeout=_READ_DEADLINE_SECONDS,
        )
    except (TimeoutError, asyncio.TimeoutError):
        log.warning("reading locator files passed its deadline")
        evidence = LocatorEvidence(
            dict.fromkeys(files),
            unread=dict.fromkeys(files, OVER_BUDGET),
            pytest_config_issue=OVER_BUDGET,
        )
        rows, _ = _plan(acs, evidence.sources, evidence.absent, evidence.unread)
        return _finish(rows, None, ""), False
    rows, todo = _plan(acs, evidence.sources, evidence.absent, evidence.unread)
    results = None
    if todo:
        wanted = {t["nodeid"].split("::", 1)[0] for t in todo}
        request: dict[str, Any] = {
            "files": {p: evidence.sources[p] for p in wanted},
            "config_issue": evidence.pytest_config_issue,
            "locators": todo,
        }
        if evidence.aux is not None:
            request["aux"] = evidence.aux
        results = await analyse_in_worker(request)
    return _finish(rows, results, evidence.ref_label), not todo or results is not None


async def resolve_locators_at_ref(
    git: Any,
    repo: str | None,
    acs: Any,
    *,
    submission_sha: str = "",
    branch: str = "",
    base: str = "",
) -> list[dict]:
    """Resolve every test-AC locator of ``acs``: read at one commit, analyse in the worker.

    The single door for callers (review brief, epic approve). Reading goes
    through git within the byte and read budgets; the analysis runs in the
    bounded worker process, so a hostile file can cost it its budget and
    nothing else. The commit is resolved first so the answer can be cached by
    (repo, commit, locators, analyser version) and so identical concurrent
    requests are one computation.
    """
    files = locator_files(acs)
    kw = {"submission_sha": submission_sha, "branch": branch, "base": base}
    picked = None
    if files and repo:
        sha, label, _ = await _pick_ref(
            git, repo, submission_sha.strip(), branch.strip(), base or ""
        )
        picked = (sha, label) if sha else None
    if picked is None:
        rows, _ = await _compute(git, repo, acs, files, picked=None, **kw)
        return rows
    wanted = tuple(
        (getattr(ac, "id", "?"), getattr(ac, "test_ref", None))
        for ac in acs
        if _verifiable_by(ac) == "test"
    )
    key = (repo, picked, wanted, locator_worker.VERSION)
    if key in _cache:
        _cache.move_to_end(key)
        return copy.deepcopy(_cache[key])
    loop = asyncio.get_running_loop()
    flight = (key, id(loop))
    task = _inflight.get(flight)
    if task is None:

        async def run() -> list[dict]:
            rows, cacheable = await _compute(git, repo, acs, files, picked=picked, **kw)
            if cacheable:
                _cache[key] = copy.deepcopy(rows)
                while len(_cache) > _CACHE_SIZE:
                    _cache.popitem(last=False)
            return rows

        task = loop.create_task(run())
        _inflight[flight] = task
        task.add_done_callback(lambda _t: _inflight.pop(flight, None))
    return copy.deepcopy(await asyncio.shield(task))
