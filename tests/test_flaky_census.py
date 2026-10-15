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

SHA = "573a671a9527d910d011100eb015a6a8e4bd6c61"
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
