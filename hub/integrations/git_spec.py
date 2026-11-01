"""Чтение файлов спецификации с ветки по умолчанию (#2214).

Примесь ``GitOpsIntegration``: ``git_ops.py`` стоит у потолка размера
(docs/agent-context/file-size-budget.json), поэтому новая тема живёт рядом.

Читаются объекты git по ref ``origin/<branch>``, не рабочая копия: правка без
коммита или чужая ветка в чекауте индекс не меняют. Ответ различает три случая,
как остальные чтения git_ops (#725): ``read`` - ref прочитан (в том числе когда
файлов нет вовсе), ``unreadable`` - спросить не удалось. Второе не равно «файлов
нет»: вызывающий не вправе стирать по нему прежний индекс.
"""

from __future__ import annotations

import re
from typing import Any

from hub.integrations import proc

#: ``specs/<capability>/spec.md``: один файл на способность, без вложенности.
SPEC_PATH = re.compile(r"^specs/([^/]+)/spec\.md$")
#: Потолок одного файла; больший - ошибка файла, а не усечение (обрезанный
#: файл дал бы индекс без хвоста требований).
MAX_SPEC_BYTES = 1_000_000
_REGULAR_BLOBS = ("100644", "100755")
_STEP_TIMEOUT = 30


def _unreadable(ref: str, reason: str, commit: str = "") -> dict[str, Any]:
    return {
        "state": "unreadable",
        "ref": ref,
        "commit": commit,
        "files": [],
        "reason": reason,
    }


async def _resolve_commit(repo: str, branch: str) -> tuple[str, str]:
    """``(ref, commit)``: ``origin/<branch>``, а при его отсутствии - локальная ветка."""
    for ref in (f"origin/{branch}", branch):
        rc, out, _ = await proc.run(
            "git",
            "-C",
            repo,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
            cwd=repo,
            timeout=_STEP_TIMEOUT,
            check=False,
        )
        if rc == 0 and out:
            return ref, out.strip()
    return f"origin/{branch}", ""


def _spec_entries(listing: str) -> list[tuple[str, str, str, str]]:
    """``(mode, type, blob_sha, path)`` записей ``ls-tree -r -z`` по пути спецификации."""
    entries = []
    for entry in listing.split("\0"):
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) == 3 and SPEC_PATH.match(path):
            entries.append((fields[0], fields[1], fields[2], path))
    return sorted(entries, key=lambda e: e[3])


async def _read_blob(repo: str, path: str, sha: str) -> tuple[int, dict[str, Any]]:
    """``(rc, file)``: rc != 0 - git не ответил; ``file.error`` - сам файл негоден."""
    rc, body, err = await proc.run_bytes(
        "git",
        "-C",
        repo,
        "cat-file",
        "blob",
        sha,
        cwd=repo,
        timeout=_STEP_TIMEOUT,
        max_bytes=MAX_SPEC_BYTES,
    )
    entry: dict[str, Any] = {"path": path, "blob_sha": sha, "text": "", "error": ""}
    if rc == -2:
        entry["error"] = f"file is larger than {MAX_SPEC_BYTES} bytes"
        return 0, entry
    if rc != 0:
        entry["error"] = err[:150] or f"cat-file rc={rc}"
        return rc, entry
    try:
        entry["text"] = body.decode("utf-8")
    except UnicodeDecodeError:
        entry["error"] = "file is not valid UTF-8"
    return 0, entry


async def read_spec_files(repo: str, branch: str) -> dict[str, Any]:
    """``{state, ref, commit, files: [{path, blob_sha, text, error}], reason}``.

    ``blob_sha`` - SHA blob файла в дереве коммита ``origin/<branch>``: это и
    есть ревизия файла. Файл не обычный (симлинк, подмодуль), слишком большой
    или не UTF-8 приходит с непустым ``error``: это недостаток файла, и индекс
    откажет с его именем. Сбой самого git - ``unreadable``.
    """
    ref, commit = await _resolve_commit(repo, branch)
    if not commit:
        return _unreadable(ref, f"ref {branch!r} is not readable in the project clone")
    rc, listing, err = await proc.run(
        "git",
        "-C",
        repo,
        "ls-tree",
        "-r",
        "-z",
        commit,
        "--",
        "specs",
        cwd=repo,
        timeout=_STEP_TIMEOUT,
        check=False,
    )
    if rc != 0:
        return _unreadable(ref, f"ls-tree rc={rc}: {err[:150]}", commit)
    files: list[dict[str, Any]] = []
    for mode, kind, sha, path in _spec_entries(listing):
        if kind != "blob" or mode not in _REGULAR_BLOBS:
            files.append(
                {
                    "path": path,
                    "blob_sha": sha,
                    "text": "",
                    "error": f"not a regular file (mode {mode})",
                }
            )
            continue
        rc, entry = await _read_blob(repo, path, sha)
        if rc != 0:
            return _unreadable(ref, f"cat-file {path}: {entry['error']}", commit)
        files.append(entry)
    return {"state": "read", "ref": ref, "commit": commit, "files": files, "reason": ""}


class SpecReads:
    """Чтение спецификации как метод плагина git_ops (#2214)."""

    async def read_spec_files(self, repo: str, branch: str) -> dict[str, Any]:
        """Spec files of ``origin/<branch>`` with blob SHAs; never the working copy."""
        return await read_spec_files(repo, branch)
