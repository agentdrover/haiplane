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
TEST_JOB_HINT = "pytest"
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


class MissingResource(Exception):
    """Журнал или объект недоступен (404/410): отмечается, не выбрасывается."""


class Gh:
    """Тонкая обёртка над `gh api` (GET) с файловым кэшем ответов."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.calls = 0

    def _slot(self, path: str) -> Path:
        return self.cache_dir / (hashlib.sha1(path.encode()).hexdigest() + ".cache")

    def get(self, path: str) -> str:
        slot = self._slot(path)
        if slot.exists():
            body = slot.read_text(encoding="utf-8")
            if body == "\x00MISSING":
                raise MissingResource(path)
            return body
        self.calls += 1
        proc = subprocess.run(
            ["gh", "api", f"repos/{REPO}/{path}"], capture_output=True, text=True
        )
        if proc.returncode != 0:
            if re.search(r"HTTP (404|410)", proc.stderr):
                slot.write_text("\x00MISSING", encoding="utf-8")
                raise MissingResource(path)
            raise RuntimeError(f"gh api {path}: {proc.stderr.strip()[:300]}")
        slot.write_text(proc.stdout, encoding="utf-8")
        return proc.stdout

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


def test_job(gh: Gh, run_id: int, attempt: int) -> dict | None:
    jobs = gh.json(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")[
        "jobs"
    ]
    for job in jobs:
        if TEST_JOB_HINT in job["name"].lower():
            return job
    return None


def test_step_state(job: dict | None) -> str:
    if job is None:
        return "no_job"
    for step in job["steps"]:
        if step["name"] == "Test":
            return step["conclusion"] or "unknown"
    return "no_step"


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
    job = test_job(gh, base["id"], number)
    rec["test_step"] = test_step_state(job)
    if rec["test_step"] != "failure":
        return rec
    try:
        parsed = parse_log(gh.get(f"actions/jobs/{job['id']}/logs"))
    except MissingResource:
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
    job = test_job(gh, run_id, number)
    if job is None:
        return None
    try:
        return parse_log(gh.get(f"actions/jobs/{job['id']}/logs"))["tested_sha"]
    except MissingResource:
        return None


def green_attempts(gh: Gh, run: dict) -> list[int]:
    return [
        n
        for n in range(1, run["run_attempt"] + 1)
        if test_step_state(test_job(gh, run["id"], n)) == "success"
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


def premerge_ci(runs: dict[int, dict], task: int | None, merged_at: str) -> dict | None:
    """Прогоны ci.yml на ветках задачи до мержа: верхняя оценка проверок теста в CI."""
    if task is None:
        return None
    mine = [
        r
        for r in runs.values()
        if r["head_branch"].startswith(f"task-{task}/") and r["created_at"] <= merged_at
    ]
    by_event: dict[str, int] = {}
    for run in mine:
        by_event[run["event"]] = by_event.get(run["event"], 0) + 1
    return {
        "task": task,
        "runs": len(mine),
        "success": sum(1 for r in mine if r["conclusion"] == "success"),
        "by_event": dict(sorted(by_event.items())),
    }


def source_of(nodeid: str, rec: dict, runs: dict[int, dict], repo_dir: Path) -> dict:
    """Задача, добавившая тест, и задача, менявшая его последней до падения."""
    ref = (
        rec["tested_sha"]
        if rec["tested_sha"]
        and git("cat-file", "-e", rec["tested_sha"], cwd=repo_dir) is not None
        else rec["head_sha"]
    )
    lines = commit_lines(repo_dir, ref, nodeid) or commit_lines(
        repo_dir, HISTORY_REF[0], nodeid
    )
    if not lines:
        return {"reason": "no commit found by git log -L / -S"}
    out = {}
    for key, (sha, when, subject) in (("last_changed", lines[0]), ("added", lines[-1])):
        task = task_of(subject)
        out[key] = {
            "commit": sha[:10],
            "date": when,
            "subject": subject,
            "task": task,
            "premerge_ci": premerge_ci(runs, task, when),
        }
    later = commit_lines(repo_dir, HISTORY_REF[0], nodeid)
    out["changed_after_failure"] = [
        {"commit": sha[:10], "date": when, "task": task_of(subject)}
        for sha, when, subject in reversed(later)
        if when > rec["created_at"]
    ][:3]
    return out


def attach_context(
    evidence: dict, rec: dict, nodeid: str | None, ctx: tuple, status: str
) -> None:
    runs, manual, repo_dir = ctx[0], ctx[2], ctx[3]
    if nodeid is None:
        return
    evidence["fixed_by"] = manual.get("fixes", {}).get(nodeid)
    evidence["class"] = manual.get("classes", {}).get(
        nodeid, {"class": "unknown", "why": "not assessed"}
    )
    if status == "confirmed" or evidence.get("test_file_in_run_diff") is False:
        evidence["source"] = source_of(nodeid, rec, runs, repo_dir)


def add_rates(rows: list[dict], runs: dict[int, dict], window_start: str) -> None:
    """Сколько раз тест исполнялся в CI с момента добавления и сколько раз упал.

    Исполнения: успешные PR- и dispatch-прогоны (каждый attempt гонял pytest;
    push исключён, там pytest бывает пропущен по дедупу дерева) плюс упавшие
    attempt этого nodeid. Оценка нижняя: упавшие прогоны других шагов не
    считаются, а Test в них мог не дойти.
    """
    for row in rows:
        added = ((row["evidence"].get("source") or {}).get("added") or {}).get("date")
        if not added or not row["nodeid"]:
            continue
        failed = [
            r
            for r in rows
            if r["nodeid"] == row["nodeid"]
            and r["created_at"] >= max(added, window_start)
            and r["run_id"]
        ]
        done = sum(
            r["run_attempt"]
            for r in runs.values()
            if r["event"] != "push"
            and r["conclusion"] == "success"
            and r["created_at"] >= max(added, window_start)
        )
        row["evidence"]["ci_rate"] = {
            "since": max(added, window_start),
            "executions": done + len(failed),
            "failures": len(failed),
        }


def census_rows(
    gh: Gh, runs: dict[int, dict], manual: dict, repo_dir: Path
) -> list[dict]:
    ctx = (runs, passing_trees(gh, runs, repo_dir), manual, repo_dir)
    rows: list[dict] = []
    for run in sorted(runs.values(), key=lambda r: r["id"]):
        for item in attempts_of(gh, run):
            if item["conclusion"] not in FAIL_CONCLUSIONS:
                continue
            rec = failed_attempt(gh, item, item["run_attempt"])
            if rec["test_step"] != "failure":
                continue
            tree = rec["tree_hint"] or (
                tree_of(gh, rec["tested_sha"], repo_dir) if rec["tested_sha"] else None
            )
            changed = diff_files(rec, repo_dir) if rec["tested_sha"] else None
            for nodeid in rec["nodeids"] or [None]:
                status, evidence = classify(gh, rec, nodeid, tree, ctx)
                evidence["summary_line"] = rec["summary"]
                evidence["message"] = rec["messages"].get(nodeid)
                evidence["failed_in_attempt"] = len(rec["nodeids"])
                evidence["test_file_in_run_diff"] = (
                    None
                    if changed is None or nodeid is None
                    else test_path(nodeid) in changed
                )
                attach_context(evidence, rec, nodeid, ctx, status)
                rows.append(row_for(rec, nodeid, tree, status, evidence))
    add_rates(rows, runs, min(r["created_at"] for r in runs.values()))
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
    """Строка таблицы гипотезы: сколько раз тест прошёл в CI до мержа и что из этого следует."""
    keyed = next((r for r in group if r["evidence"].get("source")), group[0])
    ev = keyed["evidence"]
    added = ((ev.get("source") or {}).get("added")) or {}
    pre = added.get("premerge_ci") or {}
    rate = ev.get("ci_rate") or {}
    runs, fails, execs = pre.get("runs"), rate.get("failures"), rate.get("executions")
    chance = "n/a"
    if runs and execs and fails is not None:
        chance = f"{1 - (1 - fails / execs) ** runs:.3f}"
    base = sum(1 for r in group if r["branch"] in {"develop", "main"})
    statuses = tally(group, lambda r: r["status"])
    return (
        f"| {nodeid} | {class_of(keyed)} | {statuses} | {base}/{len(group)} "
        f"| {added.get('task')} | {runs} | {fails}/{execs} | {chance} |"
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
        "| nodeid | class | statuses | rows on develop/main | source task | pre-merge CI runs (upper bound) | CI failures/executions in window | P(at least one failure in the pre-merge runs) |",
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
            job = test_job(gh, run["id"], item["run_attempt"])
            failed = [
                s["name"]
                for s in (job or {"steps": []})["steps"]
                if s["conclusion"] == "failure"
            ]
            label = failed[0] if failed else "no failed step in the pytest job"
            counts[label] = counts.get(label, 0) + 1
    total = sum(counts.values())
    return f"failed attempts={total}; first failed step: " + ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def load_manual(path: str | None) -> dict:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


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
    text = json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(f"runs={len(runs)} rows={len(rows)} api_calls={gh.calls}", file=sys.stderr)
    if args.markdown:
        print(render_markdown(rows))
    elif args.summary:
        print(f"runs={len(runs)}; {step_breakdown(gh, runs)}")
        print(summarize(rows))
    elif not args.out:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
