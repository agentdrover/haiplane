#!/usr/bin/env python3
"""Служба-исполнитель локального ревьюера (#1571).

Зачем она есть. Юнит хаба идёт с ProtectSystem=strict и NoNewPrivileges=yes
(аудит 14.08, ослаблять нельзя): хаб не может ни создать каталог прогона вне
своих путей, ни выполнить sudo к пользователю ревьюера. Поэтому запуск вынесен
сюда — в отдельную службу ВНЕ изоляции хаба. Хаб ничего не запускает: он кладёт
задание в spool-каталог и ждёт result.json.

Чем служба защищена. Хаб открыт в интернет, а spool он пишет сам, поэтому всё,
что лежит в задании, считается чужим вводом:

* команду запускает ТОЛЬКО служба, и берёт её из СВОЕГО конфига
  (HAIPLANE_REVIEW_RUNNER_ARGV), а не из задания. Задание — закрытый список
  полей (JOB_FIELDS); любое лишнее поле отвергает задание целиком, ничего не
  запуская;
* конфиг обязан держать форму ``sudo -n -u <не root> /абсолютный/путь``:
  служба запускает врапер под пользователем ревьюера, а не что угодно;
* промт читается один раз и тут же удаляется с диска; дальше он живёт в памяти
  и в stdin процесса (в argv его нет: аргументы видны в ps любому);
* файлы spool открываются с O_NOFOLLOW, имена заданий — по строгому шаблону;
* служба работает от пользователя хаба (у него уже есть право на sudo к
  ревьюеру) и ничего не получает сверх этого. NoNewPrivileges в её юните
  НЕТ намеренно: с ним sudo не работает, и вся причина существования службы
  пропала бы.

Только стандартная библиотека: служба ставится на хост одним файлом.

Протокол (хаб и служба держат одни имена; тест сверяет их):

    <spool>/job-<16 hex>/prompt.txt   промт (0660, удаляется службой сразу)
    <spool>/job-<16 hex>/job.json     {"version": 1, "timeout_sec": N}; пишется
                                      ПОСЛЕДНИМ и атомарно — это и есть «готово»
    <job>/claimed                     служба взяла задание
    <job>/cancel                      хаб просит снять прогон (таймаут)
    <job>/result.json                 итог; пишется атомарно
    <spool>/heartbeat                 служба жива (mtime обновляется)

Снятие: файл cancel, исчезновение job.json (хаб остановлен и отозвал задание)
или исчезновение самого каталога. Служба посылает группе процесса SIGTERM, а
после grace — SIGKILL: SIGKILL от непривилегированного пользователя до sudo
(root) не дойдёт, а SIGTERM sudo пересылает своему потомку.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import stat
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

SPOOL_PROMPT = "prompt.txt"
SPOOL_JOB = "job.json"
SPOOL_CLAIMED = "claimed"
SPOOL_CANCEL = "cancel"
SPOOL_RESULT = "result.json"
SPOOL_HEARTBEAT = "heartbeat"

JOB_VERSION = 1
# Закрытый список: ни команды, ни путей, ни окружения задание нести не может.
JOB_FIELDS = ("version", "timeout_sec")

OUTPUT_CAP = 200_000
PROMPT_CAP = 4 * 1024 * 1024
JOB_CAP = 4096
TIMEOUT_RC = 124

_JOB_NAME = re.compile(r"^job-[0-9a-f]{16}$")
_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "LC_CTYPE")

log = logging.getLogger("haiplane-review-runner")


class ConfigError(Exception):
    """Конфиг службы негоден: служба не стартует, а не запускает «примерно»."""


class JobRejected(Exception):
    """Задание отвергнуто: причина уходит хабу в result.json."""


@dataclass(frozen=True)
class Config:
    spool: str
    argv: tuple[str, ...]
    scratch: str
    max_timeout: int = 1800
    poll: float = 1.0
    heartbeat_every: float = 5.0
    term_grace: float = 10.0
    stale_sec: float = 3600.0
    output_cap: int = field(default=OUTPUT_CAP)


def _argv_problem(argv: list[str]) -> str:
    """Название изъяна формы ``sudo -n -u <user> /abs/wrapper [args]`` или ""."""
    if len(argv) < 5:
        return "команда должна быть вида: sudo -n -u <пользователь> /путь/к/враперу"
    if os.path.basename(argv[0]) != "sudo":
        return "команда должна начинаться с sudo"
    if argv[1] != "-n":
        return "нужен -n: без него sudo может спросить пароль у промта в stdin"
    if argv[2] != "-u" or not argv[3] or argv[3].startswith(("-", "#")):
        return "после -n нужен -u <имя пользователя ревьюера>"
    if argv[3] == "root":
        return "запуск под root не изоляция; нужен пользователь ревьюера"
    if not argv[4].startswith("/"):
        return "путь к враперу обязан быть абсолютным"
    return ""


def load_config(env: Mapping[str, str]) -> Config:
    """Собрать конфиг из окружения; негодный — ConfigError с названной причиной."""

    def need(name: str) -> str:
        value = (env.get(name) or "").strip()
        if not value:
            raise ConfigError(f"{name} не задана")
        return value

    spool = need("HAIPLANE_REVIEW_RUNNER_SPOOL_DIR")
    scratch = need("HAIPLANE_REVIEW_RUNNER_SCRATCH_DIR")
    for name, path in (("SPOOL_DIR", spool), ("SCRATCH_DIR", scratch)):
        if not os.path.isabs(path):
            raise ConfigError(f"HAIPLANE_REVIEW_RUNNER_{name} должен быть абсолютным")
    try:
        argv = shlex.split((env.get("HAIPLANE_REVIEW_RUNNER_ARGV") or "").strip())
    except ValueError as exc:
        raise ConfigError(f"HAIPLANE_REVIEW_RUNNER_ARGV не разобран: {exc}") from exc
    problem = _argv_problem(argv)
    if problem:
        raise ConfigError(f"HAIPLANE_REVIEW_RUNNER_ARGV: {problem}")
    raw = (env.get("HAIPLANE_REVIEW_RUNNER_MAX_TIMEOUT_SEC") or "").strip()
    try:
        cap = int(raw) if raw else 1800
    except ValueError as exc:
        raise ConfigError("HAIPLANE_REVIEW_RUNNER_MAX_TIMEOUT_SEC не число") from exc
    if cap <= 0:
        raise ConfigError("HAIPLANE_REVIEW_RUNNER_MAX_TIMEOUT_SEC должен быть > 0")
    return Config(spool=spool, argv=tuple(argv), scratch=scratch, max_timeout=cap)


def parse_job(raw: bytes, max_timeout: int) -> int:
    """Срок прогона из задания, или JobRejected с причиной. Команды не читает."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise JobRejected("job.json не разобран как JSON") from exc
    if not isinstance(data, dict):
        raise JobRejected("job.json должен быть объектом")
    extra = sorted(str(key) for key in set(data) - set(JOB_FIELDS))
    if extra:
        raise JobRejected(
            "в задании неизвестные поля: "
            + ", ".join(extra)[:200]
            + "; задание несёт только "
            + ", ".join(JOB_FIELDS)
            + ", а команду службы задаёт её конфиг"
        )
    if data.get("version") != JOB_VERSION:
        raise JobRejected(f"версия задания не поддерживается (нужна {JOB_VERSION})")
    timeout = data.get("timeout_sec")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise JobRejected("timeout_sec должен быть целым числом больше нуля")
    return min(timeout, max_timeout)


