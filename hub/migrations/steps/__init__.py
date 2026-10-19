"""Миграции по одному файлу (#1854): загрузчик каталога ``hub/migrations/steps``.

Новая миграция - один файл ``m<YYYYMMDDHHMM>_<задача>_<суть>.py`` в этом
каталоге с константой ``MIGRATIONS: list[tuple[str, str]]``. Части
``part_1..part_3`` заморожены, поэтому две задачи с миграциями не правят один
файл и не конфликтуют в git. Загрузчик находит файлы сам и добавляет их после
частей в порядке имён файлов; ничего регистрировать не нужно.

Правила шага:

* Влитый файл не правится и не удаляется (``scripts/migration_steps_add_only.py``
  сверяет его с merge-base). Исправление - новый файл с новым именем.
* Шаг не импортирует ничего из ``hub`` (ни ``hub.db``, ни константы
  приложения): это данные, а не код. Загрузчик отклоняет такой импорт.
* Раннер ``hub.db._migrate`` исполняет каждый кортеж как отдельный оператор;
  файл целиком не атомарен.
* Независимые шаги обязаны терпеть любой порядок применения: шаг B мог быть
  применён раньше, чем в ветку пришёл шаг A с более ранним именем. Шаг,
  зависящий от другого, ставится после него: зависимость задач и более
  позднее имя файла.

Любое нарушение - ошибка при импорте ``hub.migrations``, то есть при старте.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

STEPS_DIR = Path(__file__).resolve().parent

# m<YYYYMMDDHHMM>_<задача>_<суть>.py: нижний регистр, цифры и подчёркивания,
# точка допустима только перед расширением.
STEP_FILE_RE = re.compile(r"m[0-9]{12}_[a-z0-9]+(?:_[a-z0-9]+)+\.py")

# .DS_Store: метаданные macOS не должны ронять старт (Docker копирует каталог).
_IGNORED = frozenset({"__init__.py", "__pycache__", ".DS_Store"})


class MigrationStepError(RuntimeError):
    """Каталог шагов или список миграций нарушает правила."""


def _inventory(directory: Path) -> list[str]:
    """Имена записей каталога до любого импорта; ``[]``, если каталога нет."""
    if not directory.is_dir():
        return []
    with os.scandir(directory) as entries:
        names = sorted(e.name for e in entries if e.name not in _IGNORED)
    bad = [n for n in names if not STEP_FILE_RE.fullmatch(n)]
    if bad:
        raise MigrationStepError(
            f"имя файла шага не по шаблону m<YYYYMMDDHHMM>_<задача>_<суть>.py: {bad}"
        )
    links = [n for n in names if (directory / n).is_symlink()]
    if links:
        raise MigrationStepError(f"шаг должен быть обычным файлом, не ссылкой: {links}")
    return names


def _check_no_hub_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = ["." * node.level + (node.module or "")]
        else:
            continue
        for module in modules:
            if module.startswith(".") or module.split(".")[0] == "hub":
                raise MigrationStepError(
                    f"{path.name}: шаг не импортирует ничего из hub ({module})"
                )


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_migration_step_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise MigrationStepError(f"{path.name}: файл не загружается как модуль")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def load_step_modules(directory: Path | None = None) -> list[tuple[str, ModuleType]]:
    """Модули шагов каталога в порядке имён файлов (загрузка из ``directory``)."""
    target = STEPS_DIR if directory is None else Path(directory)
    result: list[tuple[str, ModuleType]] = []
    for filename in _inventory(target):
        path = target / filename
        _check_no_hub_imports(path)
        module = _load_module(path)
        _validated(filename, getattr(module, "MIGRATIONS", None))
        result.append((filename, module))
    return result


def load_steps(directory: Path | None = None) -> list[tuple[str, str]]:
    """Миграции всех шагов каталога одним списком, в порядке имён файлов."""
    return [
        pair for _, module in load_step_modules(directory) for pair in module.MIGRATIONS
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
