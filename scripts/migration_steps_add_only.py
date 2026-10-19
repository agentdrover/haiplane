#!/usr/bin/env python3
"""Каталог миграций-шагов под присмотром CI (#1854): add-only и строгая опись.

Миграции хаба лежат по одному файлу в ``hub/migrations/steps/``. Прод уже
отметил их имена в ``_migrations`` и больше не исполнит: правка влитого файла
молча расходится с базой, а удаление теряет миграцию для свежих баз.
Исправление - новый файл с новым именем.

    uv run python scripts/migration_steps_add_only.py                    # origin/develop
    uv run python scripts/migration_steps_add_only.py --base origin/main
    # CI: база - состояние ДО события (push: github.event.before, PR: база PR)
    uv run python scripts/migration_steps_add_only.py --github-event push --before <sha>

Вторая проверка - строгая опись каталога: запись не по шаблону имени шага
(``README.md``, ``.keep``, ``*.py~``) и шаг, который не читается как литерал,
красят CI. Старт хаба такие записи терпит (пропускает с предупреждением), поэтому
строгость живёт здесь.

Файлы, которые есть на merge-base с базой, обязаны совпадать с рабочим деревом
байт в байт; новые файлы разрешены. На push в develop/main HEAD уже содержит
правку, поэтому база там обязана быть состоянием до события (``before``), а не
веткой, в которую пушат: merge-base с самой собой пуст. Код возврата: 0 - чисто,
1 - названы нарушения, 2 - сравнить не удалось (нет базы, нет git): молчаливое
«чисто» при отсутствии базы запрещено.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

DEFAULT_BASE = "origin/develop"
STEPS_PREFIX = "hub/migrations/steps/"
_NOT_STEPS = ("__init__.py",)


_NO_SHA = "0" * 40


def ci_base(
    event: str, before: str = "", base_ref: str = "", default: str = DEFAULT_BASE
) -> str:
    """База сравнения для события CI: состояние репозитория ДО него.

    pull_request - ветка, в которую идёт PR; push - ``before`` (нулевой sha у
    новой ветки не база); остальное (workflow_dispatch, локально) - ``default``.
    """
    if event == "pull_request" and base_ref:
        return f"origin/{base_ref}"
    if event == "push" and before and before != _NO_SHA:
        return before
    return default


class CompareError(RuntimeError):
    """Базу сравнения получить не удалось."""


def _git(args: list[str], repo: Path, *, text: bool = True) -> str | bytes:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=text, check=False
        )
    except OSError as exc:
        raise CompareError(f"git не запускается: {exc}") from exc
    if result.returncode != 0:
        err = result.stderr if text else result.stderr.decode(errors="replace")
        raise CompareError(f"git {' '.join(args)}: {err.strip()}")
    return result.stdout


def _snapshot(
    repo: Path, base: str, exact: bool, git: Callable[..., str | bytes]
) -> str:
    """Коммит, с которым сравниваем: merge-base с базой или ровно база события."""
    if not exact:
        return str(git(["merge-base", base, "HEAD"], repo)).strip()
    try:
        return str(git(["rev-parse", "--verify", f"{base}^{{commit}}"], repo)).strip()
    except CompareError:
        # Прежняя вершина после force-push может быть не в клоне: один добор.
        git(["fetch", "-q", "--no-tags", "origin", base], repo)
        return str(git(["rev-parse", "--verify", f"{base}^{{commit}}"], repo)).strip()


def violations(
    repo: Path,
    base: str = DEFAULT_BASE,
    *,
    exact: bool = False,
    git: Callable[..., str | bytes] = _git,
) -> list[str]:
    """Строки-нарушения: файл шага со снимка изменён, удалён или сменил тип/режим.

    Снимок - merge-base с базой (локально, PR) или ровно ``base`` (``exact``:
    push, где merge-base при force-push откатился бы к общему предку и скрыл
    шаг). Содержимое сравнивается как его видит git (``hash-object`` применяет
    те же фильтры, что и ``add``: autocrlf не делает файл «изменённым»).
    """
    merge_base = _snapshot(repo, base, exact, git)
    listing = str(git(["ls-tree", "-r", "-z", merge_base, "--", STEPS_PREFIX], repo))
    found: list[str] = []
    for entry in sorted(e for e in listing.split("\0") if e):
        meta, path = entry.split("\t", 1)
        mode, _kind, blob = meta.split()
        if path.removeprefix(STEPS_PREFIX) in _NOT_STEPS or "__pycache__" in path:
            continue
        at = f"на снимке {merge_base[:12]}"
        current = repo / path
        if not (current.is_file() or current.is_symlink()):
            found.append(f"{path}: удалён (есть {at})")
        elif current.is_symlink() != (mode == "120000"):
            found.append(f"{path}: сменил тип файла (на {at} режим {mode})")
        elif mode != "120000" and os.access(current, os.X_OK) != (mode == "100755"):
            found.append(f"{path}: сменил режим (на {at} {mode})")
        elif str(git(["hash-object", "--", path], repo)).strip() != blob:
            found.append(f"{path}: изменён (на {at} другой)")
    return found


def _load_loader(repo: Path):  # noqa: ANN202
    """Загрузчик проверяемого дерева по пути файла, а не через ``import hub``.

    ``import hub.migrations`` собрал бы список по настоящему steps/ ещё до
    проверки и упал бы трейсбеком на том самом плохом файле, который надо
    назвать; файл загрузчика зависит только от stdlib.
    """
    path = repo / STEPS_PREFIX / "__init__.py"
    spec = importlib.util.spec_from_file_location("_steps_loader_under_check", path)
    if spec is None or spec.loader is None:
        raise CompareError(f"загрузчик не найден: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # NamedTuple ищет модуль по имени
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - любая причина = «не удалось»
        raise CompareError(f"загрузчик {path} не импортируется: {exc}") from exc
    return module


def structure_problems(repo: Path) -> list[str]:
    """Строгая опись каталога шагов: посторонние записи и нечитаемые шаги."""
    loader = _load_loader(repo)
    try:
        loader.load_steps(repo / STEPS_PREFIX, strict=True)
    except loader.MigrationStepError as exc:
        return [str(exc)]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--github-event", default="", help="pull_request | push | ...")
    parser.add_argument("--before", default="", help="github.event.before")
    parser.add_argument("--base-ref", default="", help="github.base_ref")
    args = parser.parse_args(argv)
    exact = False
    if args.github_event:
        args.base = ci_base(args.github_event, args.before, args.base_ref, args.base)
        exact = args.github_event == "push" and args.base == args.before
    try:
        found = [
            *structure_problems(args.repo),
            *violations(args.repo, args.base, exact=exact),
        ]
    except CompareError as exc:
        print(f"migration steps: сравнить не удалось: {exc}", file=sys.stderr)
        return 2
    if found:
        print("migration steps: нарушения в hub/migrations/steps/:", file=sys.stderr)
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
