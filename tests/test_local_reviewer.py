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
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest

from hub import config
from hub.integrations import local_reviewer, review_snapshot

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
    # Каталог прогонов, каким его требует снимок (#1599): sticky + setgid.
    os.chmod(scratch, 0o3770)
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
    values.setdefault("reviewer_user", "nobody")
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
    # Снимок (#1599): имя файла, версия задания со снимком и потолок размера.
    assert local_reviewer.SPOOL_SNAPSHOT == runner_mod.SPOOL_SNAPSHOT == "src.tar"
    assert local_reviewer.JOB_VERSION_SNAPSHOT == runner_mod.JOB_VERSION_SNAPSHOT == 2
    assert runner_mod.JOB_VERSIONS == (
        local_reviewer.JOB_VERSION,
        local_reviewer.JOB_VERSION_SNAPSHOT,
    ), "служба принимает ровно версии, которые умеет слать хаб"
    assert (
        config.LOCAL_REVIEW_SNAPSHOT_MAX_BYTES
        <= runner_mod.Config.__dataclass_fields__["snapshot_max_bytes"].default
    ), "потолок хаба не больше потолка службы: иначе допустимый архив отвергнут"


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


async def test_stopping_the_hub_leaves_no_snapshot_if_the_runner_never_unpacked_it(
    spool, monkeypatch, tmp_path
) -> None:
    """Служба взяла задание, но снимок не распаковала: хаб уходит — src.tar убран."""
    _snapshot_config(monkeypatch)
    repo, sha = _make_repo(tmp_path)
    snapshot = await _archive(repo, sha)
    _beat(spool)

    async def slow_service() -> None:
        while True:
            for jobdir in _jobs(spool):
                if (jobdir / "job.json").exists():
                    (jobdir / "claimed").write_text("")
            await asyncio.sleep(0.01)

    service = asyncio.create_task(slow_service())
    try:
        task = asyncio.create_task(
            local_reviewer.run_review("КОД", timeout=30, snapshot=snapshot)
        )
        assert await _until(
            lambda: any((j / "claimed").exists() for j in _jobs(spool)), 3
        )
        jobdir = _jobs(spool)[0]
        assert (jobdir / "src.tar").exists(), "предпосылка: снимок лежит в задании"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (jobdir / "src.tar").exists(), "src.tar остался после остановки хаба"
        assert not (jobdir / "prompt.txt").exists()
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


async def test_a_dead_runner_before_the_claim_is_not_called_alive(
    spool, monkeypatch
) -> None:
    """Heartbeat устарел, задание не взято: причина — молчание, а не «свежий».

    Порог взятия (60 с) больше порога heartbeat (30 с), и жёсткий текст
    «heartbeat свежий» называл мёртвую службу живой (находка a718d2711448e87f).
    """
    from hub.services.review_dispatch import _local_failure_reason

    monkeypatch.setattr(local_reviewer, "RUNNER_HEARTBEAT_MAX_AGE_SEC", 2.0)
    monkeypatch.setattr(local_reviewer, "RUNNER_PICKUP_SEC", 30.0)
    _beat(spool)
    gone = time.time() - 100

    async def goes_silent() -> None:
        # Служба «жива» к заказу и умирает до того, как взяла задание.
        while True:
            if _jobs(spool):
                os.utime(spool / "heartbeat", (gone, gone))
            await asyncio.sleep(0.01)

    service = asyncio.create_task(goes_silent())
    started = time.monotonic()
    try:
        assert await local_reviewer.run_review("промт", timeout=60) is None
    finally:
        service.cancel()
    assert time.monotonic() - started < 10, "хаб ждал порог взятия вместо heartbeat"
    named = local_reviewer.refusal()
    assert "перестала отвечать до того, как взяла задание" in named, named
    assert "свежий" not in named
    card = _local_failure_reason(None)
    assert "перестала отвечать" in card and "свежий" not in card
    assert _jobs(spool) == []


# ------------------------------------------------- выкладка хаба (#1588)


def _drain_marker(spool: Path, ahead: int = 600, owner: str = "deploy-test") -> None:
    (spool / "draining").write_text(
        f"owner={owner}\nexpires={int(time.time()) + ahead}\n"
    )


def _answering_service(spool: Path, seen: list[str]) -> "asyncio.Task[None]":
    """Служба-заглушка: берёт задание и отвечает result.json (как в AC-1)."""

    async def _serve() -> None:
        while True:
            for jobdir in _jobs(spool):
                if (jobdir / "job.json").exists() and jobdir.name not in seen:
                    seen.append(jobdir.name)
                    (jobdir / "claimed").write_text("")
                    (jobdir / "result.json").write_text(
                        json.dumps(
                            {
                                "status": "ok",
                                "rc": 0,
                                "output": "ок",
                                "dropped": 0,
                                "timed_out": False,
                                "duration_ms": 1,
                                "reason": "",
                            }
                        )
                    )
            await asyncio.sleep(0.01)

    return asyncio.create_task(_serve())


async def test_drain_marker_checked_with_publish_under_lock(spool, monkeypatch) -> None:
    """AC-3: маркер проверяется ДО слота и под замком при публикации job.json.

    Три гонки с управляемыми барьерами: (а) слот занят, маркер свежий — отказ
    сразу и слот не освобождается; (б) маркер появился, пока заказ ждал слот;
    (в) маркер появился во время подготовки промта. Идущий прогон при этом не
    трогается, а просроченный маркер не мешает.
    """
    import fcntl

    _beat(spool)
    running = _write_job(
        spool, "job-0123456789abcdef", {"version": 1, "timeout_sec": 5}
    )
    (running / "claimed").write_text("")
    before = sorted(p.name for p in running.iterdir())

    # Что хаб ПОПЫТАЛСЯ опубликовать: каталог задания хаб убирает за собой, так
    # что по содержимому spool «опубликовано, а потом убрано» не отличить.
    published: list[str] = []
    real_submit = local_reviewer._submit_job

    def _spy(spool_dir: str, prompt: str, limit: int) -> str:
        published.append(prompt)
        return real_submit(spool_dir, prompt, limit)

    monkeypatch.setattr(local_reviewer, "_submit_job", _spy)

    async def _order(**kw):
        """Прогон И причина отказа — в одном контексте: причина лежит в ContextVar."""
        run = await local_reviewer.run_review("п", timeout=5, **kw)
        return run, local_reviewer.refusal()

    # (а) слот занят чужим прогоном, маркер свежий: отказ сразу, не ждать слот.
    await local_reviewer._HOST_BUDGET.acquire()
    try:
        _drain_marker(spool)
        started = time.monotonic()
        run, why = await asyncio.wait_for(_order(), 2)
        assert run is None
        assert time.monotonic() - started < 1.0, "заказ ждал слот вместо отказа"
        assert local_reviewer.DRAIN_REASON in why
        assert local_reviewer._HOST_BUDGET.locked(), "слот освобождён чужим отказом"
    finally:
        local_reviewer._HOST_BUDGET.release()
    assert sorted(p.name for p in running.iterdir()) == before, (
        "идущий прогон тронут: отказ новому заказу не отменяет его"
    )
    assert published == []
    (spool / "draining").unlink()

    # (б) маркер появился, пока заказ ждал слот: промт не готовится, job.json нет.
    prepared: list[str] = []

    async def _prompt(ready: str) -> str:
        prepared.append(ready)
        return ready

    await local_reviewer._HOST_BUDGET.acquire()
    waiting = asyncio.create_task(_order(prompt_at_slot=_prompt))
    await asyncio.sleep(0.1)
    assert not waiting.done(), "предпосылка: заказ ждёт слот"
    _drain_marker(spool)
    local_reviewer._HOST_BUDGET.release()
    run, why = await asyncio.wait_for(waiting, 2)
    assert run is None and local_reviewer.DRAIN_REASON in why
    assert prepared == [], "промт с одноразовым кодом готовился под запретом"
    assert published == []
    (spool / "draining").unlink()

    # (в) маркер появился во время подготовки промта: ловит только проверка
    # при самой публикации.
    async def _prompt_then_marker(ready: str) -> str:
        _drain_marker(spool)
        return ready

    run, why = await _order(prompt_at_slot=_prompt_then_marker)
    assert run is None and local_reviewer.DRAIN_REASON in why
    assert published == [], "job.json опубликован под запретом"
    (spool / "draining").unlink()

    # (в') проверка и публикация — под ОДНИМ файловым замком: заказ дошёл до
    # публикации, замок держит «деплой», маркер ставится под замком — заказ,
    # получив замок, обязан его увидеть. Проверка ДО замка этого не поймает.
    lock_fd = os.open(spool / ".drain.lock", os.O_RDWR | os.O_CREAT, 0o660)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        order = asyncio.create_task(_order())
        await asyncio.sleep(0.2)
        assert not order.done(), "публикация не должна идти мимо замка"
        _drain_marker(spool)
    finally:
        os.close(lock_fd)
    run, why = await asyncio.wait_for(order, 3)
    assert run is None and local_reviewer.DRAIN_REASON in why, why
    assert published == [], "маркер, поставленный под замком, не увидели"
    (spool / "draining").unlink()

    # Замок занят дольше, чем он вправе: отказ с названной причиной, не зависание.
    monkeypatch.setattr(local_reviewer, "DRAIN_LOCK_WAIT_SEC", 0.2)
    lock_fd = os.open(spool / ".drain.lock", os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        run, why = await asyncio.wait_for(_order(), 3)
    finally:
        os.close(lock_fd)
    assert run is None and ".drain.lock" in why
    assert published == []

    # Просроченный маркер не мешает, а свой запрет ставят только свежие.
    (running / "result.json").write_text("{}")
    _drain_marker(spool, ahead=-5)
    seen: list[str] = [running.name]  # старый прогон служба-заглушка не берёт
    service = _answering_service(spool, seen)
    try:
        run, why = await _order()
    finally:
        service.cancel()
    assert run is not None and run.output == "ок", why
    assert len(published) == 1 and len(seen) == 2, "просроченный маркер остановил заказ"
    assert (spool / "draining").exists(), "хаб чужой маркер не снимает"


async def test_drain_marker_is_not_a_readiness_problem(spool) -> None:
    """Запрет — только в пути запуска: not_ready() и runner_problem() не знают."""
    _beat(spool)
    assert local_reviewer.not_ready() == []
    _drain_marker(spool)
    assert local_reviewer.not_ready() == [], "готовность читают проект и UI"
    assert local_reviewer.runner_problem() == []
    assert local_reviewer.drain_refusal().startswith(local_reviewer.DRAIN_REASON)


@pytest.mark.parametrize(
    "body",
    [
        "owner=x\n",  # нет срока
        "owner=x\nexpires=abc\n",  # срок не число
        "owner=x\nexpires=1\n",  # давно просрочен
        "owner=x\nexpires=99999999999\n",  # «навечно»: не маркер
    ],
)
async def test_a_broken_or_expired_marker_does_not_block(spool, body: str) -> None:
    (spool / "draining").write_text(body)
    assert local_reviewer.drain_refusal() == ""


async def test_marker_is_ignored_for_the_direct_transport(spool, monkeypatch) -> None:
    _drain_marker(spool)
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "direct")
    assert local_reviewer.drain_refusal() == ""


