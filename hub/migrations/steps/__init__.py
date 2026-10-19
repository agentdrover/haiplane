"""Миграции по одному файлу (#1854): загрузчик каталога ``hub/migrations/steps``.

Новая миграция - один файл ``m<YYYYMMDDHHMM>_<задача>_<суть>.py`` в этом
каталоге. Части ``part_1..part_3`` заморожены, поэтому две задачи с миграциями
не правят один файл и не конфликтуют в git. Загрузчик находит файлы сам и
добавляет их после частей в порядке имён файлов; ничего регистрировать не нужно.

Шаг - ДАННЫЕ, а не код. Файл состоит только из необязательной докстроки,
необязательного ``from __future__ import ...`` и одного литерального
присваивания ``MIGRATIONS = [("имя", "SQL"), ...]`` (допустима аннотация
``MIGRATIONS: list[tuple[str, str]] = [...]``). Загрузчик НЕ исполняет файл: он
разбирает его ``ast`` и берёт значение через ``ast.literal_eval``. Вызов
функции, импорт (в том числе ``__import__``/``importlib``), сложение строк -
всё это отвергается до любого исполнения; длинный SQL пишется неявной склейкой
соседних строковых литералов или тройными кавычками.

Правила шага:

* Влитый файл не правится и не удаляется (``scripts/migration_steps_add_only.py``
  сверяет его с состоянием ДО события: merge-base в PR и ``before`` в push).
  Исправление - новый файл с новым именем.
* Раннер ``hub.db._migrate`` исполняет каждый кортеж как отдельный оператор;
  файл целиком не атомарен.
* Независимые шаги обязаны терпеть любой порядок применения: шаг B мог быть
  применён раньше, чем в ветку пришёл шаг A с более ранним именем. Шаг,
  зависящий от другого, ставится после него: зависимость задач и более
  позднее имя файла.

Старт терпим к посторонним файлам (``README.md``, ``.keep``, ``*.py~``): всё, что
не по шаблону имени шага, пропускается с предупреждением в лог. Строгая
проверка каталога - в CI (``scripts/migration_steps_add_only.py``,
``strict=True``). Файл ПО шаблону, но с неверным содержимым, роняет импорт: шаг,
который нельзя прочесть, нельзя и пропустить молча.
"""

from __future__ import annotations

import ast
import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

STEPS_DIR = Path(__file__).resolve().parent

# m<YYYYMMDDHHMM>_<задача>_<суть>.py: нижний регистр, цифры и подчёркивания,
# точка допустима только перед расширением.
STEP_FILE_RE = re.compile(r"m[0-9]{12}_[a-z0-9]+(?:_[a-z0-9]+)+\.py")

_IGNORED = frozenset({"__init__.py", "__pycache__"})


class MigrationStepError(RuntimeError):
    """Каталог шагов или список миграций нарушает правила."""


class StepFile(NamedTuple):
    """Один прочитанный файл шага: имя, путь и его литеральные миграции."""

    name: str
    path: Path
    migrations: list[tuple[str, str]]


def stray_entries(directory: Path) -> list[str]:
    """Записи каталога, которые не шаг и не служебные (``__init__.py``, ``__pycache__``)."""
    if not directory.is_dir():
        return []
    with os.scandir(directory) as entries:
        names = sorted(e.name for e in entries if e.name not in _IGNORED)
    return [n for n in names if not STEP_FILE_RE.fullmatch(n)]


def _inventory(directory: Path, *, strict: bool) -> list[str]:
    """Имена файлов шагов по шаблону, до чтения любого файла; ``[]``, если каталога нет."""
    if not directory.is_dir():
        return []
    stray = stray_entries(directory)
    if stray and strict:
        raise MigrationStepError(
            f"в каталоге шагов посторонние записи (имя не по шаблону "
            f"m<YYYYMMDDHHMM>_<задача>_<суть>.py): {stray}"
        )
    for name in stray:
        log.warning("migration steps: %s пропущен - имя не по шаблону шага", name)
    with os.scandir(directory) as entries:
        names = sorted(e.name for e in entries if STEP_FILE_RE.fullmatch(e.name))
    links = [n for n in names if (directory / n).is_symlink()]
    if links:
        raise MigrationStepError(f"шаг должен быть обычным файлом, не ссылкой: {links}")
    return names


def _literal_migrations(path: Path) -> object:
    """Значение литерального ``MIGRATIONS`` без исполнения файла."""
    name = path.name
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (
        SyntaxError,
        UnicodeDecodeError,
        ValueError,
        RecursionError,
        MemoryError,
    ) as exc:
        raise MigrationStepError(f"{name}: файл не разбирается: {exc}") from exc
    found: ast.expr | None = None
    for index, node in enumerate(tree.body):
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue  # докстрока
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id == "MIGRATIONS" and found is None:
            found = value
            continue
        raise MigrationStepError(
            f"{name}:{node.lineno}: шаг - данные, допустимо только литеральное "
            "MIGRATIONS = [...] (без вызовов, импортов и другого кода)"
        )
    if found is None:
        raise MigrationStepError(f"{name}: MIGRATIONS отсутствует")
    try:
        return ast.literal_eval(found)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        raise MigrationStepError(f"{name}: MIGRATIONS не литерал: {exc}") from exc


def _validated(name: str, value: object) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        raise MigrationStepError(f"{name}: MIGRATIONS отсутствует или не list")
    for item in value:
        if not (
            isinstance(item, tuple)
            and len(item) == 2
            and all(isinstance(part, str) and part for part in item)
        ):
            raise MigrationStepError(
                f"{name}: каждый элемент MIGRATIONS - кортеж (имя, SQL) из двух строк"
            )
    return value


def load_step_files(
    directory: Path | None = None, *, strict: bool = False
) -> list[StepFile]:
    """Файлы шагов каталога в порядке имён; ``strict`` отвергает посторонние записи."""
    target = STEPS_DIR if directory is None else Path(directory)
    result: list[StepFile] = []
    for filename in _inventory(target, strict=strict):
        path = target / filename
        migrations = _validated(filename, _literal_migrations(path))
        result.append(StepFile(filename, path, migrations))
    return result


def load_steps(
    directory: Path | None = None, *, strict: bool = False
) -> list[tuple[str, str]]:
    """Миграции всех шагов каталога одним списком, в порядке имён файлов."""
    return [
        pair
        for step in load_step_files(directory, strict=strict)
        for pair in step.migrations
    ]


def assemble(
    frozen: Sequence[tuple[str, str]], directory: Path | None = None
) -> list[tuple[str, str]]:
    """Замороженные части, затем шаги; имя миграции уникально во всём списке."""
    combined = [*frozen, *load_steps(directory)]
    seen: set[str] = set()
    for name, _ in combined:
        if name in seen:
            raise MigrationStepError(f"дубль имени миграции: {name}")
        seen.add(name)
    return combined
