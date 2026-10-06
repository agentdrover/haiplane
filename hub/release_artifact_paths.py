"""Серверные копии артефактов деплоя: границы путей и хэш файла (#1591).

Лист без зависимостей от остального хаба: им пользуются и валидатор политики
(``hub.models``), и читатель на момент релиза (``hub.services.release_artifacts``).
Хаб вычисляет ТОЛЬКО sha256 и содержимое никуда не пишет и не возвращает.

Защита стоит в двух местах намеренно. Валидатор отказывает при записи политики,
а ``server_sha256`` проверяет заново при каждом чтении: сохранённая политика
могла лечь до смены списка каталогов, а файл — подмениться ссылкой после записи.
"""

from __future__ import annotations

import hashlib
import os
import stat

_CHUNK = 1 << 20
MAX_SERVER_FILE_BYTES = 256 * 1024 * 1024


def _parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def server_path_problem(path: object, dirs: tuple[str, ...] | list[str]) -> str:
    """Почему ``path`` не годится как серверный путь; пустая строка — годится.

    Чисто лексическая проверка: абсолютный путь без ``.``, ``..`` и пустых
    компонентов, строго ВНУТРИ одного из каталогов по границам компонентов
    (``/usr/local/sbinX`` — не внутри ``/usr/local/sbin``).
    """
    if not isinstance(path, str) or not path.strip():
        return "server_path must be a non-empty string"
    if "\x00" in path:
        return "server_path must not contain NUL"
    if not path.startswith("/"):
        return "server_path must be absolute"
    components = path.split("/")[1:]
    if any(c in ("", ".", "..") for c in components):
        return "server_path must not contain '..', '.', empty components or a trailing slash"
    if _matching_dir(path, dirs) is None:
        return (
            "server_path must be inside an allowed directory "
            f"({', '.join(dirs) or 'none configured'})"
        )
    return ""


def _matching_dir(path: str, dirs: tuple[str, ...] | list[str]) -> str | None:
    """Самый длинный разрешённый каталог, строго содержащий ``path``."""
    target = _parts(path)
    best: str | None = None
    for directory in dirs:
        if not isinstance(directory, str) or not directory.startswith("/"):
            continue
        base = _parts(directory)
        if len(target) > len(base) and target[: len(base)] == base:
            if best is None or len(base) > len(_parts(best)):
                best = directory
    return best


def server_sha256(
    path: str, dirs: tuple[str, ...] | list[str]
) -> tuple[str | None, str]:
    """(sha256 hex | None, причина). Причина пуста, когда хэш получен.

    Пустой путь, ``..``, чужой каталог, symlink в пути ниже разрешённого
    каталога или в самом файле, не обычный файл — отказ БЕЗ открытия файла.
    Открытие — ``O_NOFOLLOW``, затем ``fstat`` сверяется с ``lstat`` до чтения
    и ``lstat`` после: подмена между проверкой и чтением видна как отказ.
    """
    problem = server_path_problem(path, dirs)
    if problem:
        return None, problem
    base = _matching_dir(path, dirs)
    if base is None:  # unreachable after server_path_problem; kept as a refusal
        return None, "server_path вне разрешённых каталогов"
    relative = _parts(path)[len(_parts(base)) :]
    current = os.path.realpath(base)
    try:
        for name in relative[:-1]:
            current = os.path.join(current, name)
            mode = os.lstat(current).st_mode
            if stat.S_ISLNK(mode):
                return None, f"symlink в пути: {current}"
            if not stat.S_ISDIR(mode):
                return None, f"не каталог в пути: {current}"
        final = os.path.join(current, relative[-1])
        before = os.lstat(final)
        if stat.S_ISLNK(before.st_mode):
            return None, "файл — symlink"
        if not stat.S_ISREG(before.st_mode):
            return None, "не обычный файл"
        if before.st_size > MAX_SERVER_FILE_BYTES:
            return None, "файл слишком большой"
        fd = os.open(final, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_ino, opened.st_dev) != (
                before.st_ino,
                before.st_dev,
            ):
                return None, "файл подменён во время чтения"
            digest = hashlib.sha256()
            while chunk := os.read(fd, _CHUNK):
                digest.update(chunk)
        finally:
            os.close(fd)
        after = os.lstat(final)
        if (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev):
            return None, "файл подменён во время чтения"
        return digest.hexdigest(), ""
    except FileNotFoundError:
        return None, "файл отсутствует"
    except PermissionError:
        return None, "нет прав на чтение"
    except OSError as exc:
        return None, f"ошибка чтения: {exc.strerror or type(exc).__name__}"


def repo_path_problem(path: object) -> str:
    """Путь файла в репо: относительный, без ``..`` и пустых компонентов."""
    if not isinstance(path, str) or not path.strip():
        return "repo_path must be a non-empty string"
    if "\x00" in path or path.startswith("/") or path.startswith(":"):
        return "repo_path must be relative"
    if any(c in ("", ".", "..") for c in path.split("/")):
        return "repo_path must not contain '..', '.', or empty components"
    return ""