def test_the_marker_and_the_lock_never_look_like_a_job(runner_mod) -> None:
    for name in (local_reviewer.SPOOL_DRAIN_MARKER, local_reviewer.SPOOL_DRAIN_LOCK):
        assert not runner_mod._JOB_NAME.match(name)


# ===================================================== снимок исходников (#1599)
#
# Ревьюер получает одноразовый снимок git archive на закреплённом sha, только
# для чтения. Что здесь проверено НАСТОЯЩИМ: хаб снимает архив из настоящего
# git-репозитория, служба (её модуль) и распаковщик (его модуль) исполняются
# как есть, процесс-заглушка читает снимок и пытается в него писать.
#
# Чего здесь НЕТ и что за проверенное не выдаётся (перенесено в ручную пробу
# владельца, deploy/LOCAL-REVIEW.md «Проба снимка»): второй uid ревьюера и
# rootless podman. Тестовый процесс — тот же uid, что служба, поэтому отказ
# записи он получает по битам режима (0550/0440), а не по чужому uid; чтение
# через группу и монтирование :ro проверяет только владелец на хосте.

_UNPACK_FILE = _ROOT / "deploy/review-runner/snapshot_unpack.py"
_OLD_RUNNER = _ROOT / "tests/fixtures/review_runner_v1.py.txt"
_WRAPPER = _ROOT / "deploy/review-runner/haiplane-review-run"
_ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0


