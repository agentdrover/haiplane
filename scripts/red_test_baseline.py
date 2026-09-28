"""Прогнать тесты ветки поверх кода merge-base: что падало до фикса (#913).

Шаг CI на ветках task-*. Отвечает на один вопрос: падал ли тест, который
ветка принесла или изменила, на коде ДО её фикса. Прогон на самом merge-base
ничего не доказывает — нового теста там нет, — поэтому код берётся из базы, а
изменённые в диффе файлы ``tests/**`` — из ветки:

1. ``git worktree`` на merge-base ветки с базой, отдельно от рабочего дерева;
2. в него копируются изменённые в диффе ``tests/**`` (включая conftest и
   хелперы) в редакции вершины ветки;
3. pytest по изменённым тестовым модулям с ``--junitxml``;
4. статус по каждому nodeid: ``failed`` (assert), ``passed``, ``error``
   (упавшая сборка модуля, ImportError, упавшая фикстура), ``skipped``.

Красным хаб считает только ``failed``; различать failure и error надёжно
можно только по junitxml. Падение внутри теста на ImportError тоже ``error``:
тест упал не на проверке, а на том, чего в базе ещё нет.

Два правила, как у ci_report_to_hub.py: скрипт никогда не красит джоб (любой
исход — exit 0 и JSON с причиной), и падение теста на базе — это данные, а не
ошибка. Результат уходит в отчёт хабу полем ``baseline``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess  # nosec B404 - git and pytest with fixed argv, in CI
import sys
import tempfile
import time
import xml.etree.ElementTree as ET  # nosec B405 - junit XML pytest just wrote
from pathlib import Path, PurePosixPath

TESTS_DIR = "tests/"
_RUN_TIMEOUT = 480
_MESSAGE_MAX = 300
#: Сообщения, по которым провал внутри теста — неверная причина, а не assert.
_WRONG_REASON = ("ImportError", "ModuleNotFoundError")


def log(msg: str) -> None:
    print(f"[red-test-baseline] {msg}", flush=True)


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(  # nosec B603 B607 - git with fixed argv
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def is_test_module(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def changed_test_files(repo: Path, merge_base: str, head: str) -> list[str]:
    """Изменённые в диффе и существующие на вершине файлы ``tests/**``."""
    out = git(
        repo, "diff", "--name-only", "--diff-filter=d", merge_base, head, "--", "tests"
    )
    return sorted(p for p in out.splitlines() if p.startswith(TESTS_DIR))


def copy_from_head(repo: Path, head: str, paths: list[str], dest: Path) -> None:
    """Положить файлы в редакции ``head`` в дерево ``dest``."""
    for rel in paths:
        blob = subprocess.run(  # nosec B603 B607 - git with fixed argv
            ["git", "show", f"{head}:{rel}"], cwd=repo, check=True, capture_output=True
        ).stdout
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)


def _nodeid(case: ET.Element) -> str:
    """nodeid из testcase junit (xunit1: есть атрибут file)."""
    file = case.get("file") or ""
    name = case.get("name") or ""
    parts = (case.get("classname") or "").split(".")
    stem = PurePosixPath(file).stem
    rest = parts[parts.index(stem) + 1 :] if stem in parts else []
    return "::".join([file, *rest, name])


def _case_status(case: ET.Element) -> str:
    for child in case:
        if child.tag == "error":
            return "error"
        if child.tag == "failure":
            message = child.get("message") or ""
            return "error" if message.startswith(_WRONG_REASON) else "failed"
        if child.tag == "skipped":
            return "skipped"
    return "passed"


def parse_junit(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """({nodeid: статус}, {файл: причина}) — второй для несобравшихся модулей."""
    tests: dict[str, str] = {}
    collection_errors: dict[str, str] = {}
    root = ET.parse(path).getroot()  # nosec B314 - file pytest just wrote
    for case in root.iter("testcase"):
        if not (case.get("classname") or "") and case.find("error") is not None:
            text = (case.find("error").text or "").strip().splitlines()  # type: ignore[union-attr]
            last = next((ln for ln in reversed(text) if ln.startswith("E ")), "")
            file = case.get("file") or case.get("name") or ""
            collection_errors[file] = (last[1:].strip() or "collection failure")[
                :_MESSAGE_MAX
            ]
            continue
        tests[_nodeid(case)] = _case_status(case)
    return tests, collection_errors


def run_pytest(
    tree: Path, modules: list[str], runner: list[str], timeout: int
) -> tuple[int, Path]:
    """pytest по ``modules`` в ``tree``; (rc, путь к junit)."""
    junit = tree / ".red-test-baseline.xml"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(tree), env.get("PYTHONPATH", "")) if p
    )
    argv = [
        *runner,
        "-q",
        "-p",
        "no:cacheprovider",
        "--continue-on-collection-errors",
        f"--junitxml={junit}",
        "-o",
        "junit_family=xunit1",
        *modules,
    ]
    # Вывод не нужен (данные — junitxml) и копить его в памяти нельзя (#509).
    # Своя группа процессов: таймаут убивает и внуков, а не только pytest.
    proc = subprocess.Popen(  # nosec B603 - fixed runner argv over our own files
        argv,
        cwd=tree,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return proc.wait(timeout=timeout), junit
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise


def _counts(tests: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for status in tests.values():
        out[status] = out.get(status, 0) + 1
    return out


def _run_in_worktree(
    repo: Path, report: dict, copied: list[str], runner: list[str], timeout: int
) -> None:
    """Worktree на merge-base, копия тестов ветки, прогон; всё — в ``report``."""
    tree = Path(tempfile.mkdtemp(prefix="red-test-baseline-"))
    try:
        git(repo, "worktree", "add", "--detach", str(tree), report["merge_base"])
        copy_from_head(repo, report["head"], copied, tree)
        rc, junit = run_pytest(tree, report["ran_files"], runner, timeout)
        report["pytest_rc"] = rc
        if not junit.exists():
            report.update(state="error", reason=f"pytest rc={rc} без junitxml")
            return
        tests, errors = parse_junit(junit)
        report.update(
            state="ran", tests=tests, collection_errors=errors, counts=_counts(tests)
        )
    finally:
        subprocess.run(  # nosec B603 B607 - git with fixed argv
            ["git", "worktree", "remove", "--force", str(tree)],
            cwd=repo,
            capture_output=True,
        )
        shutil.rmtree(tree, ignore_errors=True)
        subprocess.run(  # nosec B603 B607 - git with fixed argv
            ["git", "worktree", "prune"], cwd=repo, capture_output=True
        )


def build_baseline(
    repo: Path,
    base: str,
    *,
    head: str = "HEAD",
    runner: list[str] | None = None,
    timeout: int = _RUN_TIMEOUT,
) -> dict:
    """Baseline ветки против ``base``; никогда не бросает — причина в JSON."""
    report: dict = {"state": "error", "base_ref": base, "tests": {}, "reason": ""}
    started = time.monotonic()
    try:
        report["head"] = git(repo, "rev-parse", head)
        report["merge_base"] = git(repo, "merge-base", base, report["head"])
        copied = changed_test_files(repo, report["merge_base"], report["head"])
        report["copied"] = copied
        report["ran_files"] = [p for p in copied if is_test_module(p)]
        if not report["ran_files"]:
            report.update(state="no_tests", reason="дифф не меняет тестовых модулей")
        else:
            _run_in_worktree(
                repo,
                report,
                copied,
                runner or [sys.executable, "-m", "pytest"],
                timeout,
            )
    except subprocess.TimeoutExpired:
        report.update(state="error", reason=f"pytest дольше {timeout} с")
    except (subprocess.CalledProcessError, OSError, ET.ParseError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        report.update(state="error", reason=str(detail).strip()[:_MESSAGE_MAX])
    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base", required=True, help="база ветки, напр. origin/develop"
    )
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--timeout", type=int, default=_RUN_TIMEOUT)
    args = parser.parse_args(argv)

    report = build_baseline(Path.cwd(), args.base, head=args.head, timeout=args.timeout)
    Path(args.json_out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(
        f"state={report['state']} merge_base={str(report.get('merge_base'))[:12]} "
        f"files={report.get('ran_files', [])} counts={report.get('counts', {})} "
        f"collection_errors={sorted(report.get('collection_errors', {}))} "
        f"{report.get('reason', '')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
