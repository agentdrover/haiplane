#!/usr/bin/env python3
"""Перепись падений pytest в ci.yml за окно дат (#1685).

Зачем: CI на develop/main падал на тестах, которых дифф прогона не менял.
Скрипт собирает ВСЕ падения шага Test за окно, восстанавливает проверенный
коммит и дерево каждого attempt и честно делит случаи на четыре статуса:

  confirmed       тот же nodeid прошёл на ТОМ ЖЕ tested_tree (другой attempt,
                  другой прогон с тем же деревом или локальный повтор из
                  --manual, где записана команда);
  fixed           такого прохода нет, но между падением и следующим зелёным
                  прогоном той же ветки менялся файл теста или записан фикс из
                  --manual: дерево сменилось, нестабильность не доказана;
  unconfirmed     прохода на том же дереве нет, причина в evidence;
  log_unavailable журнал шага недоступен (срок хранения, 404/410).

Прогон, где pytest пропущен (шаг Test skipped, дедуп дерева ci.yml:87-118),
подтверждением не считается. Для pull_request проверенный коммит — merge-коммит
из строки `git log -1 --format=%H` журнала checkout, а не голова ветки.

Только stdlib и gh (read-only: gh api GET). Секреты не читаются: токен знает gh.
Ответы API кладутся в --cache-dir; при заданных границах окна и неизменной
истории результат детерминирован (строки отсортированы, времени запуска в
файле нет).

    uv run python scripts/flaky_census.py --since 2026-08-16 --until 2026-10-15 \\
        --manual docs/agent-context/flaky-tests-census-2026-10.manual.json \\
        --out docs/agent-context/flaky-tests-census-2026-10.json
    uv run python scripts/flaky_census.py ... --markdown   # таблицы для .md
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = "agentdrover/haiplane"
SLICE_DAYS = 3
PER_PAGE = 100
HISTORY_REF = ["origin/develop"]  # --history-ref: фиксируется для воспроизводимости
# Имена job и шага, как их называет .github/workflows/ci.yml; tests/test_flaky_census.py
# сверяет эти константы с самим workflow (#1767).
PART_JOB_PREFIX = "Tests part "  # job частей набора: "Tests part N/3"
TEST_STEP_NAME = "Test"
TEST_JOB_HINT = "pytest"  # прогоны до #1767: один job "Ruff and pytest" с шагом Test
FAIL_CONCLUSIONS = {"failure", "timed_out"}
FAILED_RE = re.compile(r"^\S+\s+(FAILED|ERROR) (\S+::\S+)(?: - (.*))?")
SUMMARY_RE = re.compile(
    r"^\S+\s+(?:=+ )?(\d+ (?:failed|passed|error)[^\n]*? in [\d.]+s[^\n]*)$"
)
TREE_RE = re.compile(r"^\S+\s+Current tree: ([0-9a-f]{40})")
SHA_RE = re.compile(r"^\S+\s+([0-9a-f]{40})$")
TASK_RE = re.compile(r"\(#(\d+)\)\s*$")
ROW_KEYS = (
    "run_id",
    "run_attempt",
    "head_sha",
    "tested_sha",
    "tested_tree",
    "branch",
    "event",
    "created_at",
    "pytest_outcome",
    "nodeid",
    "status",
    "evidence",
)


GIT_TAG = "git:"
HASH_RE = re.compile(r"[0-9a-f]{7,40}")
# Поля, значения которых берутся из источника-хеша: GitHub API (head_sha), строка
# `git log -1 --format=%H` журнала checkout, `Current tree:` журнала и вывод git
# (rev-parse, log). Остальные строки не трогаются: hex в сообщении теста или в
# произвольном тексте должен по-прежнему видеть detect-secrets.
SHA_KEYS = frozenset({"head_sha", "tested_sha", "tested_tree", "tree", "commit"})
SHA_LIST_KEYS = frozenset({"commits"})


LEGACY_NAME = "open" + "claw"  # собрано, чтобы файл не ловил сам себя
LEGACY_RE = re.compile(LEGACY_NAME, re.IGNORECASE)


def scrub_legacy(text: str) -> str:
    """Темы старых коммитов называют прежнее имя продукта; tests/test_no_legacy_name.py
    запрещает его в любом файле под git, так что в данных оно заменяется маркером."""
    return LEGACY_RE.sub("<legacy-name>", text)


def _map_hashes(value, convert, key=None):
    if isinstance(value, dict):
        return {k: _map_hashes(v, convert, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_hashes(v, convert, key) for v in value]
    if isinstance(value, str) and (key in SHA_KEYS or key in SHA_LIST_KEYS):
        return convert(value)
    return value


def _add_tag(text: str) -> str:
    return GIT_TAG + text if HASH_RE.fullmatch(text) else text


def _drop_tag(text: str) -> str:
    return text[len(GIT_TAG) :] if text.startswith(GIT_TAG) else text


def tag_hashes(value):
    """Хеши git пишутся как «git:<hex>» только в полях SHA_KEYS: detect-secrets
    читает кавычки с одним hex как High Entropy String и красит Secret scan на
    публичных SHA. Менять scripts/secret_scan.py ради данных нельзя (это
    ci_script под ci_integrity), поэтому формат данных иной. Прочие строки идут
    как есть (кроме замены прежнего имени продукта)."""
    return _scrub_all(_map_hashes(value, _add_tag))


def _scrub_all(value):
    if isinstance(value, str):
        return scrub_legacy(value)
    if isinstance(value, list):
        return [_scrub_all(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_all(v) for k, v in value.items()}
    return value


def untag_hashes(value):
    return _map_hashes(value, _drop_tag)


class MissingResource(Exception):
    """Журнал или объект недоступен (404/410): отмечается, не выбрасывается."""


ESCAPE_FLAG = "--allow-escape-sequences"
ESCAPE_HINT = "escape sequences"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def strip_escapes(text: str) -> str:
    """Одинаково для любой версии gh: ни токенов, ни сырого ANSI в кэше и в данных."""
    return ANSI_RE.sub("", text)


class Gh:
    """Тонкая обёртка над `gh api` (GET) с файловым кэшем ответов."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.calls = 0
        self.escape_flag: bool | None = None

    def _slot(self, path: str) -> Path:
        return self.cache_dir / (hashlib.sha1(path.encode()).hexdigest() + ".cache")

    def supports_escape_flag(self) -> bool:
        """gh >= 2.102 отказывается печатать журнал job с управляющими
        последовательностями без --allow-escape-sequences; старый gh флага не знает.
        Поддержка определяется один раз по `gh api --help`."""
        if self.escape_flag is None:
            help_text = subprocess.run(
                ["gh", "api", "--help"], capture_output=True, text=True
            )
            self.escape_flag = ESCAPE_FLAG in help_text.stdout + help_text.stderr
        return self.escape_flag

    def _argv(self, path: str, with_flag: bool) -> list[str]:
        argv = ["gh", "api", f"repos/{REPO}/{path}"]
        return argv + [ESCAPE_FLAG] if with_flag else argv

    def _run(self, path: str) -> subprocess.CompletedProcess:
        wants_flag = path.endswith("/logs") and self.supports_escape_flag()
        proc = subprocess.run(
            self._argv(path, wants_flag), capture_output=True, text=True
        )
        if proc.returncode != 0 and not wants_flag and ESCAPE_HINT in proc.stderr:
            self.escape_flag = True  # справка флаг не показала, но gh его требует
            proc = subprocess.run(
                self._argv(path, True), capture_output=True, text=True
            )
        return proc

    def get(self, path: str) -> str:
        slot = self._slot(path)
        if slot.exists():
            body = slot.read_text(encoding="utf-8")
            if body == "\x00MISSING":
                raise MissingResource(path)
            return strip_escapes(body)
        self.calls += 1
        proc = self._run(path)
        if proc.returncode != 0:
            if re.search(r"HTTP (404|410)", proc.stderr):
                slot.write_text("\x00MISSING", encoding="utf-8")
                raise MissingResource(path)
            hint = (
                f" (gh {ESCAPE_FLAG} не помог или не поддерживается: обновите gh)"
                if ESCAPE_HINT in proc.stderr
                else ""
            )
            raise RuntimeError(f"gh api {path}: {proc.stderr.strip()[:300]}{hint}")
        body = strip_escapes(proc.stdout)
        slot.write_text(body, encoding="utf-8")
        return body

    def json(self, path: str):
        return json.loads(self.get(path))