class _TooBig(Exception):
    pass


def _read_nofollow(path: str, cap: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("не обычный файл")
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise _TooBig(f"файл больше {cap} байт")
    return data


def _write_atomic(directory: str, name: str, data: bytes) -> None:
    tmp = os.path.join(directory, name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o660)
    try:
        os.fchmod(fd, 0o660)  # nosec B103 - группе хаба и службы, не миру
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, os.path.join(directory, name))


def _unlink(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


def _claim(jobdir: str) -> bool:
    try:
        fd = os.open(
            os.path.join(jobdir, SPOOL_CLAIMED),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o660,
        )
    except OSError:
        return False
    os.close(fd)
    return True


def _withdrawn(jobdir: str) -> bool:
    """Хаб отозвал задание: удалил job.json или весь каталог."""
    return not os.path.exists(os.path.join(jobdir, SPOOL_JOB))


def _cancelled(jobdir: str) -> bool:
    return _withdrawn(jobdir) or os.path.exists(os.path.join(jobdir, SPOOL_CANCEL))


def _clean_env(workdir: str) -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = workdir
    env["TMPDIR"] = workdir
    return env


def _outcome(status: str, **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": status,
        "rc": 0,
        "output": "",
        "dropped": 0,
        "timed_out": False,
        "duration_ms": 0,
        "reason": "",
    }
    base.update(over)
    return base


class _Sink:
    """Хвост вывода с потолком; остальное сливается и считается."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.kept: list[bytes] = []
        self.size = 0
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        room = self.cap - self.size
        if room > 0:
            self.kept.append(chunk[:room])
            self.size += min(room, len(chunk))
        self.dropped += max(0, len(chunk) - max(room, 0))

    def text(self) -> str:
        return b"".join(self.kept).decode(errors="replace")


async def _pump(proc: asyncio.subprocess.Process, prompt: bytes, sink: _Sink) -> None:
    async def feed() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                assert proc.stdin is not None
                proc.stdin.close()

    feeding = asyncio.create_task(feed())
    try:
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(65536):
            sink.add(chunk)
        await proc.wait()
    finally:
        feeding.cancel()


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.send_signal(sig)


async def _terminate(proc: asyncio.subprocess.Process, grace: float) -> None:
    """SIGTERM группе (sudo перешлёт его потомку), после grace — SIGKILL."""
    if proc.returncode is None:
        _signal_group(proc, signal.SIGTERM)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), grace)
    _signal_group(proc, signal.SIGKILL)
    with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError, OSError):
        await asyncio.wait_for(proc.wait(), 5)


async def _watch(
    cfg: Config, jobdir: str, pump: asyncio.Task[None], timeout: int
) -> str:
    """Ждать конца прогона: ``done``, ``timeout`` или ``cancelled``."""
    end = time.monotonic() + timeout
    while True:
        done, _ = await asyncio.wait({pump}, timeout=cfg.poll)
        if done:
            return "done"
        if _cancelled(jobdir):
            return "cancelled"
        if time.monotonic() >= end:
            return "timeout"


async def _run(
    cfg: Config, jobdir: str, prompt: bytes, timeout: int
) -> tuple[dict[str, object], bool]:
    """Прогнать команду службы. Второе значение — «хаб отозвал, убрать за ним»."""
    started = time.monotonic()
    workdir = tempfile.mkdtemp(prefix="haiplane-review-", dir=cfg.scratch)
    proc: asyncio.subprocess.Process | None = None
    try:
        # Работает в каталоге чужой пользователь (ревьюер): доступ группе.
        os.chmod(workdir, 0o770)  # nosec B103 - группе, не миру
        proc = await asyncio.create_subprocess_exec(
            *cfg.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=workdir,
            env=_clean_env(workdir),
            start_new_session=True,
        )
        sink = _Sink(cfg.output_cap)
        pump = asyncio.create_task(_pump(proc, prompt, sink))
        try:
            verdict = await _watch(cfg, jobdir, pump, timeout)
        except asyncio.CancelledError:
            await _terminate(proc, min(cfg.term_grace, 2.0))
            raise
        if verdict != "done":
            await _terminate(proc, cfg.term_grace)
            await asyncio.wait({pump}, timeout=2)
            pump.cancel()
        elapsed = int((time.monotonic() - started) * 1000)
        if verdict == "timeout":
            return _outcome(
                "ok", rc=TIMEOUT_RC, timed_out=True, duration_ms=elapsed
            ), False
        done = verdict == "done"
        return (
            _outcome(
                "ok",
                rc=(proc.returncode or 0) if done else TIMEOUT_RC,
                output=sink.text() if done else "",
                dropped=sink.dropped if done else 0,
                duration_ms=elapsed,
                reason="" if done else "прогон снят по просьбе хаба",
            ),
            verdict == "cancelled" and _withdrawn(jobdir),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _execute(cfg: Config, jobdir: str) -> tuple[dict[str, object], bool]:
    prompt_path = os.path.join(jobdir, SPOOL_PROMPT)
    try:
        timeout = parse_job(
            _read_nofollow(os.path.join(jobdir, SPOOL_JOB), JOB_CAP), cfg.max_timeout
        )
        prompt = _read_nofollow(prompt_path, PROMPT_CAP)
    except JobRejected as exc:
        return _outcome("rejected", reason=str(exc)), False
    except (OSError, _TooBig) as exc:
        return _outcome(
            "error", reason=f"задание не прочитано: {exc}"[:300]
        ), _withdrawn(jobdir)
    finally:
        # Промт с одноразовым кодом живёт на диске только до чтения.
        _unlink(prompt_path)
    try:
        return await _run(cfg, jobdir, prompt, timeout)
    except OSError as exc:
        return _outcome(
            "error", reason=f"команда службы не запустилась: {exc}"[:300]
        ), False


async def process_job(cfg: Config, name: str) -> None:
    """Взять задание, прогнать, записать результат, убрать за собой."""
    jobdir = os.path.join(cfg.spool, name)
    if not _claim(jobdir):
        return
    try:
        outcome, abandoned = await _execute(cfg, jobdir)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - одно задание не роняет службу
        log.exception("job %s failed", name)
        outcome, abandoned = (
            _outcome("error", reason=f"{type(exc).__name__}: {exc}"[:300]),
            False,
        )
    with contextlib.suppress(OSError):
        _write_atomic(jobdir, SPOOL_RESULT, json.dumps(outcome).encode())
    if abandoned or not os.path.isdir(jobdir):
        shutil.rmtree(jobdir, ignore_errors=True)


def _job_dirs(spool: str) -> list[os.DirEntry[str]]:
    with os.scandir(spool) as entries:
        return [
            e
            for e in entries
            if _JOB_NAME.match(e.name) and e.is_dir(follow_symlinks=False)
        ]


def _pending(cfg: Config) -> list[str]:
    ready: list[tuple[float, str]] = []
    for entry in _job_dirs(cfg.spool):
        job = os.path.join(entry.path, SPOOL_JOB)
        if os.path.exists(os.path.join(entry.path, SPOOL_CLAIMED)):
            continue
        with contextlib.suppress(OSError):
            ready.append((os.lstat(job).st_mtime, entry.name))
    return [name for _, name in sorted(ready)]


async def run_pending(cfg: Config) -> int:
    """Обработать все ожидающие задания, по одному. Сколько обработано."""
    handled = 0
    for name in _pending(cfg):
        await process_job(cfg, name)
        handled += 1
    return handled


def _sweep(cfg: Config) -> None:
    """Убрать то, за чем некому прийти: хаб упал, не забрав результат."""
    now = time.time()
    for entry in _job_dirs(cfg.spool):
        with contextlib.suppress(OSError):
            if now - entry.stat(follow_symlinks=False).st_mtime > (
                cfg.stale_sec + cfg.max_timeout
            ):
                shutil.rmtree(entry.path, ignore_errors=True)


async def _heartbeat(cfg: Config) -> None:
    while True:
        payload = json.dumps({"pid": os.getpid(), "time": time.time()}).encode()
        try:
            _write_atomic(cfg.spool, SPOOL_HEARTBEAT, payload)
        except OSError as exc:
            log.warning("heartbeat not written: %s", exc)
        await asyncio.sleep(cfg.heartbeat_every)


async def serve(cfg: Config) -> None:
    """Главный цикл: heartbeat, задания, уборка. Возвращается только отменой."""
    beat = asyncio.create_task(_heartbeat(cfg))
    try:
        while True:
            try:
                await run_pending(cfg)
                _sweep(cfg)
            except OSError as exc:
                log.warning("spool not readable: %s", exc)
            await asyncio.sleep(cfg.poll)
    finally:
        beat.cancel()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        print(f"haiplane-review-runner: {exc}", file=sys.stderr)
        return 2

    async def _main() -> None:
        task = asyncio.current_task()
        assert task is not None
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
        await serve(cfg)

    with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
        asyncio.run(_main())
    return 0


if __name__ == "__main__":
    sys.exit(main())
