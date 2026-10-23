"""Общие помощники и таблица пар для tests/test_comment_only_diff.py (#2032).

Вынесены, чтобы файл тестов оставался в пороге размера дерева tests (800 строк).
Каждая запись таблицы — пара «было → стало» одного файла (плюс неизменные
соседи) и ожидаемый вид нарушения; тесты гоняют её через настоящий git.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.comment_only_diff_cases import CASES, CLEAN

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
SCRIPT = SCRIPTS / "comment_only_diff.py"
sys.path.insert(0, str(SCRIPTS))

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.test",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _commit(repo: Path, files: Mapping[str, str | bytes | None], msg: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _pair(
    tmp_path: Path,
    base: Mapping[str, str | bytes],
    head: Mapping[str, str | bytes | None],
) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    base_sha = _commit(repo, base, "base")
    head_sha = _commit(repo, head, "head")
    return repo, base_sha, head_sha


def _argv(repo: Path, base: str, head: str, files: list[str], *extra: str) -> list[str]:
    return ["--repo", str(repo), "--base", base, "--head", head, *extra, *files]


def _run(
    repo: Path, base: str, head: str, files: list[str], *extra: str
) -> tuple[int, str]:
    """Проверка в этом же процессе: код возврата и весь вывод."""
    import comment_only_diff

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = comment_only_diff.main(_argv(repo, base, head, files, *extra))
    return rc, buffer.getvalue()


def run_process(
    repo: Path, base: str, head: str, files: list[str], *extra: str
) -> tuple[int, str]:
    """Настоящий запуск скрипта: код возврата процесса, а не возврат функции."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *_argv(repo, base, head, files, *extra)],
        capture_output=True,
        text=True,
        env={**_GIT_ENV},
    )
    return proc.returncode, proc.stdout + proc.stderr


def _case_params() -> list:
    return [pytest.param(name, id=name) for name in CASES]


def _run_case(tmp_path: Path, name: str) -> tuple[int, str]:
    case = CASES[name]
    both = case.get("extra_both", {})
    target = case.get("file", "m.py")
    base = {target: case["base"], **both}
    head: dict[str, str | bytes | None] = {target: case["head"], **both}
    head.update(case.get("extra_head", {}))
    repo, base_sha, head_sha = _pair(tmp_path, base, head)
    extra = [a for name in case.get("allow", []) for a in ("--allow-docstring", name)]
    extra += [a for name in case.get("allow_files", []) for a in ("--allow-file", name)]
    return _run(repo, base_sha, head_sha, [target], *extra)


def _check_case(name: str, rc: int, out: str) -> None:
    kind = CASES[name]["kind"]
    if kind is CLEAN:
        assert rc == 0, f"{name}: ждали rc=0, получили {rc}\n{out}"
    elif kind == "error":
        assert rc == 2, f"{name}: ждали rc=2, получили {rc}\n{out}"
    else:
        assert rc == 1, f"{name}: ждали rc=1, получили {rc}\n{out}"
        assert f"[{kind}]" in out, f"{name}: нет нарушения {kind}\n{out}"