def git(*args: str, cwd: Path | None = None) -> str | None:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    return proc.stdout.strip() if proc.returncode == 0 else None


def day_slices(since: dt.date, until: dt.date):
    start = since
    while start <= until:
        end = min(start + dt.timedelta(days=SLICE_DAYS - 1), until)
        yield start, end
        start = end + dt.timedelta(days=1)


def collect_runs(gh: Gh, since: dt.date, until: dt.date) -> dict[int, dict]:
    runs: dict[int, dict] = {}
    for first, last in day_slices(since, until):
        page = 1
        while True:
            query = f"actions/workflows/ci.yml/runs?per_page={PER_PAGE}&page={page}&created={first}..{last}"
            batch = gh.json(query)["workflow_runs"]
            for item in batch:
                runs[item["id"]] = item
            if len(batch) < PER_PAGE:
                break
            page += 1
    return runs


def attempts_of(gh: Gh, run: dict) -> list[dict]:
    """Все attempt прогона: последний — сам run, прежние — из /attempts/N."""
    out = []
    for number in range(1, run["run_attempt"]):
        out.append(gh.json(f"actions/runs/{run['id']}/attempts/{number}"))
    out.append(run)
    return out


def test_jobs(gh: Gh, run_id: int, attempt: int) -> list[dict]:
    """Job-ы, где гонялся набор: все части "Tests part N/3", иначе старый job.

    Старый job (один "pytest" в имени) читается для прогонов до #1767. Если
    частей нет, а старый job есть, но шага Test в нём нет, это видно из
    test_step_state как no_step, а не как «чисто».
    """
    jobs = gh.json(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")[
        "jobs"
    ]
    parts = [j for j in jobs if j["name"].startswith(PART_JOB_PREFIX)]
    if parts:
        return parts
    return [j for j in jobs if TEST_JOB_HINT in j["name"].lower()][:1]


_PART_RE = re.compile(r"^" + re.escape(PART_JOB_PREFIX) + r"(\d+)/(\d+)$")


def parts_complete(jobs: list[dict]) -> bool:
    """Все ли части 1..N на месте. Старый job (без "N/M" в имени) — одна часть.

    N берётся из имени и должно быть одним у всех; повтор номера или дыра — не полный набор.
    """
    if not any(j["name"].startswith(PART_JOB_PREFIX) for j in jobs):
        return len(jobs) == 1
    found = [_PART_RE.match(j["name"]) for j in jobs]
    if not all(found):
        return False
    numbers = [(int(m.group(1)), int(m.group(2))) for m in found if m]
    totals = {n for _, n in numbers}
    if len(totals) != 1:
        return False
    return sorted(i for i, _ in numbers) == list(range(1, totals.pop() + 1))


