"""Стенд GUI-тестов и их место в CI: то, что проверяется без браузера (#1996).

Браузерные сценарии лежат в tests/gui и в обычный набор не входят: им нужен
Chromium. Здесь проверяются три вещи, которым браузер не нужен:

* AC-1: обычный сбор тестов не видит tests/gui, явная GUI-команда видит;
* AC-2: в ci.yml есть необязательный job GUI с установкой Chromium, таймаутом и
  артефактами при сбое, а основные jobs tests/gui не запускают;
* AC-3: стенд (локальный хаб в режиме CSRF require) отвечает /health, режет POST
  без токена и пропускает POST с токеном страницы, а после остановки порт свободен
  даже тогда, когда тест упал исключением.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _collect(*args: str) -> list[str]:
    """Узлы, которые pytest соберёт с такими аргументами (без запуска)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-1500:]
    return [line for line in done.stdout.splitlines() if "::" in line]


def test_gui_tests_are_excluded_from_the_default_run() -> None:
    gui_files = sorted((ROOT / "tests" / "gui").glob("test_*.py"))
    assert gui_files, "tests/gui не содержит ни одного test_*.py: исключать нечего"

    default_run = _collect()
    assert default_run, "обычный сбор не нашёл ни одного теста"
    assert not [n for n in default_run if n.startswith("tests/gui")]

    # Явная команда собирает их: так их запускает make gui-test.
    explicit = _collect("tests/gui")
    assert explicit and all(n.startswith("tests/gui") for n in explicit)

    made = subprocess.run(
        ["make", "-n", "gui-test"], cwd=ROOT, capture_output=True, text=True
    )
    assert made.returncode == 0, made.stderr
    assert "pytest" in made.stdout and "tests/gui" in made.stdout


def _jobs() -> dict[str, dict]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def test_ci_has_an_optional_gui_job() -> None:
    jobs = _jobs()
    gui = jobs.get("gui")
    assert gui is not None, "в ci.yml нет job gui"
    assert gui["name"] == "GUI"
    # Необязателен: красный GUI не делает красным прогон (гейт доставки читает его).
    assert gui["continue-on-error"] is True
    assert 0 < int(gui["timeout-minutes"]) <= 30

    steps = gui["steps"]
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    assert re.search(r"playwright install\b[^\n]*--with-deps[^\n]*chromium", runs)
    # Браузеры кэшируются по версии Playwright и ОС.
    caches = [s for s in steps if str(s.get("uses", "")).startswith("actions/cache@")]
    assert caches and "ms-playwright" in str(caches[0]["with"]["path"])
    key = str(caches[0]["with"]["key"])
    assert "runner.os" in key and "hashFiles('uv.lock')" in key
    # GUI запускается явной командой.
    assert "make gui-test" in runs
    # При сбое сохраняются trace, снимки экрана и лог сервера.
    uploads = [
        s
        for s in steps
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert uploads, "нет загрузки артефактов"
    failure_upload = uploads[0]
    assert "failure()" in str(failure_upload["if"])
    assert failure_upload["with"]["path"]
    # Ни секретов, ни токена отчёта: job исполняет код ветки.
    assert "secrets." not in yaml.safe_dump(gui)
    # Основные jobs tests/gui не запускают.
    for name, job in jobs.items():
        if name == "gui":
            continue
        for step in job.get("steps") or []:
            run = str(step.get("run", ""))
            assert "tests/gui" not in run and "gui-test" not in run, (name, step)


def _free(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def test_stand_enforces_csrf_and_cleans_up(tmp_path: Path) -> None:
    import httpx

    from tests.gui.conftest import running_stand

    seen: dict = {}
    with pytest.raises(RuntimeError, match="boom"):
        with running_stand(tmp_path) as stand:
            seen["port"], seen["pid"] = stand.port, stand.process.pid
            task_id = stand.seed_task(title="возврат на стенде", status="review")
            assert httpx.get(f"{stand.url}/health", timeout=10).status_code == 200

            cookies = {stand.cookie_name: stand.session}
            action = f"{stand.url}/tasks/{task_id}/web-return-to-work"
            # Без токена страницы cookie-сессия не пишет: стенд защиту не обходит.
            refused = httpx.post(
                action,
                data={"reason": "причина"},
                cookies=cookies,
                headers={"Origin": stand.url},
                follow_redirects=False,
                timeout=10,
            )
            assert refused.status_code == 403
            assert "csrf_failed" in refused.text
            assert stand.status_of(task_id) == "review"

            # Адрес стенда не тот, что в HUB_URL: тоже отказ.
            alien = httpx.post(
                action,
                data={"reason": "причина"},
                cookies=cookies,
                headers={"Origin": stand.url.replace("127.0.0.1", "localhost")},
                follow_redirects=False,
                timeout=10,
            )
            assert alien.status_code == 403

            page = httpx.get(
                f"{stand.url}/tasks/{task_id}", cookies=cookies, timeout=20
            )
            assert page.status_code == 200
            token = re.search(r'name="csrf-token" content="([0-9a-f]+)"', page.text)
            assert token, "страница не отдала токен CSRF"
            allowed = httpx.post(
                action,
                data={"reason": "причина"},
                cookies=cookies,
                headers={"Origin": stand.url, "X-CSRF-Token": token.group(1)},
                follow_redirects=False,
                timeout=10,
            )
            assert allowed.status_code == 303, allowed.text[:500]
            assert stand.status_of(task_id) == "open"
            raise RuntimeError("boom")

    assert stand_exited(seen["pid"])
    assert _free(seen["port"]), "порт стенда занят после остановки"


def stand_exited(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # Процесс ещё числится: дочерний уже собран wait(), значит это чужой pid.
    return False
