"""Одноразовый Hub для eval и синтетические MCP-трассы (#1223).

Платная модель и сеть наружу здесь не вызываются. Тесты поднимают настоящий
процесс хаба на loopback с временной БД и ходят в него по MCP; внешние адаптеры
в нём — заглушки с журналом попыток.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agent_eval import sandbox as sb  # noqa: E402
from agent_eval import sandbox_server as srv  # noqa: E402

# Снятый префикс собирается из частей: страж имён (test_no_legacy_name) не
# пускает старое имя в HEAD литералом, как и в hub/brand.py.
RETIRED = ("open" + "claw").upper() + "_"
PROD_URL = "https://agenthai.ru"
PROD_TOKEN = "prod-token-must-never-be-used-1234"
TEST_SECRET = "synthetic-secret-value-98765"  # pragma: allowlist secret


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        os.waitpid(pid, os.WNOHANG)  # дочерний зомби не считается живым
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _port_open(url: str) -> bool:
    port = int(url.rsplit(":", 1)[1])
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _lan_ip() -> str | None:
    """Адрес не из loopback у этой машины, если он есть (без трафика наружу)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # UDP connect: пакетов не шлёт
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") else ip


def _tables(db_path: Path) -> dict[str, list[tuple]]:
    con = sqlite3.connect(db_path)
    try:
        names = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return {
            n: [tuple(r) for r in con.execute(f'PRAGMA table_info("{n}")')]
            for n in names
        }
    finally:
        con.close()


@pytest.fixture
def clean_env(monkeypatch):
    """Фактическое окружение процесса без hub-адресов и токенов (чистая машина)."""
    for name in list(os.environ):
        if sb._forbidden_reason(name, "https://example.invalid") or (
            name.upper().startswith(("HAIPLANE_", RETIRED))
        ):
            monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        for variant in (name, name.lower()):
            monkeypatch.delenv(variant, raising=False)
    return dict(os.environ)


