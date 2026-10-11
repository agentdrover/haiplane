"""Защищённый распаковщик снимка исходников для локального ревьюера (#1599).

Где он живёт и почему. Снимок — это ``src.tar``, который кладёт в spool ХАБ, а
хаб открыт в интернет: всё, что в spool, считается чужим вводом. Поэтому
проверка и распаковка выполняются кодом, который хаб изменить НЕ МОЖЕТ: этот
файл ставится рядом со службой-исполнителем в каталог, принадлежащий root и
недоступный на запись ни хабу, ни службе (deploy/LOCAL-REVIEW.md, «Установка
распаковщика»). Служба и direct-транспорт хаба грузят его ПО ЯВНОМУ ПУТИ и
перед загрузкой проверяют, что файл и каталог не доступны на запись никому,
кроме root. Из spool, cwd и клона он не импортируется никогда.

Чего здесь нет намеренно: импортов из ``hub``. Установленная служба — два
файла на хосте без пакета хаба; тест запускает её именно так.

Что принимается. Только НЕСЖАТЫЙ ustar-tar из обычных файлов и каталогов, как
его кладёт ``git archive`` после перепаковки хабом. Отвергается всё остальное,
каждое — с названной причиной и ДО распаковки (полный проход по заголовкам):
сжатие и чужой формат, символьные и жёсткие ссылки, устройства и каналы,
sparse, любые PAX-переопределения (``x``: path, linkpath, size, sparse-поля),
GNU-расширения имён, повторные и конфликтующие пути, абсолютные пути,
``..``/``.``, NUL в имени, размер в двоичной кодировке, неверную контрольную
сумму, обрезанный архив и мусор после конца. Допускается единственный штатный
глобальный PAX-заголовок ``git archive`` (тип ``g``, первым, только ключ
``comment`` со значением из hex-символов). Потолки: размер архива и суммарный
логический объём, число записей (с неявными каталогами), глубина.

Как распаковывается. Архив копируется в приватный файл рабочего каталога
(0600) один раз, чтобы его нельзя было подменить между проходом проверки и
записью. Запись идёт в приватный каталог ``.src-staging`` (0700), недоступный
ревьюеру, ОТНОСИТЕЛЬНО дескрипторов каталогов: каждый компонент пути
открывается ``O_NOFOLLOW|O_DIRECTORY`` от дескриптора предка, файл создаётся
``O_CREAT|O_EXCL|O_NOFOLLOW``. uid, gid, режим и время из архива не
восстанавливаются: файлы получают 0440, каталоги 0550, группа — явно заданная.
Только после полной распаковки каталог переименовывается в ``src``, рядом
создаётся ``home`` (0770, единственное место, где ревьюер пишет), и сам
рабочий каталог закрывается на запись (0550): в нём нельзя ни создать соседа
``src``, ни переименовать ``src``.

Уборка. ``remove_tree`` снимает рабочий каталог, не следуя ссылкам:
дескрипторами, с восстановлением прав владельца у каталогов, которым они были
срезаны (0550 здесь намеренные).

Только стандартная библиотека.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
from typing import BinaryIO, NamedTuple

BLOCK = 512
STAGING = ".src-staging"
PRIVATE_COPY = ".snapshot.tar"
SRC_NAME = "src"
HOME_NAME = "home"
FILE_MODE = 0o440
DIR_MODE = 0o550
TOP_MODE = 0o550
HOME_MODE = 0o770

_NAME_MAX = 255
_PATH_MAX = 4096
_PAX_MAX = 1024
_COMMENT = re.compile(rb"^[0-9a-f]{40,64}$")
_COPY_CHUNK = 1 << 20


class SnapshotRefused(Exception):
    """Снимок отвергнут: причина уходит хабу в result.json, модель не стартует."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Limits:
    """Потолки распаковки. ``max_total_bytes`` по умолчанию равен ``max_bytes``.

    Обычный класс, а не dataclass: модуль грузится по пути (служба, direct,
    тесты), и dataclass с отложенными аннотациями требует, чтобы модуль был
    зарегистрирован в sys.modules, — лишнее условие для файла, который
    должен работать без них.
    """

    def __init__(
        self,
        max_bytes: int = 64 * 1024 * 1024,
        max_entries: int = 50_000,
        max_depth: int = 32,
        max_total_bytes: int | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.max_total_bytes = max_total_bytes

    @property
    def total(self) -> int:
        return self.max_bytes if self.max_total_bytes is None else self.max_total_bytes


class _Entry(NamedTuple):
    parts: tuple[bytes, ...]
    is_dir: bool
    size: int
    offset: int


# ---------------------------------------------------------------- разбор tar


def _all_zero(block: bytes) -> bool:
    return not block.strip(b"\x00")


def _octal(field: bytes, what: str) -> int:
    if field[:1] and field[0] & 0x80:
        raise SnapshotRefused(f"{what}: двоичная кодировка (base-256) не допускается")
    text = field.strip(b"\x00 ")
    if not text:
        return 0
    if not re.fullmatch(rb"[0-7]+", text):
        raise SnapshotRefused(f"{what}: не восьмеричное число")
    return int(text, 8)


def _checked_header(block: bytes) -> None:
    if block[257:265] != b"ustar\x0000":
        raise SnapshotRefused(
            "формат: нужен несжатый ustar-tar (сжатый или иной архив не принимается)"
        )
    declared = _octal(block[148:156], "контрольная сумма")
    actual = sum(block[:148]) + 8 * 0x20 + sum(block[156:])
    if declared != actual:
        raise SnapshotRefused("контрольная сумма заголовка не сходится")


def _field_name(field: bytes) -> bytes:
    """Поле имени: NUL-дополнение допустимо, NUL в середине — нет."""
    head, nul, tail = field.partition(b"\x00")
    if nul and tail.strip(b"\x00"):
        raise SnapshotRefused("небезопасный путь: NUL внутри имени")
    return head


def _entry_name(block: bytes) -> bytes:
    name = _field_name(block[0:100])
    prefix = _field_name(block[345:500])
    return prefix + b"/" + name if prefix else name


def _split_path(raw: bytes, is_dir: bool, limits: Limits) -> tuple[bytes, ...]:
    if not raw:
        raise SnapshotRefused("небезопасный путь: пустое имя")
    if raw.startswith(b"/"):
        raise SnapshotRefused("небезопасный путь: абсолютный")
    if is_dir and raw.endswith(b"/"):
        raw = raw[:-1]
    if not raw or len(raw) > _PATH_MAX:
        raise SnapshotRefused("небезопасный путь: пустой или слишком длинный")
    parts = tuple(raw.split(b"/"))
    for part in parts:
        if not part or part in (b".", b"..") or len(part) > _NAME_MAX:
            raise SnapshotRefused("небезопасный путь: пустой, «.» или «..» компонент")
    if len(parts) > limits.max_depth:
        raise SnapshotRefused(f"глубина пути больше {limits.max_depth}")
    return parts


def _pax_global(data: bytes) -> None:
    """Единственный допустимый PAX: глобальный ``comment=<hex>`` от git archive."""
    pos = 0
    if not data or len(data) > _PAX_MAX:
        raise SnapshotRefused("PAX-заголовок недопустимого размера")
    while pos < len(data):
        space = data.find(b" ", pos)
        length_text = data[pos:space] if space > pos else b""
        if not length_text.isdigit():
            raise SnapshotRefused("PAX-запись не разобрана")
        end = pos + int(length_text)
        record = data[space + 1 : end]
        if end > len(data) or not record.endswith(b"\n"):
            raise SnapshotRefused("PAX-запись не разобрана")
        key, eq, value = record[:-1].partition(b"=")
        if not eq or key != b"comment" or not _COMMENT.fullmatch(value):
            raise SnapshotRefused(
                "PAX-переопределения запрещены: допустим только глобальный "
                "ключ comment=<sha> от git archive"
            )
        pos = end


class _Scan:
    """Накопитель полного прохода: пути, повторы, конфликты, потолки."""

    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.entries: list[_Entry] = []
        self.explicit: set[tuple[bytes, ...]] = set()
        self.kinds: dict[tuple[bytes, ...], str] = {}
        self.total = 0

    def add(
        self, parts: tuple[bytes, ...], is_dir: bool, size: int, offset: int
    ) -> None:
        if parts in self.explicit:
            raise SnapshotRefused("повторный путь в архиве")
        if self.kinds.get(parts) == "dir" and not is_dir:
            raise SnapshotRefused("конфликт путей: файл поверх каталога")
        for depth in range(1, len(parts)):
            parent = parts[:depth]
            if self.kinds.get(parent) == "file":
                raise SnapshotRefused("конфликт путей: каталог внутри файла")
            self.kinds.setdefault(parent, "dir")
        self.explicit.add(parts)
        self.kinds[parts] = "dir" if is_dir else "file"
        self.total += size
        if self.total > self.limits.total:
            raise SnapshotRefused(
                f"суммарный объём файлов больше {self.limits.total} байт"
            )
        if len(self.kinds) > self.limits.max_entries:
            raise SnapshotRefused(f"записей больше {self.limits.max_entries}")
        self.entries.append(_Entry(parts, is_dir, size, offset))


def _read_block(fh: BinaryIO, pos: int) -> bytes:
    fh.seek(pos)
    block = fh.read(BLOCK)
    if len(block) < BLOCK:
        raise SnapshotRefused("архив обрезан: заголовок неполный")
    return block


def _finish_archive(fh: BinaryIO, pos: int) -> None:
    """Конец архива: второй нулевой блок, дальше только нули."""
    if _read_block(fh, pos) != b"\x00" * BLOCK:
        raise SnapshotRefused("архив обрезан: нет завершающего нулевого блока")
    fh.seek(pos + BLOCK)
    while chunk := fh.read(_COPY_CHUNK):
        if chunk.strip(b"\x00"):
            raise SnapshotRefused("данные после конца архива")


def _typed_entry(block: bytes, scan: _Scan, pos: int, file_size: int) -> int:
    """Один заголовок: записать в scan, вернуть позицию следующего. -1 — ``g``."""
    flag = block[156:157]
    size = _octal(block[124:136], "размер")
    if flag in (b"2", b"1"):
        raise SnapshotRefused("ссылки (symlink/hardlink) в снимке запрещены")
    if flag == b"S":
        raise SnapshotRefused("sparse-записи запрещены")
    if flag == b"x" or flag == b"g":
        raise SnapshotRefused("PAX-переопределения запрещены (запись типа x)")
    if flag not in (b"0", b"\x00", b"5"):
        raise SnapshotRefused(f"недопустимый тип записи {flag!r}")
    is_dir = flag == b"5"
    if is_dir and size:
        raise SnapshotRefused("размер каталога должен быть нулём")
    raw = _entry_name(block)
    if not is_dir and raw.endswith(b"/"):
        raise SnapshotRefused("небезопасный путь: файл с косой чертой на конце")
    parts = _split_path(raw, is_dir, scan.limits)
    data_at = pos + BLOCK
    nxt = data_at + (size + BLOCK - 1) // BLOCK * BLOCK
    if data_at + size > file_size:
        raise SnapshotRefused("архив обрезан: данных меньше, чем в заголовке")
    scan.add(parts, is_dir, size, data_at)
    return nxt


_COMPRESSED = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ", b"\x28\xb5\x2f\xfd")


def scan_archive(fh: BinaryIO, file_size: int, limits: Limits) -> list[_Entry]:
    """Полный проход по заголовкам БЕЗ записи на диск; отказ — SnapshotRefused."""
    fh.seek(0)
    if fh.read(6).startswith(_COMPRESSED):
        raise SnapshotRefused("формат: сжатый архив не принимается, нужен несжатый tar")
    scan = _Scan(limits)
    pos = 0
    first = True
    while True:
        block = _read_block(fh, pos)
        if _all_zero(block):
            _finish_archive(fh, pos + BLOCK)
            return scan.entries
        _checked_header(block)
        if block[156:157] == b"g" and first:
            size = _octal(block[124:136], "размер")
            if size > _PAX_MAX or pos + BLOCK + size > file_size:
                raise SnapshotRefused("PAX-заголовок недопустимого размера")
            fh.seek(pos + BLOCK)
            _pax_global(fh.read(size))
            pos += BLOCK + (size + BLOCK - 1) // BLOCK * BLOCK
        else:
            pos = _typed_entry(block, scan, pos, file_size)
        first = False


# ---------------------------------------------------------------- файловая часть


def _open_dir(path: str, dir_fd: int | None = None) -> int:
    return os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=dir_fd,
    )


