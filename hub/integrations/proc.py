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
from pathlib import Path

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
    rc = proc.returncode or 0
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    if check and rc != 0:
        log.warning("%s failed (rc=%d): %s", " ".join(cmd[:4]), rc, err)
    return rc, out, err
