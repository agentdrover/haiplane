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
    """Окружение без hub-адресов и токенов — как у чистой машины разработчика."""
    for name in list(os.environ):
        up = name.upper()
        if up.startswith(("HAIPLANE_", "OPENCLAW_")) or up in {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GITVERSE_TOKEN",
            "CURSOR_API_KEY",
        }:
            monkeypatch.delenv(name, raising=False)
    return dict(os.environ)


# --------------------------------------------------------------------------
# AC-1: production в окружении -> отказ; иначе временная БД и loopback
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"HAIPLANE_HUB_URL": PROD_URL},
        {"HAIPLANE_HUB_URL": "http://10.0.0.5:8080"},
        {"HAIPLANE_HUB_TOKENS": f"ci:{PROD_TOKEN}:admin"},
        {"HAIPLANE_STEWARD_HUB_TOKEN": PROD_TOKEN},
        {"HAIPLANE_CURSOR_REVIEWER_HUB_TOKEN": PROD_TOKEN},
        {"GITVERSE_TOKEN": PROD_TOKEN},
        {"GITHUB_TOKEN": PROD_TOKEN},
        {"GH_TOKEN": PROD_TOKEN},
        {"CURSOR_API_KEY": PROD_TOKEN},
    ],
)
async def test_sandbox_rejects_production_environment(bad, tmp_path, monkeypatch):
    spawned: list = []
    real_popen = subprocess.Popen

    def spy(*a, **k):
        spawned.append(a)
        return real_popen(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    env = {"PATH": "/usr/bin", **bad}
    base = tmp_path / "base"
    base.mkdir()

    with pytest.raises(sb.SandboxRefused) as exc:
        async with sb.Sandbox(env=env, base_dir=base):
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

    async with sb.Sandbox(env=clean_env, base_dir=tmp_path) as box:
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
    async with sb.Sandbox(env=clean_env, base_dir=tmp_path) as box:
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
    monkeypatch.setattr(app_mod, "_register_plugins", app_mod._register_plugins)
    monkeypatch.setattr(app_mod, "start_poller", app_mod.start_poller)
    real_poller = app_mod.start_poller
    srv.configure(app_mod, registry, seen.append)

    assert app_mod.start_poller is not real_poller
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
    # повторная регистрация из lifespan не возвращает настоящие адаптеры
    app_mod._register_plugins()
    assert isinstance(registry.git_ops, srv.DeniedAdapter)


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


def test_recorder_redacts_secret_declared_after_the_call():
    rec = sb.TraceRecorder()
    rec.record(
        session_id="s-1", tool="t", args={"a": "late-secret-777"}, result="ok", ok=True
    )
    rec.add_secret("late-secret-777")
    assert "late-secret-777" not in rec.to_json()


async def test_synthetic_trace_redacts_secrets(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_PROBE_API_KEY", "env-secret-abcdef")
    async with sb.Sandbox(
        env=dict(clean_env), base_dir=tmp_path, extra_secrets=[TEST_SECRET]
    ) as box:
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
    async with sb.Sandbox(env=clean_env, base_dir=tmp_path) as box:
        first_task = await box.seed_task("state must not leak")
        first = await _snapshot(box)
        first_db = box.db_path
    _assert_released(*first)
    assert box.closed

    # 2. ошибка внутри блока
    with pytest.raises(RuntimeError, match="boom"):
        async with sb.Sandbox(env=clean_env, base_dir=tmp_path) as box:
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
        async with sb.Sandbox(env=clean_env, base_dir=tmp_path) as b:
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
    async with sb.Sandbox(env=clean_env, base_dir=tmp_path) as box:
        assert box.db_path != first_db != second_db
        assert box.pid != first[0]
        agent = box.session("human", session_id="eval-next")
        listing = await agent.call_tool("hub_list_tasks", {})
        assert f"#{first_task}" not in json.dumps(listing) or first_task is None
        assert "state must not leak" not in json.dumps(listing)
        assert box.recorder.calls[0].session_id == "eval-next"
    assert list(tmp_path.iterdir()) == [], "no leftovers in base dir"


async def test_sandbox_start_failure_releases_everything(clean_env, tmp_path):
    box = sb.Sandbox(env=clean_env, base_dir=tmp_path, start_timeout=0)
    with pytest.raises(sb.SandboxStartError):
        await box.__aenter__()
    assert box.closed
    assert list(tmp_path.iterdir()) == []
    await box.close()  # идемпотентно
