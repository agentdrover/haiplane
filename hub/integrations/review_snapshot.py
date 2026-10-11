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

import os
import stat
import sys
import tempfile
import types
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


def read_trusted_source(path: str, trusted_uid: int) -> tuple[bytes, str]:
    """Прочитать файл распаковщика ТЕМ ЖЕ проходом, что и проверка: ``(байты, причина)``.

    Раньше проверялась цепочка после ``realpath``, а загрузка шла по исходному
    пути: ссылка-алиас в пути переключалась между проверкой и загрузкой
    (находка Codex, TOCTOU). Теперь путь абсолютный и без ссылок: каждый
    каталог открывается от корня ``O_NOFOLLOW|O_DIRECTORY`` от дескриптора
    предка, владелец и режим судятся по ``fstat`` ЭТОГО дескриптора, файл
    открывается так же и читается из него. Исполняется прочитанное, а не путь
    повторно. Любая ссылка в пути — отказ. Доверяем только владельцу
    ``trusted_uid`` (root): хаб и служба — один пользователь, и «владеет
    служба» ничего не защищает; каталог с записью группе или всем допустим
    только с sticky-битом (/tmp).
    """
    if not os.path.isabs(path) or any(
        p in ("", ".", "..") for p in path.split("/")[1:]
    ):
        return (
            b"",
            f"распаковщик {path}: нужен абсолютный путь без «.», «..» и пустых компонентов",
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    parts = [p for p in path.split("/") if p]
    fds: list[int] = []
    try:
        current = os.open("/", flags | os.O_DIRECTORY)
        fds.append(current)
        walked = ""
        for name in parts[:-1]:
            walked += "/" + name
            try:
                current = os.open(name, flags | os.O_DIRECTORY, dir_fd=current)
            except OSError as exc:
                return (
                    b"",
                    f"каталог {walked} в пути распаковщика не открыт без следования по ссылке: {exc.strerror}",
                )
            fds.append(current)
            dinfo = os.fstat(current)
            if dinfo.st_uid not in (0, trusted_uid):
                return (
                    b"",
                    (
                        f"каталог {walked} над распаковщиком принадлежит uid "
                        f"{dinfo.st_uid}, а должен принадлежать root (uid {trusted_uid})"
                    ),
                )
            if dinfo.st_mode & 0o022 and not dinfo.st_mode & stat.S_ISVTX:
                return b"", f"каталог {walked} над распаковщиком доступен на запись"
        try:
            fd = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=current)
        except FileNotFoundError:
            return b"", f"распаковщик {path} не найден"
        except OSError as exc:
            return b"", f"распаковщик {path} не обычный файл (ссылка?): {exc.strerror}"
        fds.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return b"", f"распаковщик {path} не обычный файл (ссылка?)"
        if info.st_uid != trusted_uid:
            return b"", (
                f"распаковщик {path} принадлежит uid {info.st_uid}, а должен "
                f"принадлежать root (uid {trusted_uid}): иначе хаб, работающий "
                "под тем же пользователем, мог бы его заменить"
            )
        if info.st_mode & 0o022:
            return b"", f"распаковщик {path} доступен на запись группе или всем"
        if info.st_size > 1 << 20:
            return b"", f"распаковщик {path} подозрительно велик"
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks), ""
    except OSError as exc:
        return b"", f"распаковщик {path} не прочитан: {exc.strerror}"
    finally:
        for fd in fds:
            os.close(fd)


def unpacker_problem(path: str, trusted_uid: int) -> str:
    """Почему этому файлу нельзя доверить проверку чужого tar, или ``""``."""
    return read_trusted_source(path, trusted_uid)[1]