def _load_unpacker():
    spec = importlib.util.spec_from_file_location("snapshot_unpack", _UNPACK_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    import subprocess

    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    return done.stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    """Настоящий репозиторий: файл вне диффа, бинарный файл, ссылка, подкаталоги."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "outside_the_diff.py").write_text("SECRET_SYMBOL = 42\n")
    (repo / "bin.dat").write_bytes(b"\xff\xfe\x00\n\n")
    (repo / "top.txt").write_text("top\n")
    (repo / "link").symlink_to("top.txt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "changed.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the submission")
    return repo, _git(repo, "rev-parse", "HEAD")


def _snapshot_config(monkeypatch, *, enabled: bool = True) -> None:
    monkeypatch.setattr(config, "LOCAL_REVIEW_SNAPSHOT", enabled, raising=False)


async def _archive(repo: Path, sha: str, max_bytes: int = 8 * 1024 * 1024):
    from hub.integrations.git_ops import GitOpsIntegration

    return await GitOpsIntegration().snapshot_archive(str(repo), sha, max_bytes)


# Заглушка модели: читает путь снимка ИЗ ПРОМТА (как это сделал бы ревьюер),
# читает файл вне диффа и пробует то, чего ей нельзя. «/work/src» — путь
# контейнера: заглушка играет роль враппера и ведёт его на $PWD/src.
_READER_BODY = r"""
import json, os, re
path = re.search(r"Снимок: (\S+)", prompt).group(1)
real = path.replace("/work/src", os.path.join(os.getcwd(), "src"))
out = {"path": path, "real": real, "cwd": os.getcwd(), "home": os.environ.get("HOME")}
out["text"] = open(os.path.join(real, "pkg", "outside_the_diff.py")).read()
out["bin"] = open(os.path.join(real, "bin.dat"), "rb").read().hex()
out["names"] = sorted(os.listdir(real))
def attempt(name, fn):
    try:
        fn()
        out[name] = "ALLOWED"
    except OSError as exc:
        out[name] = type(exc).__name__
attempt("write_old", lambda: open(os.path.join(real, "top.txt"), "w").write("x"))
attempt("write_new", lambda: open(os.path.join(real, "new.txt"), "w").write("x"))
attempt("mkdir_in", lambda: os.mkdir(os.path.join(real, "pkg", "d")))
attempt("rename_src", lambda: os.rename(real, real + ".moved"))
attempt("replace_in_parent", lambda: os.mkdir(os.path.join(os.path.dirname(real), "evil")))
attempt("write_home", lambda: open(os.path.join(out["home"], "ok.txt"), "w").write("x"))
print(json.dumps(out))
"""
_READER = "import sys\nprompt = sys.stdin.read()\n" + _READER_BODY


def _parse_reader(output: str) -> dict:
    line = next(ln for ln in output.splitlines() if ln.startswith("{"))
    return json.loads(line)


def _assert_read_only(seen: dict) -> None:
    assert seen["text"] == "SECRET_SYMBOL = 42\n", "файл вне диффа не прочитан"
    assert bytes.fromhex(seen["bin"]) == b"\xff\xfe\x00\n\n", "бинарные байты искажены"
    assert "link" not in seen["names"], "символьная ссылка попала в снимок"
    assert seen["write_home"] == "ALLOWED", "рабочий каталог ревьюера недоступен"
    if _ROOT_USER:  # root обходит биты режима: утверждать нечего
        return
    for attempt in ("write_old", "write_new", "mkdir_in", "rename_src"):
        assert seen[attempt] == "PermissionError", (attempt, seen)
    assert seen["replace_in_parent"] == "PermissionError", (
        "в родителе src нельзя создать соседа: подменить src нечем"
    )


def _spy_spool_writes(monkeypatch) -> list[tuple[str, bytes]]:
    """Порядок записей хаба в spool: имя и содержимое."""
    seen: list[tuple[str, bytes]] = []
    real = local_reviewer._write_spool_file

    def _spy(jobdir: str, name: str, data: bytes) -> None:
        seen.append((name, bytes(data)))
        real(jobdir, name, data)

    monkeypatch.setattr(local_reviewer, "_write_spool_file", _spy)
    return seen


async def test_the_reviewer_reads_a_read_only_snapshot_at_the_submission_sha(
    spool, runner_mod, tmp_path, monkeypatch
) -> None:
    """AC-1: снимок на sha сдачи доезжает до модели через runner и через direct.

    Настоящее: git-репозиторий и ``git archive`` (с pax_global_header), модуль
    службы и модуль распаковщика. Модель — заглушка того же uid: запись в src
    ей не удаётся по битам режима; чтение ГРУППОЙ и :ro-монтирование проверяет
    ручная проба владельца.
    """
    _snapshot_config(monkeypatch)
    repo, sha = _make_repo(tmp_path)
    snapshot = await _archive(repo, sha)
    assert snapshot.data is not None and snapshot.sha == sha, snapshot
    import dataclasses

    snapshot = dataclasses.replace(
        snapshot, placeholder=review_snapshot.PATH_PLACEHOLDER
    )
    prompt = f"Снимок: {review_snapshot.PATH_PLACEHOLDER}\n"

    # ---- runner
    unpacker_uid = os.getuid()
    cfg = _runner_cfg(
        runner_mod,
        tmp_path,
        spool,
        _fake_cli(tmp_path, _READER_BODY),
        unpacker_trusted_uid=unpacker_uid,
        snapshot_unpacker=str(_UNPACK_FILE),
    )
    writes = _spy_spool_writes(monkeypatch)
    service = asyncio.create_task(runner_mod.serve(cfg))
    try:
        assert await _until(lambda: (spool / "heartbeat").exists(), 3)
        run = await local_reviewer.run_review(prompt, timeout=20, snapshot=snapshot)
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)
    assert run is not None and run.rc == 0, (run, local_reviewer.refusal())
    seen = _parse_reader(run.output)
    assert seen["path"] == "/work/src", "в контейнере снимок лежит в /work/src"
    _assert_read_only(seen)
    names = [name for name, _ in writes]
    assert names.index(local_reviewer.SPOOL_SNAPSHOT) < names.index(
        local_reviewer.SPOOL_JOB
    ), "src.tar обязан быть опубликован ДО job.json"
    job = json.loads(dict(writes)[local_reviewer.SPOOL_JOB])
    assert job == {"version": 2, "timeout_sec": 20}, "задание: версия и срок, без путей"
    assert str(tmp_path) not in json.dumps(job)
    assert _jobs(spool) == [], "задание удалено"
    assert list((tmp_path / "scratch").iterdir()) == [], "workdir удалён после прогона"

    # ---- настройка выключена: version=1 и никакого снимка, как до задачи.
    _snapshot_config(monkeypatch, enabled=False)
    writes.clear()
    plain = _fake_cli(tmp_path, "print('ok')\n")
    cfg_plain = _runner_cfg(runner_mod, tmp_path, spool, plain)
    service = asyncio.create_task(runner_mod.serve(cfg_plain))
    try:
        assert await _until(lambda: (spool / "heartbeat").exists(), 3)
        run = await local_reviewer.run_review("промт", timeout=20, snapshot=snapshot)
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)
    assert run is not None and run.rc == 0
    assert [n for n, _ in writes] == ["prompt.txt", "job.json"], writes
    assert json.loads(dict(writes)["job.json"]) == {"version": 1, "timeout_sec": 20}
    _snapshot_config(monkeypatch)

    # ---- нет снимка (нет sha, сверх потолка): version=1, состояние — в промте
    from hub.integrations.protocols import SnapshotArchive

    for absent in (
        SnapshotArchive(sha="", data=None, reason="нет закреплённого sha сдачи"),
        await _archive(repo, sha, max_bytes=1000),
    ):
        assert absent.data is None and absent.reason, absent
        writes.clear()
        service = asyncio.create_task(runner_mod.serve(cfg_plain))
        try:
            assert await _until(lambda: (spool / "heartbeat").exists(), 3)
            run = await local_reviewer.run_review("промт", timeout=20, snapshot=absent)
        finally:
            service.cancel()
            await asyncio.gather(service, return_exceptions=True)
        assert run is not None and run.rc == 0
        assert [n for n, _ in writes] == ["prompt.txt", "job.json"], writes

    # ---- direct без контейнера: путь — <workdir>/src, тот же распаковщик
    scratch = tmp_path / "direct-scratch"
    scratch.mkdir()
    os.chmod(scratch, 0o3770)
    import pwd

    me = pwd.getpwuid(os.getuid()).pw_name
    sudo = tmp_path / "sudo"
    sudo.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
    sudo.chmod(0o755)
    # Ревьюер в тесте — тот же пользователь, что владелец scratch; настоящего
    # второго uid нет, поэтому для проверки владельца ему назначен чужой uid
    # (сама проверка владельца проверена отдельно ниже).
    monkeypatch.setattr(local_reviewer, "_reviewer_uid", lambda: os.getuid() + 1)
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "direct")
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", f"{sudo} -n -u {me} {sys.executable}"
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", shlex.join(["-c", _READER]))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(scratch))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SNAPSHOT_UNPACKER", str(_UNPACK_FILE))
    monkeypatch.setattr(review_snapshot, "TRUSTED_UID", os.getuid())
    run = await local_reviewer.run_review(prompt, timeout=20, snapshot=snapshot)
    assert run is not None and run.rc == 0, (run, local_reviewer.refusal())
    seen = _parse_reader(run.output)
    assert seen["path"] == os.path.join(seen["cwd"], "src"), (
        "direct без контейнера: <workdir>/src"
    )
    _assert_read_only(seen)
    assert list(scratch.iterdir()) == [], "workdir direct-прогона удалён"


def _tar_header(
    name: bytes,
    typeflag: bytes = b"0",
    size: int = 0,
    linkname: bytes = b"",
    *,
    size_field: bytes | None = None,
    magic: bytes = b"ustar\x0000",
    checksum_ok: bool = True,
) -> bytes:
    block = bytearray(512)
    block[0 : len(name)] = name
    block[100:108] = b"0000644\x00"
    block[108:116] = b"0000000\x00"
    block[116:124] = b"0000000\x00"
    block[124:136] = size_field or (b"%011o\x00" % size)
    block[136:148] = b"00000000000\x00"
    block[148:156] = b"        "
    block[156:157] = typeflag
    block[157 : 157 + len(linkname)] = linkname
    block[257:265] = magic
    total = sum(block) + (0 if checksum_ok else 7)
    block[148:156] = b"%06o\x00 " % total
    return bytes(block)


def _pad(data: bytes) -> bytes:
    return data + b"\x00" * (-len(data) % 512)


def _tar(*members: tuple, end: bool = True, tail: bytes = b"") -> bytes:
    """Tar вручную: каждый член — (заголовок-kwargs, данные)."""
    out = b""
    for kwargs, data in members:
        out += _tar_header(size=len(data), **kwargs) + _pad(data)
    return out + (b"\x00" * 1024 if end else b"") + tail


def _file(name: str, data: bytes = b"x") -> tuple:
    return ({"name": name.encode()}, data)


def _dir(name: str) -> tuple:
    return ({"name": name.encode() + b"/", "typeflag": b"5"}, b"")


def _pax(typeflag: bytes, records: dict[str, str]) -> tuple:
    body = b""
    for key, value in records.items():
        rec = f" {key}={value}\n".encode()
        length = len(rec) + len(str(len(rec)))
        length = len(rec) + len(str(length))
        body += str(length).encode() + rec
    return ({"name": b"pax_global_header", "typeflag": typeflag}, body)


_GOOD = _tar(_pax(b"g", {"comment": "a" * 40}), _dir("d"), _file("d/f.txt"))

# (имя случая, tar, ожидаемый фрагмент причины)
_HOSTILE: list[tuple[str, bytes, str]] = [
    (
        "symlink",
        _tar(({"name": b"l", "typeflag": b"2", "linkname": b"/etc"}, b"")),
        "ссылк",
    ),
    (
        "hardlink",
        _tar(({"name": b"l", "typeflag": b"1", "linkname": b"f"}, b"")),
        "ссылк",
    ),
    ("absolute", _tar(_file("/etc/x")), "путь"),
    ("dotdot", _tar(_file("a/../../x")), "путь"),
    ("dotdot_dir", _tar(_dir("..")), "путь"),
    ("nul", _tar(_file("a\x00b")), "путь"),
    ("device", _tar(({"name": b"dev", "typeflag": b"3"}, b"")), "тип"),
    ("fifo", _tar(({"name": b"p", "typeflag": b"6"}, b"")), "тип"),
    ("sparse", _tar(({"name": b"s", "typeflag": b"S"}, b"")), "sparse"),
    ("gnu_longname", _tar(({"name": b"l", "typeflag": b"L"}, b"x" * 5)), "тип"),
    ("pax_path", _tar(_pax(b"x", {"path": "../../escape"}), _file("a")), "PAX"),
    ("pax_size", _tar(_pax(b"x", {"size": "1"}), _file("a")), "PAX"),
    ("pax_sparse", _tar(_pax(b"x", {"GNU.sparse.major": "1"}), _file("a")), "PAX"),
    ("pax_global_path", _tar(_pax(b"g", {"path": "x"}), _file("a")), "PAX"),
    ("duplicate", _tar(_file("a", b"1"), _file("a", b"2")), "повтор"),
    ("dup_dir", _tar(_dir("d"), _dir("d")), "повтор"),
    ("file_then_dir", _tar(_file("a"), _file("a/b")), "конфликт"),
    ("dir_then_file", _tar(_dir("a"), _file("a")), "повтор"),
    ("too_big", _tar(_file("big", b"z" * 50000)), "объём"),
    ("too_many", _tar(*[_file(f"f{i}") for i in range(8)]), "записей"),
    ("too_deep", _tar(_file("a/b/c/d/e/f")), "глубин"),
    ("bad_checksum", _tar(({"name": b"a", "checksum_ok": False}, b"")), "контрольн"),
    ("truncated", _tar(_file("a", b"y" * 700), end=False)[:700], "обрезан"),
    (
        "size_binary",
        _tar(({"name": b"a", "size_field": b"\x80" + b"\x00" * 10 + b"\x01"}, b"")),
        "размер",
    ),
    ("not_ustar", _tar(({"name": b"a", "magic": b"ustar  \x00"}, b"")), "формат"),
    ("garbage_after_end", _GOOD + b"junk" * 128, "после конца"),
    ("gzip", __import__("gzip").compress(_GOOD), "формат"),
    ("empty_name", _tar(_file("")), "путь"),
]
_HOSTILE_LIMITS = {
    "snapshot_max_bytes": 40000,
    "snapshot_max_entries": 6,
    "snapshot_max_depth": 4,
}


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


async def test_a_hostile_snapshot_is_refused_before_the_run(
    spool, runner_mod, tmp_path
) -> None:
    """AC-2: враждебный tar отвергается ДО запуска модели, следов вне каталога нет.

    Один случай — одно задание version=2 со своим src.tar. Для каждого: итог
    ``rejected`` с причиной, заглушка модели НЕ запускалась, scratch пуст
    (частичная распаковка убрана), вне spool и scratch ничего не появилось.
    Прежде всего сверен хороший tar: без него «отвергнут всё» было бы зелёным
    и при сломанном принятии.
    """
    ran = tmp_path / "model_ran"
    argv = _fake_cli(
        tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('x')\n"
    )
    cfg = _runner_cfg(
        runner_mod,
        tmp_path,
        spool,
        argv,
        unpacker_trusted_uid=os.getuid(),
        snapshot_unpacker=str(_UNPACK_FILE),
        **_HOSTILE_LIMITS,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")

    def publish(name: str, blob: bytes | None, *, as_symlink: bool = False) -> Path:
        jobdir = _write_job(spool, name, {"version": 2, "timeout_sec": 5})
        if as_symlink:
            (jobdir / "src.tar").symlink_to(outside / "keep.txt")
        elif blob is not None:
            (jobdir / "src.tar").write_bytes(blob)
        return jobdir

    # Хороший tar принимается и модель стартует.
    good = publish("job-" + "0" * 16, _GOOD)
    await runner_mod.run_pending(cfg)
    result = json.loads((good / "result.json").read_text())
    assert result["status"] == "ok" and ran.exists(), result
    ran.unlink()

    before = _tree(tmp_path)
    failures: list[str] = []
    cases = [(n, b, why) for n, b, why in _HOSTILE] + [
        ("src_is_symlink", None, "ссылк")
    ]
    for index, (case, blob, fragment) in enumerate(cases, start=1):
        jobdir = publish(
            "job-" + f"{index:016x}", blob, as_symlink=case == "src_is_symlink"
        )
        await runner_mod.run_pending(cfg)
        result = json.loads((jobdir / "result.json").read_text())
        if result["status"] != "rejected":
            failures.append(f"{case}: принят ({result})")
        elif not result["reason"].startswith("снимок отвергнут"):
            failures.append(f"{case}: причина без названия: {result['reason']}")
        elif fragment not in result["reason"]:
            failures.append(f"{case}: в причине нет «{fragment}»: {result['reason']}")
        if ran.exists():
            failures.append(f"{case}: модель запускалась")
            ran.unlink()
        left = list((tmp_path / "scratch").iterdir())
        if left:
            failures.append(f"{case}: частичная распаковка не убрана: {left}")
        if (jobdir / "src.tar").exists() and case != "src_is_symlink":
            failures.append(f"{case}: src.tar остался в задании")
    assert not failures, "\n".join(failures)
    assert (outside / "keep.txt").read_text() == "keep"
    created = [p for p in _tree(tmp_path) if p not in before]
    assert all(p.startswith("spool/") for p in created), (
        f"что-то создано вне spool: {created}"
    )

    # Тот же распаковщик при direct: враждебный tar не доходит до модели.
    unpacker = _load_unpacker()
    work = tmp_path / "direct-work"
    work.mkdir(mode=0o700)
    bad = tmp_path / "bad.tar"
    bad.write_bytes(_HOSTILE[0][1])
    with pytest.raises(unpacker.SnapshotRefused):
        unpacker.prepare_workdir(
            str(work),
            str(bad),
            unpacker.Limits(max_bytes=40000, max_entries=6, max_depth=4),
            os.getgid(),
        )
    assert list(work.iterdir()) == [], "частичная распаковка direct не убрана"
    # суммарный логический объём — отдельный потолок (tar при этом помещается)
    fat = tmp_path / "fat.tar"
    fat.write_bytes(_tar(_file("big", b"z" * 2000)))
    with pytest.raises(unpacker.SnapshotRefused, match="суммарный объём"):
        unpacker.prepare_workdir(
            str(work),
            str(fat),
            unpacker.Limits(max_bytes=40000, max_total_bytes=1000),
            os.getgid(),
        )
    assert list(work.iterdir()) == []
    # рабочий каталог, доступный другим, не приватен: распаковка отказывает
    os.chmod(work, 0o770)
    with pytest.raises(unpacker.SnapshotRefused, match="не приватен"):
        unpacker.prepare_workdir(
            str(work), str(fat), unpacker.Limits(max_bytes=40000), os.getgid()
        )
    os.chmod(work, 0o700)
    # job.json без src.tar при version=2 и src.tar при version=1: отказ, не запуск.
    lone = _write_job(spool, "job-" + "e" * 16, {"version": 2, "timeout_sec": 5})
    mixed = _write_job(spool, "job-" + "f" * 16, {"version": 1, "timeout_sec": 5})
    (mixed / "src.tar").write_bytes(_GOOD)
    await runner_mod.run_pending(cfg)
    for jobdir in (lone, mixed):
        result = json.loads((jobdir / "result.json").read_text())
        assert result["status"] == "rejected" and "src.tar" in result["reason"], result
    assert not (mixed / "src.tar").exists(), "src.tar отвергнутого задания остался"
    assert not ran.exists()


def _load_old_runner():
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("haiplane_review_runner_v1", str(_OLD_RUNNER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


async def test_the_runner_snapshot_survives_install_cancel_and_old_runner(
    spool, runner_mod, tmp_path, monkeypatch
) -> None:
    """AC-3: установка без hub, старый раннер, отмена и timeout со снимком."""
    import subprocess

    _snapshot_config(monkeypatch)
    repo, sha = _make_repo(tmp_path)
    snapshot = await _archive(repo, sha)
    assert snapshot.data is not None

    # ---- установленный раннер: два файла в каталоге, пакета hub рядом нет.
    install = tmp_path / "install"
    install.mkdir()
    (install / "haiplane-review-runner.py").write_bytes(_RUNNER_FILE.read_bytes())
    (install / "snapshot_unpack.py").write_bytes(_UNPACK_FILE.read_bytes())
    hostile_cwd = tmp_path / "hostile-cwd"
    hostile_cwd.mkdir()
    marker = tmp_path / "hostile_imported"
    (hostile_cwd / "snapshot_unpack.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n"
    )
    (hostile_cwd / "hub").mkdir()
    (hostile_cwd / "hub" / "__init__.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('hub')\n"
    )

    def check(*extra: str, cwd: Path = hostile_cwd, isolated: bool = False):
        return subprocess.run(
            [
                sys.executable,
                *(["-I"] if isolated else []),
                str(install / "haiplane-review-runner.py"),
                "--check-install",
                *extra,
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(hostile_cwd)},
            timeout=60,
        )

    # Без -I: cwd и PYTHONPATH враждебны и стоят на пути поиска модулей, а
    # служба обязана взять распаковщик ТОЛЬКО по своему явному пути.
    done = check("--trusted-uid", str(os.getuid()))
    assert done.returncode == 0, done.stderr + done.stdout
    isolated = check("--trusted-uid", str(os.getuid()), isolated=True)
    assert isolated.returncode == 0, isolated.stderr + isolated.stdout
    info = json.loads(done.stdout)
    assert info["unpacker"] == str(install / "snapshot_unpack.py")
    assert info["hub_imported"] is False, "установленный раннер тянет пакет hub"
    assert not marker.exists(), "подхвачен модуль из cwd/PYTHONPATH"
    if not _ROOT_USER:
        bare = check()
        assert bare.returncode != 0 and "root" in bare.stderr, (
            "распаковщик не root-овый: служба обязана отказаться, а не доверять"
        )
    os.chmod(install / "snapshot_unpack.py", 0o666)
    writable = check("--trusted-uid", str(os.getuid()))
    assert writable.returncode != 0 and "запис" in writable.stderr, writable.stderr
    os.chmod(install / "snapshot_unpack.py", 0o644)

    # ---- старый раннер отклоняет version=2 ДО запуска модели; хаб не молчит.
    ran = tmp_path / "old_model_ran"
    old = _load_old_runner()
    old_cfg = old.Config(
        spool=str(spool),
        argv=_fake_cli(
            tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('x')\n"
        ),
        scratch=str(tmp_path / "scratch"),
        max_timeout=60,
        poll=0.02,
        heartbeat_every=0.05,
    )
    (tmp_path / "scratch").mkdir(exist_ok=True)
    service = asyncio.create_task(old.serve(old_cfg))
    try:
        assert await _until(lambda: (spool / "heartbeat").exists(), 3)
        run = await local_reviewer.run_review("промт", timeout=10, snapshot=snapshot)
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)
    assert run is None and not ran.exists(), "модель запущена старым раннером"
    why = local_reviewer.refusal()
    assert "версия задания не поддерживается" in why and "снимк" in why, why

    # ---- отмена и timeout со снимком: workdir удалён, права восстановлены
    #      без следования ссылкам.
    escape_target = tmp_path / "escape-target"
    escape_target.mkdir()
    (escape_target / "keep.txt").write_text("keep")
    sleeper = (
        "import os, time, pathlib\n"
        "home = os.environ['HOME']\n"
        "os.makedirs(home + '/locked/inner')\n"
        "open(home + '/locked/inner/f', 'w').write('x')\n"
        f"os.symlink({str(escape_target)!r}, home + '/escape')\n"
        "os.chmod(home + '/locked/inner', 0)\n"
        "os.chmod(home + '/locked', 0)\n"
        f"pathlib.Path({str(tmp_path / 'ups')!r}, str(os.getpid())).write_text('x')\n"
        "time.sleep(30)\n"
    )
    cfg = _runner_cfg(
        runner_mod,
        tmp_path,
        spool,
        _fake_cli(tmp_path, sleeper),
        unpacker_trusted_uid=os.getuid(),
        snapshot_unpacker=str(_UNPACK_FILE),
    )
    scratch = tmp_path / "scratch"
    (tmp_path / "ups").mkdir()
    service = asyncio.create_task(runner_mod.serve(cfg))
    try:
        assert await _until(lambda: (spool / "heartbeat").exists(), 3)
        # timeout
        run = await local_reviewer.run_review("промт", timeout=2, snapshot=snapshot)
        assert run is not None and run.timed_out, (run, local_reviewer.refusal())
        assert await _until(lambda: not any(scratch.iterdir()), 5), (
            f"после timeout остался workdir: {list(scratch.iterdir())}"
        )
        # отмена (остановка хаба)
        for old_marker in (tmp_path / "ups").iterdir():
            old_marker.unlink()
        task = asyncio.create_task(
            local_reviewer.run_review("промт", timeout=60, snapshot=snapshot)
        )
        assert await _until(
            lambda: any((tmp_path / "ups").iterdir()) and any(scratch.iterdir()), 8
        ), "модель не стартовала"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _until(lambda: not any(scratch.iterdir()), 8), (
            f"после отмены остался workdir: {list(scratch.iterdir())}"
        )
        assert await _until(lambda: _jobs(spool) == [], 5), "задание и src.tar остались"
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)
    assert (escape_target / "keep.txt").read_text() == "keep", (
        "уборка пошла по ссылке и тронула чужой каталог"
    )


# ------------------------------------------------ места применения снимка


def _runner_unpacker_problem(runner_mod, path: Path, uid: int) -> str:
    return runner_mod.unpacker_problem(str(path), uid)


def test_the_unpacker_is_trusted_only_in_a_root_owned_unwritable_install(
    runner_mod, tmp_path
) -> None:
    """Служба и хаб (direct) доверяют распаковщику только защищённой установке."""
    me = os.getuid()
    install = tmp_path / "install"
    install.mkdir()
    unpacker = install / "snapshot_unpack.py"
    unpacker.write_bytes(_UNPACK_FILE.read_bytes())
    os.chmod(unpacker, 0o644)
    both = (
        lambda path, uid: runner_mod.unpacker_problem(str(path), uid),
        lambda path, uid: review_snapshot.unpacker_problem(str(path), uid),
    )
    for problem in both:
        assert problem(unpacker, me) == "", "своя защищённая установка принимается"
        if not _ROOT_USER:
            assert "root" in problem(unpacker, 0), (
                "хаб — тот же пользователь: файл, принадлежащий ему, не защищён"
            )
        os.chmod(unpacker, 0o664)
        assert "запис" in problem(unpacker, me), "запись группе — отказ"
        os.chmod(unpacker, 0o646)
        assert "запис" in problem(unpacker, me), "запись всем — отказ"
        os.chmod(unpacker, 0o644)
        os.chmod(install, 0o777)
        assert "каталог" in problem(unpacker, me), (
            "каталог над файлом пишем всем — отказ"
        )
        os.chmod(install, 0o755)
        link = install / "link.py"
        link.symlink_to(unpacker)
        assert "не обычный файл" in problem(link, me), "ссылка вместо файла — отказ"
        link.unlink()
        assert "не найден" in problem(install / "нет.py", me)


async def test_the_direct_transport_refuses_an_unprotected_unpacker(
    spool, tmp_path, monkeypatch
) -> None:
    """Direct: нет или не защищён распаковщик — модель не запускается, причина названа."""
    _snapshot_config(monkeypatch)
    repo, sha = _make_repo(tmp_path)
    snapshot = await _archive(repo, sha)
    scratch = tmp_path / "direct-scratch"
    scratch.mkdir()
    os.chmod(scratch, 0o2770)
    import pwd

    sudo = tmp_path / "sudo"
    sudo.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
    sudo.chmod(0o755)
    me = pwd.getpwuid(os.getuid()).pw_name
    ran = tmp_path / "ran"
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "direct")
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", f"{sudo} -n -u {me} {sys.executable}"
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(
            ["-c", f"import pathlib; pathlib.Path({str(ran)!r}).write_text('x')"]
        ),
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(scratch))
    for unpacker, why in (
        ("", "не задан"),
        (str(_UNPACK_FILE), "root"),  # файл репозитория принадлежит не root
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SNAPSHOT_UNPACKER", unpacker)
        monkeypatch.setattr(
            review_snapshot, "TRUSTED_UID", 0 if not _ROOT_USER else 12345
        )
        assert review_snapshot.blocker("direct"), "заказ узнаёт об отказе заранее"
        run = await local_reviewer.run_review("промт", timeout=10, snapshot=snapshot)
        assert run is None and not ran.exists(), "модель запущена без снимка молча"
        assert why in local_reviewer.refusal(), local_reviewer.refusal()
        assert list(scratch.iterdir()) == [], "workdir остался"
    assert review_snapshot.blocker("runner") == "", (
        "при runner хаб распаковщик не грузит"
    )


async def test_the_snapshot_is_written_before_the_drain_lock_and_removed_on_refusal(
    spool, monkeypatch, tmp_path
) -> None:
    """Архив пишется ДО замка выкладки; отказ из-за маркера убирает staged-каталог."""
    import fcntl

    _snapshot_config(monkeypatch)
    repo, sha = _make_repo(tmp_path)
    snapshot = await _archive(repo, sha)
    _beat(spool)
    lock_fd = os.open(spool / ".drain.lock", os.O_RDWR | os.O_CREAT, 0o660)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:

        async def _order():
            run = await local_reviewer.run_review("промт", timeout=5, snapshot=snapshot)
            return run, local_reviewer.refusal()

        task = asyncio.create_task(_order())
        assert await _until(
            lambda: any((j / "src.tar").exists() for j in _jobs(spool)), 3
        ), "src.tar не появился, пока замок занят: архив пишется под замком"
        staged = _jobs(spool)[0]
        assert not (staged / "job.json").exists(), "job.json опубликован мимо замка"
        assert not (staged / "prompt.txt").exists(), (
            "под замком пишутся только короткие файлы"
        )
        _drain_marker(spool)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    run, why = await asyncio.wait_for(task, 5)
    assert run is None and local_reviewer.DRAIN_REASON in why, why
    assert _jobs(spool) == [], "staged-каталог со снимком остался после отказа"


def test_the_spool_write_survives_a_short_write(tmp_path, monkeypatch) -> None:
    """Снимок — десятки мегабайт: одна ``os.write`` может записать не всё."""
    real = os.write
    chunks: list[int] = []

    def _short(fd: int, data) -> int:
        part = bytes(data)[:1000]
        chunks.append(len(part))
        return real(fd, part)

    monkeypatch.setattr(local_reviewer.os, "write", _short)
    payload = os.urandom(5000)
    local_reviewer._write_spool_file(str(tmp_path), "src.tar", payload)
    assert (tmp_path / "src.tar").read_bytes() == payload
    assert len(chunks) == 5


def test_the_snapshot_modes_keep_the_reviewer_uid_out_of_write(tmp_path) -> None:
    """Права снимка: чтение группе, записи и подмены src нет ни у кого (биты режима).

    Это проверка РЕЖИМОВ и группы. Реальный второй uid в тесте без root недоступен
    (его проверяет следующий тест при root и ручная проба владельца).
    """
    unpacker = _load_unpacker()
    work = tmp_path / "w"
    work.mkdir(mode=0o700)
    tar = tmp_path / "s.tar"
    tar.write_bytes(_GOOD)
    gid = os.getgid()
    unpacker.prepare_workdir(str(work), str(tar), unpacker.Limits(), gid)
    assert sorted(p.name for p in work.iterdir()) == ["home", "src"], (
        "в рабочем каталоге остались лишние файлы (приватная копия архива?)"
    )
    assert stat.S_IMODE(work.stat().st_mode) == 0o550, "родитель src закрыт на запись"
    for path in (work / "src", work / "src" / "d"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o550, path
    assert stat.S_IMODE((work / "src" / "d" / "f.txt").stat().st_mode) == 0o440
    assert stat.S_IMODE((work / "home").stat().st_mode) == 0o770
    for path in [work, *work.rglob("*")]:
        info = path.lstat()
        assert info.st_gid == gid, f"группа задана явно: {path}"
        assert not stat.S_ISLNK(info.st_mode)
        assert not info.st_mode & 0o007, f"права «всем» на {path}"
        if path != work / "home":
            assert not info.st_mode & 0o020, f"запись группе на {path}"
    assert unpacker.remove_tree(str(work)) is True and not work.exists()


@pytest.mark.skipif(
    not _ROOT_USER,
    reason="нужен root, чтобы стать вторым uid; без него перенесено в ручную "
    "пробу владельца (deploy/LOCAL-REVIEW.md, «Проба снимка»)",
)
def test_a_second_uid_in_the_group_reads_but_cannot_write_or_replace_src(
    tmp_path,
) -> None:
    import subprocess

    unpacker = _load_unpacker()
    work = tmp_path / "w"
    work.mkdir(mode=0o700)
    tar = tmp_path / "s.tar"
    tar.write_bytes(_GOOD)
    unpacker.prepare_workdir(str(work), str(tar), unpacker.Limits(), 12345)
    script = (
        "import os, sys\n"
        f"w = {str(work)!r}\n"
        "print(open(w + '/src/d/f.txt').read())\n"
        "for fn in (lambda: open(w + '/src/d/f.txt', 'w'), "
        "lambda: os.rename(w + '/src', w + '/src2'), lambda: os.mkdir(w + '/evil')):\n"
        "    try:\n        fn(); print('ALLOWED')\n    except OSError:\n        print('DENIED')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        preexec_fn=lambda: (os.setgroups([12345]), os.setgid(12345), os.setuid(65534)),
    )
    assert done.stdout.split() == ["x", "DENIED", "DENIED", "DENIED"], done


def _run_wrapper_template(tmp_path: Path, *, with_src: bool, conf: dict[str, str]):
    """Запуск шаблона враппера с подменённым podman: argv, который он получил."""
    import subprocess
    import uuid

    confdir = tmp_path / "conf"
    confdir.mkdir(parents=True, exist_ok=True)
    (confdir / "image").write_text("localhost/img:1\n")
    (confdir / "model.env").write_text("K=v\n")
    for name, value in conf.items():
        (confdir / name).write_text(value)
    stub = tmp_path / "podman"
    stub.write_text(
        "#!/usr/bin/env python3\nimport os, shlex, sys\n"
        "open(os.environ['ARGV_LOG'], 'a').write(shlex.join(sys.argv[1:]) + '\\n')\n"
    )
    stub.chmod(0o755)
    script = tmp_path / "haiplane-review-run"
    script.write_text(_WRAPPER.read_text().replace("/usr/bin/podman", str(stub)))
    script.chmod(0o755)
    run_dir = tmp_path / f"run-{uuid.uuid4().hex[:6]}"
    run_dir.mkdir()
    if with_src:
        (run_dir / "src").mkdir()
    log = tmp_path / f"argv-{uuid.uuid4().hex}.log"
    done = subprocess.run(
        [str(script), "cli", "--print"],
        cwd=run_dir,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "ARGV_LOG": str(log), "HAIPLANE_REVIEW_CONF": str(confdir)},
    )
    return done, shlex.split(log.read_text()) if log.exists() else [], run_dir


def test_the_wrapper_template_matches_the_doc_skeleton_byte_for_byte() -> None:
    """Шаблон враппера в репозитории — тот же скрипт, что скелет в документе."""
    import textwrap

    blocks = [
        b
        for b in re.findall(r"```sh\n(.*?)```", _DOC.read_text(), re.S)
        if "podman run" in b
    ]
    assert len(blocks) == 1
    assert _WRAPPER.read_text() == textwrap.dedent(blocks[0]), (
        "шаблон разошёлся со скелетом: правьте скелет и перегенерируйте файл"
    )
    assert os.access(_WRAPPER, os.X_OK), "шаблон враппера не исполняемый"


def test_the_wrapper_mounts_the_snapshot_read_only_only_when_it_exists(
    tmp_path,
) -> None:
    """Со src — ``-v …/src:/work/src:ro --workdir /work``; без src — прежний запуск."""
    plain, argv, _ = _run_wrapper_template(tmp_path / "a", with_src=False, conf={})
    assert plain.returncode == 0, plain.stderr
    assert (
        "-v" not in argv
        and "--workdir" not in argv
        and "keep-groups" not in " ".join(argv)
    )
    image = argv.index("localhost/img:1")
    assert argv[image + 1 :] == ["cli", "--print"], "argv CLI доехал после образа"

    with_src, argv, run_dir = _run_wrapper_template(
        tmp_path / "b", with_src=True, conf={}
    )
    assert with_src.returncode == 0, with_src.stderr
    mount = argv[argv.index("-v") + 1]
    assert mount == f"{run_dir}/src:/work/src:ro", mount
    assert argv[argv.index("--workdir") + 1] == "/work"
    assert argv[argv.index("--group-add") + 1] == "keep-groups"
    assert argv.index("--workdir") < argv.index("localhost/img:1"), (
        "флаги монтирования обязаны стоять ДО образа"
    )
    assert argv[argv.index("localhost/img:1") + 1 :] == ["cli", "--print"]

    selinux, argv, run_dir = _run_wrapper_template(
        tmp_path / "c", with_src=True, conf={"src-mount": "ro,z\n"}
    )
    assert argv[argv.index("-v") + 1] == f"{run_dir}/src:/work/src:ro,z"

    # путь с пробелом смонтировать нельзя: отказ, а не молчаливый запуск без снимка
    spaced = tmp_path / "d" / "with space"
    spaced.mkdir(parents=True)
    done, argv, _ = _run_wrapper_template(spaced, with_src=True, conf={})
    assert done.returncode == 64 and argv == [], (done.returncode, argv)


def test_the_runner_and_the_unpacker_import_only_the_standard_library() -> None:
    """Установленная служба — два файла без пакета hub (и без чужих зависимостей)."""
    import ast

    for path in (_RUNNER_FILE, _UNPACK_FILE):
        tree = ast.parse(path.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imported.add(node.module.split(".")[0])
        outside = sorted(imported - set(sys.stdlib_module_names))
        assert outside == [], (
            f"{path.name} импортирует не из стандартной библиотеки: {outside}"
        )
        assert "hub" not in imported, f"{path.name} импортирует пакет hub"


def test_the_release_artifacts_example_ships_the_unpacker_beside_the_runner() -> None:
    """snapshot_unpack.py — точная копия под тем же root-овым каталогом, что и служба."""
    text = (_ROOT / "deploy/CD.md").read_text()
    block = text[text.index('{"gate_policy": {"release_artifacts"') :]
    block = block[: block.index("]}}") + 3]
    pairs = json.loads(block)["gate_policy"]["release_artifacts"]
    by_repo = {p["repo_path"]: p for p in pairs}
    runner = by_repo["deploy/review-runner/haiplane-review-runner.py"]
    unpack = by_repo["deploy/review-runner/snapshot_unpack.py"]
    assert (_ROOT / unpack["repo_path"]).is_file()
    assert os.path.dirname(unpack["server_path"]) == os.path.dirname(
        runner["server_path"]
    ), "распаковщик ставится рядом со службой: ровно туда, где служба его ищет"
    assert "deploy/review-runner/haiplane-review-run" not in by_repo, (
        "обёртка — шаблон, а не точная копия: сверять её нельзя"
    )


def test_the_doc_fixes_the_snapshot_rollout_order_and_the_manual_probe() -> None:
    """Рецепт: распаковщик → служба → обёртка → настройка хаба; проба названа ручной."""
    doc = _DOC.read_text()
    section = doc[doc.index("### Выкат снимка") :]
    section = section[: section.index("### Проба снимка")]
    order = [
        section.index("snapshot_unpack.py"),
        section.index("systemctl restart haiplane-review-runner"),
        section.index("/usr/local/bin/haiplane-review-run"),
        section.index("HAIPLANE_LOCAL_REVIEW_SNAPSHOT=1"),
    ]
    assert order == sorted(order), "порядок выката в рецепте нарушен"
    assert "не доказательство" in section and "path_notice" in section
    probe = doc[doc.index("### Проба снимка") :]
    assert "НАСТОЯЩИЙ второй uid" in probe and "не проверено" in probe
    assert "rootless podman" in probe.lower()


def test_the_runner_accepts_only_the_known_job_versions(runner_mod) -> None:
    for version in (0, 3, "2", None, True, 2.5):
        job = json.dumps({"version": version, "timeout_sec": 5}).encode()
        with pytest.raises(runner_mod.JobRejected, match="версия задания"):
            runner_mod.parse_job_ex(job, 60)
    for version in (1, 2):
        job = json.dumps({"version": version, "timeout_sec": 5}).encode()
        assert runner_mod.parse_job_ex(job, 60) == (5, version)


def test_the_snapshot_group_is_set_explicitly_not_inherited(tmp_path) -> None:
    """Группа каталога и файлов задаётся ЯВНО: setgid предка не должен быть единственным."""
    others = [g for g in os.getgroups() if g != os.getgid()]
    if not others:
        pytest.skip(
            "у пользователя нет второй группы: проверка группы остаётся ручной пробе"
        )
    unpacker = _load_unpacker()
    work = tmp_path / "w"
    work.mkdir(mode=0o700)
    tar = tmp_path / "s.tar"
    tar.write_bytes(_GOOD)
    unpacker.prepare_workdir(str(work), str(tar), unpacker.Limits(), others[0])
    for path in [work, *work.rglob("*")]:
        assert path.lstat().st_gid == others[0], f"группа не задана: {path}"
    assert unpacker.remove_tree(str(work))


# ------------------------------------------- круг Codex: находки P1-1…P2-9 (#1599)


def _snapshot_jobdir(spool: Path, name: str, blob: bytes | None, **job) -> Path:
    jobdir = _write_job(spool, name, {"version": 2, "timeout_sec": 5, **job})
    if blob is not None:
        (jobdir / "src.tar").write_bytes(blob)
    return jobdir


async def test_a_scratch_that_lets_the_reviewer_rename_the_workdir_gets_no_snapshot(
    spool, runner_mod, tmp_path, monkeypatch
) -> None:
    """P1-1: без sticky-бита или с владельцем-ревьюером снимок не выдаётся.

    Режим 0550 рабочего каталога не запрещает переименовать ЕГО САМ через
    родителя; запрещает sticky-бит на родителе, владелец которого — не ревьюер.
    Реальный mv от второго uid проверяет ручная проба владельца.
    """
    ran = tmp_path / "model_ran"
    argv = _fake_cli(
        tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('x')\n"
    )

    def cfg_for(**over):
        return _runner_cfg(
            runner_mod,
            tmp_path,
            spool,
            argv,
            unpacker_trusted_uid=os.getuid(),
            snapshot_unpacker=str(_UNPACK_FILE),
            **over,
        )

    scratch = tmp_path / "scratch"
    cases = [
        ("no_sticky", 0o2770, "nobody", "sticky"),
        (
            "reviewer_owns",
            0o3770,
            os.environ.get("USER") or __import__("pwd").getpwuid(os.getuid()).pw_name,
            "самому ревьюеру",
        ),
        ("unknown_reviewer", 0o3770, "no-such-user-1599", "не разрешается"),
    ]
    for index, (case, mode, reviewer, fragment) in enumerate(cases):
        cfg = cfg_for(reviewer_user=reviewer)
        os.chmod(scratch, mode)
        jobdir = _snapshot_jobdir(spool, f"job-{index:016x}", _GOOD)
        await runner_mod.run_pending(cfg)
        result = json.loads((jobdir / "result.json").read_text())
        assert result["status"] == "rejected", (case, result)
        assert fragment in result["reason"], (case, result["reason"])
        assert not ran.exists(), f"{case}: модель запущена"
        assert list(scratch.iterdir()) == [], f"{case}: workdir остался"
    # а с sticky и чужим владельцем снимок принимается
    os.chmod(scratch, 0o3770)
    ok = _snapshot_jobdir(spool, "job-" + "a" * 16, _GOOD)
    await runner_mod.run_pending(cfg_for(reviewer_user="nobody"))
    assert json.loads((ok / "result.json").read_text())["status"] == "ok"

    # то же правило у direct (hub): без sticky распаковка отказывает
    unpacker = _load_unpacker()
    work_parent = tmp_path / "wp"
    work_parent.mkdir()
    work = work_parent / "w"
    work.mkdir(mode=0o700)
    tar = tmp_path / "s.tar"
    tar.write_bytes(_GOOD)
    os.chmod(work_parent, 0o2770)
    with pytest.raises(unpacker.SnapshotRefused, match="sticky"):
        unpacker.prepare_workdir(
            str(work), str(tar), unpacker.Limits(), os.getgid(), os.getuid() + 1
        )
    assert review_snapshot.lay_out_direct(str(work), b"x", str(work_parent), None)


def test_the_runner_unit_runs_python_in_isolated_mode() -> None:
    """P1-2: ExecStart с -I: user site, PYTHONPATH и cwd не участвуют в импортах."""
    unit = (_ROOT / "deploy/review-runner/haiplane-review-runner.service").read_text()
    exec_lines = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
    assert len(exec_lines) == 1
    words = exec_lines[0].split()
    assert words[0].endswith("python3") and words[1] == "-I", exec_lines[0]
    doc = _DOC.read_text()
    assert "--check-install" in doc and "python3 -I" in doc


def test_the_unpacker_path_is_read_without_following_links_and_executed_as_read(
    runner_mod, tmp_path, monkeypatch
) -> None:
    """P1-3: ссылка в пути — отказ; исполняется прочитанное, а не путь повторно."""
    me = os.getuid()
    real = tmp_path / "real"
    real.mkdir()
    (real / "snapshot_unpack.py").write_bytes(_UNPACK_FILE.read_bytes())
    os.chmod(real / "snapshot_unpack.py", 0o644)
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    for problem in (
        lambda path: runner_mod.unpacker_problem(str(path), me),
        lambda path: review_snapshot.unpacker_problem(str(path), me),
    ):
        assert problem(real / "snapshot_unpack.py") == ""
        assert "ссылк" in problem(alias / "snapshot_unpack.py"), (
            "ссылка-алиас в пути: проверка видела бы только цель, загрузка — алиас"
        )
        assert "абсолютный" in problem(Path("rel/snapshot_unpack.py"))
        assert "абсолютный" in problem(real / ".." / "real" / "snapshot_unpack.py")

    # загружается ровно то, что прочитано при проверке
    bait = tmp_path / "bait.py"
    bait.write_text("MARK = 2\n")
    os.chmod(bait, 0o644)
    monkeypatch.setattr(
        runner_mod, "read_trusted_source", lambda path, uid: (b"MARK = 1\n", "")
    )
    assert runner_mod.load_unpacker(str(bait), me).MARK == 1
    monkeypatch.setattr(config, "LOCAL_REVIEW_SNAPSHOT_UNPACKER", str(bait))
    monkeypatch.setattr(review_snapshot, "TRUSTED_UID", me)
    monkeypatch.setattr(
        review_snapshot, "read_trusted_source", lambda path, uid: (b"MARK = 1\n", "")
    )
    assert review_snapshot.load_unpacker().MARK == 1


async def test_a_fifo_instead_of_the_snapshot_does_not_hang_the_service(
    spool, runner_mod, tmp_path
) -> None:
    """P2-4: FIFO без писателя — отказ за секунды, служба не встаёт."""
    import threading

    ran = tmp_path / "ran"
    cfg = _runner_cfg(
        runner_mod,
        tmp_path,
        spool,
        _fake_cli(
            tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('x')\n"
        ),
        unpacker_trusted_uid=os.getuid(),
        snapshot_unpacker=str(_UNPACK_FILE),
    )
    jobdir = _snapshot_jobdir(spool, "job-" + "b" * 16, None)
    os.mkfifo(jobdir / "src.tar")
    # run_pending синхронно распаковывает: зависание не прервать из event loop,
    # поэтому он идёт в своём демоне-потоке, и тест ждёт его с лимитом.
    runner_thread = threading.Thread(
        target=lambda: asyncio.run(runner_mod.run_pending(cfg)), daemon=True
    )
    runner_thread.start()
    runner_thread.join(15)
    assert not runner_thread.is_alive(), "служба повисла на FIFO вместо src.tar"
    result = json.loads((jobdir / "result.json").read_text())
    assert result["status"] == "rejected" and "обычный файл" in result["reason"], result
    assert not ran.exists()

    unpacker = _load_unpacker()
    work = tmp_path / "w"
    work.mkdir(mode=0o700)
    fifo = tmp_path / "f.tar"
    os.mkfifo(fifo)
    outcome: list[str] = []

    def attempt() -> None:
        try:
            unpacker.prepare_workdir(str(work), str(fifo), unpacker.Limits(), None)
        except unpacker.SnapshotRefused as exc:
            outcome.append(exc.reason)

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive(), "open() на FIFO повис"
    assert outcome and "обычный файл" in outcome[0]


async def test_cancelling_the_hub_during_publication_leaves_no_job(
    spool, monkeypatch, tmp_path
) -> None:
    """P2-5: поток публикации не уходит в обход отмены; job.json не остаётся."""
    import threading

    _snapshot_config(monkeypatch)
    repo, sha = _make_repo(tmp_path)
    snapshot = await _archive(repo, sha)
    _beat(spool)
    gate = threading.Event()
    real = local_reviewer._write_spool_file

    def _slow(jobdir: str, name: str, data: bytes) -> None:
        if name == "prompt.txt":
            gate.wait(10)
        real(jobdir, name, data)

    monkeypatch.setattr(local_reviewer, "_write_spool_file", _slow)
    task = asyncio.create_task(
        local_reviewer.run_review("КОД", timeout=30, snapshot=snapshot)
    )
    assert await _until(lambda: any((j / "src.tar").exists() for j in _jobs(spool)), 3)
    task.cancel()
    asyncio.get_running_loop().call_later(0.3, gate.set)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _jobs(spool) == [] or all(
        not (j / "job.json").exists() or (j / "cancel").exists() for j in _jobs(spool)
    ), "отменённый заказ остался опубликованным и не отозван"
    assert all(not (j / "prompt.txt").exists() for j in _jobs(spool))


async def test_an_early_refusal_of_the_runner_still_removes_the_snapshot_archive(
    spool, runner_mod, tmp_path
) -> None:
    """P2-6: версия, JSON, промт — на каждом раннем отказе src.tar удалён."""
    cfg = _runner_cfg(
        runner_mod,
        tmp_path,
        spool,
        _fake_cli(tmp_path, "pass\n"),
        unpacker_trusted_uid=os.getuid(),
        snapshot_unpacker=str(_UNPACK_FILE),
    )
    bad_version = _snapshot_jobdir(spool, "job-" + "c" * 16, _GOOD)
    (bad_version / "job.json").write_text(json.dumps({"version": 99, "timeout_sec": 5}))
    broken = _snapshot_jobdir(spool, "job-" + "d" * 16, _GOOD)
    (broken / "job.json").write_text("{not json")
    no_prompt = _snapshot_jobdir(spool, "job-" + "e" * 16, _GOOD)
    (no_prompt / "prompt.txt").unlink()
    (no_prompt / "prompt.txt").symlink_to(tmp_path / "elsewhere")
    await runner_mod.run_pending(cfg)
    for name, jobdir in (
        ("version", bad_version),
        ("json", broken),
        ("prompt", no_prompt),
    ):
        result = json.loads((jobdir / "result.json").read_text())
        assert result["status"] in ("rejected", "error"), (name, result)
        assert not (jobdir / "src.tar").exists(), f"{name}: src.tar остался"


async def test_direct_with_a_container_wrapper_names_the_reviewer_visible_path(
    spool, tmp_path, monkeypatch
) -> None:
    """P2-7: direct + контейнерная обёртка: путь в промте — настройка, а не хостовый."""
    import pwd

    _snapshot_config(monkeypatch)
    repo, sha = _make_repo(tmp_path)
    import dataclasses

    snapshot = dataclasses.replace(
        await _archive(repo, sha), placeholder=review_snapshot.PATH_PLACEHOLDER
    )
    scratch = tmp_path / "direct-scratch"
    scratch.mkdir()
    os.chmod(scratch, 0o3770)
    me = pwd.getpwuid(os.getuid()).pw_name
    sudo = tmp_path / "sudo"
    sudo.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
    sudo.chmod(0o755)
    # «Обёртка»: видит то же, что настоящая: снимок по /work/src (здесь — cwd/src).
    wrapper = tmp_path / "haiplane-review-run"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} -c "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(local_reviewer, "_reviewer_uid", lambda: os.getuid() + 1)
    monkeypatch.setattr(review_snapshot, "TRUSTED_UID", os.getuid())
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "direct")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", f"{sudo} -n -u {me} {wrapper}")
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", shlex.join([_READER]))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(scratch))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SNAPSHOT_UNPACKER", str(_UNPACK_FILE))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SNAPSHOT_PATH", "")
    why = review_snapshot.blocker("direct")
    assert "LOCAL_REVIEW_SNAPSHOT_PATH" in why and "/work/src" in why, (
        "контейнерная обёртка без явного пути: хостовый путь был бы ложью"
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SNAPSHOT_PATH", "/work/src")
    assert review_snapshot.blocker("direct") == ""
    run = await local_reviewer.run_review(
        f"Снимок: {review_snapshot.PATH_PLACEHOLDER}\n", timeout=20, snapshot=snapshot
    )
    assert run is not None and run.rc == 0, (run, local_reviewer.refusal())
    seen = _parse_reader(run.output)
    assert seen["path"] == "/work/src", "в промте путь глазами ревьюера, не хостовый"
    assert seen["text"] == "SECRET_SYMBOL = 42\n", "снимок при этом распакован"


async def test_the_prompt_is_untouched_when_snapshot_is_off_and_literals_survive(
    spool, monkeypatch, tmp_path
) -> None:
    """P2-8: плейсхолдер в диффе — данные; выключенная настройка не меняет промт."""
    literal = "дифф: +x = '@@SNAPSHOT_DIR@@'\n"
    _beat(spool)
    writes = _spy_spool_writes(monkeypatch)

    async def one(snapshot):
        async def _service() -> None:
            while True:
                for jobdir in _jobs(spool):
                    if (jobdir / "job.json").exists():
                        (jobdir / "claimed").write_text("")
                        (jobdir / "result.json").write_text(
                            json.dumps({"status": "ok", "rc": 0, "output": ""})
                        )
                await asyncio.sleep(0.01)

        service = asyncio.create_task(_service())
        try:
            writes.clear()
            return await local_reviewer.run_review(
                literal, timeout=10, snapshot=snapshot
            )
        finally:
            service.cancel()

    # настройка выключена, снимка нет: промт байт в байт
    _snapshot_config(monkeypatch, enabled=False)
    await one(None)
    assert dict(writes)["prompt.txt"].decode() == literal
    # включена, снимок заказан со своим маркером: чужой литерал цел, маркер заменён
    _snapshot_config(monkeypatch)
    repo, sha = _make_repo(tmp_path)
    import dataclasses

    snap = dataclasses.replace(
        await _archive(repo, sha), placeholder="@@SNAPSHOT_DIR:abc123@@"
    )
    await one(
        dataclasses.replace(snap, data=None)
    )  # absent: маркер заменяется, литерал цел
    sent = dict(writes)["prompt.txt"].decode()
    assert "'@@SNAPSHOT_DIR@@'" in sent, "буквальный плейсхолдер в диффе испорчен"
