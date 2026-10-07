"""Запуск внешних процессов для git и форжа — один слой на двоих (#1113).

Вынесено из ``git_ops`` не ради красоты, а потому что иначе не разъехаться:
адаптер форжа зовёт ``gh`` тем же способом, каким git_ops зовёт ``git``, и
если оставить запуск в git_ops, то forge импортирует git_ops, а git_ops —
forge. Цикл. Здесь лежит ровно то, что нужно обоим, и ничего больше: своя
сессия процесса, таймаут, который убивает потомка, и окружение.

Ни одна деталь поведения тут не меняется — код перенесён как есть.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Collection
from pathlib import Path
from typing import Any

from hub.config import WORKSPACE_REPO_LINK
from hub.process_kill import kill_process_group

log = logging.getLogger(__name__)

# Exit code for a killed-on-timeout command: the shell convention, and distinct
# from any rc git itself returns, so a caller can tell a timeout from a refusal.
TIMEOUT_RC = 124


def repo_root() -> str:
    p = WORKSPACE_REPO_LINK
    if p.is_symlink():
        p = p.resolve()
    return str(p)


# Чем подписывается коммит, который делает ХАБ, а не человек (#1192). Мерж
# доставки, авто-коммит и сквош создают коммиты, и git отказывается их
# создавать, пока не знает, кто автор. На боевом хосте identity не задана
# нигде, а вывести её git не может: у vm-5c8197 нет домена, и кандидат
# <служебный-пользователь>@vm-5c8197.(none) отвергается. Итог — доставка на
# GitVerse встала целиком: мержу было нечем подписаться.
#
# Значения те же, что уже коммитят workflow-шаблоны (#476): личность хаба
# одна на репозиторий, две разных в git log читались бы как два автора.
HUB_GIT_NAME = "Haiplane Hub"
HUB_GIT_EMAIL = "hub@haiplane.local"


def git_env() -> dict[str, str]:
    """Build env dict with SSH key for GitHub push."""
    env = os.environ.copy()
    ssh_key = Path.home() / ".ssh" / "id_ed25519"
    if ssh_key.exists():
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o StrictHostKeyChecking=accept-new"
    # #377: anonymous https against a private repo must fail fast, not hang
    # waiting for credentials on a headless server.
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Identity едет ЗДЕСЬ, в окружении процесса, а не пишется в конфиг машины
    # (#1192): хаб разворачивают и в контейнере, и на чужом сервере, и правка
    # ~/.gitconfig чинит ровно одну машину до первого переезда. setdefault, а
    # не присвоение: развёртывание, объявившее свою личность через окружение,
    # остаётся при ней.
    for key, value in (
        ("GIT_AUTHOR_NAME", HUB_GIT_NAME),
        ("GIT_AUTHOR_EMAIL", HUB_GIT_EMAIL),
        ("GIT_COMMITTER_NAME", HUB_GIT_NAME),
        ("GIT_COMMITTER_EMAIL", HUB_GIT_EMAIL),
    ):
        env.setdefault(key, value)
    return env


async def run(
    *cmd: str,
    cwd: str | None = None,
    timeout: int = 60,
    check: bool = True,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=git_env(),
        # Required by kill_process_group: without its own session the child
        # shares the hub's process group, and the group kill below would refuse
        # to fire (killing our own group would take the hub down with it).
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        # #363 I5. This used to propagate. The poller wraps its entire tick in
        # one try/except, so a single hung git call skipped every remaining
        # stage of that tick — review, ci_check, stale sweeps, claim expiry —
        # and did so again every tick until the cause went away. Worse, the
        # child survived: asyncio cancels the read, not the process.
        await kill_process_group(proc)
        detail = f"timed out after {timeout}s: {' '.join(cmd[:4])}"
        log.error("_run: %s", detail)
        return TIMEOUT_RC, "", detail
    except asyncio.CancelledError:
        # #1603: отмена (бюджет снимка, обрыв запроса) обрывает чтение, а не
        # процесс — тот же приём, что у таймаута: убить группу целиком и
        # пробросить отмену дальше, иначе git переживает вызвавшую его задачу.
        await kill_process_group(proc)
        raise
    rc = proc.returncode or 0
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    if check and rc != 0:
        log.warning("%s failed (rc=%d): %s", " ".join(cmd[:4]), rc, err)
    return rc, out, err


async def run_bytes(
    *cmd: str,
    cwd: str | None = None,
    timeout: int = 60,
    max_bytes: int = 32 * 1024 * 1024,
) -> tuple[int, bytes, str]:
    """Как ``run``, но stdout остаётся СЫРЫМИ БАЙТАМИ (#1591).

    ``run`` декодирует с ``errors="replace"`` и делает ``strip()``: для хэша
    файла это двойная порча — не-UTF-8 байты становятся U+FFFD, а завершающий
    перевод строки пропадает. Вывод длиннее ``max_bytes`` — отказ (rc=-2).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=git_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        await kill_process_group(proc)
        return TIMEOUT_RC, b"", f"timed out after {timeout}s: {' '.join(cmd[:4])}"
    if len(stdout) > max_bytes:
        return -2, b"", f"output longer than {max_bytes} bytes"
    return proc.returncode or 0, stdout, stderr.decode(errors="replace").strip()


async def _drain_capped(
    stream: asyncio.StreamReader | None, cap: int
) -> tuple[bytes, bool]:
    """Читать поток порциями; ``(байты, превышен ли потолок)``.

    Копится не больше ``cap`` байт: превышение обнаруживается, как только
    пришла порция, не помещающаяся в остаток, и чтение на этом заканчивается.
    """
    if stream is None:
        return b"", False
    kept = bytearray()
    while chunk := await stream.read(65536):
        if len(kept) + len(chunk) > cap:
            return b"", True
        kept += chunk
    return bytes(kept), False


async def _discard(stream: asyncio.StreamReader | None) -> None:
    while stream is not None and await stream.read(65536):
        pass


async def _stop_reading(
    proc: asyncio.subprocess.Process, readers: Collection[asyncio.Future[Any]]
) -> None:
    """Убить процесс, не оставив его трубы без читателя.

    Транспорт asyncio сообщает о выходе процесса (``wait()``) только когда
    ОБЕ его трубы дочитаны до EOF, а труба, на которой читатель встал на
    потолке, приостановлена и EOF не увидит: ``kill_process_group`` без
    читателя повисал бы навсегда (замечено при написании: зависало через
    раз). Поэтому порции после потолка читаются и выбрасываются, пока
    процесс не умрёт.
    """
    for task in readers:
        task.cancel()
    await asyncio.gather(*readers, return_exceptions=True)
    sinks = [asyncio.ensure_future(_discard(s)) for s in (proc.stdout, proc.stderr)]
    try:
        await kill_process_group(proc)
    finally:
        for sink in sinks:
            sink.cancel()
        await asyncio.gather(*sinks, return_exceptions=True)


async def run_capped(
    *cmd: str,
    cwd: str | None = None,
    timeout: int = 60,
    max_bytes: int,
    max_stderr: int = 64 * 1024,
) -> tuple[int, bytes, str]:
    """Как ``run_bytes``, но потолок держится НА ЧТЕНИИ, а не после него (#1599).

    ``run_bytes`` собирает весь вывод ``communicate()`` и мерит его потом: архив
    репозитория, который больше памяти, был бы прочитан целиком прежде отказа.
    Здесь stdout и stderr читаются порциями по 64 КБ одновременно; как только
    один из потоков превысил свой потолок, процесс убивается вместе с группой
    и дожидается (``kill_process_group`` ждёт ``proc.wait()``), а вызывающий
    получает отказ без вывода.

    Коды: ``-2`` — stdout длиннее ``max_bytes``, ``-3`` — stderr длиннее
    ``max_stderr``, ``TIMEOUT_RC`` — не уложился в срок. Вывод — сырые байты:
    ни decode, ни strip. Отмена (CancelledError) снимает процесс сразу и
    передаётся дальше: ребёнок, оставшийся без читателя, встал бы на полной
    трубе навсегда.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=git_env(),
        start_new_session=True,
    )
    out_task = asyncio.ensure_future(_drain_capped(proc.stdout, max_bytes))
    err_task = asyncio.ensure_future(_drain_capped(proc.stderr, max_stderr))
    readers = {out_task, err_task}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        waiting = set(readers)
        while waiting:
            done, waiting = await asyncio.wait(
                waiting,
                timeout=max(0.0, deadline - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await _stop_reading(proc, readers)
                detail = f"timed out after {timeout}s: {' '.join(cmd[:4])}"
                return TIMEOUT_RC, b"", detail
            for task in done:
                if task.result()[1]:
                    await _stop_reading(proc, readers)
                    if task is out_task:
                        return -2, b"", f"output longer than {max_bytes} bytes"
                    return -3, b"", f"stderr longer than {max_stderr} bytes"
        await asyncio.wait_for(proc.wait(), max(1.0, deadline - loop.time()))
    except (TimeoutError, asyncio.TimeoutError):
        await _stop_reading(proc, readers)
        return TIMEOUT_RC, b"", f"timed out after {timeout}s: {' '.join(cmd[:4])}"
    except BaseException:
        # Включая CancelledError: убрать процесс и читателей, отдать отмену.
        await _stop_reading(proc, readers)
        raise
    finally:
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
    stderr = err_task.result()[0]
    return (
        proc.returncode or 0,
        out_task.result()[0],
        stderr.decode(errors="replace").strip(),
    )
