"""Список миграций схемы hub.db (#1809, #1854).

Части ``part_1..part_3`` ЗАМОРОЖЕНЫ: в них ничего не добавлять, не переставлять
и не переименовывать - их имена и порядок определяют таблицу ``_migrations`` на
проде (охраняет ``tests/test_db_migrations.py::test_frozen_parts_unchanged``).

Новая миграция - ТОЛЬКО новый файл ``hub/migrations/steps/m<YYYYMMDDHHMM>_
<задача>_<суть>.py`` с константой ``MIGRATIONS: list[tuple[str, str]]``;
загрузчик добавляет шаги после частей в порядке имён файлов. Влитый файл шага
не правится: исправление - новый файл с новым именем. Каждый кортеж исполняется
как отдельный оператор, файл целиком не атомарен. Подробности - в
``hub/migrations/steps/__init__.py``.

Механизм применения (``hub.db._migrate``) читает ``hub.db._MIGRATIONS``.
"""

from __future__ import annotations

from hub.migrations.part_1 import MIGRATIONS_1
from hub.migrations.part_2 import MIGRATIONS_2
from hub.migrations.part_3 import MIGRATIONS_3
from hub.migrations.steps import assemble

FROZEN_MIGRATIONS: list[tuple[str, str]] = [
    *MIGRATIONS_1,
    *MIGRATIONS_2,
    *MIGRATIONS_3,
]

MIGRATIONS: list[tuple[str, str]] = assemble(FROZEN_MIGRATIONS)
