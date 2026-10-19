#!/usr/bin/env python3
"""Влитый файл миграции-шага не меняется и не удаляется (#1854).

Миграции хаба лежат по одному файлу в ``hub/migrations/steps/``. Прод уже
отметил их имена в ``_migrations`` и больше не исполнит: правка влитого файла
молча расходится с базой, а удаление теряет миграцию для свежих баз.
Исправление - новый файл с новым именем.

    uv run python scripts/migration_steps_add_only.py                    # origin/develop
    uv run python scripts/migration_steps_add_only.py --base origin/main

Файлы, которые есть на merge-base с базой, обязаны совпадать с рабочим деревом
байт в байт; новые файлы разрешены. Код возврата: 0 - чисто, 1 - названы
изменённые или удалённые файлы, 2 - сравнить не удалось (нет базы, нет git):
молчаливое «чисто» при отсутствии базы запрещено.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

DEFAULT_BASE = "origin/develop"
STEPS_PREFIX = "hub/migrations/steps/"
_NOT_STEPS = ("__init__.py",)


class CompareError(RuntimeError):
    """Базу сравнения получить не удалось."""


def _git(args: list[str], repo: Path, *, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=text, check=False
    )
    if result.returncode != 0:
        err = result.stderr if text else result.stderr.decode(errors="replace")
        raise CompareError(f"git {' '.join(args)}: {err.strip()}")
    return result.stdout


def violations(
    repo: Path,
    base: str = DEFAULT_BASE,
    *,
    git: Callable[..., str | bytes] = _git,
) -> list[str]:
    """Строки-нарушения: файл шага с merge-base изменён, удалён или сменил тип.

    Содержимое сравнивается как его видит git (``hash-object`` применяет те же
    фильтры, что и ``add``: autocrlf не делает файл «изменённым»), режим
    записи - как в дереве merge-base (ссылка вместо файла - нарушение).
    """
    merge_base = str(git(["merge-base", base, "HEAD"], repo)).strip()
    listing = str(git(["ls-tree", "-r", "-z", merge_base, "--", STEPS_PREFIX], repo))
    found: list[str] = []
    for entry in sorted(e for e in listing.split("\0") if e):
        meta, path = entry.split("\t", 1)
        mode, _kind, blob = meta.split()
        if path.removeprefix(STEPS_PREFIX) in _NOT_STEPS or "__pycache__" in path:
            continue
        at = f"на merge-base {merge_base[:12]}"
        current = repo / path
        if not (current.is_file() or current.is_symlink()):
            found.append(f"{path}: удалён (есть {at})")
        elif current.is_symlink() != (mode == "120000"):
            found.append(f"{path}: сменил тип файла (на {at} режим {mode})")
        elif str(git(["hash-object", "--", path], repo)).strip() != blob:
            found.append(f"{path}: изменён (на {at} другой)")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        found = violations(args.repo, args.base)
    except CompareError as exc:
        print(f"migration steps: сравнить не удалось: {exc}", file=sys.stderr)
        return 2
    if found:
        print("migration steps: влитые файлы шагов править нельзя:", file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        print(
            "Исправление - новый файл в hub/migrations/steps/ с новым именем.",
            file=sys.stderr,
        )
        return 1
    print(f"migration steps: влитые файлы шагов не тронуты (база {args.base})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
