"""Список миграций схемы hub.db (#1809).

Новая миграция — в конец ``part_3.MIGRATIONS_3`` (если он перевалит порог
размера файла — заведите ``part_4``). Порядок и имена миграций определяют
таблицу ``_migrations`` на проде: ничего не переставлять и не переименовывать.
Механизм применения (``hub.db._migrate``) читает ``hub.db._MIGRATIONS``.
"""

from __future__ import annotations

from hub.migrations.part_1 import MIGRATIONS_1
from hub.migrations.part_2 import MIGRATIONS_2
from hub.migrations.part_3 import MIGRATIONS_3

MIGRATIONS: list[tuple[str, str]] = [*MIGRATIONS_1, *MIGRATIONS_2, *MIGRATIONS_3]