def test_step_state(jobs: list[dict]) -> str:
    """Итог шага Test по всем job набора.

    failure, если упал хотя бы один; success, только если шаг есть и зелёный
    во всех; нет job — no_job; нет шага Test хотя бы в одном — no_step
    (неизвестно, не «чисто»); иначе первый не-success исход (skipped и т.п.).
    """
    if not jobs:
        return "no_job"
    states: list[str | None] = []
    for job in jobs:
        step = next((s for s in job["steps"] if s["name"] == TEST_STEP_NAME), None)
        states.append(None if step is None else (step["conclusion"] or "unknown"))
    if "failure" in states:
        return "failure"
    if None in states or not parts_complete(jobs):
        # a part that never ran proves nothing about its tests (#1767)
        return "no_step"
    return next((s for s in states if s != "success"), "success") or "unknown"


def attempt_proven(jobs: list[dict]) -> bool:
    """Все части на месте и в каждой Test дошёл до success или failure."""
    if not jobs or not parts_complete(jobs):
        return False
    for job in jobs:
        step = next((s for s in job["steps"] if s["name"] == TEST_STEP_NAME), None)
        if step is None or step["conclusion"] not in {"success", "failure"}:
            return False
    return True


def failed_test_jobs(jobs: list[dict]) -> list[dict]:
    """Job-ы, у которых шаг Test упал: журнал читается у каждого из них."""
    return [
        j
        for j in jobs
        if any(
            s["name"] == TEST_STEP_NAME and s["conclusion"] == "failure"
            for s in j["steps"]
        )
    ]


def merge_logs(parsed: list[dict]) -> dict:
    """Разбор журналов нескольких упавших частей в одну запись."""
    first = lambda key: next((p[key] for p in parsed if p[key]), None)  # noqa: E731
    messages: dict = {}
    for p in parsed:
        messages.update(p["messages"])
    summaries = [p["summary"] for p in parsed if p["summary"]]
    return {
        "nodeids": [n for p in parsed for n in p["nodeids"]],
        "messages": messages,
        "summary": " | ".join(summaries) if summaries else None,
        "tested_sha": first("tested_sha"),
        "tree": first("tree"),
        "killed": any(p["killed"] for p in parsed),
    }


def parse_log(text: str) -> dict:
    """Из журнала job: упавшие nodeid, итоговая строка pytest, коммит, дерево."""
    parsed: dict = {
        "nodeids": [],
        "messages": {},
        "summary": None,
        "tested_sha": None,
        "tree": None,
    }
    after_checkout = False
    for line in text.splitlines():
        if "git log -1 --format=%H" in line:
            after_checkout = True
            continue
        if after_checkout and parsed["tested_sha"] is None:
            found = SHA_RE.match(line)
            if found:
                parsed["tested_sha"] = found.group(1)
            after_checkout = False
        tree = TREE_RE.match(line)
        if tree and parsed["tree"] is None:
            parsed["tree"] = tree.group(1)
        failed = FAILED_RE.match(line)
        if failed and failed.group(2) not in parsed["nodeids"]:
            parsed["nodeids"].append(failed.group(2))
            parsed["messages"][failed.group(2)] = (failed.group(3) or "")[:200]
        done = SUMMARY_RE.match(line)
        if done:
            parsed["summary"] = done.group(1).strip("= ")
    parsed["killed"] = "exceeded the maximum execution time" in text
    return parsed


def tree_of(gh: Gh, sha: str, repo_dir: Path) -> str | None:
    tree = git("rev-parse", f"{sha}^{{tree}}", cwd=repo_dir)
    if tree:
        return tree
    try:
        return gh.json(f"git/commits/{sha}")["tree"]["sha"]
    except MissingResource:
        return None


def base_ref_at(created_at: str, branch: str, repo_dir: Path) -> str | None:
    """Коммит origin/<база> на момент прогона: основа диффа, не зависит от сегодня."""
    return git(
        "rev-list", "-1", f"--before={created_at}", f"origin/{branch}", cwd=repo_dir
    )


def diff_files(attempt: dict, repo_dir: Path) -> set[str] | None:
    """Файлы, которые менял дифф прогона.

    push: коммит против первого родителя. Остальное: голова против
    merge-base с базой на момент прогона (мерж-коммит PR локально обычно
    недостижим, а дифф PR и дифф его головы от merge-base совпадают).
    """
    sha = attempt["tested_sha"]
    if attempt["event"] == "push":
        out = git("diff", "--name-only", f"{sha}^1", sha, cwd=repo_dir)
    else:
        base = base_ref_at(attempt["created_at"], attempt["base_branch"], repo_dir)
        merge_base = (
            git("merge-base", base, attempt["head_sha"], cwd=repo_dir) if base else None
        )
        out = (
            git("diff", "--name-only", merge_base, attempt["head_sha"], cwd=repo_dir)
            if merge_base
            else None
        )
    return set(out.splitlines()) if out is not None else None


RELEASE_BASES = {"develop": "main", "main": "develop"}