def _set_group(fd: int, gid: int | None) -> None:
    if gid is None or os.fstat(fd).st_gid == gid:
        return
    try:
        os.fchown(fd, -1, gid)
    except OSError as exc:
        raise SnapshotRefused(f"группа {gid} не задана: {exc.strerror}") from exc


class _Walker:
    """Идёт по путям от дескрипторов; держит только цепочку последнего пути."""

    def __init__(self, root_fd: int) -> None:
        self.chain: list[tuple[bytes, int]] = []
        self.root = root_fd
        self.made: set[tuple[bytes, ...]] = set()

    def _pop_to(self, depth: int) -> None:
        while len(self.chain) > depth:
            os.close(self.chain.pop()[1])

    def parent_fd(self, parts: tuple[bytes, ...]) -> int:
        dirs = parts[:-1]
        common = 0
        while (
            common < len(self.chain)
            and common < len(dirs)
            and self.chain[common][0] == dirs[common]
        ):
            common += 1
        self._pop_to(common)
        for depth in range(common, len(dirs)):
            at = self.chain[-1][1] if self.chain else self.root
            self.mkdir(dirs[: depth + 1], at)
            self.chain.append((dirs[depth], _open_dir(os.fsdecode(dirs[depth]), at)))
        return self.chain[-1][1] if self.chain else self.root

    def mkdir(self, parts: tuple[bytes, ...], at: int) -> None:
        if parts in self.made:
            return
        os.mkdir(os.fsdecode(parts[-1]), 0o700, dir_fd=at)
        self.made.add(parts)

    def close(self) -> None:
        self._pop_to(0)


