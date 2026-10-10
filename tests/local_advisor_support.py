"""Общая оснастка тестов локального советника на GLM (#1649).

Подставная служба-исполнитель берёт задания из настоящего spool-каталога, как
настоящая, и пишет result.json; «CLI» советника — функция ``judge``, которая
сдаёт суждение тем же контрактом, что и живой прогон. Фикстуры регистрирует
tests/conftest.py, чтобы их видели все четыре файла тестов этой задачи.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path

import pytest

from hub import config
from hub.integrations import local_reviewer
from hub.services import chat_pair
from hub.services import steward_shadow as sh

GLM = "glm-5.1"


def capabilities(**over) -> dict:
    caps: dict = {
        "protocol": 1,
        "job_versions": [1, 2, 3],
        "profiles": ["review", "advisor"],
        "advisor_model": GLM,
        "timeouts": {"max_sec": 1800, "review_sec": 1800, "advisor_sec": 900},
    }
    caps.update(over)
    return caps


def beat_caps(spool: Path, caps: dict | None = None, age: float = 0.0) -> None:
    """Heartbeat службы: JSON с возможностями и mtime «age секунд назад»."""
    body: dict = {"pid": 1, "time": time.time()}
    if caps is not None:
        body["capabilities"] = caps
    beat = spool / "heartbeat"
    beat.write_text(json.dumps(body))
    stamp = time.time() - age
    os.utime(beat, (stamp, stamp))


@pytest.fixture
def local_spool(tmp_path, monkeypatch) -> Path:
    """Хаб с локальным советником: транспорт runner, живая служба с advisor."""
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o770)
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 50)
    monkeypatch.setattr(config, "STEWARD_ADVISOR_WAIT_MAX", 60)
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 0)
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", "steward-token")
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "runner")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SPOOL_DIR", str(spool))
    monkeypatch.setattr(config, "STEWARD_ADVISOR_LOCAL_MODEL", GLM)
    monkeypatch.setattr(config, "STEWARD_ADVISOR_MODELS", ())
    monkeypatch.setattr(local_reviewer, "RUNNER_POLL_SEC", 0.02)
    monkeypatch.setattr(local_reviewer, "RUNNER_PICKUP_SEC", 3.0)
    monkeypatch.setattr(local_reviewer, "RUNNER_GRACE_SEC", 2.0)
    monkeypatch.setattr(local_reviewer, "_HOST_BUDGET", asyncio.Lock())
    beat_caps(spool, capabilities())
    return spool


@pytest.fixture
def local_identity(monkeypatch) -> list[tuple]:
    """Принципал стюарда разрешается, а выпуск кода считается поимённо."""
    minted: list[tuple] = []

    async def _principal(_db):
        return 42

    async def _issue(
        _db, principal_id, *, kind, bound_task_id, bound_generation, **_kw
    ):
        minted.append((principal_id, kind, bound_task_id, bound_generation))
        return "ABCD-2345", 300

    monkeypatch.setattr(sh, "steward_principal_id", _principal)
    monkeypatch.setattr(chat_pair, "issue_code", _issue)
    return minted


class Service:
    """Подставная служба: забирает задания из spool, «CLI» — ``judge``."""

    def __init__(self, spool: Path, *, judge=None, hold=False, rc=0, output=""):
        self.spool = spool
        self.judge = judge
        self.hold = hold
        self.rc = rc
        self.output = output
        self.jobs: list[dict] = []
        self.prompts: list[str] = []
        self.cancelled: list[str] = []
        self._task: asyncio.Task | None = None
        self._children: list[asyncio.Task] = []

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for task in [self._task, *self._children]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _loop(self) -> None:
        seen: set[str] = set()
        while True:
            for jobdir in sorted(self.spool.glob("job-*")):
                if jobdir.name in seen or not (jobdir / "job.json").exists():
                    continue
                seen.add(jobdir.name)
                self._children.append(asyncio.create_task(self._handle(jobdir)))
            await asyncio.sleep(0.02)

    async def _handle(self, jobdir: Path) -> None:
        (jobdir / "claimed").write_text("")
        self.jobs.append(json.loads((jobdir / "job.json").read_text()))
        self.prompts.append((jobdir / "prompt.txt").read_text())
        if self.judge is not None:
            await self.judge()
        result = {
            "status": "ok",
            "rc": self.rc,
            "output": self.output,
            "dropped": 0,
            "timed_out": False,
            "cancelled": False,
            "duration_ms": 5,
            "reason": "",
        }
        if self.hold:
            while (jobdir / "job.json").exists() and not (jobdir / "cancel").exists():
                await asyncio.sleep(0.02)
            self.cancelled.append(jobdir.name)
            result.update(rc=124, cancelled=True, output="")
        if jobdir.exists():
            (jobdir / "result.json").write_text(json.dumps(result))


@pytest.fixture
async def local_service(local_spool):
    made: list[Service] = []

    def _make(**kw) -> Service:
        svc = Service(local_spool, **kw)
        svc.start()
        made.append(svc)
        return svc

    yield _make
    for svc in made:
        await svc.stop()
    from hub.services.steward_advisor_local import cancel_local_advisors

    await cancel_local_advisors()
