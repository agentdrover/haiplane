"""Карантин копий задач: маркер, защитные refs, снятие защиты (#2038, раунд 5).

``hp-hub worktree prune --apply`` переносит копию в ``.trash`` (см.
``cli_worktree_prune``), ``purge`` убирает её окончательно, ``restore`` возвращает.
Имя каталога не доказательство того, что копию положил в карантин prune, и срок в
имени не доказательство возраста: каталог можно создать или перенести руками.
Поэтому каждый перенос оставляет два следа, на которые опираются purge и restore:

* маркер ``<gitdir копии>/haiplane-quarantine`` (JSON: время переноса, id задачи,
  старый путь, ветка, HEAD, имя в карантине). Файл лежит в gitdir копии и уходит
  вместе с ней; prune внутрь ``.trash`` не заглядывает, поэтому в белом списке
  gitdir prune для него места нет;
* защитные refs ``refs/haiplane-trash/<имя>/<k>`` в общем клоне: HEAD, каждая
  запись reflog HEAD, каждая sha из ORIG_HEAD, FETCH_HEAD, AUTO_MERGE. Без них gc
  вправе убрать объекты, на которые указывает только reflog или псевдо-ref копии.
  Refs снимает только purge после удачного ``git worktree remove`` (или restore).

Функции получают ``git`` - вызываемое ``(cwd, *args) -> (rc, out, err)`` вызывающего
модуля, чтобы подмена git в тестах действовала и здесь.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from typing import Any, Callable

MARKER = "haiplane-quarantine"
REF_PREFIX = "refs/haiplane-trash"
PSEUDO_REFS = ("ORIG_HEAD", "FETCH_HEAD", "AUTO_MERGE")
_SHA = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})(?:\s|$)", re.M)

Git = Callable[..., tuple[int, str, str]]


def pseudo_ref_shas(gitdir: str) -> tuple[list[tuple[str, str]], str]:
    """(имя, sha) из ORIG_HEAD, FETCH_HEAD, AUTO_MERGE (первое поле строки) и ошибка."""
    found: list[tuple[str, str]] = []
    for name in PSEUDO_REFS:
        try:
            with open(os.path.join(gitdir, name), encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as exc:
            return [], f"{name} не прочитан ({type(exc).__name__})"
        shas = [m.group(1) for m in _SHA.finditer(text)]
        if text.strip() and not shas:
            return [], f"{name}: содержимое не разобрано, sha не найдено"
        found.extend((name, sha) for sha in shas)
    return found, ""


def reflog_shas(gitdir: str) -> tuple[list[str], str]:
    """Старое и новое значение каждой строки сырого logs/HEAD (кроме нулевой sha).

    ``git reflog --format=%H`` отдаёт только новые значения: старое значение
    первой сохранившейся строки нигде больше может не лежать.
    """
    path = os.path.join(gitdir, "logs", "HEAD")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"logs/HEAD не прочитан ({type(exc).__name__})"
    found: list[str] = []
    for line in text.splitlines():
        for sha in line.split("\t", 1)[0].split()[:2]:
            if _SHA.match(sha) and set(sha) != {"0"}:
                found.append(sha)
    return found, ""


def copy_shas(gitdir: str) -> tuple[list[tuple[str, str]], str]:
    """ВСЕ sha, которые держит копия: обе стороны logs/HEAD и ORIG/FETCH/AUTO_MERGE.

    Единственный читатель для защитных refs, проверок prune и проверки purge:
    (источник, sha) без повторов и ошибка чтения. Нечитаемое - не «пусто».
    """
    logged, err = reflog_shas(gitdir)
    if err:
        return [], err
    pseudo, err = pseudo_ref_shas(gitdir)
    if err:
        return [], err
    seen = dict.fromkeys(logged)
    found = [("записи reflog HEAD", sha) for sha in seen]
    found += [(name, sha) for name, sha in pseudo if sha not in seen]
    return found, ""


def protect(git: Git, clone: str, path: str, name: str) -> tuple[list[str], str]:
    """Создать защитные refs для копии ``path``; при сбое откатить и вернуть ошибку."""
    rc, gitdir, _ = git(path, "rev-parse", "--absolute-git-dir")
    rc2, head, _ = git(path, "rev-parse", "HEAD")
    if rc or rc2 or not gitdir.strip():
        return [], "HEAD или gitdir копии не прочитаны"
    held, err = copy_shas(gitdir.strip())
    if err:
        return [], err
    shas = list(dict.fromkeys([head.strip(), *(sha for _, sha in held)]))
    created: list[str] = []
    for k, sha in enumerate(shas):
        ref = f"{REF_PREFIX}/{name}/{k}"
        rc, _, err = git(clone, "update-ref", ref, sha)
        if rc != 0:
            unprotect_refs(git, clone, created)
            return [], f"git update-ref {ref} rc {rc}: {err.strip()[:120]}"
        created.append(ref)
    return created, ""


def unprotect_refs(git: Git, clone: str, refs: list[str]) -> str:
    """Удалить перечисленные refs; ошибка первой неудачи или пусто."""
    bad = ""
    for ref in refs:
        rc, _, err = git(clone, "update-ref", "-d", ref)
        if rc != 0 and not bad:
            bad = f"git update-ref -d {ref} rc {rc}: {err.strip()[:120]}"
    return bad


def unprotect(git: Git, clone: str, name: str) -> str:
    """Снять все защитные refs копии ``name``."""
    rc, out, err = git(
        clone, "for-each-ref", "--format=%(refname)", f"{REF_PREFIX}/{name}/"
    )
    if rc != 0:
        return f"git for-each-ref rc {rc}: {err.strip()[:120]}"
    return unprotect_refs(git, clone, out.split())


def write_marker(gitdir: str, data: dict[str, Any]) -> str:
    """Записать маркер атомарно; ошибка или пусто."""
    path = os.path.join(gitdir, MARKER)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True)
        os.replace(path + ".tmp", path)
    except OSError as exc:
        return f"маркер не записан ({type(exc).__name__}: {exc})"
    return ""


def make_marker(
    task_id: int, old_path: str, branch: str, head: str, name: str
) -> dict[str, Any]:
    now = int(time.time())
    return {
        "version": 1,
        "moved_epoch": now,
        "moved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "task_id": task_id,
        "old_path": old_path,
        "branch": branch,
        "head": head,
        "name": name,
    }


def read_marker(gitdir: str) -> tuple[dict[str, Any] | None, str]:
    """Маркер копии или причина, почему ему нельзя верить."""
    try:
        with open(os.path.join(gitdir, MARKER), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, "нет маркера карантина: prune эту копию не клал"
    except (OSError, ValueError) as exc:
        return None, f"маркер не разобран ({type(exc).__name__})"
    fields = ("moved_epoch", "task_id", "old_path", "branch", "head", "name")
    if not isinstance(data, dict) or any(k not in data for k in fields):
        return None, "маркер неполный"
    if not isinstance(data["moved_epoch"], int) or not isinstance(data["task_id"], int):
        return None, "маркер: время или id задачи не число"
    return data, ""


def days_arg(value: str) -> float:
    """Тип ``--older-than-days``: конечное число не меньше 1 (nan, inf, <1 - отказ)."""
    try:
        days = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"не число: {value!r}") from None
    if not math.isfinite(days) or days < 1:
        raise argparse.ArgumentTypeError(
            f"нужно конечное число не меньше 1 (сутки), получено {value!r}"
        )
    return days


def orphan_refs(
    git: Git, clone: str, live: set[str]
) -> tuple[dict[str, list[str]], str]:
    """Защитные refs копий, которых нет среди зарегистрированных в .trash."""
    rc, out, err = git(clone, "for-each-ref", "--format=%(refname)", f"{REF_PREFIX}/")
    if rc != 0:
        return {}, f"git for-each-ref rc {rc}: {err.strip()[:120]}"
    found: dict[str, list[str]] = {}
    for ref in out.split():
        parts = ref[len(REF_PREFIX) + 1 :].split("/")
        if len(parts) == 2 and parts[0] not in live:
            found.setdefault(parts[0], []).append(ref)
    return found, ""


def unpinned_shas(
    git: Git, clone: str, name: str, gitdir: str
) -> tuple[list[tuple[str, str]], str]:
    """(источник, sha) копии без защитного ref и вне refs/remotes; ошибка чтения."""
    held, err = copy_shas(gitdir)
    if err:
        return [], err
    rc, out, ferr = git(
        clone, "for-each-ref", "--format=%(objectname)", f"{REF_PREFIX}/{name}/"
    )
    if rc != 0:
        return [], f"защитные refs не прочитаны (git rc {rc}: {ferr.strip()[:120]})"
    pinned = set(out.split())
    missing: list[tuple[str, str]] = []
    for label, sha in held:
        if sha in pinned:
            continue
        rc, listed, ferr = git(
            clone, "for-each-ref", "--count=1", "--contains", sha, "refs/remotes"
        )
        if rc != 0:
            return [], f"{sha[:12]} не сверена с refs/remotes (git rc {rc})"
        if not listed.strip():
            missing.append((label, sha))
    return missing, ""


LATE = "late-"


def late_refs(git: Git, clone: str, name: str) -> tuple[list[str], str]:
    """Refs работы, найденной в карантине (``late-<sha>``): они держат копию от purge."""
    rc, out, err = git(
        clone, "for-each-ref", "--format=%(refname)", f"{REF_PREFIX}/{name}/{LATE}*"
    )
    if rc != 0:
        return [], f"git for-each-ref rc {rc}: {err.strip()[:120]}"
    return out.split(), ""


def pin_late(git: Git, clone: str, name: str, shas: list[str]) -> tuple[list[str], str]:
    """Закрепить sha как ``late-<полная sha>``: имя по sha идемпотентно и не затирает.

    Уже закреплённые sha сюда не попадают (их отсекает ``unpinned_shas``).

    Возвращает (созданные ЭТИМ проходом refs, ошибка). При сбое созданные в этом
    проходе откатываются, как в ``protect``; прежние ``late-*`` не трогаются.
    """
    created: list[str] = []
    for sha in dict.fromkeys(shas):
        ref = f"{REF_PREFIX}/{name}/{LATE}{sha}"
        rc, _, uerr = git(clone, "update-ref", ref, sha)
        if rc != 0:
            unprotect_refs(git, clone, created)
            return [], f"git update-ref {ref} rc {rc}: {uerr.strip()[:120]}"
        created.append(ref)
    return created, ""