def _write_file(src_fh: BinaryIO, entry: _Entry, parent: int, gid: int | None) -> None:
    fd = os.open(
        os.fsdecode(entry.parts[-1]),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent,
    )
    try:
        src_fh.seek(entry.offset)
        left = entry.size
        while left:
            chunk = src_fh.read(min(left, _COPY_CHUNK))
            if not chunk:
                raise SnapshotRefused("архив обрезан во время распаковки")
            view = memoryview(chunk)
            while view:
                view = view[os.write(fd, view) :]
            left -= len(chunk)
        _set_group(fd, gid)
        os.fchmod(fd, FILE_MODE)
    finally:
        os.close(fd)


def _extract(
    src_fh: BinaryIO, entries: list[_Entry], root_fd: int, gid: int | None
) -> None:
    walker = _Walker(root_fd)
    try:
        for entry in entries:
            parent = walker.parent_fd(entry.parts)
            if entry.is_dir:
                walker.mkdir(entry.parts, parent)
            else:
                _write_file(src_fh, entry, parent, gid)
    finally:
        walker.close()


def _finalize(dir_fd: int, gid: int | None) -> None:
    """Обойти дерево: убедиться, что в нём только файлы и каталоги, и закрыть права."""
    for name in os.listdir(dir_fd):
        info = os.lstat(name, dir_fd=dir_fd)
        if stat.S_ISDIR(info.st_mode):
            child = _open_dir(name, dir_fd)
            try:
                _finalize(child, gid)
            finally:
                os.close(child)
        elif not stat.S_ISREG(info.st_mode):
            raise SnapshotRefused("в распакованном дереве не файл и не каталог")
    _set_group(dir_fd, gid)
    os.fchmod(dir_fd, DIR_MODE)


