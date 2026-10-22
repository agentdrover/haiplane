"""Стенд GUI-тестов: настоящий хаб в режиме CSRF require и браузер Playwright (#1996).

Серверные тесты (TestClient) не видят JS, HTMX, автообновление и то, как браузер
отправляет форму. Стенд поднимает ``uvicorn hub.app:app`` отдельным процессом на
свободном порту 127.0.0.1 с временной базой и ведёт себя как прод в тех частях,
где прод ломался:

* ``HAIPLANE_CSRF_MODE=require`` и токены заданы (без них аутентификация выключена
  и CSRF не проверяется вовсе);
* ``HAIPLANE_HUB_URL`` — РОВНО адрес, по которому ходит браузер: ``localhost``
  вместо ``127.0.0.1`` даёт ``origin_mismatch`` и «Форма устарела»;
* человек и его cookie-сессия созданы admin-сервисом, без пароля;
* состояние проверяется отдельным соединением к базе, а не ответом страницы.

Модуль безопасно импортировать без Playwright: он нужен только фикстурам браузера
и подгружается внутри них. Так тест стенда (tests/test_gui_harness.py) идёт в
обычном наборе, где Chromium нет.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest

from hub import brand

ROOT = Path(__file__).resolve().parents[2]

# HTMX страницы хаба (hub/templates/base.html) — из репозитория, а не с unpkg в
# момент прогона: сбой сети не должен красить GUI-job. Версия и хеш закреплены;
# смена версии в шаблоне без обновления файла роняет test_gui_harness.
HTMX_VERSION = "2.0.4"
HTMX_FILE = Path(__file__).resolve().parent / "vendor" / f"htmx-{HTMX_VERSION}.min.js"
HTMX_SHA384 = (
    "1c67f3b687e8b5fb21705efef27e382502f6a099"  # pragma: allowlist secret
    "a8c150a13d3838f12fa3bd9a33b4f7efc03efd382fcb4ed1ba74ca7e"  # pragma: allowlist secret
)
# Перехватывается весь unpkg: запрос HTMX закреплённой версии (с /dist и без)
# получает файл из репозитория, любой другой — отказ. Так промах шаблона не
# уходит молча в сеть, а роняет test_htmx_is_loaded_on_the_card.
UNPKG_GLOB = "https://unpkg.com/**"
HTMX_URL_PREFIX = f"https://unpkg.com/htmx.org@{HTMX_VERSION}"


def htmx_bytes() -> bytes:
    """Файл HTMX с проверкой хеша; нет файла или хеш другой — исключение."""
    body = HTMX_FILE.read_bytes()
    digest = hashlib.sha384(body).hexdigest()
    if digest != HTMX_SHA384:
        raise AssertionError(f"{HTMX_FILE.name}: sha384 {digest} != закреплённый")
    return body


STARTUP_SECONDS = 90.0

T = TypeVar("T")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _in_thread(factory: Callable[[], Awaitable[T]]) -> T:
    """Выполнить корутину в своём потоке: у синхронного Playwright цикл уже занят."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()  # type: ignore[arg-type]


