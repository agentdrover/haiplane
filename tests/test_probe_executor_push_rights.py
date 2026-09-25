"""Разовая проба прав пуша облачного прогона (#1409).

Скрипт тратит деньги владельца и держит в процессе ключ Cursor, поэтому
проверяется ровно то, что может подвести при единственном запуске: разбор
ответа, отмена под 429, ключ вне вывода и безопасный режим по умолчанию.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from hub import config
from hub.integrations import cursor_cloud

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "probe_executor_push_rights.py"
)
_SECRET = "cur_TEST_SECRET_do_not_print_4f1c9a"


def _load():
    spec = importlib.util.spec_from_file_location("probe_push_rights", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclass ищет свой модуль в sys.modules — без регистрации не соберётся.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe():
    return _load()


async def _no_sleep(_: float) -> None:
    return None


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeProvider:
    """Провайдер, который умеет F0: отмена отвечает 429 заданное число раз."""

    def __init__(self, *, finish_after_polls: int | None, cancel_429: int = 0) -> None:
        self.finish_after_polls = finish_after_polls
        self.cancel_429 = cancel_429
        self.polls = 0
        self.cancel_calls = 0
        self.cancelled = False
        self.calls: list[str] = []

    async def create(self, body):
        self.calls.append("create")
        return {"agent": {"id": "bc-1"}, "run": {"id": "run-1"}}, None

    async def find(self, name):
        self.calls.append("find")
        return cursor_cloud.Reconciliation("", "", True)

    async def get_run(self, agent_id, run_id):
        self.calls.append("get_run")
        self.polls += 1
        if self.cancelled:
            return {"status": "CANCELLED"}
        if (
            self.finish_after_polls is not None
            and self.polls >= self.finish_after_polls
        ):
            return {
                "status": "FINISHED",
                "result": "готово\nBEGIN_PROBE\n=== control\nrc=0\nEND_PROBE\n",
            }
        return {"status": "RUNNING"}

    async def cancel(self, agent_id, run_id):
        self.calls.append("cancel")
        self.cancel_calls += 1
        if self.cancel_calls <= self.cancel_429:
            return None, cursor_cloud.Refusal(
                status=429, code="rate_limit_exceeded", detail="slow down"
            )
        self.cancelled = True
        return {"id": run_id}, None

    async def usage(self, agent_id, run_id):
        self.calls.append("usage")
        return {"totalUsage": {"totalTokens": 1234}, "chargedCents": 5.5}


# --- разбор маркеров ---------------------------------------------------------


def test_extract_probe_block_takes_last_block_verbatim(probe):
    text = (
        "Формат такой:\nBEGIN_PROBE\n<пример>\nEND_PROBE\n"
        "Выполнил.\nBEGIN_PROBE\n=== develop: git push --dry-run\n"
        " ! [remote rejected] HEAD -> develop (push declined)\nrc=1\nEND_PROBE\n"
    )
    assert probe.extract_probe_block(text) == (
        "=== develop: git push --dry-run\n"
        " ! [remote rejected] HEAD -> develop (push declined)\nrc=1"
    )


@pytest.mark.parametrize(
    "text",
    [None, "", "нет маркеров", "BEGIN_PROBE\nбез конца", "END_PROBE\nBEGIN_PROBE"],
)
def test_extract_probe_block_none_without_complete_block(probe, text):
    assert probe.extract_probe_block(text) is None


def test_redact_hides_credentials_in_urls_and_tokens(probe):
    raw = (
        "To https://x-access-token:ghs_abcdefghijklmnopqrstuvwxyz0123@github.com/a/b\n"
        "token ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 end"
    )
    out = probe.redact(raw)
    assert "ghs_" not in out
    assert "https://***@github.com/a/b" in out


# --- отмена ------------------------------------------------------------------


def test_cancel_survives_five_429_and_confirms_on_sixth(probe):
    import asyncio

    provider = FakeProvider(finish_after_polls=None, cancel_429=5)
    timing = probe.Timing(cancel_attempts=10, cancel_pause=0)
    status = asyncio.run(probe.cancel_run(provider, "bc-1", "run-1", timing, _no_sleep))
    assert status == "CANCELLED"
    assert provider.cancel_calls == 6


def test_cancel_gives_up_at_ceiling_without_claiming_success(probe):
    import asyncio

    provider = FakeProvider(finish_after_polls=None, cancel_429=100)
    timing = probe.Timing(cancel_attempts=3, cancel_pause=0)
    status = asyncio.run(probe.cancel_run(provider, "bc-1", "run-1", timing, _no_sleep))
    assert status == ""
    assert provider.cancel_calls == 3


def test_live_cancels_unfinished_run_after_deadline(probe, monkeypatch, capsys):
    monkeypatch.setattr(config, "CURSOR_API_KEY", _SECRET)
    provider = FakeProvider(finish_after_polls=None, cancel_429=5)
    clock = _Clock()

    async def sleep(seconds: float) -> None:
        clock.now += seconds

    timing = probe.Timing(poll_interval=60, poll_deadline=900, cancel_pause=30)
    rc = probe.main(
        ["--live"], provider=provider, timing=timing, sleep=sleep, clock=clock
    )
    out = capsys.readouterr().out
    assert rc == 1  # прогон снят, но блока нет
    assert provider.cancel_calls == 6
    assert "Конечный статус прогона: CANCELLED." in out
    assert "chargedCents=5.5" in out


def test_live_finished_run_prints_block_and_is_not_cancelled(
    probe, monkeypatch, capsys
):
    monkeypatch.setattr(config, "CURSOR_API_KEY", _SECRET)
    provider = FakeProvider(finish_after_polls=3)
    rc = probe.main(
        ["--live"],
        provider=provider,
        timing=probe.Timing(),
        sleep=_no_sleep,
        clock=_Clock(),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert provider.cancel_calls == 0
    assert "BEGIN_PROBE\n=== control\nrc=0\nEND_PROBE" in out


# --- ключ --------------------------------------------------------------------


def test_key_never_reaches_output_on_real_client_path(
    probe, monkeypatch, capsys, caplog
):
    """Настоящий CursorProvider поверх подменённого HTTP: ключ уходит в
    заголовок, но не в stdout, stderr и журнал — ни в одном режиме."""
    monkeypatch.setattr(config, "CURSOR_API_KEY", _SECRET)
    seen_auth: list[str] = []
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization", ""))
        path = request.url.path
        if request.method == "POST" and path == "/v1/agents":
            return httpx.Response(
                200, json={"agent": {"id": "bc-9"}, "run": {"id": "run-9"}}
            )
        if path.endswith("/cancel"):
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})
        if path.endswith("/usage"):
            return httpx.Response(200, json={"totalUsage": {"totalTokens": 7}})
        polls["n"] += 1
        return httpx.Response(200, json={"status": "RUNNING", "result": ""})

    real_client = httpx.AsyncClient

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cursor_cloud.httpx, "AsyncClient", client_factory)
    caplog.set_level(logging.DEBUG)
    timing = probe.Timing(
        poll_interval=0, poll_deadline=0, cancel_attempts=2, cancel_pause=0
    )
    rc_live = probe.main(["--live"], timing=timing, sleep=_no_sleep, clock=_Clock())
    rc_dry = probe.main([])
    captured = capsys.readouterr()
    assert rc_live == 3 and rc_dry == 0
    assert seen_auth and all(_SECRET in h for h in seen_auth)
    for stream in (captured.out, captured.err, caplog.text):
        assert _SECRET not in stream
    assert "Ключ Cursor в окружении: задан" in captured.out


# --- --dry-run ---------------------------------------------------------------


def test_dry_run_is_default_and_touches_no_network(probe, monkeypatch, capsys):
    monkeypatch.setattr(config, "CURSOR_API_KEY", _SECRET)

    def no_network(**kwargs: Any) -> httpx.AsyncClient:
        raise AssertionError("dry-run вызвал сеть")

    monkeypatch.setattr(cursor_cloud.httpx, "AsyncClient", no_network)
    provider = FakeProvider(finish_after_polls=1)
    assert probe.main([], provider=provider) == 0
    assert probe.main(["--dry-run"], provider=provider) == 0
    assert provider.calls == []
    out = capsys.readouterr().out
    assert "--dry-run, сеть не трогается" in out
    assert '"name": "haiplane:probe:t1409:g0:a1"' in out
    assert '"startingRef": "develop"' in out
    assert '"id": "claude-sonnet-5"' in out
    assert _SECRET not in out


def test_live_without_key_stops_before_network(probe, monkeypatch, capsys):
    monkeypatch.setattr(config, "CURSOR_API_KEY", "")
    provider = FakeProvider(finish_after_polls=1)
    assert probe.main(["--live"], provider=provider) == 2
    assert provider.calls == []


def test_prompt_pushes_only_dry_run_to_protected_branches(probe):
    lines = [ln for ln in probe.PROMPT.splitlines() if "git push" in ln]
    protected = [
        ln for ln in lines if "refs/heads/develop" in ln or "refs/heads/main" in ln
    ]
    assert protected and all("--dry-run" in ln for ln in protected)
    control = [ln for ln in lines if "task-1409/push-probe" in ln]
    assert control and not any("--dry-run" in ln for ln in control)
