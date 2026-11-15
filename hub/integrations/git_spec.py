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
#: файл дал бы индекс без хвоста требований). Размер известен из дерева ДО
#: чтения: больший файл не читается вовсе.
MAX_SPEC_BYTES = 1_000_000
#: Потолки чтения за один проход: число файлов, их суммарный размер и вывод
#: ``ls-tree`` (в ``specs`` может лежать что угодно). Превышение - ``unreadable``
#: с названным потолком: индекс по неполному чтению не строится и не стирается.
MAX_SPEC_FILES = 500
MAX_SPEC_TOTAL_BYTES = 8_000_000
MAX_TREE_LISTING_BYTES = 1_000_000
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


async def _resolve_commit(repo: str, branch: str) -> tuple[str, str, str]:
    """``(ref, commit, problem)``: ``origin/<branch>``, иначе локальная ветка.

    ``rev-parse --verify --quiet`` отвечает 1, когда ref нет; любой другой код
    (в том числе таймаут) - сбой git, а не отсутствие ref, и называется сбоем.
    """
    for ref in (f"origin/{branch}", branch):
        rc, out, err = await proc.run(
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
            return ref, out.strip(), ""
        if rc not in (0, 1):
            return ref, "", f"git rev-parse {ref} failed (rc={rc}): {err[:150]}"
    return (
        f"origin/{branch}",
        "",
        f"ref {branch!r} is not readable in the project clone",
    )


def _spec_entries(listing: str) -> list[tuple[str, str, str, int, str]]:
    """``(mode, type, blob_sha, size, path)`` записей ``ls-tree -r -z -l`` по пути спецификации.

    Размер ``-1``, когда git его не назвал (не blob): такая запись всё равно
    приходит, чтобы отказ назвал её имя.
    """
    entries = []
    for entry in listing.split("\0"):
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) == 4 and SPEC_PATH.match(path):
            size = int(fields[3]) if fields[3].isdigit() else -1
            entries.append((fields[0], fields[1], fields[2], size, path))
    return sorted(entries, key=lambda e: e[4])