def load_unpacker() -> Any:
    """Защищённый распаковщик для direct. ``SnapshotUnavailable`` — нельзя."""
    path = (config.LOCAL_REVIEW_SNAPSHOT_UNPACKER or "").strip()
    if not path:
        raise SnapshotUnavailable(
            "LOCAL_REVIEW_SNAPSHOT_UNPACKER не задан: при direct снимок "
            "распаковывает только защищённый модуль"
        )
    source, problem = read_trusted_source(path, TRUSTED_UID)
    if problem:
        raise SnapshotUnavailable(problem)
    module = types.ModuleType("haiplane_snapshot_unpack")
    module.__file__ = path
    sys.modules["haiplane_snapshot_unpack"] = module
    try:
        exec(compile(source, path, "exec"), module.__dict__)  # noqa: S102  # nosec B102 - проверенный root-овый код
    except Exception as exc:  # noqa: BLE001 - любая неудача загрузки = нет снимка
        raise SnapshotUnavailable(f"распаковщик {path} не загружен: {exc}") from exc
    return module


def blocker(transport: str) -> str:
    """Причина, по которой снимок на этом транспорте дать нельзя, или ``""``.

    Только то, что известно хабу ДО прогона. При runner хаб не знает версию
    службы: это узнаётся ответом службы (старая отклоняет version=2).
    """
    if transport == "direct":
        if (
            _is_container_wrapper()
            and not (config.LOCAL_REVIEW_SNAPSHOT_PATH or "").strip()
        ):
            return (
                "песочница direct — контейнерная обёртка haiplane-review-run: она "
                "монтирует снимок в /work/src, а промту без явной настройки "
                "достался бы хостовый путь. Задайте LOCAL_REVIEW_SNAPSHOT_PATH="
                f"{RUNNER_PATH}"
            )
        try:
            load_unpacker()
        except SnapshotUnavailable as exc:
            return str(exc)
    return ""


def _is_container_wrapper() -> bool:
    """Последний токен песочницы — обёртка репозитория (контейнерный запуск)."""
    import shlex

    try:
        parts = shlex.split(config.LOCAL_REVIEW_SANDBOX or "")
    except ValueError:
        return False
    return bool(parts) and os.path.basename(parts[-1]) == "haiplane-review-run"


def lay_out_direct(
    workdir: str, data: bytes, scratch: str, reviewer_uid: int | None
) -> str:
    """Разложить снимок в ``workdir`` защищённым распаковщиком; ``""`` — готово.

    Непустая строка — отказ распаковщика (враждебный tar) или невозможность
    его загрузить: модель тогда не запускается. Архив пишется ВНЕ рабочего
    каталога (он закрывается на запись) и удаляется сразу.
    """
    if reviewer_uid is None:
        return (
            "снимок отвергнут: пользователь ревьюера из песочницы не разрешился, "
            "проверить каталог прогонов нельзя"
        )
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
        unpacker.prepare_workdir(workdir, tar_path, limits, gid, reviewer_uid)
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


def resolve_path(prompt: str, marker: str, path: str) -> str:
    """Подставить фактический путь снимка в ТОТ маркер, что вставил заказ.

    Маркер у каждого заказа свой (случайный): буквальный ``@@SNAPSHOT_DIR@@`` в
    диффе — это данные ревьюера, и глобальная замена испортила бы их (находка
    Codex). Нет маркера (снимка не заказывали) — промт возвращается как есть,
    байт в байт.
    """
    if not marker:
        return prompt
    return prompt.replace(marker, path or NO_SNAPSHOT_PATH)


def reviewer_visible_path(workdir: str) -> str:
    """Путь снимка глазами ревьюера при direct.

    По умолчанию — ``<workdir>/src`` (ревьюер работает на файловой системе
    хоста). Если песочница — контейнерный враппер, он монтирует снимок в другое
    место, и оператор называет это явно настройкой
    ``LOCAL_REVIEW_SNAPSHOT_PATH`` (для обёртки репозитория — ``/work/src``).
    """
    named = (config.LOCAL_REVIEW_SNAPSHOT_PATH or "").strip()
    return named or os.path.join(workdir, "src")
