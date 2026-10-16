"""#1685: правила подсчёта и разметки в scripts/flaky_census.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "flaky_census",
    Path(__file__).resolve().parent.parent / "scripts" / "flaky_census.py",
)
fc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fc)

SHA = "ab12" * 10  # 40 hex, not a literal: detect-secrets reads quoted hex
NODE = "tests/test_x.py::test_a"


class _Blobs:
    """Дерево коммита: sha -> текст файла теста; нет ключа — объекта нет локально."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def read(self, spec: str) -> bytes | None:
        sha, _, path = spec.partition(":")
        if sha not in self.files:
            return None
        if not path:
            return b"commit"
        return self.files[sha].encode() if path == "tests/test_x.py" else None


def _rec(run: int, attempt: int, sha: str, failed, created="2026-10-02T00:00:00Z"):
    return {
        "run_id": run,
        "attempt": attempt,
        "created_at": created,
        "branch": "task-7/x",
        "head_sha": sha,
        "failed": failed,
    }


def test_tag_hashes_only_marks_known_sha_fields() -> None:
    data = {"tested_sha": SHA, "evidence": {"message": SHA, "commits": ["abcdef1"]}}
    tagged = fc.tag_hashes(data)
    assert tagged["tested_sha"] == "git:" + SHA
    assert tagged["evidence"]["commits"] == ["git:abcdef1"]
    assert tagged["evidence"]["message"] == SHA, "hex в сообщении не маскируется"
    assert fc.untag_hashes(tagged) == data


def test_executions_count_each_attempt_once_and_only_where_the_test_exists() -> None:
    blobs = _Blobs(
        {
            "a": "def test_a():\n pass\n",
            "b": "def other():\n pass\n",
            "c": "def test_a():\n pass\n",
        }
    )
    pool = [
        _rec(1, 1, "a", {NODE}),  # упал: выполнение и падение
        _rec(1, 2, "a", set()),  # повтор того же прогона: отдельное выполнение
        _rec(2, 1, "b", set()),  # теста в дереве нет: не выполнение
        _rec(3, 1, "missing", set()),  # объекта нет: отдельный счётчик
        _rec(4, 1, "c", None),  # шаг Test не доказан: не выполнение
    ]
    got = fc.count_executions(pool, blobs, NODE, "2026-10-01T00:00:00Z", None, None)
    assert got == {"executions": 2, "failures": 1, "unverifiable_attempts": 1}


def test_pre_merge_window_ends_at_the_merge_date_and_uses_the_task_branch() -> None:
    blobs = _Blobs({"a": "def test_a():\n pass\n"})
    pool = [
        _rec(1, 1, "a", {NODE}, "2026-10-02T00:00:00Z"),
        _rec(2, 1, "a", set(), "2026-10-09T00:00:00Z"),
    ]
    got = fc.count_executions(
        pool, blobs, NODE, "2026-10-01T00:00:00Z", "2026-10-05T00:00:00Z", "task-7/"
    )
    assert got["executions"] == 1 and got["failures"] == 1


def test_source_is_unknown_when_history_has_no_such_test(monkeypatch) -> None:
    monkeypatch.setattr(fc, "commit_lines", lambda *args: [])
    assert "source unknown" in fc.source_of(NODE, Path("."))["reason"]


def test_m1_probability_uses_the_budgeted_repeat_count() -> None:
    rows = [
        {"nodeid": NODE, "evidence": {"ci_rate": {"failures": 2, "executions": 1000}}},
        {
            "nodeid": NODE + "2",
            "evidence": {"ci_rate": {"failures": 0, "executions": 50}},
        },
    ]
    manual = {"loaded_seconds_per_repeat": {NODE: 0.5, NODE + "2": 0.1}}
    table = fc.m1_table(rows, manual)
    k = 240  # 120 s / 0.5 s
    assert f"| {k} | {1 - (1 - 0.002) ** k:.3f} |" in table
    assert "n/a (p=0)" in table and "mean over 1 not reproduced" in table


class _Proc:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _fake_gh(monkeypatch, help_text: str, replies: list[_Proc]) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[-1] == "--help":
            return _Proc(out=help_text)
        return replies.pop(0)

    monkeypatch.setattr(fc.subprocess, "run", run)
    return calls


def test_logs_request_carries_the_escape_flag_only_when_gh_knows_it(
    monkeypatch, tmp_path
) -> None:
    calls = _fake_gh(monkeypatch, "  --allow-escape-sequences  x", [_Proc(out="log")])
    fc.Gh(tmp_path / "new").get("actions/jobs/1/logs")
    assert calls[-1][-1] == "--allow-escape-sequences"

    calls = _fake_gh(monkeypatch, "  --jq  x", [_Proc(out="log"), _Proc(out="{}")])
    gh = fc.Gh(tmp_path / "old")
    gh.get("actions/jobs/1/logs")
    gh.get("actions/runs/2")
    assert "--allow-escape-sequences" not in calls[-2] + calls[-1]


def test_escape_sequence_refusal_is_retried_with_the_flag_or_named(
    monkeypatch, tmp_path
) -> None:
    refusal = _Proc(
        1,
        "",
        "the response contains terminal escape sequences; pass --allow-escape-sequences",
    )
    calls = _fake_gh(
        monkeypatch, "no flag listed", [refusal, _Proc(out="\x1b[36;1mok\x1b[0m")]
    )
    assert fc.Gh(tmp_path / "a").get("actions/jobs/1/logs") == "ok", "ANSI снят"
    assert calls[-1][-1] == "--allow-escape-sequences"

    _fake_gh(monkeypatch, "no flag listed", [refusal, refusal])
    try:
        fc.Gh(tmp_path / "b").get("actions/jobs/1/logs")
    except RuntimeError as err:
        assert "обновите gh" in str(err)
    else:
        raise AssertionError("пустой результат вместо ошибки")