@dataclass
class Stand:
    """Запущенный хаб: адрес, база, процесс, сессия человека."""

    url: str
    port: int
    db_path: Path
    log_path: Path
    process: subprocess.Popen[bytes]
    api_token: str
    session: str = ""
    human_id: int = 0
    cookie_name: str = brand.COOKIE_NAME
    _extra: dict[str, Any] = field(default_factory=dict)

    # ---- состояние: отдельное соединение, только чтение ----

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def status_of(self, task_id: int) -> str:
        rows = self.query("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert rows, f"задачи #{task_id} нет в базе"
        return str(rows[0]["status"])

    def task(self, task_id: int) -> dict[str, Any]:
        rows = self.query("SELECT * FROM tasks WHERE id=?", (task_id,))
        assert rows, f"задачи #{task_id} нет в базе"
        return rows[0]

    def updates_of(self, task_id: int) -> list[dict[str, Any]]:
        return self.query(
            "SELECT kind, agent, content FROM task_updates WHERE task_id=? ORDER BY id",
            (task_id,),
        )

    def wait_for(
        self, check: Callable[[], T], *, what: str, seconds: float = 20.0
    ) -> T:
        """Ждать состояния в базе опросом, а не паузой."""
        deadline = time.monotonic() + seconds
        last: Any = None
        while time.monotonic() < deadline:
            last = check()
            if last:
                return last
            time.sleep(0.2)
        raise AssertionError(f"не дождались: {what} (последнее значение: {last!r})")

    # ---- сид: тем же кодом хаба, что пишет в базу сервер ----

    def run(self, work: Callable[[Any], Awaitable[T]]) -> T:
        """Выполнить ``work(conn)`` на отдельном соединении и зафиксировать запись."""
        from hub import db as hub_db

        async def go() -> T:
            conn = await hub_db.connect(str(self.db_path))
            try:
                result = await work(conn)
                await conn.commit()
                return result
            finally:
                await conn.close()

        return _in_thread(go)

    def seed_task(self, *, title: str, status: str = "open", **fields: Any) -> int:
        from hub import repository as repo

        async def work(conn: Any) -> int:
            task_id = await repo.create_task(
                conn,
                title=title,
                description=fields.pop("description", ""),
                runtime="auto",
                source="human",
                assigned_agent="",
                rationale="",
                status=status,
                auto_review=True,
                task_type=fields.pop("task_type", "task"),
                parent_id=None,
                priority="medium",
            )
            if fields:
                await repo.update_task(conn, task_id, **fields)
            return task_id

        return self.run(work)

    def seed_review_task(self, *, title: str, sha: str = "a" * 40) -> int:
        """Задача в review с одной сдачей: вердикт и возврат имеют к чему применяться."""
        from hub import repository as repo

        task_id = self.seed_task(title=title, status="review")

        async def work(conn: Any) -> None:
            await repo.update_task(
                conn,
                task_id,
                submission_generation=1,
                submission_sha=sha,
            )
            await repo.record_submission(
                conn, task_id=task_id, generation=1, sha=sha, base_branch="develop"
            )

        self.run(work)
        return task_id

    def seed_ready_draft(self, *, title: str) -> int:
        """Черновик, прошедший DoR: «Approve to Backlog» его принимает."""
        body = {
            "title": title,
            "description": "Черновик для GUI-сценария одобрения.",
            "user_story": "Как владелец, я хочу одобрять черновики кнопкой.",
            "problem_statement": "Кнопка одобрения должна работать в браузере.",
            "business_value": "Одобрение не ломается незаметно.",
            "size": "S",
            "wip_tag": "feature_work",
            "plain_summary": "Черновик для проверки кнопки одобрения.",
            "scope_in": ["tests/gui: сценарий одобрения"],
            "validation_commands": ["uv run pytest -q tests/gui"],
            "affected_areas": ["tests/gui"],
        }
        created = httpx.post(
            f"{self.url}/api/tasks",
            json=body,
            headers={"Authorization": f"Bearer {self.api_token}"},
            timeout=20,
        )
        assert created.status_code == 200, created.text
        task_id = int(created.json()["id"])
        criterion = httpx.post(
            f"{self.url}/api/tasks/{task_id}/acceptance_criteria",
            json={
                "id": "AC-1",
                "given": "черновик",
                "when": "человек жмёт одобрение",
                "then": "задача уходит в open",
                "verifiable_by": "manual",
            },
            headers={"Authorization": f"Bearer {self.api_token}"},
            timeout=20,
        )
        assert criterion.status_code in (200, 201), criterion.text

        async def work(conn: Any) -> None:
            from hub import repository as repo

            await repo.update_task(conn, task_id, status="draft")

        self.run(work)
        return task_id

    def seed_slot_world(self, *, title: str, host: str = "k8") -> int:
        """Проект в режиме manual, включённый хост с RSA-ключом и свободная open-задача.

        Потребителя очереди заказов нет, поэтому заказ после нажатия остаётся
        pending: это и проверяет сценарий.
        """
        import json

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from hub import repository as repo
        from hub.services import admin as admin_svc

        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        public_pem = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        human_id = self.human_id

        async def world(conn: Any) -> tuple[int, str]:
            project_id = await repo.create_project(conn, slug="k8-gui", name="k8-gui")
            await repo.update_project(
                conn,
                project_id,
                gate_policy=json.dumps({"executor_launch": "manual"}),
                forge="github",
                repo="agentdrover/haiplane",
            )
            issued = await admin_svc.create_api_key(conn, human_id, name="gui-stand")
            return project_id, str(issued["plaintext_key"])

        project_id, human_key = self.run(world)
        created = httpx.post(
            f"{self.url}/api/admin/executor-hosts",
            json={"name": host, "public_key_pem": public_pem},
            headers={"Authorization": f"Bearer {human_key}"},
            timeout=30,
        )
        assert created.status_code == 200, created.text
        task_id = self.seed_task(title=title, status="open", project_id=project_id)

        async def branch(conn: Any) -> None:
            await repo.update_task(conn, task_id, branch=f"task-{task_id}/work")

        self.run(branch)
        return task_id


def _stand_env(root: Path, port: int, api_token: str) -> dict[str, str]:
    # Ничего из окружения разработчика, что могло бы быть боевым.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("HAIPLANE_", "CURSOR_", "GH_", "GITHUB_"))
    }
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    # Карточка review читает дифф из рабочей копии проекта и без неё отвечает 500:
    # пустой git-репозиторий хватает, сдача в сиде на коммит не опирается.
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", str(workspace)], check=True, capture_output=True
    )
    env.update(
        {
            "HAIPLANE_HUB_HOME": str(home),
            "HAIPLANE_HUB_DB": str(root / "hub.db"),
            "HAIPLANE_WORKSPACE_REPO": str(workspace),
            "HAIPLANE_HUB_URL": f"http://127.0.0.1:{port}",
            "HAIPLANE_CSRF_MODE": "require",
            "HAIPLANE_HUB_TOKENS": f"probe:{api_token}:admin",
            "HAIPLANE_HUB_CSRF_SECRET": "gui-stand-csrf-secret-1996",  # pragma: allowlist secret
            "HAIPLANE_HUB_ALLOWED_HOSTS": "127.0.0.1,localhost",
            # Хаб не должен ходить наружу из тестового стенда.
            "HAIPLANE_EGRESS_WATCH": "off",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _wait_ready(process: subprocess.Popen[bytes], url: str, log_path: Path) -> None:
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"стенд завершился с кодом {process.returncode} до готовности:\n"
                + _tail(log_path)
            )
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError(
        f"стенд не ответил на /health за {STARTUP_SECONDS:.0f} с:\n" + _tail(log_path)
    )


def _tail(path: Path, lines: int = 40) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError as exc:
        return f"(лог недоступен: {exc})"


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Остановить свой процесс (по PID, не по шаблону имени) и собрать его."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)


def _seed_human(stand: Stand) -> None:
    from hub.services import admin as admin_svc

    async def work(conn: Any) -> tuple[int, str]:
        human = await admin_svc.create_principal(
            conn, kind="human", username="probe-owner", role_slug="admin"
        )
        return int(human["id"]), await admin_svc.create_browser_session(
            conn, int(human["id"])
        )

    stand.human_id, stand.session = stand.run(work)


@contextmanager
def running_stand(root: Path) -> Iterator[Stand]:
    """Поднять стенд; остановить гарантированно, в том числе при исключении."""
    root.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    api_token = secrets.token_hex(16)
    log_path = root / "server.log"
    process: subprocess.Popen[bytes] | None = None
    with log_path.open("wb") as log:
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "hub.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=_stand_env(root, port, api_token),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            stand = Stand(
                url=f"http://127.0.0.1:{port}",
                port=port,
                db_path=root / "hub.db",
                log_path=log_path,
                process=process,
                api_token=api_token,
            )
            _wait_ready(process, stand.url, log_path)
            _seed_human(stand)
            yield stand
        finally:
            if process is not None:
                _stop(process)
            # Лог стенда сохраняется всегда, без признака сбоя: сбой подъёма
            # до yield, сбой browser.new_context() и обычный прогон pytest
            # различает по-разному, а безусловное сохранение покрывает все.
            save_server_log(log_path, "stand")