def _copy_private(tar_path: str, top_fd: int, limits: Limits) -> int:
    """Копия src.tar в приватный файл; ``-> размер``. Ссылка и не-файл — отказ."""
    try:
        # O_NONBLOCK: FIFO без писателя иначе вешает open() навсегда, до fstat.
        src = os.open(
            tar_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        )
    except OSError as exc:
        raise SnapshotRefused(
            f"src.tar не открыт без следования по ссылке ({exc.strerror}): "
            "ссылка или не обычный файл"
        ) from exc
    try:
        info = os.fstat(src)
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotRefused("src.tar не обычный файл (ссылка или устройство)")
        if info.st_size > limits.max_bytes:
            raise SnapshotRefused(f"объём src.tar больше {limits.max_bytes} байт")
        dst = os.open(
            PRIVATE_COPY,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=top_fd,
        )
        copied = 0
        try:
            while chunk := os.read(src, _COPY_CHUNK):
                copied += len(chunk)
                if copied > limits.max_bytes:
                    raise SnapshotRefused(
                        f"объём src.tar больше {limits.max_bytes} байт"
                    )
                view = memoryview(chunk)
                while view:
                    view = view[os.write(dst, view) :]
        finally:
            os.close(dst)
        return copied
    finally:
        os.close(src)