async def _read_blob(repo: str, path: str, sha: str) -> tuple[int, dict[str, Any]]:
    """``(rc, file)``: rc != 0 - git не ответил; ``file.error`` - сам файл негоден."""
    rc, body, err = await proc.run_capped(
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
    if rc != 0:
        entry["error"] = err[:150] or f"cat-file rc={rc}"
        return rc, entry
    try:
        entry["text"] = body.decode("utf-8")
    except UnicodeDecodeError:
        entry["error"] = "file is not valid UTF-8"
    return 0, entry


async def _list_spec_entries(
    repo: str, commit: str
) -> tuple[list[tuple[str, str, str, int, str]], str]:
    """``(entries, problem)``: записи ``specs/<cap>/spec.md`` коммита или причина отказа."""
    rc, raw, err = await proc.run_capped(
        "git",
        "-C",
        repo,
        "ls-tree",
        "-r",
        "-z",
        "-l",
        commit,
        "--",
        "specs",
        cwd=repo,
        timeout=_STEP_TIMEOUT,
        max_bytes=MAX_TREE_LISTING_BYTES,
    )
    if rc != 0:
        return [], f"ls-tree failed (rc={rc}): {err[:150]}"
    entries = _spec_entries(raw.decode("utf-8", errors="replace"))
    if len(entries) > MAX_SPEC_FILES:
        return [], f"more than {MAX_SPEC_FILES} spec files"
    if sum(max(e[3], 0) for e in entries) > MAX_SPEC_TOTAL_BYTES:
        return [], f"spec files together exceed {MAX_SPEC_TOTAL_BYTES} bytes"
    return entries, ""


def _refusal_of_entry(mode: str, kind: str, size: int) -> str:
    """Почему файл не читается вообще (до обращения к git за содержимым)."""
    if kind != "blob" or mode not in _REGULAR_BLOBS:
        return f"not a regular file (mode {mode})"
    if size > MAX_SPEC_BYTES:
        return f"file is larger than {MAX_SPEC_BYTES} bytes"
    return ""


async def read_spec_files(repo: str, branch: str) -> dict[str, Any]:
    """``{state, ref, commit, files: [{path, blob_sha, text, error}], reason}``.

    ``blob_sha`` - SHA blob файла в дереве коммита ``origin/<branch>``: это и
    есть ревизия файла. Файл не обычный (симлинк, подмодуль), слишком большой
    или не UTF-8 приходит с непустым ``error``: это недостаток файла, и индекс
    откажет с его именем; размер известен из дерева, больший потолка файл не
    читается. Сбой самого git и превышение потолков чтения - ``unreadable``.
    """
    ref, commit, problem = await _resolve_commit(repo, branch)
    if not commit:
        return _unreadable(ref, problem)
    return await _read_commit_files(repo, ref, commit)


async def _read_commit_files(repo: str, ref: str, commit: str) -> dict[str, Any]:
    """Файлы ``specs/<cap>/spec.md`` одного коммита; ответ как у ``read_spec_files``."""
    entries, problem = await _list_spec_entries(repo, commit)
    if problem:
        return _unreadable(ref, problem, commit)
    files: list[dict[str, Any]] = []
    for mode, kind, sha, size, path in entries:
        refusal = _refusal_of_entry(mode, kind, size)
        if refusal:
            files.append({"path": path, "blob_sha": sha, "text": "", "error": refusal})
            continue
        rc, entry = await _read_blob(repo, path, sha)
        if rc != 0:
            return _unreadable(ref, f"cat-file {path}: {entry['error']}", commit)
        files.append(entry)
    return {"state": "read", "ref": ref, "commit": commit, "files": files, "reason": ""}


def _delta_unreadable(head: str, reason: str) -> dict[str, Any]:
    return {
        "state": "unreadable",
        "head": head,
        "base": "",
        "reason": reason,
        "head_files": [],
        "base_files": [],
        "changed": [],
    }


async def _commit_of(repo: str, rev: str) -> tuple[str, str]:
    """``(commit, problem)``: коммит ``rev`` в клоне. Нет объекта - причина, не сбой."""
    rc, out, err = await proc.run(
        "git",
        "-C",
        repo,
        "rev-parse",
        "--verify",
        "--quiet",
        f"{rev}^{{commit}}",
        cwd=repo,
        timeout=_STEP_TIMEOUT,
        check=False,
    )
    if rc == 0 and out.strip():
        return out.strip(), ""
    if rc == 1:
        return "", f"commit {rev[:12]} is not in the project clone"
    return "", f"git rev-parse {rev[:12]} failed (rc={rc}): {err[:150]}"


async def _merge_base(repo: str, head: str, tip: str) -> tuple[str, str]:
    rc, out, err = await proc.run(
        "git",
        "-C",
        repo,
        "merge-base",
        head,
        tip,
        cwd=repo,
        timeout=_STEP_TIMEOUT,
        check=False,
    )
    if rc == 0 and out.strip():
        return out.strip(), ""
    return "", f"no merge base of {head[:12]} and {tip[:12]} (rc={rc}): {err[:120]}"


async def _changed_spec_paths(repo: str, base: str, head: str) -> tuple[list[str], str]:
    """Пути под ``specs/``, изменённые между базой и head: любые, не только spec.md."""
    rc, raw, err = await proc.run_capped(
        "git",
        "-C",
        repo,
        "diff",
        "--name-only",
        "-z",
        base,
        head,
        "--",
        "specs",
        cwd=repo,
        timeout=_STEP_TIMEOUT,
        max_bytes=MAX_TREE_LISTING_BYTES,
    )
    if rc != 0:
        return [], f"git diff failed (rc={rc}): {err[:150]}"
    paths = raw.decode("utf-8", errors="replace").split("\0")
    return sorted(path for path in paths if path), ""


async def read_spec_delta(repo: str, head_sha: str, branch: str) -> dict[str, Any]:
    """Файлы спецификации на head_sha и на его merge-base с ``origin/<branch>`` (#2220).

    ``{state, head, base, head_files, base_files, changed, reason}``. База сверки
    - merge-base, а не вершина ветки по умолчанию: чужие слияния в базу после
    ответвления автора в его diff не попадают. ``unreadable`` - сверить нечем
    (коммита нет в клоне, git не ответил): это не «diff чист».
    """
    ref, tip, problem = await _resolve_commit(repo, branch)
    if not tip:
        return _delta_unreadable(head_sha, problem)
    head, problem = await _commit_of(repo, head_sha)
    if not head:
        return _delta_unreadable(head_sha, problem)
    base, problem = await _merge_base(repo, head, tip)
    if not base:
        return _delta_unreadable(head, problem)
    changed, problem = await _changed_spec_paths(repo, base, head)
    if problem:
        return _delta_unreadable(head, problem)
    head_read = await _read_commit_files(repo, head, head)
    base_read = await _read_commit_files(repo, ref, base)
    for side in (head_read, base_read):
        if side["state"] != "read":
            return _delta_unreadable(head, side["reason"] or "spec files not read")
    return {
        "state": "read",
        "head": head,
        "base": base,
        "reason": "",
        "head_files": head_read["files"],
        "base_files": base_read["files"],
        "changed": changed,
    }


class SpecReads:
    """Чтение спецификации как метод плагина git_ops (#2214)."""

    async def read_spec_files(self, repo: str, branch: str) -> dict[str, Any]:
        """Spec files of ``origin/<branch>`` with blob SHAs; never the working copy."""
        return await read_spec_files(repo, branch)

    async def read_spec_delta(
        self, repo: str, head_sha: str, branch: str
    ) -> dict[str, Any]:
        """Spec files at ``head_sha`` and at its merge base with ``origin/<branch>``."""
        return await read_spec_delta(repo, head_sha, branch)