# ---------------------------------------------------------------------------
# Фикстуры браузера: Playwright подгружается здесь, не при импорте модуля
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def stand(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stand]:
    with running_stand(tmp_path_factory.mktemp("gui-stand")) as running:
        yield running


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    # Локально можно взять установленный Chrome и ничего не скачивать:
    # HAIPLANE_GUI_CHANNEL=chrome. В CI ставится Chromium Playwright.
    channel = os.environ.get("HAIPLANE_GUI_CHANNEL") or None
    with sync_playwright() as playwright:
        launched = playwright.chromium.launch(headless=True, channel=channel)
        try:
            yield launched
        finally:
            launched.close()


def artifacts_dir() -> Path:
    return Path(os.environ.get("HAIPLANE_GUI_ARTIFACTS", ROOT / "gui-artifacts"))


def save_server_log(log_path: Path, name: str) -> None:
    out = artifacts_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}-server.log").write_text(_tail(log_path, 400))


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


@pytest.fixture
def context(
    browser: Any, stand: Stand, request: pytest.FixtureRequest
) -> Iterator[Any]:
    """Контекст человека: cookie-сессия и trace на случай сбоя.

    Запрос страницы к unpkg за HTMX отдаётся из tests/gui/vendor с проверкой
    sha384: тот же файл, что грузит прод, без сети в момент прогона.
    """
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
    try:
        body = htmx_bytes()
        served: list[str] = []

        def serve_htmx(route: Any) -> None:
            url = route.request.url
            if url == HTMX_URL_PREFIX or url.startswith(HTMX_URL_PREFIX + "/"):
                served.append(url)
                route.fulfill(
                    status=200, body=body, content_type="application/javascript"
                )
            else:
                route.abort()

        ctx.route(UNPKG_GLOB, serve_htmx)
        ctx.htmx_served = served
        ctx.add_cookies(
            [{"name": stand.cookie_name, "value": stand.session, "url": stand.url}]
        )
        ctx.tracing.start(screenshots=True, snapshots=True, sources=False)
        yield ctx
    finally:
        out = artifacts_dir()
        out.mkdir(parents=True, exist_ok=True)
        name = request.node.name
        # Лог сервера пишется всегда: ошибка в setup или teardown фикстур тоже
        # его требует, а отчёт teardown самой фикстуре ещё не виден.
        (out / f"{name}-server.log").write_text(_tail(stand.log_path, 400))
        failed = any(
            getattr(getattr(request.node, f"rep_{when}", None), "failed", False)
            for when in ("setup", "call")
        )
        if failed:
            ctx.tracing.stop(path=str(out / f"{name}-trace.zip"))
            for index, page in enumerate(ctx.pages):
                try:
                    page.screenshot(
                        path=str(out / f"{name}-{index}.png"), full_page=True
                    )
                except Exception:  # noqa: BLE001 - снимок страницы не должен прятать сбой теста
                    pass
        else:
            ctx.tracing.stop()
        ctx.close()


@pytest.fixture
def page(context: Any) -> Any:
    opened = context.new_page()
    opened.set_default_timeout(15000)
    return opened