# ---- #1767: the suite runs in the jobs "Tests part N/3" -----------------------

_LOG_FAILED = (
    f"x git log -1 --format=%H\n{SHA}\n"
    f"gw0 FAILED {NODE} - boom\n"
    "=== 1 failed, 5 passed in 1.00s ===\n"
)


class _FakeGh:
    """Отвечает на jobs попытки и на журналы job-ов."""

    def __init__(self, jobs: list[dict], logs: dict[int, str] | None = None) -> None:
        self.jobs = jobs
        self.logs = logs or {}
        self.read: list[int] = []

    def json(self, path: str) -> dict:
        assert path.endswith("/jobs?per_page=100"), path
        return {"jobs": self.jobs}

    def get(self, path: str, *args, **kwargs) -> str:
        job_id = int(path.split("/")[2])
        self.read.append(job_id)
        return self.logs[job_id]


def _job(job_id: int, name: str, step: str | None, conclusion: str | None = "success"):
    steps = [{"name": "Checkout", "conclusion": "success"}]
    if step is not None:
        steps.append({"name": step, "conclusion": conclusion})
    return {"id": job_id, "name": name, "steps": steps}


_BASE = {
    "id": 9,
    "event": "push",
    "head_branch": "develop",
    "head_sha": SHA,
    "created_at": "2026-10-16T00:00:00Z",
    "conclusion": "failure",
    "pull_request_base": None,
}


def _parts(*conclusions: str) -> list[dict]:
    jobs = [_job(1, "Ruff and pytest", None)]
    for i, c in enumerate(conclusions, 1):
        jobs.append(_job(10 + i, f"Tests part {i}/3", fc.TEST_STEP_NAME, c))
    return jobs


def test_one_failed_part_fails_the_attempt_and_its_log_is_read(monkeypatch) -> None:
    monkeypatch.setattr(fc, "pr_base", lambda run: "")
    gh = _FakeGh(_parts("success", "failure", "success"), {12: _LOG_FAILED})
    jobs = fc.test_jobs(gh, 9, 1)
    assert [j["id"] for j in jobs] == [11, 12, 13]
    assert fc.test_step_state(jobs) == "failure"
    rec = fc.failed_attempt(gh, _BASE, 1)
    assert rec["test_step"] == "failure" and rec["log"] == "ok"
    assert rec["nodeids"] == [NODE]
    assert gh.read == [12], "only the failed part's log is read"
    # several failed parts: each one's log is read
    gh = _FakeGh(
        _parts("failure", "failure", "success"), {11: _LOG_FAILED, 12: _LOG_FAILED}
    )
    fc.failed_attempt(gh, _BASE, 1)
    assert gh.read == [11, 12]


def test_an_attempt_is_green_only_when_every_part_is_green() -> None:
    def state(*conclusions: str) -> str:
        return fc.test_step_state(fc.test_jobs(_FakeGh(_parts(*conclusions)), 9, 1))

    assert state("success", "success", "success") == "success"
    assert state("success", "success", "skipped") == "skipped"
    assert state("success", "success", "failure") == "failure"
    gh = _FakeGh(_parts("success", "success", "success"))
    run = {"id": 9, "run_attempt": 1}
    assert fc.green_attempts(gh, run) == [1]
    gh = _FakeGh(_parts("success", "cancelled", "success"))
    assert fc.green_attempts(gh, run) == []


def test_missing_parts_or_a_missing_test_step_is_unknown_not_clean() -> None:
    # no parts and no old job: nothing is known
    assert fc.test_step_state(fc.test_jobs(_FakeGh([_job(1, "Lint", None)]), 9, 1)) == (
        "no_job"
    )
    # the new job without its Test step, whether alone or next to green parts
    only_new = _FakeGh(_parts("success")[:1])  # just "Ruff and pytest", no Test
    assert fc.test_step_state(fc.test_jobs(only_new, 9, 1)) == "no_step"
    jobs = [
        _job(11, "Tests part 1/3", fc.TEST_STEP_NAME),
        _job(12, "Tests part 2/3", None),
    ]
    assert fc.test_step_state(jobs) == "no_step"


def test_a_run_before_the_split_is_still_read() -> None:
    old = [_job(5, "Ruff and pytest", fc.TEST_STEP_NAME, "failure")]
    gh = _FakeGh(old, {5: _LOG_FAILED})
    assert fc.test_step_state(fc.test_jobs(gh, 9, 1)) == "failure"
    ok = _FakeGh([_job(5, "Ruff and pytest", fc.TEST_STEP_NAME)])
    assert fc.green_attempts(ok, {"id": 9, "run_attempt": 1}) == [1]


def test_the_names_the_census_searches_for_are_the_names_in_ci_yml() -> None:
    import yaml

    doc = yaml.safe_load(
        (
            Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml"
        ).read_text()
    )
    part = doc["jobs"]["pytest"]
    assert part["name"].startswith(fc.PART_JOB_PREFIX), part["name"]
    assert fc.TEST_STEP_NAME in [s["name"] for s in part["steps"]]
    # the pre-split layout is gone from the workflow, so nothing else may match
    assert fc.PART_JOB_PREFIX not in doc["jobs"]["test"]["name"]
    assert fc.TEST_STEP_NAME not in [
        s.get("name") for s in doc["jobs"]["test"]["steps"]
    ]