def pr_base(run: dict) -> str:
    """База прогона. GitHub со временем очищает pull_requests у старых прогонов,
    поэтому для релизных PR (голова develop или main) база выводится из головы:
    ответ API от давности не зависит, а переписи нужно одно и то же при повторе."""
    prs = run.get("pull_requests") or []
    if prs:
        return prs[0]["base"]["ref"]
    if run["event"] == "pull_request":
        return RELEASE_BASES.get(run["head_branch"], "develop")
    return "develop"


def failed_attempt(gh: Gh, base: dict, number: int) -> dict:
    """Запись о упавшем attempt: шаг Test, журнал, проверенный коммит."""
    rec = {
        "run_id": base["id"],
        "run_attempt": number,
        "event": base["event"],
        "branch": base["head_branch"],
        "head_sha": base["head_sha"],
        "created_at": base["created_at"],
        "conclusion": base["conclusion"],
        "tested_sha": None,
        "tree_hint": None,
        "nodeids": [],
        "messages": {},
        "summary": None,
        "log": "n/a",
        "test_step": "n/a",
        "killed": False,
        "base_branch": pr_base(base),
    }
    jobs = test_jobs(gh, base["id"], number)
    rec["test_step"] = test_step_state(jobs)
    if rec["test_step"] != "failure":
        return rec
    logs = []
    rec["log_unavailable"] = []
    rec["incomplete"] = not attempt_proven(jobs)
    for job in failed_test_jobs(jobs):
        try:
            logs.append(parse_log(gh.get(f"actions/jobs/{job['id']}/logs")))
        except MissingResource:
            # each failed part with no log stays visible (see rows_for_attempt)
            rec["log_unavailable"].append(job["name"])
    # Each log is judged BEFORE the merge: a failed part whose log names no
    # failed test (collection error, crash) leaves its tests unrun, exactly as
    # the single old job did, so the attempt is out of the denominator.
    rec["incomplete"] = (
        rec["incomplete"]
        or bool(rec["log_unavailable"])
        or any(not parsed_log["nodeids"] for parsed_log in logs)
    )
    parsed = merge_logs(logs) if logs else None
    if parsed is None:
        rec["log"] = "unavailable"
        rec["tested_sha"] = (
            base["head_sha"] if base["event"] != "pull_request" else None
        )
        return rec
    rec.update(
        log="ok",
        nodeids=parsed["nodeids"],
        messages=parsed["messages"],
        summary=parsed["summary"],
        tested_sha=parsed["tested_sha"],
        tree_hint=parsed["tree"],
    )
    rec["killed"] = parsed["killed"]
    return rec


def passing_trees(
    gh: Gh, runs: dict[int, dict], repo_dir: Path
) -> dict[str, list[dict]]:
    """tree -> прогоны с этим деревом, где Test зелёный.

    Берутся push и workflow_dispatch: их проверенный коммит равен head_sha и
    дерево берётся из git без чтения журнала. PR-прогоны — через свои attempt.
    """
    by_tree: dict[str, list[dict]] = {}
    for run in sorted(runs.values(), key=lambda r: r["id"]):
        if run["event"] not in {"push", "workflow_dispatch"}:
            continue
        if run["conclusion"] not in {"success", "failure"}:
            continue
        tree = tree_of(gh, run["head_sha"], repo_dir)
        if tree:
            by_tree.setdefault(tree, []).append(run)
    return by_tree


def attempt_tested_sha(gh: Gh, run_id: int, number: int) -> str | None:
    for job in test_jobs(gh, run_id, number):
        try:
            return parse_log(gh.get(f"actions/jobs/{job['id']}/logs"))["tested_sha"]
        except MissingResource:
            continue
    return None


def green_attempts(gh: Gh, run: dict) -> list[int]:
    return [
        n
        for n in range(1, run["run_attempt"] + 1)
        if test_step_state(test_jobs(gh, run["id"], n)) == "success"
    ]


def passes_on_tree(gh: Gh, rec: dict, tree: str, ctx: tuple) -> list[dict]:
    """Attempt с зелёным шагом Test на том же дереве, кроме самого упавшего.

    Две дороги: повтор того же прогона (тот же проверенный коммит сверяется по
    журналу, а не предполагается) и другие push/dispatch-прогоны с тем же деревом.
    """
    runs, pool = ctx[0], ctx[1]
    found: dict[tuple[int, int], str] = {}
    own = runs[rec["run_id"]]
    for number in green_attempts(gh, own):
        if (
            number != rec["run_attempt"]
            and attempt_tested_sha(gh, own["id"], number) == rec["tested_sha"]
        ):
            found[(own["id"], number)] = (
                "rerun of the same run on the same commit, Test green"
            )
    for run in pool.get(tree, []):
        for number in green_attempts(gh, run):
            key = (run["id"], number)
            if key != (rec["run_id"], rec["run_attempt"]):
                found.setdefault(key, "other run on the same tree, Test green")
    return [
        {"run_id": k[0], "run_attempt": k[1], "how": how}
        for k, how in sorted(found.items())
    ]