def _require_private_top(top_fd: int) -> None:
    info = os.fstat(top_fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise SnapshotRefused(
            "рабочий каталог не приватен: нужен владелец службы и режим 0700"
        )


def _unpack_into(
    top_fd: int, tar_path: str, limits: Limits, gid: int | None
) -> dict[str, int]:
    size = _copy_private(tar_path, top_fd, limits)
    private = os.fdopen(
        os.open(
            PRIVATE_COPY, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=top_fd
        ),
        "rb",
    )
    try:
        entries = scan_archive(private, size, limits)
        os.mkdir(STAGING, 0o700, dir_fd=top_fd)
        stage = _open_dir(STAGING, top_fd)
        try:
            _extract(private, entries, stage, gid)
            _finalize(stage, gid)
        finally:
            os.close(stage)
    finally:
        private.close()
    os.unlink(PRIVATE_COPY, dir_fd=top_fd)
    os.rename(STAGING, SRC_NAME, src_dir_fd=top_fd, dst_dir_fd=top_fd)
    os.mkdir(HOME_NAME, HOME_MODE, dir_fd=top_fd)
    home = _open_dir(HOME_NAME, top_fd)
    try:
        _set_group(home, gid)
        os.fchmod(home, HOME_MODE)
    finally:
        os.close(home)
    _set_group(top_fd, gid)
    os.fchmod(top_fd, TOP_MODE)
    return {
        "entries": len(entries),
        "bytes": sum(e.size for e in entries),
    }


def scratch_problem(scratch: str, reviewer_uid: int) -> str:
    """Почему каталог прогонов не защищает ``<workdir>`` от подмены, или ``""``.

    Режим 0550 рабочего каталога не запрещает переименовать ЕГО САМ через
    родителя: ревьюер состоит в группе каталога прогонов и может
    ``mv "$W" "$W.saved"``, а потом подложить свой ``$W/src`` до запуска
    враппера (находка Codex). Закрывает это sticky-бит на родителе (в нём
    переименовать и удалить чужую запись может только её владелец или
    владелец каталога) при условии, что владелец каталога — НЕ сам ревьюер.
    Без обоих условий снимок не выдаётся.
    """
    try:
        info = os.lstat(scratch)
    except OSError as exc:
        return f"каталог прогонов {scratch} не прочитан: {exc.strerror}"
    if not stat.S_ISDIR(info.st_mode):
        return f"каталог прогонов {scratch} не каталог"
    if not info.st_mode & stat.S_ISVTX:
        return (
            f"на каталоге прогонов {scratch} нет sticky-бита (нужен режим 3770): "
            "ревьюер, состоящий в группе, мог бы переименовать рабочий каталог "
            "и подложить свой src"
        )
    if info.st_uid == reviewer_uid:
        return (
            f"каталог прогонов {scratch} принадлежит самому ревьюеру (uid "
            f"{reviewer_uid}): владелец каталога вправе переименовать любую "
            "запись в нём, sticky-бит его не ограничивает"
        )
    return ""


def prepare_workdir(
    workdir: str,
    tar_path: str,
    limits: Limits,
    gid: int | None = None,
    reviewer_uid: int | None = None,
) -> dict[str, int]:
    """Проверить ``tar_path`` и разложить его как ``<workdir>/src`` (+ ``home``).

    ``workdir`` обязан быть приватным (0700, владелец — вызывающий): до конца
    проверки ревьюеру в него хода нет. После успеха рабочий каталог закрыт на
    запись (0550), ``src`` — 0550/0440, ``home`` — 0770. При ЛЮБОМ отказе
    рабочий каталог остаётся пустым (частичная распаковка убрана) и
    поднимается ``SnapshotRefused``.
    """
    if reviewer_uid is not None:
        problem = scratch_problem(os.path.dirname(workdir.rstrip("/")), reviewer_uid)
        if problem:
            raise SnapshotRefused(problem)
    top = _open_dir(workdir)
    try:
        _require_private_top(top)
        try:
            return _unpack_into(top, tar_path, limits, gid)
        except SnapshotRefused:
            _clean_top(top)
            raise
        except OSError as exc:
            _clean_top(top)
            raise SnapshotRefused(
                f"распаковка не удалась: {exc.strerror or exc}"
            ) from exc
    finally:
        os.close(top)


def _clean_top(top_fd: int) -> None:
    os.fchmod(top_fd, 0o700)
    for name in os.listdir(top_fd):
        _remove_entry(top_fd, name)


def _owner_rwx(parent_fd: int, name: str, mode: int) -> None:
    """Вернуть владельцу rwx каталогу ``name``, НЕ следуя по ссылке.

    Каталог с режимом 0 не открыть на чтение даже владельцу, поэтому права
    возвращаются ДО открытия. ``chmod`` по имени пошёл бы за подменной ссылкой,
    а отказаться от следования ему нечем (на Linux ``AT_SYMLINK_NOFOLLOW`` у
    chmod нет): берётся дескриптор самой записи (``O_PATH|O_NOFOLLOW``),
    проверяется, что это каталог, и права ставятся через /proc/self/fd.
    """
    want = mode | stat.S_IRWXU
    if os.chmod in os.supports_follow_symlinks:
        os.chmod(name, want, dir_fd=parent_fd, follow_symlinks=False)
        return
    flag = getattr(os, "O_PATH", 0)
    if not flag:
        raise OSError(errno.ENOSYS, "chmod без следования по ссылке недоступен")
    fd = os.open(name, flag | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.ENOTDIR, "не каталог")
        os.chmod(f"/proc/self/fd/{fd}", want)
    finally:
        os.close(fd)


def _remove_entry(parent_fd: int, name: str) -> bool:
    """Убрать запись, не следуя ссылкам. ``True`` — убрана целиком."""
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except OSError:
        return True
    if not stat.S_ISDIR(info.st_mode):
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError:
            return False
        return True
    if stat.S_IMODE(info.st_mode) & stat.S_IRWXU != stat.S_IRWXU:
        with contextlib.suppress(OSError):
            _owner_rwx(parent_fd, name, stat.S_IMODE(info.st_mode))
    try:
        child = _open_dir(name, parent_fd)
    except OSError:
        return False
    clean = True
    try:
        try:
            names = os.listdir(child)
        except OSError:
            return False
        for inner in names:
            clean = _remove_entry(child, inner) and clean
    finally:
        os.close(child)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        return False
    return clean


def remove_tree(path: str) -> bool:
    """Снять каталог прогона: права восстанавливаются, ссылки не разыменовываются.

    Нужна потому, что ``src`` и рабочий каталог закрыты на запись намеренно, и
    обычный ``rmtree`` на них спотыкается. ``True`` — убрано целиком; остаток
    (чужой каталог с режимом 0 внутри ``home``) возвращает ``False``, и
    вызывающий называет его, а не молчит.
    """
    parent, name = os.path.split(path.rstrip("/"))
    try:
        parent_fd = _open_dir(parent or ".")
    except OSError as exc:
        return exc.errno == errno.ENOENT
    try:
        return _remove_entry(parent_fd, name)
    finally:
        os.close(parent_fd)
