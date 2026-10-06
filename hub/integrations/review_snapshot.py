"""Снимок исходников локального ревьюера: хабовая сторона (#1599).

Здесь только то, что делает ХАБ: решает, можно ли снимок давать, называет путь,
который увидит ревьюер, и при транспорте ``direct`` загружает ЗАЩИЩЁННЫЙ
распаковщик. Сам распаковщик (проверка враждебного tar, открытия
относительно каталогов, права) — файл ``deploy/review-runner/snapshot_unpack.py``,
установленный на хост в каталог root. Из этого пакета он НЕ импортируется:
пакет хаба хаб может изменить, а распаковщик — нет. Хаб грузит его по пути из
``LOCAL_REVIEW_SNAPSHOT_UNPACKER`` и только после проверки, что файл и каталоги
над ним принадлежат root и не доступны на запись никому другому.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
from typing import Any

from hub import config

# Имена протокола хаб↔служба; тест сверяет их с исходником службы.
SPOOL_SNAPSHOT = "src.tar"
JOB_VERSION_SNAPSHOT = 2

#: Подставляется в промт при сборке заказа и заменяется на фактический путь,
#: когда он известен (direct знает свой workdir только после mkdtemp).
PATH_PLACEHOLDER = "@@SNAPSHOT_DIR@@"
#: Путь снимка в контейнере враппера: контракт обёртки (deploy/review-runner/).
RUNNER_PATH = "/work/src"
#: Чем заменяется токен, если снимка в прогоне нет.
NO_SNAPSHOT_PATH = "(снимка в этом прогоне нет)"

#: Доверенный владелец распаковщика. Не настройка: хаб не должен иметь способа
#: объявить доверенным собственного пользователя. Тесты подменяют его явно.
TRUSTED_UID = 0

#: Группа каталога прогона имеет право читать снимок; тип «прочее» — нет.
_UPLOAD_PREFIX = ".upload-"


class SnapshotUnavailable(Exception):
    """Снимок дать нельзя; текст — причина для промта и карточки."""


def wanted() -> bool:
    """Включён ли снимок настройкой хаба (по умолчанию нет)."""
    return bool(config.LOCAL_REVIEW_SNAPSHOT)


def unpacker_problem(path: str, trusted_uid: int) -> str:
    """Почему файлу нельзя доверить проверку чужого tar, или ``""``.

    То же правило, что у службы: хаб — обычный пользователь, и файл или каталог
    под его записью подменили бы проверку целиком.
    """
    try:
        info = os.lstat(path)
    except OSError as exc:
        return f"распаковщик {path} не найден: {exc.strerror}"
    if not stat.S_ISREG(info.st_mode):
        return f"распаковщик {path} не обычный файл"
    if info.st_uid != trusted_uid:
        return (
            f"распаковщик {path} принадлежит uid {info.st_uid}, а должен "
            f"принадлежать root (uid {trusted_uid})"
        )
    if info.st_mode & 0o022:
        return f"распаковщик {path} доступен на запись группе или всем"
    directory = os.path.dirname(os.path.realpath(path))
    while True:
        dinfo = os.lstat(directory)
        if dinfo.st_uid not in (0, trusted_uid):
            return (
                f"каталог {directory} над распаковщиком принадлежит uid {dinfo.st_uid}"
            )
        if dinfo.st_mode & 0o022 and not dinfo.st_mode & stat.S_ISVTX:
            return f"каталог {directory} над распаковщиком доступен на запись"
        parent = os.path.dirname(directory)
        if parent == directory:
            return ""
        directory = parent


def load_unpacker() -> Any:
    """Защищённый распаковщик для direct. ``SnapshotUnavailable`` — нельзя."""
    path = (config.LOCAL_REVIEW_SNAPSHOT_UNPACKER or "").strip()
    if not path:
        raise SnapshotUnavailable(
            "LOCAL_REVIEW_SNAPSHOT_UNPACKER не задан: при direct снимок "
            "распаковывает только защищённый модуль"
        )
    problem = unpacker_problem(path, TRUSTED_UID)
    if problem:
        raise SnapshotUnavailable(problem)
    spec = importlib.util.spec_from_file_location("haiplane_snapshot_unpack", path)
    if spec is None or spec.loader is None:
        raise SnapshotUnavailable(f"распаковщик {path} не загружается")
    module = importlib.util.module_from_spec(spec)
    sys.modules["haiplane_snapshot_unpack"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - любая неудача загрузки = нет снимка
        raise SnapshotUnavailable(f"распаковщик {path} не загружен: {exc}") from exc
    return module


def blocker(transport: str) -> str:
    """Причина, по которой снимок на этом транспорте дать нельзя, или ``""``.

    Только то, что известно хабу ДО прогона. При runner хаб не знает версию
    службы: это узнаётся ответом службы (старая отклоняет version=2).
    """
    if transport == "direct":
        try:
            load_unpacker()
        except SnapshotUnavailable as exc:
            return str(exc)
    return ""


def lay_out_direct(workdir: str, data: bytes, scratch: str) -> str:
    """Разложить снимок в ``workdir`` защищённым распаковщиком; ``""`` — готово.

    Непустая строка — отказ распаковщика (враждебный tar) или невозможность
    его загрузить: модель тогда не запускается. Архив пишется ВНЕ рабочего
    каталога (он закрывается на запись) и удаляется сразу.
    """
    try:
        unpacker = load_unpacker()
    except SnapshotUnavailable as exc:
        return str(exc)
    limits = unpacker.Limits(max_bytes=config.LOCAL_REVIEW_SNAPSHOT_MAX_BYTES)
    fd, tar_path = tempfile.mkstemp(prefix=_UPLOAD_PREFIX, dir=scratch)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        gid = os.stat(scratch).st_gid
        unpacker.prepare_workdir(workdir, tar_path, limits, gid)
    except unpacker.SnapshotRefused as exc:
        return f"снимок отвергнут: {exc.reason}"
    except OSError as exc:
        return f"снимок отвергнут: распаковка не удалась: {exc.strerror or exc}"
    finally:
        try:
            os.unlink(tar_path)
        except OSError:
            pass
    return ""


def remove_workdir(workdir: str, with_snapshot: bool) -> None:
    """Снять каталог прогона; со снимком — не следуя ссылкам и с правами."""
    import shutil

    if with_snapshot:
        try:
            if load_unpacker().remove_tree(workdir):
                return
        except SnapshotUnavailable:
            pass
    shutil.rmtree(workdir, ignore_errors=True)


def resolve_path(prompt: str, path: str) -> str:
    """Подставить в промт фактический путь снимка (или «нет снимка»)."""
    return prompt.replace(PATH_PLACEHOLDER, path or NO_SNAPSHOT_PATH)