def test_path(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def row_for(
    rec: dict, nodeid: str | None, tree: str | None, status: str, evidence: dict
) -> dict:
    outcome = (
        "failed"
        if nodeid
        else ("step_killed" if rec.get("killed") else "failed_no_nodeid")
    )
    if rec["log"] == "unavailable":
        outcome = "unknown"
    return {
        "run_id": rec["run_id"],
        "run_attempt": rec["run_attempt"],
        "head_sha": rec["head_sha"],
        "tested_sha": rec["tested_sha"],
        "tested_tree": tree,
        "branch": rec["branch"],
        "event": rec["event"],
        "created_at": rec["created_at"],
        "pytest_outcome": outcome,
        "nodeid": nodeid,
        "status": status,
        "evidence": evidence,
    }


def local_runs(manual: dict, nodeid: str | None, tree: str | None) -> list[dict]:
    if not nodeid or not tree:
        return []
    return [
        r
        for r in manual.get("local_runs", [])
        if r["nodeid"] == nodeid and r["tree"] == tree
    ]


def next_green_commits(
    rec: dict, runs: dict[int, dict], file: str, repo_dir: Path
) -> list[str]:
    """Коммиты, менявшие файл теста между падением и следующим зелёным прогоном ветки."""
    later = [
        r
        for r in runs.values()
        if r["head_branch"] == rec["branch"]
        and r["conclusion"] == "success"
        and r["created_at"] > rec["created_at"]
    ]
    if not later or not rec["tested_sha"]:
        return []
    nxt = min(later, key=lambda r: r["created_at"])
    out = git(
        "log",
        "--format=%h",
        f"{rec['head_sha']}..{nxt['head_sha']}",
        "--",
        file,
        cwd=repo_dir,
    )
    return out.splitlines() if out else []


def classify(
    gh: Gh, rec: dict, nodeid: str | None, tree: str | None, ctx: tuple
) -> tuple[str, dict]:
    runs, manual, repo_dir = ctx[0], ctx[2], ctx[3]
    if rec["log"] == "unavailable":
        return "log_unavailable", {"reason": "job log returned 404/410"}
    if tree is None:
        return "unconfirmed", {
            "reason": "tested tree not recoverable (commit not reachable via git or API)"
        }
    if nodeid is None:
        return "unconfirmed", {
            "reason": "no failed nodeid in the log (step killed or crashed)"
        }
    passes = passes_on_tree(gh, rec, tree, ctx)
    local = local_runs(manual, nodeid, tree)
    if passes or any(run["passed"] > 0 for run in local):
        return "confirmed", {"passes_on_same_tree": passes, "local_runs": local}
    fix = manual.get("fixes", {}).get(nodeid)
    if fix:
        return "fixed", {"fix": fix, "tree_changed": True, "local_runs": local}
    commits = next_green_commits(rec, runs, test_path(nodeid), repo_dir)
    if commits:
        return "fixed", {
            "reason": "test file changed before the next green run of the branch",
            "commits": commits,
            "local_runs": local,
        }
    return "unconfirmed", {
        "reason": "no pass on the same tested tree; code changed or no later green run",
        "local_runs": local,
    }


def func_name(nodeid: str) -> str:
    return nodeid.split("::")[-1].split("[")[0]


def commit_lines(repo_dir: Path, ref: str, nodeid: str) -> list[list[str]]:
    """Коммиты, менявшие функцию теста (новые первыми): hash, дата, тема."""
    name, file = func_name(nodeid), test_path(nodeid)
    fmt = "--format=%H%x09%ct%x09%s"
    out = git("log", f"-L:{name}:{file}", fmt, "-s", ref, cwd=repo_dir)
    if not out:
        out = git("log", f"-S def {name}", fmt, ref, "--", file, cwd=repo_dir)
    parts = [
        line.split("\t", 2)
        for line in (out or "").splitlines()
        if line.count("\t") == 2
    ]
    return [[sha, utc_iso(int(epoch)), subject] for sha, epoch, subject in parts]


def utc_iso(epoch: int) -> str:
    """Дата коммита в том же формате, что created_at у GitHub (UTC, Z)."""
    moment = dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def task_of(subject: str) -> int | None:
    found = TASK_RE.search(subject)
    return int(found.group(1)) if found else None


class BlobReader:
    """Один процесс `git cat-file --batch`: есть ли объект и что лежит в файле."""

    def __init__(self, repo_dir: Path) -> None:
        self.proc = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=repo_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self.cache: dict[str, bytes | None] = {}

    def read(self, spec: str) -> bytes | None:
        if spec not in self.cache:
            self.proc.stdin.write(spec.encode() + b"\n")
            self.proc.stdin.flush()
            header = self.proc.stdout.readline().split()
            body = None
            if len(header) == 3 and header[1] in (b"blob", b"commit"):
                size = int(header[2])
                body = self.proc.stdout.read(size + 1)[:size]
            self.cache[spec] = body
        return self.cache[spec]

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait()


def test_exists(blobs: BlobReader, sha: str, nodeid: str) -> bool | None:
    """Есть ли функция теста в дереве коммита; None — коммита нет локально."""
    if blobs.read(sha) is None:
        return None
    text = blobs.read(f"{sha}:{test_path(nodeid)}")
    pattern = rf"def {re.escape(func_name(nodeid))}\b".encode()
    return bool(text and re.search(pattern, text))


def count_executions(
    pool: list[dict],
    blobs: BlobReader,
    nodeid: str,
    since: str,
    until: str | None,
    branch_prefix: str | None,
) -> dict:
    """Уникальные (run_id, attempt), где шаг Test выполнялся и тест был в дереве.

    Проверка наличия идёт по head_sha прогона (мерж-коммит PR локально обычно
    недоступен); коммит без объекта считается отдельно и в знаменатель не входит.
    """
    done = failed = unverifiable = 0
    for rec in pool:
        if rec["created_at"] < since or (until and rec["created_at"] > until):
            continue
        if branch_prefix and not rec["branch"].startswith(branch_prefix):
            continue
        present = test_exists(blobs, rec["head_sha"], nodeid)
        if present is None:
            unverifiable += 1
        elif present and rec["failed"] is not None:
            done += 1
            failed += 1 if nodeid in rec["failed"] else 0
    return {
        "executions": done,
        "failures": failed,
        "unverifiable_attempts": unverifiable,
    }


def source_of(nodeid: str, repo_dir: Path) -> dict:
    """Задача, добавившая тест (дата мержа = дата коммита в истории), и последняя
    менявшая его. Только история HISTORY_REF: от наличия мерж-коммита PR в локальном
    клоне результат не зависит. Нет тела теста в истории — источник неизвестен."""
    lines = commit_lines(repo_dir, HISTORY_REF[0], nodeid)
    if not lines:
        return {
            "reason": f"nodeid not found in the history of {HISTORY_REF[0]}: source unknown"
        }
    out: dict = {
        "changes": [
            {"commit": sha[:10], "date": when, "task": task_of(subject)}
            for sha, when, subject in reversed(lines)
        ][:6]
    }
    sha, when, subject = lines[-1]
    out["added"] = {
        "commit": sha[:10],
        "date": when,
        "subject": subject,
        "task": task_of(subject),
    }
    return out


def last_change_before(source: dict, moment: str) -> dict | None:
    before = [c for c in source.get("changes", []) if c["date"] <= moment]
    return before[-1] if before else None


def changes_after(source: dict, moment: str) -> list[dict]:
    return [c for c in source.get("changes", []) if c["date"] > moment][:3]


def attach_context(
    evidence: dict, rec: dict, nodeid: str | None, ctx: tuple, status: str
) -> None:
    manual, repo_dir = ctx[2], ctx[3]
    if nodeid is None:
        return
    evidence["fixed_by"] = manual.get("fixes", {}).get(nodeid)
    evidence["class"] = manual.get("classes", {}).get(
        nodeid, {"class": "unknown", "why": "not assessed"}
    )
    if status == "confirmed" or evidence.get("test_file_in_run_diff") is False:
        source = source_of(nodeid, repo_dir)
        source["last_changed_before_failure"] = last_change_before(
            source, rec["created_at"]
        )
        source["changed_after_failure"] = changes_after(source, rec["created_at"])
        evidence["source"] = source


def add_rates(
    rows: list[dict], pool: list[dict], blobs: BlobReader, window_start: str
) -> None:
    """Знаменатель частоты и проверки до мержа по уникальным (run_id, attempt).

    Исполнение = attempt ci.yml не из push (там pytest бывает пропущен по дедупу
    дерева), где шаг Test выполнялся (success/failure; упавший без разобранных
    nodeid не берётся) и функция теста есть в дереве head_sha. Окно — от даты
    мержа добавившего коммита (в окне не раньше его начала). До мержа считаются
    attempt на ветках `task-N/` задачи-источника, созданные не позже даты мержа.
    """
    cache: dict[str, tuple[dict, dict | None]] = {}
    for row in rows:
        source = row["evidence"].get("source") or {}
        added = source.get("added")
        if not added or not row["nodeid"]:
            continue
        nodeid = row["nodeid"]
        if nodeid not in cache:
            since = max(added["date"], window_start)
            after = count_executions(pool, blobs, nodeid, since, None, None)
            after["since"] = since
            pre = None
            if added["task"]:
                pre = count_executions(
                    pool,
                    blobs,
                    nodeid,
                    window_start,
                    added["date"],
                    f"task-{added['task']}/",
                )
                pre.update(task=added["task"], merged_at=added["date"])
            cache[nodeid] = (after, pre)
        row["evidence"]["ci_rate"] = cache[nodeid][0]
        row["evidence"]["premerge_ci"] = cache[nodeid][1]


def attempt_record(item: dict, failed: set[str] | None) -> dict:
    return {
        "run_id": item["id"],
        "attempt": item["run_attempt"],
        "created_at": item["created_at"],
        "branch": item["head_branch"],
        "head_sha": item["head_sha"],
        "failed": failed,
    }


def missing_objects(shas: list[str], repo_dir: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        input="\n".join(shas) + "\n",
        capture_output=True,
        text=True,
        cwd=repo_dir,
    )
    return [sha for sha in shas if f"{sha} missing" in proc.stdout.splitlines()]


def ensure_objects(runs: dict[int, dict], repo_dir: Path) -> None:
    """Головы всех прогонов должны быть в клоне: от этого зависят diff_files,
    next_green_commits и проверка «тест есть в дереве». Недостающие подкачиваются
    одним fetch всех PR-голов (refs/census/pr/*), остаток — по одному sha; так
    результат не зависит от того, что клон уже видел."""
    wanted = sorted({r["head_sha"] for r in runs.values()})
    missing = missing_objects(wanted, repo_dir)
    if missing:
        git(
            "fetch",
            "-q",
            "--no-tags",
            "origin",
            "+refs/pull/*/head:refs/census/pr/*",
            cwd=repo_dir,
        )
        missing = missing_objects(missing, repo_dir)
    for sha in missing:
        git("fetch", "-q", "--no-tags", "origin", sha, cwd=repo_dir)


def rows_for_attempt(gh: Gh, rec: dict, ctx: tuple, repo_dir: Path) -> list[dict]:
    tree = rec["tree_hint"] or (
        tree_of(gh, rec["tested_sha"], repo_dir) if rec["tested_sha"] else None
    )
    changed = diff_files(rec, repo_dir) if rec["tested_sha"] else None
    rows = []
    for nodeid in rec["nodeids"] or [None]:
        status, evidence = classify(gh, rec, nodeid, tree, ctx)
        evidence["summary_line"] = rec["summary"]
        evidence["message"] = rec["messages"].get(nodeid)
        evidence["failed_in_attempt"] = len(rec["nodeids"])
        evidence["test_file_in_run_diff"] = (
            None if changed is None or nodeid is None else test_path(nodeid) in changed
        )
        attach_context(evidence, rec, nodeid, ctx, status)
        rows.append(row_for(rec, nodeid, tree, status, evidence))
    for name in rec.get("log_unavailable", []) if rec["log"] == "ok" else []:
        # a failed part whose log is gone: its failures are unknown, and that
        # is said in a row of its own instead of being dropped (#1767)
        reason = {"reason": "job log returned 404/410", "part": name}
        unknown = {**rec, "log": "unavailable"}
        rows.append(row_for(unknown, None, tree, "log_unavailable", reason))
    return rows


def pool_failed(rec: dict) -> set[str] | None:
    """nodeid, упавшие в attempt, где Test выполнялся; None — выполнение не доказано."""
    if rec.get("incomplete"):
        # #1767: a part that did not reach Test (or whose log is gone) leaves its
        # tests unrun or unknown. Counting the attempt would give every test of
        # that part executions=1, failures=0, so the whole attempt is left out
        # of the denominator. Counting only the failed parts' tests would be
        # more exact, but the job log names no test that did not fail.
        return None
    if rec["test_step"] == "success":
        return set()
    if rec["test_step"] == "failure" and rec["nodeids"]:
        return set(rec["nodeids"])
    return None


def census_rows(
    gh: Gh, runs: dict[int, dict], manual: dict, repo_dir: Path
) -> list[dict]:
    ensure_objects(runs, repo_dir)
    ctx = (runs, passing_trees(gh, runs, repo_dir), manual, repo_dir)
    rows: list[dict] = []
    pool: list[dict] = []
    for run in sorted(runs.values(), key=lambda r: r["id"]):
        for item in attempts_of(gh, run):
            if item["conclusion"] == "success" and item["event"] != "push":
                pool.append(attempt_record(item, set()))
            if item["conclusion"] not in FAIL_CONCLUSIONS:
                continue
            rec = failed_attempt(gh, item, item["run_attempt"])
            if item["event"] != "push":
                pool.append(attempt_record(item, pool_failed(rec)))
            if rec["test_step"] == "failure":
                rows += rows_for_attempt(gh, rec, ctx, repo_dir)
    blobs = BlobReader(repo_dir)
    try:
        add_rates(rows, pool, blobs, min(r["created_at"] for r in runs.values()))
    finally:
        blobs.close()
    rows += [
        {key: extra.get(key) for key in ROW_KEYS}
        for extra in manual.get("extra_rows", [])
    ]
    return sorted(
        rows, key=lambda r: (r["run_id"] or 0, r["run_attempt"] or 0, r["nodeid"] or "")
    )


def render_markdown(rows: list[dict]) -> str:
    lines = [
        "| run | att | branch | event | tested_sha | status | class | nodeid |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        sha = (r["tested_sha"] or "-")[:8]
        head = f"| {r['run_id'] or '-'} | {r['run_attempt'] or '-'} | {r['branch']} | {r['event']} | {sha} "
        lines.append(
            f"{head}| {r['status']} | {class_of(r) or '-'} | {r['nodeid'] or '-'} |"
        )
    return "\n".join(lines)


def tally(rows: list[dict], key) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        label = str(key(row))
        counts[label] = counts.get(label, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


DETERMINISTIC = "детерминированная регрессия"


def class_of(row: dict) -> str | None:
    return (row["evidence"].get("class") or {}).get("class")


def flake_candidates(rows: list[dict]) -> dict[str, list[dict]]:
    """nodeid -> строки; нестабильные кандидаты = всё, что не назвали детерминированной регрессией."""
    out: dict[str, list[dict]] = {}
    for row in rows:
        if row["nodeid"] and row["status"] != "log_unavailable":
            if class_of(row) not in {DETERMINISTIC, None}:
                out.setdefault(row["nodeid"], []).append(row)
    return out


def premerge_line(nodeid: str, group: list[dict]) -> str:
    """Строка таблицы гипотезы: сколько раз тест исполнялся в CI до мержа и что из этого следует."""
    keyed = next((r for r in group if r["evidence"].get("ci_rate")), group[0])
    ev = keyed["evidence"]
    added = ((ev.get("source") or {}).get("added")) or {}
    pre, rate = ev.get("premerge_ci") or {}, ev.get("ci_rate") or {}
    runs, fails, execs = (
        pre.get("executions"),
        rate.get("failures"),
        rate.get("executions"),
    )
    chance = "n/a"
    if runs and execs and fails is not None:
        chance = f"{1 - (1 - fails / execs) ** runs:.3f}"
    base = sum(1 for r in group if r["branch"] in {"develop", "main"})
    statuses = tally(group, lambda r: r["status"])
    return (
        f"| {nodeid} | {class_of(keyed)} | {statuses} | {base}/{len(group)} "
        f"| {added.get('task')} | {runs} ({pre.get('failures')} failed) | {fails}/{execs} | {chance} |"
    )


def summarize(rows: list[dict]) -> str:
    flaky = flake_candidates(rows)
    flaky_rows = [r for group in flaky.values() for r in group]
    lines = [
        f"rows={len(rows)}; by status: {tally(rows, lambda r: r['status'])}",
        f"by event: {tally(rows, lambda r: r['event'])}",
        f"by class: {tally(rows, class_of)}",
        f"distinct failing nodeids: {len({r['nodeid'] for r in rows if r['nodeid']})}",
        f"flaky candidates (class is not deterministic): nodeids={len(flaky)} rows={len(flaky_rows)}",
        f"  by status: {tally(flaky_rows, lambda r: r['status'])}",
        f"  by class: {tally(flaky_rows, class_of)}",
        f"  rows on develop/main: {sum(1 for r in flaky_rows if r['branch'] in {'develop', 'main'})}",
        "",
        "| nodeid | class | statuses | rows on develop/main | source task | pre-merge executions on the source branch (failed) | CI failures/executions in window | P(at least one failure in the pre-merge runs) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [premerge_line(nodeid, group) for nodeid, group in sorted(flaky.items())]
    return "\n".join(lines)


def step_breakdown(gh: Gh, runs: dict[int, dict]) -> str:
    """Все упавшие attempt окна по первому упавшему шагу: что из них — pytest."""
    counts: dict[str, int] = {}
    for run in sorted(runs.values(), key=lambda r: r["id"]):
        for item in attempts_of(gh, run):
            if item["conclusion"] not in FAIL_CONCLUSIONS:
                continue
            jobs = test_jobs(gh, run["id"], item["run_attempt"])
            failed = [
                s["name"]
                for job in jobs
                for s in job["steps"]
                if s["conclusion"] == "failure"
            ]
            label = failed[0] if failed else "no failed step in the pytest job"
            counts[label] = counts.get(label, 0) + 1
    total = sum(counts.values())
    return f"failed attempts={total}; first failed step: " + ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )


M1_BUDGET_SECONDS = 120
M1_MAX_REPEATS = 400


def m1_table(rows: list[dict], manual: dict) -> str:
    """Таблица мер M1: K = min(400, 120 с / длительность повтора под нагрузкой),
    P(detect) = 1 - (1 - p)^K, p = падения/выполнения из ci_rate. Все числа
    документа берутся отсюда, а не набираются руками."""
    secs = manual.get("loaded_seconds_per_repeat", {})
    reproduced = set(manual.get("m1_reproduced", []))
    lines = [
        "| nodeid | failures/executions (p) | s per repeat (loaded) | K | P(detect) | reproduced locally |",
        "|---|---|---|---|---|---|",
    ]
    probs: dict[bool, list[float]] = {True: [], False: []}
    seen: set[str] = set()
    for row in rows:
        rate = row["evidence"].get("ci_rate")
        nodeid = row["nodeid"]
        if not rate or nodeid in seen or nodeid not in secs:
            continue
        seen.add(nodeid)
        p = rate["failures"] / rate["executions"] if rate["executions"] else 0.0
        k = min(M1_MAX_REPEATS, int(M1_BUDGET_SECONDS / secs[nodeid]))
        prob = 1 - (1 - p) ** k
        repro = nodeid in reproduced
        if p > 0:
            probs[repro].append(prob)
        shown = f"{prob:.3f}" if p > 0 else "n/a (p=0)"
        lines.append(
            f"| {nodeid} | {rate['failures']}/{rate['executions']} ({p * 100:.2f}%) | {secs[nodeid]} | {k} | {shown} | {'yes' if repro else 'no'} |"
        )
    rest, every = probs[False], probs[False] + probs[True]
    lines.append("")
    lines.append(
        f"mean over {len(rest)} not reproduced with p>0: {sum(rest) / len(rest):.3f}; "
        f"mean over all {len(every)} with p>0: {sum(every) / len(every):.3f}"
    )
    return "\n".join(lines)


def load_manual(path: str | None) -> dict:
    if not path:
        return {}
    return untag_hashes(json.loads(Path(path).read_text(encoding="utf-8")))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--since", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--until", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--cache-dir", default=".flaky-cache", type=Path)
    parser.add_argument(
        "--manual", help="JSON с локальными повторами и записанными фиксами"
    )
    parser.add_argument(
        "--history-ref",
        default="origin/develop",
        help="ref, по которому ищутся правки тестов после падения (для воспроизводимости укажите sha)",
    )
    parser.add_argument("--out", help="куда записать JSON переписи")
    parser.add_argument(
        "--markdown", action="store_true", help="напечатать таблицу для .md"
    )
    parser.add_argument(
        "--m1", action="store_true", help="напечатать таблицу P(detect) для меры M1"
    )
    parser.add_argument(
        "--summary", action="store_true", help="напечатать сводку и числа по гипотезе"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    repo_dir = Path(__file__).resolve().parent.parent
    HISTORY_REF[0] = args.history_ref
    gh = Gh(args.cache_dir)
    runs = collect_runs(gh, args.since, args.until)
    rows = census_rows(gh, runs, load_manual(args.manual), repo_dir)
    text = (
        json.dumps(tag_hashes(rows), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(f"runs={len(runs)} rows={len(rows)} api_calls={gh.calls}", file=sys.stderr)
    if args.markdown:
        print(render_markdown(rows))
    elif args.m1:
        print(m1_table(rows, load_manual(args.manual)))
    elif args.summary:
        print(f"runs={len(runs)}; {step_breakdown(gh, runs)}")
        print(summarize(rows))
    elif not args.out:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
