"""``hp-hub worktree purge`` - окончательная уборка карантина (#2038).

``hp-hub worktree prune --apply`` не удаляет копии, а переносит их в
``<корень-копий>/.trash/task-N-<UTC yyyymmddThhmmss>`` через ``git worktree
move``: gitdir, refs и содержимое целы, копия остаётся в ``git worktree list``.
Удаление необратимо, поэтому его делает только эта команда и только для копий
из ``.trash`` старше ``--older-than-days`` (7) по времени в имени.

Список без ``--apply`` ничего не трогает и показывает возраст каждой копии.
Путь вне ``.trash`` (в том числе симлинк и каноническое ``task-N``) purge не
принимает: ``git worktree remove --force`` применяется только к тому, что
именно эта команда положила в карантин.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from calendar import timegm
from typing import Any, Callable

from hub.cli_worktree_prune import (
    TRASH_DIR,
    Entry,
    _git,
    _parse_porcelain,
    _real,
    trash_root,
)

_TRASH_NAME = re.compile(r"^(task-\d+)-(\d{8}T\d{6})(?:-\d+)?$")
_DAY = 86400.0


def _age_days(stamp: str) -> float | None:
    try:
        moved = timegm(time.strptime(stamp, "%Y%m%dT%H%M%S"))
    except ValueError:
        return None
    return (time.time() - moved) / _DAY


def _classify(entry: Entry, trash: str, older: float) -> tuple[str, str]:
    """(действие, текст): purge | keep | skip (не относится к карантину)."""
    path = os.path.abspath(entry.path)
    name = os.path.basename(path)
    match = _TRASH_NAME.match(name)
    inside = _real(os.path.dirname(path)) == trash
    if not match and not inside:
        return "skip", ""
    if not inside:
        return "keep", f"имя как у карантина, но путь вне {trash}: purge не принимает"
    if not match:
        return "keep", f"имя не по шаблону task-N-<UTC>: {name}"
    if os.path.islink(path):
        return "keep", "симлинк: purge не принимает"
    age = _age_days(match.group(2))
    if age is None:
        return "keep", f"время в имени не разобрано: {name}"
    if age < older:
        return "keep", f"в карантине {age:.1f} сут < {older:g}"
    return "purge", f"в карантине {age:.1f} сут >= {older:g}"


def _failed_purge(path: str, gitdir: str, rc: int, err: str) -> str:
    here, meta = os.path.lexists(path), os.path.lexists(gitdir)
    tail = f"git rc {rc}: {err.strip()[:200]}"
    if here and meta:
        return f"ошибка удаления, путь и gitdir на месте (возможна частичная очистка): {tail}"
    if here or meta:
        return f"удалена частично ({'путь' if here else 'gitdir'} остался): {tail}"
    return f"результат неизвестен: путь и gitdir исчезли, но git вернул ошибку: {tail}"


def run_purge(args: Any, out: Callable[[str], None] = print) -> int:
    from hub.integrations.git_ops import clone_root

    clone = asyncio.run(clone_root(args.repo or "."))
    if not clone:
        print(f"{args.repo or '.'} не git-клон: нечего убирать", file=sys.stderr)
        return 1
    rc, text, err = _git(clone, "worktree", "list", "--porcelain")
    if rc != 0:
        print(
            f"git worktree list не прочитан (rc {rc}): {err.strip()}", file=sys.stderr
        )
        return 1
    trash, older = trash_root(clone), float(args.older_than_days)
    plan = [(e, *_classify(e, trash, older)) for e in _parse_porcelain(text)]
    for entry, action, why in plan:
        if action != "skip":
            word = "удалить  " if action == "purge" else "оставить "
            out(f"{word} {entry.path}: {why}")
    if not args.apply:
        out(
            f"dry-run: ничего не удалено; --apply убирает копии со строкой «удалить» из {TRASH_DIR}"
        )
        return 0
    failed = False
    for entry, action, _why in plan:
        if action != "purge":
            continue
        _, gitdir, _ = _git(entry.path, "rev-parse", "--absolute-git-dir")
        rc, _, err = _git(clone, "worktree", "remove", "--force", entry.path)
        if rc == 0:
            out(f"удалено   {entry.path}")
        else:
            failed = True
            out(
                f"ошибка    {entry.path}: {_failed_purge(entry.path, gitdir.strip(), rc, err)}"
            )
    return 1 if failed else 0
