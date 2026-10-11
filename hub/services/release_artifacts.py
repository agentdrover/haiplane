"""Сверка серверных копий артефактов деплоя с релизом ДО слияния (#1591).

Обёртка CI запускает только закреплённую на сервере копию deploy-скрипта, а
служба ревьюера ставится руками. Забытая копия обнаруживалась красным деплоем
ПОСЛЕ слияния. Здесь она обнаруживается до него: для каждой объявленной пары
``repo_path`` → ``server_path`` сравниваются sha256 файла на закреплённой
голове релизного PR и файла на сервере.

Что это доказывает и чего нет. Совпал файл НА ДИСКЕ в момент чтения. Версию
РАБОТАЮЩЕГО процесса это не подтверждает: служба, заменённая на диске и не
перезапущенная, продолжает исполнять старый код — поэтому update_hint службы
обязан включать перезапуск. Обёртка CI остаётся финальной защитой.

Каждая причина начинается словами «серверная копия <путь>»: по ним
``release_alert`` узнаёт причину артефакта и поднимает алерт сразу, без
ожидания циклов.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from hub import config
from hub.integrations import proc
from hub.release_artifact_paths import server_sha256

log = logging.getLogger(__name__)

#: Начало каждой причины артефакта; читает release_alert.
REASON_MARKER = "серверная копия"
_REGULAR_MODES = frozenset({"100644", "100755"})


def allowed_dirs() -> tuple[str, ...]:
    """Каталоги, которые хаб вправе читать; читается при КАЖДОМ обращении."""
    return tuple(config.RELEASE_ARTIFACT_DIRS)


async def _git_bytes(workspace: str, *args: str) -> tuple[int, bytes, str]:
    return await proc.run_bytes("git", "-C", workspace, *args, cwd=workspace)


async def repo_blob_sha256(
    workspace: str, sha: str, repo_path: str
) -> tuple[str | None, str]:
    """(sha256 | None, причина): сырые байты blob файла ``repo_path`` на ``sha``.

    Не ``file_at_ref``: тот декодирует и делает ``strip()``, и завершающий
    перевод строки с не-UTF-8 байтами в хэш не попадают. Объект, которого нет
    в клоне, один раз доставляется ``git fetch origin <sha>``.
    """
    tree: tuple[int, bytes, str] = (1, b"", "")
    for attempt in range(2):
        tree = await _git_bytes(workspace, "ls-tree", "-z", sha, "--", repo_path)
        if tree[0] == 0 or attempt:
            break
        await proc.run(
            "git",
            "-C",
            workspace,
            "fetch",
            "origin",
            sha,
            cwd=workspace,
            timeout=120,
            check=False,
        )
    rc, out, err = tree
    if rc != 0:
        return None, f"дерево головы не прочитано: {err[:120] or f'rc={rc}'}"
    entry = out.split(b"\0", 1)[0]
    if not entry:
        return None, "файла нет на голове релиза"
    meta, _, name = entry.partition(b"\t")
    fields = meta.decode(errors="replace").split()
    if name.decode(errors="replace") != repo_path or len(fields) != 3:
        return None, "файла нет на голове релиза"
    mode, kind, obj = fields
    if kind != "blob" or mode not in _REGULAR_MODES:
        return None, f"в репо не обычный файл (mode {mode})"
    rc, body, err = await _git_bytes(workspace, "cat-file", "blob", obj)
    if rc != 0:
        return None, f"blob не прочитан: {err[:120] or f'rc={rc}'}"
    return hashlib.sha256(body).hexdigest(), ""


def _hint(pair: dict[str, Any]) -> str:
    return f"; обновить: {pair['update_hint']}" if pair.get("update_hint") else ""


async def _problem_of(pair: dict[str, Any], workspace: str, head: str) -> str:
    server_path, repo_path = pair["server_path"], pair["repo_path"]
    dirs = allowed_dirs()
    server, why = await asyncio.to_thread(server_sha256, server_path, dirs)
    if server is None:
        return f"{REASON_MARKER} {server_path} недоступна: {why}{_hint(pair)}"
    local, why = await repo_blob_sha256(workspace, head, repo_path)
    if local is None:
        return (
            f"{REASON_MARKER} {server_path} без сверки: {repo_path} "
            f"на голове {head[:12]}: {why}"
        )
    if local != server:
        return (
            f"{REASON_MARKER} {server_path} устарела: на сервере sha256 "
            f"{server[:12]}, в репо {repo_path}@{head[:12]} sha256 {local[:12]}"
            f"{_hint(pair)}"
        )
    return ""


async def check_pairs(
    pairs: list[dict[str, Any]], workspace: str, head: str
) -> list[str]:
    """Причины по каждой расходящейся паре; пустой список — всё совпало.

    Не бросает: сбой чтения — тоже причина, а исключение уехало бы в общий
    «релиз не удалось провести», который ждёт циклов.
    """
    problems: list[str] = []
    for pair in pairs:
        try:
            problem = await _problem_of(pair, workspace, head)
        except Exception as exc:  # noqa: BLE001 - a reason, not a failure
            log.exception("release artifact check crashed")
            problem = (
                f"{REASON_MARKER} {pair.get('server_path', '?')} без сверки: "
                f"{type(exc).__name__}"
            )
        if problem:
            problems.append(problem)
    return problems
