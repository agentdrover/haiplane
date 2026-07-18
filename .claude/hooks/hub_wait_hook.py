#!/usr/bin/env python3
"""Stop-hook: ждёт событий человека в OpenClaw Hub и будит агента.

Механизм:
- Агент перед завершением хода пишет .claude/hub-wait.json со списком ожиданий:
    {"waits": [{"task_id": 346, "reason": "жду вердикт ревью",
                "baseline": {"status": "review", "verdict": null}}]}
  baseline — снимок полей, изменение которых означает "кнопка нажата".
- Хук (asyncRewake) запускается в фоне при каждом Stop:
  * нет файла ожиданий — мгновенно выходит (exit 0);
  * есть — поллит хаб раз в POLL_SEC; как только live-значения полей из baseline
    отличаются, печатает событие и выходит с кодом 2 → Claude Code будит агента;
  * по таймауту MAX_WAIT_SEC выходит тихо (exit 0), файл ожиданий остаётся —
    следующий Stop или сообщение пользователя перезапустит ожидание.
- Лок-файл защищает от параллельных поллеров при повторных Stop.

Токен и URL берутся из ~/.claude.json (mcpServers.openclaw-hub) — как у MCP.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CLAUDE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = CLAUDE_DIR / "hub-wait.json"
LOCK_FILE = CLAUDE_DIR / "hub-wait.lock"
POLL_SEC = int(os.environ.get("HUB_WAIT_POLL_SEC", "15"))  # fallback без фида
FEED_WAIT_SEC = int(os.environ.get("HUB_WAIT_FEED_SEC", "55"))  # long-poll #349
MAX_WAIT_SEC = int(os.environ.get("HUB_WAIT_MAX_SEC", "14400"))  # 4 часа


def hub_config() -> tuple[str, str]:
    cfg = json.loads((Path.home() / ".claude.json").read_text())
    srv = cfg["mcpServers"]["openclaw-hub"]
    base = srv["url"].rsplit("/mcp", 1)[0].rstrip("/")
    return base, srv["headers"]["Authorization"]


def fetch_task(base: str, auth: str, task_id: int) -> dict | None:
    req = urllib.request.Request(
        f"{base}/api/tasks/{task_id}", headers={"Authorization": auth}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None  # сеть моргнула — попробуем в следующем цикле


def fetch_events(base: str, auth: str, since: int, wait: int = 0) -> dict | None:
    """Long-poll событийного фида (#349): ответ сразу при новом событии."""
    req = urllib.request.Request(
        f"{base}/api/events?since={since}&wait={wait}",
        headers={"Authorization": auth},
    )
    try:
        with urllib.request.urlopen(req, timeout=wait + 25) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None  # фид недоступен (старый прод?) — вернёмся к поллингу


def feed_tail_cursor(base: str, auth: str) -> int | None:
    """Домотать курсор до хвоста фида, чтобы ждать только новые события."""
    cursor = 0
    for _ in range(50):  # прюнинг держит фид коротким; это страховка
        page = fetch_events(base, auth, cursor)
        if page is None:
            return None
        if not page.get("events"):
            return page.get("next_cursor", cursor)
        cursor = page["next_cursor"]
    return cursor


def fetch_project(base: str, auth: str, project_id: int) -> dict | None:
    """Ожидания по кнопкам проекта (Activate/Provision): /api/projects."""
    req = urllib.request.Request(
        f"{base}/api/projects?include_archived=true",
        headers={"Authorization": auth},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            for p in json.load(resp):
                if p.get("id") == project_id:
                    return p
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        pass
    return None


def live_value(task: dict, field: str):
    """status | verdict | verdict_at | updated_at | decision_count и т.п."""
    if field == "verdict":
        return (task.get("latest_review") or {}).get("verdict")
    if field == "verdict_at":
        lr = task.get("latest_review") or {}
        return lr.get("created_at") or lr.get("submitted_at")
    return task.get(field)


def changed_fields(task: dict, baseline: dict) -> dict:
    diff = {}
    for field, expected in baseline.items():
        actual = live_value(task, field)
        if actual != expected:
            diff[field] = {"was": expected, "now": actual}
    return diff


def acquire_lock() -> bool:
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)  # жив ли предыдущий поллер
            return False
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            LOCK_FILE.unlink(missing_ok=True)
            return acquire_lock()


def main() -> int:
    try:
        sys.stdin.read()  # payload Stop-хука не нужен, но стрим надо дочитать
    except Exception:
        pass

    if not STATE_FILE.exists():
        return 0
    try:
        state = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    waits = state.get("waits") or []
    if not waits:
        return 0

    if not acquire_lock():
        return 0
    try:
        base, auth = hub_config()
        deadline = time.monotonic() + MAX_WAIT_SEC
        cursor = feed_tail_cursor(base, auth)  # None → фид недоступен
        first_pass = True
        while time.monotonic() < deadline:
            if not STATE_FILE.exists():  # агент снял ожидание — уходим
                return 0
            # Перечитываем ожидания КАЖДЫЙ цикл: агент мог переписать файл
            # после старта поллера; работа по снимку со старта затирала
            # свежие ожидания при срабатывании (инцидент #383/#385).
            try:
                state = json.loads(STATE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                return 0
            waits = state.get("waits") or []
            if not waits:
                return 0
            # Просыпаемся от событий фида (#349, ~1с реакции); baseline по
            # задачам остаётся источником истины. Первый проход всегда
            # проверяет baseline напрямую: событие могло случиться до
            # установки курсора. Без фида — старый 15с-поллинг.
            if not first_pass and cursor is not None:
                page = fetch_events(base, auth, cursor, wait=FEED_WAIT_SEC)
                if page is not None:
                    cursor = page.get("next_cursor", cursor)
                else:
                    time.sleep(POLL_SEC)
            elif not first_pass:
                time.sleep(POLL_SEC)
            first_pass = False
            fired, remaining = [], []
            for wait in waits:
                if wait.get("project_id"):
                    obj = fetch_project(base, auth, wait["project_id"])
                else:
                    obj = fetch_task(base, auth, wait["task_id"])
                if obj is None:
                    remaining.append(wait)
                    continue
                diff = changed_fields(obj, wait.get("baseline") or {})
                if diff:
                    fired.append((wait, obj, diff))
                else:
                    remaining.append(wait)
            if fired:
                if remaining:
                    state["waits"] = remaining
                    STATE_FILE.write_text(
                        json.dumps(state, ensure_ascii=False, indent=2)
                    )
                else:
                    STATE_FILE.unlink(missing_ok=True)
                lines = ["Событие в OpenClaw Hub — продолжай работу по плану:"]
                for wait, obj, diff in fired:
                    parts = ", ".join(
                        f"{f}: {c['was']!r} → {c['now']!r}" for f, c in diff.items()
                    )
                    if wait.get("project_id"):
                        subject = (
                            f"проект #{wait['project_id']} «{obj.get('slug', '')}»"
                        )
                    else:
                        subject = f"задача #{wait['task_id']} «{obj.get('title', '')}»"
                    lines.append(
                        f"- {subject}: {parts}. "
                        f"Ожидание было: {wait.get('reason', '—')}."
                    )
                msg = "\n".join(lines)
                print(msg)
                print(msg, file=sys.stderr)
                return 2  # exit 2 + asyncRewake → Claude Code будит агента
        return 0  # таймаут: ожидания остаются, следующий Stop перезапустит
    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
