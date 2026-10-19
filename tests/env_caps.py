"""Предикаты пропуска тестов по возможностям окружения (#1851).

Тест не зависит от настроек машины: зависимость убирают в самом тесте или
фикстуре (env, шим, проходимый каталог). Пропуск допустим, только когда нужной
возможности в окружении нет и обойти это нельзя, — и только вне CI: в CI
(``GITHUB_ACTIONS=true``) каждый предикат возвращает ``None``, и тест
выполняется, а не молча пропускается.

Предикат возвращает причину пропуска (непустая строка) или ``None`` — «не
пропускать». Использование::

    @pytest.mark.skipif(env_caps.no_world_traversable_tmp() is not None, reason=...)

Каждая публичная функция модуля — предикат; сторож
``tests/test_environment_policy.py`` находит их сам. Пробы возможностей лежат в
``PROBES``: сторож подменяет их, чтобы «принудительно убрать» возможность.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path


def _in_ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _tmp_is_world_traversable() -> bool:
    """Пройдёт ли непривилегированный uid от корня до каталога в системном tmp."""
    probe = Path(tempfile.gettempdir()).resolve()
    return all(p.stat().st_mode & 0o001 for p in (probe, *probe.parents))


PROBES: dict[str, Callable[[], bool]] = {
    "tar": lambda: shutil.which("tar") is not None,
    "world_traversable_tmp": _tmp_is_world_traversable,
}


def _missing(cap: str, reason: str) -> str | None:
    if _in_ci() or PROBES[cap]():
        return None
    return reason


def no_tar() -> str | None:
    """Нет tar: шим rsync без системного rsync копирует деревья через tar."""
    return _missing("tar", "нет tar: шиму rsync нечем копировать дерево")


def no_world_traversable_tmp() -> str | None:
    """Системный tmp закрыт для чужого uid: проверка второго uid не имеет смысла."""
    return _missing(
        "world_traversable_tmp",
        "системный tmp недоступен для обхода чужим uid (закрытый предок)",
    )