# --------------------------------------------------------------------------
# AC-1: production в окружении -> отказ; иначе временная БД и loopback
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"HAIPLANE_HUB_URL": PROD_URL},
        {"HAIPLANE_HUB_URL": "http://10.0.0.5:8080"},
        {RETIRED + "HUB_URL": PROD_URL},
        {RETIRED + "HUB_TOKENS": f"ci:{PROD_TOKEN}:admin"},
        {RETIRED + "STEWARD_HUB_TOKEN": PROD_TOKEN},
        {"HAIPLANE_HUB_BOOTSTRAP_ADMIN_TOKEN": PROD_TOKEN},
        {RETIRED + "HUB_BOOTSTRAP_ADMIN_TOKEN": PROD_TOKEN},
        {"HAIPLANE_HUB_CSRF_SECRET": PROD_TOKEN},
        {"HAIPLANE_HUB_TOKENS": f"ci:{PROD_TOKEN}:admin"},
        {"HAIPLANE_STEWARD_HUB_TOKEN": PROD_TOKEN},
        {"HAIPLANE_CURSOR_REVIEWER_HUB_TOKEN": PROD_TOKEN},
        {"GITVERSE_TOKEN": PROD_TOKEN},
        {"GITHUB_TOKEN": PROD_TOKEN},
        {"GH_TOKEN": PROD_TOKEN},
        {"CURSOR_API_KEY": PROD_TOKEN},
    ],
)
async def test_sandbox_rejects_production_environment(
    bad, tmp_path, monkeypatch, clean_env
):
    spawned: list = []
    real_popen = subprocess.Popen

    def spy(*a, **k):
        spawned.append(a)
        return real_popen(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    for name, value in bad.items():
        monkeypatch.setenv(name, value)  # фактическое окружение, не аргумент
    base = tmp_path / "base"
    base.mkdir()

    with pytest.raises(sb.SandboxRefused) as exc:
        async with sb.Sandbox(env={}, base_dir=base):
            pytest.fail("sandbox must not start in a production-looking env")

    # Отказ называет имя переменной, но не значение.
    assert PROD_TOKEN not in str(exc.value)
    assert next(iter(bad)) in str(exc.value)
    assert spawned == [], "no process may be spawned before the env check passes"
    assert list(base.iterdir()) == [], "no temp state may be created on refusal"


def test_check_environment_accepts_clean_env():
    sb.check_environment({"PATH": "/usr/bin", "HOME": "/home/x"})
    sb.check_environment({"HAIPLANE_HUB_URL": "http://127.0.0.1:8080"})
    sb.check_environment({"HAIPLANE_HUB_URL": "http://localhost:9"})
    sb.check_environment({"HAIPLANE_HUB_TOKENS": ""})


async def test_sandbox_uses_temp_db_and_loopback(clean_env, tmp_path):
    from hub import config

    async with sb.Sandbox(base_dir=tmp_path) as box:
        assert box.url.startswith("http://127.0.0.1:")
        assert box.db_path.is_file()
        lan = _lan_ip()
        if lan:  # слушаем только loopback: по адресу сети порт закрыт
            with socket.socket() as probe:
                probe.settimeout(0.5)
                assert probe.connect_ex((lan, int(box.url.rsplit(":", 1)[1]))) != 0
        assert tmp_path in box.db_path.parents
        assert box.db_path.resolve() != Path(config.HUB_DB_PATH).resolve()
        # Изоляция: ни prod URL, ни prod токенов в окружении сервера.
        child_env = box.child_environment()
        assert child_env["HAIPLANE_HUB_URL"] == box.url
        assert PROD_URL not in json.dumps(child_env)
        assert child_env["HAIPLANE_HUB_DB"] == str(box.db_path)
        assert child_env["PATH"] != os.environ.get("PATH")
        assert "CURSOR_API_KEY" not in child_env
        # Отдельные тестовые principals — не из окружения родителя.
        assert {"human", "agent"} <= set(box.principals)
        assert box.principals["human"].token != box.principals["agent"].token


async def test_sandbox_session_refuses_non_loopback_endpoint(clean_env, tmp_path):
    rec = sb.TraceRecorder(secrets=["x" * 8])
    with pytest.raises(sb.SandboxRefused):
        sb.SyntheticSession(PROD_URL, "t" * 12, "s-1", rec)
    with pytest.raises(sb.SandboxRefused):
        sb.SyntheticSession("http://10.1.2.3:8080", "t" * 12, "s-1", rec)
    sb.SyntheticSession("http://127.0.0.1:1", "t" * 12, "s-1", rec)


async def test_external_adapters_are_denied_and_audited(clean_env, tmp_path):
    async with sb.Sandbox(base_dir=tmp_path) as box:
        task_id = await box.seed_task("denied effects probe")
        agent = box.session("agent", session_id="eval-denied")
        claim = await agent.call_tool(
            "hub_claim_task",
            {"task_id": task_id, "agent": "eval-agent", "session_id": "eval-denied"},
        )
        assert claim and not claim.get("isError"), claim
        pair = await agent.call_tool(
            "hub_pair_start",
            {"task_id": task_id, "session_id": "eval-denied", "plan": "Plan: probe"},
        )
        assert pair and not pair.get("isError"), pair
        effects = box.denied_effects()
        # git_ops.* отработал на заглушке, а не на настоящем git.
        assert "git_ops.pair_prepare_branch" in effects, effects
        # Настоящих бинарей у процесса нет вовсе.
        assert box.child_environment()["PATH"] != os.environ.get("PATH")


async def test_denied_adapter_proxies_noop_and_records():
    from hub.integrations.noop import NoopGitOps

    seen: list[str] = []
    wrapped = srv.DeniedAdapter("git_ops", NoopGitOps(), seen.append)
    await wrapped.checkout("main")
    assert seen == ["git_ops.checkout"]
    with pytest.raises(AttributeError):
        wrapped.no_such_method_at_all


async def test_server_configure_replaces_every_outward_path(monkeypatch):
    """Поллер выключен, реестр целиком на заглушках — до первого запроса."""
    from hub import app as app_mod
    from hub.integrations.registry import PluginRegistry

    registry = PluginRegistry()
    seen: list[str] = []
    from hub.services import ac_tests, validation_run

    for mod, attr in (
        (app_mod, "_register_plugins"),
        (app_mod, "run_validation_commands"),
        (app_mod, "run_ac_tests"),
        (validation_run, "default_validation_runner"),
        (ac_tests, "default_test_runner"),
    ):
        monkeypatch.setattr(mod, attr, getattr(mod, attr))
    monkeypatch.setattr(app_mod, "start_poller", app_mod.start_poller)
    real_poller = app_mod.start_poller
    srv.configure(app_mod, registry, seen.append)

    assert app_mod.start_poller is not real_poller
    assert app_mod.run_validation_commands.__name__ == "refuse"
    assert app_mod.run_ac_tests.__name__ == "refuse"
    task = app_mod.start_poller(app_mod.app)
    try:
        assert not task.done()
        assert getattr(app_mod.app.state, "egress_task", None) is None
    finally:
        task.cancel()
    for field in (
        "dispatch",
        "git_ops",
        "forge",
        "github",
        "notes",
        "vast",
        "transcripts",
    ):
        assert isinstance(getattr(registry, field), srv.DeniedAdapter), field
    # раннеры по умолчанию тоже закрыты: вызов не через маршрут не запустит код
    from hub.services import ac_tests as ac_mod
    from hub.services import validation_run as vr_mod

    with pytest.raises(PermissionError):
        await vr_mod.default_validation_runner(["true"], "/tmp")
    with pytest.raises(PermissionError):
        await ac_mod.default_test_runner(["a::b"], "/tmp")
    assert "validation_run.default_validation_runner" in seen
    assert "ac_tests.default_test_runner" in seen
    # повторная регистрация из lifespan не возвращает настоящие адаптеры
    app_mod._register_plugins()
    assert isinstance(registry.git_ops, srv.DeniedAdapter)


async def test_sandbox_checks_actual_environment_not_env_argument(
    clean_env, tmp_path, monkeypatch
):
    """env= задаёт окружение ребёнка и не может спрятать production родителя."""
    monkeypatch.setenv("HAIPLANE_HUB_URL", PROD_URL)
    for kwargs in ({"env": {}}, {}):
        with pytest.raises(sb.SandboxRefused):
            async with sb.Sandbox(base_dir=tmp_path, **kwargs):
                pytest.fail("must not start")
    assert list(tmp_path.iterdir()) == []


async def test_environment_is_checked_at_start_not_at_construction(
    clean_env, tmp_path, monkeypatch
):
    box = sb.Sandbox(base_dir=tmp_path)  # окружение чистое
    monkeypatch.setenv("HAIPLANE_HUB_TOKENS", f"ci:{PROD_TOKEN}:admin")
    with pytest.raises(sb.SandboxRefused):
        await box.__aenter__()
    assert list(tmp_path.iterdir()) == []


async def test_env_argument_is_checked_too(clean_env, tmp_path):
    with pytest.raises(sb.SandboxRefused):
        async with sb.Sandbox(env={"GH_TOKEN": PROD_TOKEN}, base_dir=tmp_path):
            pytest.fail("must not start")


async def test_allow_env_is_explicit_and_never_reaches_the_child(
    clean_env, tmp_path, monkeypatch
):
    monkeypatch.setenv("CURSOR_API_KEY", PROD_TOKEN)
    with pytest.raises(sb.SandboxRefused):  # по умолчанию список пуст
        async with sb.Sandbox(base_dir=tmp_path):
            pytest.fail("must not start")
    async with sb.Sandbox(
        env={"CURSOR_API_KEY": PROD_TOKEN},
        allow_env=["CURSOR_API_KEY"],
        base_dir=tmp_path,
    ) as box:
        assert "CURSOR_API_KEY" not in box.child_environment()
        assert PROD_TOKEN not in json.dumps(box.child_environment())


@pytest.mark.parametrize(
    "name",
    [
        "HAIPLANE_HUB_BOOTSTRAP_ADMIN_TOKEN",
        RETIRED + "HUB_BOOTSTRAP_ADMIN_TOKEN",
        "HAIPLANE_HUB_CSRF_SECRET",
        RETIRED + "HUB_URL",
        RETIRED + "HUB_TOKENS",
        "ACME_HUB_TOKEN",
    ],
)
def test_forbidden_names_cover_bootstrap_and_retired_prefix(name):
    with pytest.raises(sb.SandboxRefused):
        sb.check_environment({name: "http://prod.example/x"})
    sb.check_environment({name: "http://prod.example/x"}, allow=[name])


def test_budget_style_names_are_not_secrets():
    sb.check_environment({"HAIPLANE_REVIEW_TOKEN_BUDGET": "300000"})
    sb.check_environment({"HAIPLANE_EXECUTOR_TOKEN_CEILING": "8000000"})


async def test_proxy_environment_is_ignored_by_sandbox_clients(
    clean_env, tmp_path, monkeypatch
):
    """Bearer тестового принципала не должен уходить на прокси из окружения."""
    hits: list[bytes] = []

    async def handler(reader, writer):
        hits.append(await reader.read(200))
        writer.close()

    proxy = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = proxy.sockets[0].getsockname()[1]
    for name in ("HTTP_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{port}")
    try:
        async with sb.Sandbox(base_dir=tmp_path) as box:
            task_id = await box.seed_task("proxy probe")
            reply = await box.session("agent", session_id="p-1").call_tool(
                "hub_whoami", {}
            )
            assert reply and not reply.get("isError")
            resp = await box.request("human", "GET", f"/api/tasks/{task_id}")
            assert resp.status_code == 200
    finally:
        proxy.close()
        await proxy.wait_closed()
    assert hits == [], "no sandbox request may go through the environment proxy"


def test_deny_execution_blocks_every_process_entry(monkeypatch):
    import _posixsubprocess

    spawned: list = []
    monkeypatch.setattr(
        _posixsubprocess, "fork_exec", lambda *a, **k: spawned.append(a)
    )
    seen: list[str] = []
    restore = srv.deny_execution(seen.append)
    try:

        async def go():
            with pytest.raises(PermissionError):
                await asyncio.create_subprocess_exec("/usr/bin/git", "--version")
            with pytest.raises(PermissionError):
                await asyncio.create_subprocess_shell("echo hi")

        asyncio.run(go())
        with pytest.raises(PermissionError):
            subprocess.run(["/usr/bin/git", "--version"], check=False)
        with pytest.raises(PermissionError):
            os.system("true")
        with pytest.raises(PermissionError):
            os.fork()
    finally:
        restore()
    assert spawned == [], "denied attempts must not reach fork/exec"
    assert "exec.subprocess.Popen" in seen
    assert "exec.os.system" in seen
    assert "exec.os.fork" in seen
    # после отката процессы снова запускаются (защита не течёт между тестами)
    assert subprocess.run(["/usr/bin/true"], check=False).returncode == 0


async def test_child_main_installs_execution_guard(clean_env, tmp_path):
    """Проводка main(): ребёнок закрывает запуск процессов до старта сервера."""
    probe = tmp_path / "probe.txt"
    script = tmp_path / "probe_server.py"
    script.write_text(
        "import runpy, subprocess, sys, uvicorn\n"
        "def probe(*a, **k):\n"
        "    try:\n"
        "        subprocess.run(['/usr/bin/true'], check=False)\n"
        "        result = 'spawned'\n"
        "    except PermissionError:\n"
        "        result = 'denied'\n"
        f"    open({str(probe)!r}, 'w').write(result)\n"
        "    raise SystemExit(0)\n"
        "uvicorn.run = probe\n"
        f"runpy.run_path({str(REPO_ROOT / 'scripts/agent_eval/sandbox_server.py')!r},"
        " run_name='__main__')\n"
    )
    with pytest.raises(sb.SandboxStartError):  # сервер не поднят: проба вышла
        async with sb.Sandbox(base_dir=tmp_path / "b", server_script=script):
            pytest.fail("probe server exits instead of serving")
    assert probe.read_text() == "denied"


async def test_validation_and_ac_test_runs_are_refused_and_audited(clean_env, tmp_path):
    async with sb.Sandbox(base_dir=tmp_path) as box:
        task_id = await box.seed_task("must not execute anything")
        for route, label in (
            ("run-validation", "validation_run.run_validation_commands"),
            ("run-ac-tests", "ac_tests.run_ac_tests"),
        ):
            resp = await box.request("human", "POST", f"/api/tasks/{task_id}/{route}")
            assert resp.status_code == 403, (route, resp.text)
            assert label in box.denied_effects()
        assert not any(e.startswith("exec.") for e in box.denied_effects())


GRANDCHILD_SERVER = """
import os, runpy, subprocess, sys
# цепочка глубины два в другой сессии: sh -> sleep
child = subprocess.Popen(
    ["/bin/sh", "-c", "/bin/sleep 600 & echo $! > " + sys.argv[2] + ".grandchild; wait"],
    start_new_session=True,
)
runpy.run_path(%r, run_name="__main__")
"""


async def test_cleanup_kills_descendants_in_other_sessions(clean_env, tmp_path):
    script = tmp_path / "server_with_grandchild.py"
    script.write_text(
        GRANDCHILD_SERVER % str(REPO_ROOT / "scripts/agent_eval/sandbox_server.py")
    )
    base = tmp_path / "base"
    gpid = 0
    try:
        async with sb.Sandbox(base_dir=base, server_script=script) as box:
            pidfile = box.root / "denied-effects.jsonl.grandchild"
            deadline = time.monotonic() + 10
            while not pidfile.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
            gpid = int(pidfile.read_text())
            assert _alive(gpid)
            assert os.getsid(gpid) != os.getsid(box.pid), "own session"
        deadline = time.monotonic() + 5
        while _alive(gpid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(gpid), "grandchild in another session must be killed"
    finally:
        if gpid and _alive(gpid):
            os.kill(gpid, 9)


# --------------------------------------------------------------------------
# AC-2: трасса с session id, секреты скрыты, схема не расширена
# --------------------------------------------------------------------------


def test_recorder_redacts_known_and_declared_secrets(monkeypatch):
    monkeypatch.setenv("EVAL_PROBE_API_KEY", "env-secret-abcdef")
    rec = sb.TraceRecorder(secrets=[TEST_SECRET])
    rec.record(
        session_id="s-1",
        tool="hub_ask_question",
        args={
            "text": f"use {TEST_SECRET} and env-secret-abcdef",
            "token": "whatever-value",
            "nested": [{"Authorization": "Bearer abc.def.ghi"}],
            "header": "Authorization: Bearer zzz.yyy.xxx",
        },
        result={"content": [{"type": "text", "text": f"echo {TEST_SECRET}"}]},
        ok=True,
    )
    # сами сохранённые записи чисты, а не только итоговый JSON
    stored = json.dumps([c.as_dict() for c in rec.calls])
    for leaked in (TEST_SECRET, "env-secret-abcdef", "whatever-value", "zzz.yyy.xxx"):
        assert leaked not in stored, leaked
    dumped = rec.to_json()
    for leaked in (
        TEST_SECRET,
        "env-secret-abcdef",
        "whatever-value",
        "abc.def.ghi",
        "zzz.yyy.xxx",
    ):
        assert leaked not in dumped
    row = json.loads(dumped)["calls"][0]
    assert row["session_id"] == "s-1"
    assert row["tool"] == "hub_ask_question"
    assert row["ok"] is True
    assert row["seq"] == 1
    assert "[redacted]" in dumped


def test_session_registers_its_own_token_as_secret():
    rec = sb.TraceRecorder()
    sb.SyntheticSession("http://127.0.0.1:1", "own-token-0123456789", "s-1", rec)
    assert "own-token-0123456789" in rec.secrets
    rec.record(
        session_id="s-1", tool="t", args="x own-token-0123456789", result=1, ok=True
    )
    assert "own-token-0123456789" not in json.dumps(rec.calls[0].as_dict())


def test_recorder_redacts_session_id_and_tool_name():
    rec = sb.TraceRecorder(secrets=["tok-in-session-0001"])
    rec.record(
        session_id="sess-tok-in-session-0001",
        tool="tool-tok-in-session-0001",
        args={},
        result=None,
        ok=False,
    )
    dumped = json.dumps(rec.calls[0].as_dict())
    assert "tok-in-session-0001" not in dumped
    assert "tok-in-session-0001" not in rec.to_json()


def test_recorder_redacts_secret_declared_after_the_call():
    rec = sb.TraceRecorder()
    rec.record(
        session_id="s-1", tool="t", args={"a": "late-secret-777"}, result="ok", ok=True
    )
    rec.add_secret("late-secret-777")
    assert "late-secret-777" not in rec.to_json()


async def test_synthetic_trace_redacts_secrets(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_PROBE_API_KEY", "env-secret-abcdef")
    async with sb.Sandbox(base_dir=tmp_path, extra_secrets=[TEST_SECRET]) as box:
        task_id = await box.seed_task(f"trace probe {TEST_SECRET}")
        agent = box.session("agent", session_id="eval-session-7")
        await agent.call_tool("hub_whoami", {})
        await agent.call_tool(
            "hub_task_status", {"task_id": task_id, "note": TEST_SECRET}
        )
        agent_token = box.principals["agent"].token
        human_token = box.principals["human"].token
        await agent.call_tool("hub_whoami", {"echo": f"{agent_token} {human_token}"})
        # вызов с ошибкой тоже значим: он остаётся в трассе с ok=False
        await agent.call_tool("hub_no_such_tool", {"x": 1})
        tokens = [p.token for p in box.principals.values()]
        out = tmp_path / "trace.json"
        box.write_trace(out)

        text = out.read_text()
        for leaked in [TEST_SECRET, "env-secret-abcdef", *tokens]:
            assert leaked not in text
        data = json.loads(text)
        calls = data["calls"]
        assert [c["tool"] for c in calls] == [
            "hub_whoami",
            "hub_task_status",
            "hub_whoami",
            "hub_no_such_tool",
        ]
        assert calls[-1]["ok"] is False
        assert calls[-1]["result"]
        assert {c["session_id"] for c in calls} == {"eval-session-7"}
        assert all(c["result"] for c in calls), "results must be kept"
        assert str(task_id) in json.dumps(calls[1]["args"])

        # Production telemetry и схема не расширены: таблицы и колонки те же,
        # что у свежей БД из той же схемы; трасса лежит вне БД.
        baseline = tmp_path / "baseline.db"
        from hub import db as hub_db

        conn = await hub_db.connect(str(baseline))
        await hub_db.bootstrap(conn)
        await conn.close()
        mine = _tables(box.db_path)
        base = _tables(baseline)
        assert mine.keys() == base.keys()
        for name in base:
            assert mine[name] == base[name], name
        assert not any("trace" in name or "eval" in name for name in mine)


# --------------------------------------------------------------------------
# AC-3: освобождение при нормальном завершении, ошибке и отмене
# --------------------------------------------------------------------------


async def _snapshot(box):
    return box.pid, box.db_path, box.root, box.url


def _assert_released(pid, db_path, root, url):
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid), "hub process must be gone"
    assert not root.exists(), "temp dir (with DB) must be removed"
    assert not db_path.exists()
    assert not _port_open(url), "loopback port must be released"


async def test_sandbox_cleanup_on_failure(clean_env, tmp_path):
    # 1. нормальное завершение
    async with sb.Sandbox(base_dir=tmp_path) as box:
        first_task = await box.seed_task("state must not leak")
        first = await _snapshot(box)
        first_db = box.db_path
    _assert_released(*first)
    assert box.closed

    # 2. ошибка внутри блока
    with pytest.raises(RuntimeError, match="boom"):
        async with sb.Sandbox(base_dir=tmp_path) as box:
            second = await _snapshot(box)
            second_db = box.db_path
            agent = box.session("agent", session_id="eval-err")
            await agent.call_tool("hub_whoami", {})
            raise RuntimeError("boom")
    _assert_released(*second)
    # трасса упавшего прогона остаётся для разбора, но без токенов
    assert box.recorder.calls and all(
        p.token not in box.recorder.to_json() for p in box.principals.values()
    )

    # 3. отмена
    started = asyncio.Event()
    holder: dict = {}

    async def runner():
        async with sb.Sandbox(base_dir=tmp_path) as b:
            holder["snap"] = await _snapshot(b)
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(runner())
    await asyncio.wait_for(started.wait(), 120)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_released(*holder["snap"])

    # 4. следующий прогон не наследует состояние
    async with sb.Sandbox(base_dir=tmp_path) as box:
        assert box.db_path != first_db != second_db
        assert box.pid != first[0]
        agent = box.session("human", session_id="eval-next")
        listing = await agent.call_tool("hub_list_tasks", {})
        assert f"#{first_task}" not in json.dumps(listing) or first_task is None
        assert "state must not leak" not in json.dumps(listing)
        assert box.recorder.calls[0].session_id == "eval-next"
    assert list(tmp_path.iterdir()) == [], "no leftovers in base dir"


async def test_sandbox_start_failure_releases_everything(clean_env, tmp_path):
    box = sb.Sandbox(base_dir=tmp_path, start_timeout=0)
    with pytest.raises(sb.SandboxStartError):
        await box.__aenter__()
    assert box.closed
    assert list(tmp_path.iterdir()) == []
    await box.close()  # идемпотентно
