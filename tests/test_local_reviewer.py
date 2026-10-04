"""Локальный ревьюер через службу-исполнитель (#1571).

Хаб на проде работает под ProtectSystem=strict и NoNewPrivileges=yes: ни
создать каталог прогона вне своих путей, ни выполнить sudo он не может. Эти
тесты держат транспорт «runner»: хаб кладёт задание в spool-каталог, отдельная
служба (deploy/review-runner/) запускает зафиксированную команду и возвращает
результат. Служба здесь НАСТОЯЩАЯ (её модуль грузится из deploy/), а вместо
sudo и враппера стоит python-заглушка: проверяется именно то, что делает
служба, а не её макет.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path

import pytest

from hub import config
from hub.integrations import local_reviewer

_ROOT = Path(__file__).resolve().parent.parent
_RUNNER_FILE = _ROOT / "deploy/review-runner/haiplane-review-runner.py"
_DOC = _ROOT / "deploy/LOCAL-REVIEW.md"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "haiplane_review_runner", _RUNNER_FILE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["haiplane_review_runner"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner_mod():
    return _load_runner()


@pytest.fixture
def spool(tmp_path, monkeypatch) -> Path:
    """Хаб в режиме runner: ни песочницы, ни CMD, ни scratch в его настройках."""
    path = tmp_path / "spool"
    path.mkdir(mode=0o770)
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "runner")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SPOOL_DIR", str(path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", "")
    monkeypatch.setattr(local_reviewer, "RUNNER_POLL_SEC", 0.02)
    monkeypatch.setattr(local_reviewer, "RUNNER_PICKUP_SEC", 1.0)
    monkeypatch.setattr(local_reviewer, "RUNNER_GRACE_SEC", 2.0)
    monkeypatch.setattr(local_reviewer, "_HOST_BUDGET", asyncio.Lock())
    return path


def _beat(spool: Path, age: float = 0.0) -> None:
    beat = spool / "heartbeat"
    beat.write_text("{}")
    stamp = time.time() - age
    os.utime(beat, (stamp, stamp))


def _jobs(spool: Path) -> list[Path]:
    return [p for p in spool.iterdir() if p.name.startswith("job-")]


async def _until(predicate, limit: float = 5.0) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def _fake_cli(tmp_path: Path, body: str) -> tuple[str, ...]:
    """Заглушка CLI: argv службы = [python, скрипт]. Тело читает stdin."""
    script = tmp_path / "fake_cli.py"
    script.write_text("import sys\nprompt = sys.stdin.read()\n" + body)
    return (sys.executable, str(script))


def _runner_cfg(runner_mod, tmp_path: Path, spool: Path, argv, **over):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    values = dict(
        spool=str(spool),
        argv=tuple(argv),
        scratch=str(scratch),
        max_timeout=60,
        poll=0.02,
        heartbeat_every=0.05,
        term_grace=1.0,
        stale_sec=3600.0,
    )
    values.update(over)
    return runner_mod.Config(**values)


def _write_job(spool: Path, name: str, job: dict, prompt: str = "промт") -> Path:
    jobdir = spool / name
    jobdir.mkdir(mode=0o770)
    (jobdir / "prompt.txt").write_text(prompt)
    (jobdir / "job.json").write_text(json.dumps(job))
    return jobdir


# --------------------------------------------------------------------- AC-1


async def test_the_hub_hands_the_run_to_the_runner_service(spool, monkeypatch) -> None:
    """AC-1: хаб пишет задание без команды и промта в argv, забирает результат.

    sudo, создание каталога вне своих путей и любой процесс из хаба запрещены:
    подставленные взрывчатки роняют тест, если хоть одна из них вызвана.
    """

    async def _no_process(*_a, **_k):
        raise AssertionError("хаб запустил процесс сам")

    def _no_mkdtemp(*_a, **_k):
        raise AssertionError("хаб создал каталог прогона сам")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_process)
    monkeypatch.setattr(tempfile, "mkdtemp", _no_mkdtemp)
    assert local_reviewer.not_ready() != [], (
        "службы ещё нет — готовности быть не должно"
    )
    _beat(spool)
    assert local_reviewer.not_ready() == [], (
        "транспорт runner не должен требовать ни песочницы, ни CMD, ни scratch "
        "в настройках хаба: всё это живёт у службы"
    )

    seen: dict = {}

    async def fake_service() -> None:
        while True:
            for jobdir in _jobs(spool):
                if (jobdir / "job.json").exists() and "job" not in seen:
                    seen["job"] = json.loads((jobdir / "job.json").read_text())
                    seen["prompt"] = (jobdir / "prompt.txt").read_text()
                    seen["mode"] = (jobdir / "prompt.txt").stat().st_mode & 0o777
                    seen["files"] = sorted(p.name for p in jobdir.iterdir())
                    (jobdir / "claimed").write_text("")
                    (jobdir / "result.json").write_text(
                        json.dumps(
                            {
                                "status": "ok",
                                "rc": 7,
                                "output": "хвост вывода",
                                "dropped": 3,
                                "timed_out": False,
                                "duration_ms": 1234,
                                "reason": "",
                            }
                        )
                    )
            await asyncio.sleep(0.01)

    service = asyncio.create_task(fake_service())
    try:
        run = await local_reviewer.run_review("ОДНОРАЗОВЫЙ-КОД-В-ПРОМТЕ", timeout=10)
    finally:
        service.cancel()

    assert run is not None
    assert (run.rc, run.output, run.dropped, run.timed_out, run.duration_ms) == (
        7,
        "хвост вывода",
        3,
        False,
        1234,
    )
    assert seen["job"] == {"version": 1, "timeout_sec": 10}, (
        "задание несёт только версию и срок: ни команды, ни путей, ни промта"
    )
    assert seen["prompt"] == "ОДНОРАЗОВЫЙ-КОД-В-ПРОМТЕ"
    assert seen["mode"] == 0o660, "промт доступен группе, не миру"
    assert "ОДНОРАЗОВЫЙ" not in json.dumps(seen["job"])
    assert _jobs(spool) == [], "забрав результат, хаб удаляет задание и промт"


# --------------------------------------------------------------------- AC-2


def test_an_absent_runner_is_named_not_ready(spool) -> None:
    """Нет heartbeat, устарел heartbeat, нет каталога: у каждого своя причина."""
    absent = local_reviewer.not_ready()
    assert any("не запущена" in r and "heartbeat" in r for r in absent), absent

    _beat(spool, age=local_reviewer.RUNNER_HEARTBEAT_MAX_AGE_SEC + 60)
    stale = local_reviewer.not_ready()
    assert any("не отвечает" in r for r in stale), stale

    _beat(spool)
    assert local_reviewer.not_ready() == []

    config.LOCAL_REVIEW_SPOOL_DIR = str(spool / "нет-такого")
    missing = local_reviewer.not_ready()
    assert any("каталога" in r and "нет" in r for r in missing), missing


async def test_an_absent_runner_is_named_and_does_not_hang(spool, monkeypatch) -> None:
    """AC-2: службы нет — причина названа, прогон не висит дольше лимита."""
    # Готовности нет: прогона нет сразу, и причина названа, а не пуста.
    started = time.monotonic()
    assert local_reviewer.not_ready(), "службы нет, готовность обязана это назвать"
    assert await local_reviewer.run_review("промт", timeout=30) is None
    assert time.monotonic() - started < 2
    from hub.services.review_dispatch import _local_failure_reason

    named = local_reviewer.refusal()
    assert "служба-исполнитель не запущена" in named, (
        "причину из not_ready() хаб обязан донести до карточки"
    )
    card = _local_failure_reason(None)
    assert (
        "служба-исполнитель не запущена" in card and "нет каталога, бинаря" not in card
    )

    # Heartbeat есть, а задание никто не берёт: служба зависла или чужая.
    _beat(spool)
    monkeypatch.setattr(local_reviewer, "RUNNER_PICKUP_SEC", 0.3)
    started = time.monotonic()
    assert await local_reviewer.run_review("промт", timeout=30) is None
    assert time.monotonic() - started < 5, "ожидание обязано кончиться раньше лимита"
    reason = local_reviewer.refusal()
    assert "не забрала" in reason, reason
    from hub.services.review_dispatch import _local_failure_reason

    assert "не забрала" in _local_failure_reason(None), (
        "карточка обязана назвать причину отказа службы, а не «нет каталога»"
    )
    assert _jobs(spool) == [], "брошенное задание и промт удалены"


# --------------------------------------------------------------------- AC-3


async def test_a_runner_run_is_cancelled_on_timeout_and_hub_stop(
    spool, runner_mod, tmp_path, monkeypatch
) -> None:
    """AC-3: и таймаут, и остановка хаба доходят до процесса через службу."""
    _beat(spool)
    marker = tmp_path / "outlived"
    argv = _fake_cli(
        tmp_path,
        "import time, pathlib\n"
        "pathlib.Path(sys.argv[0] + '.started').write_text('x')\n"
        "time.sleep(3.0)\n"
        f"pathlib.Path({str(marker)!r}).write_text('x')\n",
    )
    cfg = _runner_cfg(runner_mod, tmp_path, spool, argv)
    service = asyncio.create_task(runner_mod.serve(cfg))
    try:
        # (а) лимит хаба истёк: хаб пишет снятие, служба убивает процесс.
        run = await local_reviewer.run_review("промт", timeout=1)
        assert run is not None and run.timed_out and run.rc == local_reviewer.TIMEOUT_RC
        assert await _until(lambda: _jobs(spool) == [], 3), "задание не удалено"

        # (б) хаб останавливается: отмена корутины = снятие через службу.
        task = asyncio.create_task(local_reviewer.run_review("промт", timeout=30))
        assert await _until(
            lambda: any((j / "claimed").exists() for j in _jobs(spool)), 3
        ), "служба не забрала задание"
        jobdir = _jobs(spool)[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (jobdir / "prompt.txt").exists(), (
            "промт с одноразовым кодом остался на диске после остановки хаба"
        )
        assert await _until(lambda: _jobs(spool) == [], 3), (
            "служба не убрала задание остановленного хаба"
        )
        await asyncio.sleep(3.5)
        assert not marker.exists(), "процесс пережил снятие и дописал файл"
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)


async def test_the_hub_writes_the_cancel_when_the_limit_passes(
    spool, monkeypatch
) -> None:
    """Таймаут со стороны хаба — это запись cancel, а не молчаливый уход."""
    _beat(spool)
    seen: dict = {}

    async def stubborn_service() -> None:
        while True:
            for jobdir in _jobs(spool):
                if (jobdir / "job.json").exists():
                    (jobdir / "claimed").write_text("")
                    if (jobdir / "cancel").exists():
                        seen["cancel"] = True
                        (jobdir / "result.json").write_text(
                            json.dumps(
                                {
                                    "status": "ok",
                                    "rc": -15,
                                    "output": "",
                                    "dropped": 0,
                                    "timed_out": False,
                                    "cancelled": True,
                                    "duration_ms": 1,
                                    "reason": "",
                                }
                            )
                        )
            await asyncio.sleep(0.01)

    service = asyncio.create_task(stubborn_service())
    try:
        run = await local_reviewer.run_review("промт", timeout=1)
    finally:
        service.cancel()
    assert seen.get("cancel"), "хаб не написал снятие"
    assert run is not None and run.timed_out, "по лимиту хаба итог — таймаут"
    assert run.cancel_confirmed is True, "служба ответила снятием — это подтверждение"
    assert _jobs(spool) == []


async def test_the_card_names_whether_the_runner_confirmed_the_cancel(
    spool, monkeypatch
) -> None:
    """Таймаут на runner: «подтверждено» и «не подтверждено» — два разных текста.

    Хаб на этом транспорте никого не убивает, поэтому «процесс и вся его
    группа убиты» про него — неправда (находка 89893c4bd8451eb0).
    """
    from hub.services.review_dispatch import (
        _local_failure_reason,
        _stopped_hub_reason,
    )

    monkeypatch.setattr(local_reviewer, "RUNNER_GRACE_SEC", 0.4)
    _beat(spool)

    async def claim_only() -> None:
        while True:
            for jobdir in _jobs(spool):
                (jobdir / "claimed").write_text("")
            await asyncio.sleep(0.01)

    service = asyncio.create_task(claim_only())
    try:
        silent = await local_reviewer.run_review("промт", timeout=1)
    finally:
        service.cancel()
    assert silent is not None and silent.timed_out
    assert silent.cancel_confirmed is False
    text = _local_failure_reason(silent)
    assert "НЕ подтверждено" in text and "--timeout контейнера" in text, text
    assert "убиты" not in text

    answered = local_reviewer.LocalRun(124, "", 0, True, 1, cancel_confirmed=True)
    ok_text = _local_failure_reason(answered)
    assert "подтверждено" in ok_text and "НЕ подтверждено" not in ok_text, ok_text
    assert "убиты" not in ok_text

    direct = local_reviewer.LocalRun(124, "", 0, True, 1)
    assert "процесс и вся его группа убиты" in _local_failure_reason(direct), (
        "прямой транспорт убивает сам: прежний текст остаётся"
    )

    assert "отозвал задание" in _stopped_hub_reason()
    assert "убит вместе с хабом" not in _stopped_hub_reason()
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "direct")
    assert "убит вместе с хабом" in _stopped_hub_reason()


async def test_a_runner_that_dies_after_the_claim_is_given_up_early(
    spool, monkeypatch
) -> None:
    """Служба упала после claim: хаб не ждёт весь лимит, а называет причину."""
    monkeypatch.setattr(local_reviewer, "RUNNER_HEARTBEAT_MAX_AGE_SEC", 2.0)
    _beat(spool)

    async def dying_service() -> None:
        while True:
            for jobdir in _jobs(spool):
                if not (jobdir / "claimed").exists():
                    (jobdir / "claimed").write_text("")
                    _beat(spool, age=100.0)
            await asyncio.sleep(0.01)

    service = asyncio.create_task(dying_service())
    started = time.monotonic()
    try:
        run = await local_reviewer.run_review("промт", timeout=60)
    finally:
        service.cancel()
    assert run is None
    assert time.monotonic() - started < 10, "хаб ждал службу, которой уже нет"
    assert "перестала отвечать во время прогона" in local_reviewer.refusal()
    assert _jobs(spool) == []


def test_a_restarted_runner_closes_the_jobs_the_old_one_left(
    spool, runner_mod, tmp_path
) -> None:
    """Осиротевший claimed-каталог закрывается result.json с названной причиной."""
    cfg = _runner_cfg(runner_mod, tmp_path, spool, _fake_cli(tmp_path, "pass\n"))
    orphan = _write_job(spool, "job-" + "3" * 16, {"version": 1, "timeout_sec": 5})
    (orphan / "claimed").write_text("")
    done = _write_job(spool, "job-" + "4" * 16, {"version": 1, "timeout_sec": 5})
    (done / "claimed").write_text("")
    (done / "result.json").write_text('{"status": "ok"}')
    waiting = _write_job(spool, "job-" + "5" * 16, {"version": 1, "timeout_sec": 5})
    assert runner_mod.recover_orphans(cfg) == 1
    result = json.loads((orphan / "result.json").read_text())
    assert result["status"] == "error" and "перезапущена" in result["reason"]
    assert json.loads((done / "result.json").read_text()) == {"status": "ok"}
    assert not (waiting / "result.json").exists(), "ждущее задание не трогаем"


async def test_the_serving_runner_recovers_orphans_on_start(
    spool, runner_mod, tmp_path
) -> None:
    cfg = _runner_cfg(runner_mod, tmp_path, spool, _fake_cli(tmp_path, "pass\n"))
    orphan = _write_job(spool, "job-" + "6" * 16, {"version": 1, "timeout_sec": 5})
    (orphan / "claimed").write_text("")
    service = asyncio.create_task(runner_mod.serve(cfg))
    try:
        assert await _until(lambda: (orphan / "result.json").exists(), 3)
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)


# --------------------------------------------------------------------- AC-4


async def test_the_runner_runs_only_its_own_command(
    spool, runner_mod, tmp_path
) -> None:
    """AC-4: задание команды не несёт; лишние поля отвергаются с причиной."""
    evil = tmp_path / "evil_ran"
    record = tmp_path / "record.json"
    argv = _fake_cli(
        tmp_path,
        "import json, pathlib\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps({{'argv': sys.argv, 'stdin': prompt}}))\n",
    )
    cfg = _runner_cfg(runner_mod, tmp_path, spool, argv)

    jobdir = _write_job(
        spool,
        "job-" + "a" * 16,
        {
            "version": 1,
            "timeout_sec": 5,
            "command": f"/bin/sh -c 'touch {evil}'",
            "cwd": "/",
        },
    )
    await runner_mod.run_pending(cfg)
    result = json.loads((jobdir / "result.json").read_text())
    assert result["status"] == "rejected"
    assert "command" in result["reason"] and "cwd" in result["reason"], result
    assert not evil.exists() and not record.exists(), "отвергнутое задание исполнено"
    assert not (jobdir / "prompt.txt").exists(), "промт отвергнутого задания остался"

    ok = _write_job(
        spool, "job-" + "b" * 16, {"version": 1, "timeout_sec": 5}, "ПРОМТ-В-STDIN"
    )
    await runner_mod.run_pending(cfg)
    assert json.loads((ok / "result.json").read_text())["status"] == "ok"
    ran = json.loads(record.read_text())
    assert ran["argv"][1:] == [], "задание не добавило ни одного аргумента"
    assert ran["stdin"] == "ПРОМТ-В-STDIN", "промт уходит в stdin"
    assert not (ok / "prompt.txt").exists(), "промт удалён после прогона"


def test_the_runner_refuses_a_command_outside_the_sudo_shape(runner_mod) -> None:
    """Конфиг службы — единственный источник команды, и он держит форму sudo."""
    good = {
        "HAIPLANE_REVIEW_RUNNER_SPOOL_DIR": "/s",
        "HAIPLANE_REVIEW_RUNNER_SCRATCH_DIR": "/r",
        "HAIPLANE_REVIEW_RUNNER_ARGV": "/usr/bin/sudo -n -u haiplane-reviewer "
        "/usr/local/bin/haiplane-review-run --model x",
    }
    cfg = runner_mod.load_config(good)
    assert cfg.argv[:4] == ("/usr/bin/sudo", "-n", "-u", "haiplane-reviewer")

    for bad in (
        "",
        "/bin/sh -c id",
        "/usr/bin/sudo -u haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u root /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u haiplane-reviewer wrap",
        "/usr/bin/sudo -u haiplane-reviewer /usr/local/bin/wrap --x",
        "/bin/sh -n -u haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -S -u haiplane-reviewer /usr/local/bin/wrap",
    ):
        with pytest.raises(runner_mod.ConfigError):
            runner_mod.load_config({**good, "HAIPLANE_REVIEW_RUNNER_ARGV": bad})


# ----------------------------------------------- хаб и служба говорят одно


def test_the_hub_and_the_runner_share_one_protocol(runner_mod) -> None:
    for name in ("PROMPT", "JOB", "CLAIMED", "CANCEL", "RESULT", "HEARTBEAT"):
        assert getattr(local_reviewer, f"SPOOL_{name}") == getattr(
            runner_mod, f"SPOOL_{name}"
        ), name
    assert set(local_reviewer.JOB_FIELDS) == set(runner_mod.JOB_FIELDS)
    assert local_reviewer.JOB_VERSION == runner_mod.JOB_VERSION
    assert local_reviewer.OUTPUT_CAP == runner_mod.OUTPUT_CAP


def test_the_doc_names_every_setting_the_runner_reads(runner_mod) -> None:
    """Имена настроек службы в документе сверены с её исходником."""
    source = _RUNNER_FILE.read_text()
    read = set(re.findall(r'"(HAIPLANE_REVIEW_RUNNER_[A-Z_]+)"', source))
    assert read, "в исходнике службы не найдено ни одной настройки"
    doc = _DOC.read_text()
    example = (
        _ROOT / "deploy/review-runner/haiplane-review-runner.env.example"
    ).read_text()
    for name in sorted(read):
        assert name in doc, f"{name} не назван в deploy/LOCAL-REVIEW.md"
        assert name in example, f"{name} не назван в env.example службы"
    assert shlex.quote("HAIPLANE_LOCAL_REVIEW_TRANSPORT") in doc


# ------------------------------------- места применения, не названные в AC


def test_direct_stays_the_default_and_an_unknown_transport_is_named(
    monkeypatch,
) -> None:
    """Без настройки поведение прежнее; незнакомый транспорт — названная причина."""
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "direct")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SPOOL_DIR", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "")
    direct = local_reviewer.not_ready()
    assert any("LOCAL_REVIEW_CMD" in r for r in direct), direct
    assert not any("SPOOL" in r for r in direct), "очередь нужна только при runner"
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "")
    assert local_reviewer.transport() == "direct"
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "bogus")
    named = local_reviewer.not_ready()
    assert any("LOCAL_REVIEW_TRANSPORT" in r for r in named), named
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "runner")
    assert any("SPOOL_DIR" in r for r in local_reviewer.not_ready())


async def test_a_refusal_of_the_runner_is_named_not_swallowed(spool) -> None:
    """Служба отказала заданию: хаб возвращает None и называет её причину."""
    _beat(spool)

    async def refusing_service() -> None:
        while True:
            for jobdir in _jobs(spool):
                if (jobdir / "job.json").exists() and not (
                    jobdir / "result.json"
                ).exists():
                    (jobdir / "claimed").write_text("")
                    (jobdir / "result.json").write_text(
                        json.dumps({"status": "rejected", "reason": "лишнее поле: x"})
                    )
            await asyncio.sleep(0.01)

    service = asyncio.create_task(refusing_service())
    try:
        assert await local_reviewer.run_review("промт", timeout=10) is None
    finally:
        service.cancel()
    assert "лишнее поле: x" in local_reviewer.refusal()
    assert _jobs(spool) == []


async def test_a_job_without_job_json_is_not_pending(
    spool, runner_mod, tmp_path
) -> None:
    """job.json пишется последним: каталог с одним промтом служба не берёт."""
    cfg = _runner_cfg(runner_mod, tmp_path, spool, _fake_cli(tmp_path, "pass\n"))
    half = spool / ("job-" + "c" * 16)
    half.mkdir(mode=0o770)
    (half / "prompt.txt").write_text("промт")
    assert runner_mod._pending(cfg) == []
    assert await runner_mod.run_pending(cfg) == 0
    assert not (half / "claimed").exists()


async def test_the_runner_enforces_the_job_timeout_even_without_the_hub(
    spool, runner_mod, tmp_path, monkeypatch
) -> None:
    """Хаб упал и не пишет cancel: срок задания держит сама служба.

    Форма sudo смоделирована как есть: SIGTERM до потомка доходит (его
    пересылает sudo), SIGKILL — нет (другой uid). Тест того же uid, где
    SIGKILL работает, маскировал бы это (находка 654ca57c18089771).
    """
    marker = tmp_path / "outlived"
    argv = _fake_cli(
        tmp_path,
        "import signal, sys, time, pathlib\n"
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(143))\n"
        "time.sleep(3.0)\n"
        f"pathlib.Path({str(marker)!r}).write_text('x')\n",
    )
    sent: list[int] = []
    real = runner_mod._signal_group

    def sudo_like(proc, sig):
        sent.append(int(sig))
        if sig == runner_mod.signal.SIGKILL:
            return  # до потомка sudo не доходит
        real(proc, sig)

    monkeypatch.setattr(runner_mod, "_signal_group", sudo_like)
    cfg = _runner_cfg(runner_mod, tmp_path, spool, argv, term_grace=0.3)
    jobdir = _write_job(spool, "job-" + "d" * 16, {"version": 1, "timeout_sec": 1})
    started = time.monotonic()
    await runner_mod.run_pending(cfg)
    assert time.monotonic() - started < 2.9
    result = json.loads((jobdir / "result.json").read_text())
    assert result["status"] == "ok" and result["timed_out"] is True, result
    assert int(runner_mod.signal.SIGTERM) in sent, "SIGTERM не отправлен"
    await asyncio.sleep(3.0)
    assert not marker.exists(), "процесс пережил SIGTERM и дописал файл"


async def test_the_runner_does_not_hang_when_nothing_can_kill_the_process(
    spool, runner_mod, tmp_path, monkeypatch
) -> None:
    """Ни SIGTERM, ни SIGKILL не доходят: служба отвечает за ограниченное время.

    Остаётся --timeout контейнера; служба не ждёт процесс вечно.
    """
    argv = _fake_cli(tmp_path, "import time\ntime.sleep(4.0)\n")
    monkeypatch.setattr(runner_mod, "_signal_group", lambda proc, sig: None)
    cfg = _runner_cfg(runner_mod, tmp_path, spool, argv, term_grace=0.3)
    jobdir = _write_job(spool, "job-" + "7" * 16, {"version": 1, "timeout_sec": 1})
    started = time.monotonic()
    await runner_mod.run_pending(cfg)
    assert time.monotonic() - started < 9
    result = json.loads((jobdir / "result.json").read_text())
    assert result["timed_out"] is True, result
    await asyncio.sleep(3.2)  # пусть процесс доживёт сам, не мусорим


def test_the_job_term_may_not_be_shorter_than_the_wrapper_timeout(
    spool, runner_mod, tmp_path
) -> None:
    """Иначе хаб объявит снятие раньше, чем контейнер умрёт."""
    job = json.dumps({"version": 1, "timeout_sec": 100}).encode()
    assert runner_mod.parse_job(job, 1800, 100) == 100
    with pytest.raises(runner_mod.JobRejected) as err:
        runner_mod.parse_job(job, 1800, 101)
    assert "--timeout" in str(err.value)
    base = {
        "HAIPLANE_REVIEW_RUNNER_SPOOL_DIR": "/s",
        "HAIPLANE_REVIEW_RUNNER_SCRATCH_DIR": "/r",
        "HAIPLANE_REVIEW_RUNNER_ARGV": "/usr/bin/sudo -n -u u /w",
        "HAIPLANE_REVIEW_RUNNER_MAX_TIMEOUT_SEC": "600",
    }
    ok = runner_mod.load_config(
        {**base, "HAIPLANE_REVIEW_RUNNER_WRAPPER_TIMEOUT_SEC": "600"}
    )
    assert ok.wrapper_timeout == 600
    with pytest.raises(runner_mod.ConfigError):
        runner_mod.load_config(
            {**base, "HAIPLANE_REVIEW_RUNNER_WRAPPER_TIMEOUT_SEC": "601"}
        )


async def test_a_too_short_job_is_rejected_by_the_service(
    spool, runner_mod, tmp_path
) -> None:
    ran = tmp_path / "ran"
    argv = _fake_cli(
        tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('x')\n"
    )
    cfg = _runner_cfg(runner_mod, tmp_path, spool, argv, wrapper_timeout=30)
    jobdir = _write_job(
        spool, "job-" + "9" * 15 + "a", {"version": 1, "timeout_sec": 5}
    )
    await runner_mod.run_pending(cfg)
    result = json.loads((jobdir / "result.json").read_text())
    assert result["status"] == "rejected" and "--timeout" in result["reason"]
    assert not ran.exists()


async def test_the_runner_runs_a_claimed_job_once_and_trims_the_output(
    spool, runner_mod, tmp_path, monkeypatch
) -> None:
    """Чужое окружение не доезжает, вывод режется по потолку, второго запуска нет."""
    monkeypatch.setenv("HUB_SECRET_FOR_TEST", "tajnoe")
    runs = tmp_path / "runs"
    argv = _fake_cli(
        tmp_path,
        "import os, pathlib\n"
        f"pathlib.Path({str(runs)!r}).open('a').write('x')\n"
        "print('SEEN_SECRET' if 'HUB_SECRET_FOR_TEST' in os.environ else 'clean')\n"
        "print('y' * 5000)\n",
    )
    cfg = _runner_cfg(runner_mod, tmp_path, spool, argv, output_cap=1000)
    jobdir = _write_job(spool, "job-" + "e" * 16, {"version": 1, "timeout_sec": 10})
    (jobdir / "claimed").write_text("")
    assert await runner_mod.run_pending(cfg) == 0, "взятое задание берётся второй раз"
    assert not runs.exists()
    (jobdir / "claimed").unlink()
    await runner_mod.run_pending(cfg)
    result = json.loads((jobdir / "result.json").read_text())
    assert runs.read_text() == "x"
    assert result["output"].startswith("clean"), "окружение хаба доехало до процесса"
    assert len(result["output"]) <= 1000 and result["dropped"] > 3000, result


async def test_the_runner_does_not_follow_a_symlinked_prompt(
    spool, runner_mod, tmp_path
) -> None:
    """prompt.txt — ссылка на чужой файл: служба его не читает."""
    secret = tmp_path / "secret.txt"
    secret.write_text("ЧУЖОЙ-ФАЙЛ")
    record = tmp_path / "record"
    argv = _fake_cli(
        tmp_path, f"import pathlib\npathlib.Path({str(record)!r}).write_text(prompt)\n"
    )
    cfg = _runner_cfg(runner_mod, tmp_path, spool, argv)
    jobdir = spool / ("job-" + "f" * 16)
    jobdir.mkdir(mode=0o770)
    (jobdir / "prompt.txt").symlink_to(secret)
    (jobdir / "job.json").write_text(json.dumps({"version": 1, "timeout_sec": 5}))
    await runner_mod.run_pending(cfg)
    result = json.loads((jobdir / "result.json").read_text())
    assert result["status"] == "error", result
    assert not record.exists(), "служба прочитала файл по ссылке и запустила прогон"
    assert secret.exists(), "ссылка не должна уносить цель"


async def test_the_runner_writes_its_heartbeat_and_sweeps_stale_jobs(
    spool, runner_mod, tmp_path
) -> None:
    cfg = _runner_cfg(
        runner_mod,
        tmp_path,
        spool,
        _fake_cli(tmp_path, "pass\n"),
        stale_sec=10.0,
        max_timeout=5,
    )
    old = spool / ("job-" + "1" * 16)
    fresh = spool / ("job-" + "2" * 16)
    for d in (old, fresh):
        d.mkdir(mode=0o770)
        (d / "result.json").write_text("{}")
    long_ago = time.time() - 3600
    os.utime(old, (long_ago, long_ago))
    service = asyncio.create_task(runner_mod.serve(cfg))
    try:
        assert await _until(lambda: (spool / "heartbeat").exists(), 3)
        assert await _until(lambda: not old.exists(), 3), "брошенный каталог не убран"
        assert fresh.exists(), "свежий результат убран раньше, чем хаб его заберёт"
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)
    assert local_reviewer.runner_problem() == [], "хаб не видит живую службу"


async def test_stopping_the_hub_leaves_no_prompt_even_if_the_runner_never_read_it(
    spool,
) -> None:
    """Служба взяла задание, но промт ещё не прочла: хаб уходит — промта нет."""
    _beat(spool)

    async def slow_service() -> None:
        while True:
            for jobdir in _jobs(spool):
                if (jobdir / "job.json").exists():
                    (jobdir / "claimed").write_text("")
            await asyncio.sleep(0.01)

    service = asyncio.create_task(slow_service())
    try:
        task = asyncio.create_task(local_reviewer.run_review("КОД", timeout=30))
        assert await _until(
            lambda: any((j / "claimed").exists() for j in _jobs(spool)), 3
        )
        jobdir = _jobs(spool)[0]
        assert (jobdir / "prompt.txt").exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (jobdir / "prompt.txt").exists(), "промт остался на диске"
        assert (jobdir / "cancel").exists(), "хаб не написал снятие"
    finally:
        service.cancel()


async def test_a_claimed_job_is_not_run_twice(spool, runner_mod, tmp_path) -> None:
    runs = tmp_path / "runs"
    argv = _fake_cli(
        tmp_path, f"import pathlib\npathlib.Path({str(runs)!r}).write_text('x')\n"
    )
    cfg = _runner_cfg(runner_mod, tmp_path, spool, argv)
    jobdir = _write_job(spool, "job-" + "9" * 16, {"version": 1, "timeout_sec": 5})
    (jobdir / "claimed").write_text("")
    await runner_mod.process_job(cfg, jobdir.name)
    assert not runs.exists(), "взятое другим задание запущено повторно"


async def test_withdrawing_the_job_stops_a_running_process(
    spool, runner_mod, tmp_path
) -> None:
    """Исчезновение job.json — тоже снятие, даже без файла cancel."""
    marker = tmp_path / "outlived"
    started = tmp_path / "started"
    argv = _fake_cli(
        tmp_path,
        "import time, pathlib\n"
        f"pathlib.Path({str(started)!r}).write_text('x')\n"
        "time.sleep(3.0)\n"
        f"pathlib.Path({str(marker)!r}).write_text('x')\n",
    )
    cfg = _runner_cfg(runner_mod, tmp_path, spool, argv)
    jobdir = _write_job(spool, "job-" + "8" * 16, {"version": 1, "timeout_sec": 30})
    work = asyncio.create_task(runner_mod.run_pending(cfg))
    assert await _until(started.exists, 3)
    (jobdir / "job.json").unlink()
    await asyncio.wait_for(work, 3)
    assert not jobdir.exists(), "отозванное задание не убрано"
    await asyncio.sleep(3.0)
    assert not marker.exists(), "процесс пережил отзыв задания"
