"""Pre-deploy snapshot of the hub database (#1590).

Run by deploy/remote-deploy.sh as the hub's unix user
(``sudo -n -u <user> env ... bash -c 'exec "$0" -c "$1"' python <this text>``),
before anything on disk is replaced. Everything arrives in the environment:

  BACKUP_DB        effective database path (resolved by the caller; never guessed here)
  BACKUP_DIR       target directory; empty -> <dir of the database>/backups
  BACKUP_SHA       commit sha7 for the file name, or "manual"
  BACKUP_KEEP      how many predeploy-* files to keep (default 10)
  BACKUP_TIMEOUT   seconds for the whole step (default 300)
  BACKUP_LIVENESS  "1": stdin is a liveness channel; EOF = the deploy died, clean up and stop

The only thing printed to stdout is ONE result line:
  ok <file> size=<bytes> integrity=ok seconds=<N>
  skipped (<reason>)
  failed (<reason>)
Exit code 0 for ok/skipped, non-zero for failed. The database is opened
read-only (``mode=ro``, NOT ``immutable``: immutable ignores the WAL and would
lose committed rows that are still in it).
"""

import gzip
import hashlib
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse

PREFIX = "predeploy-"
TMP_PREFIX = ".predeploy-"
NAME_RE = re.compile(r"^predeploy-\d{8}T\d{6}Z-[0-9a-z]+(?:-\d+)?\.db\.gz$")

_tmp_paths = []
_lock = threading.Lock()
_armed = True


def _one_line(text):
    return " ".join(str(text).split())[:300]


def _cleanup():
    for path in list(_tmp_paths):
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def finish(code, line):
    """Print the single result line, remove partial files, leave at once."""
    with _lock:
        _cleanup()
        sys.stdout.write(_one_line(line) + "\n")
        sys.stdout.flush()
        os._exit(code)


def abort(code, line):
    """Called from helper threads: once the file is published it is a no-op."""
    if _armed:
        finish(code, line)


def fail(reason):
    finish(1, "failed (%s)" % _one_line(reason))


def _int_env(name, default):
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _watch_stdin():
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except Exception:  # noqa: BLE001 - EOF and a broken pipe mean the same
        pass
    abort(143, "failed (деплой оборван: сигнал или обрыв канала, снимок прерван)")


def _ensure_dir(path):
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        info = os.lstat(path)
    except OSError as exc:
        fail("каталог бэкапов %s недоступен: %s" % (path, exc.strerror))
    import stat

    if not stat.S_ISDIR(info.st_mode):
        fail("%s не каталог" % path)
    if info.st_uid != os.geteuid():
        fail("каталог %s принадлежит не пользователю хаба" % path)
    if stat.S_IMODE(info.st_mode) != 0o700:
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            fail("не удалось выставить 0700 на %s: %s" % (path, exc.strerror))


def _new_tmp(directory, suffix):
    import tempfile

    fd, path = tempfile.mkstemp(prefix=TMP_PREFIX, suffix=suffix, dir=directory)
    os.close(fd)  # mkstemp creates the file 0600
    _tmp_paths.append(path)
    return path


def _copy_database(db, raw):
    uri = "file:%s?mode=ro" % urllib.parse.quote(db)
    src = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst, pages=1024)
            # The copy is a self-contained rollback-journal file: no -wal/-shm
            # beside it, nothing to carry to a restore.
            dst.execute("PRAGMA journal_mode=DELETE").fetchall()
            rows = dst.execute("PRAGMA integrity_check").fetchall()
        finally:
            dst.close()
    finally:
        src.close()
    if rows != [("ok",)]:
        fail("integrity_check копии: %s" % _one_line(rows[:3]))


def _compress(raw, gz):
    digest = hashlib.sha256()
    with open(raw, "rb") as source, open(gz, "wb") as target:
        with gzip.GzipFile(fileobj=target, mode="wb") as packed:
            while True:
                chunk = source.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                packed.write(chunk)
        target.flush()
        os.fsync(target.fileno())
    check = hashlib.sha256()
    with gzip.open(gz, "rb") as packed:
        while True:
            chunk = packed.read(1 << 20)
            if not chunk:
                break
            check.update(chunk)
    if check.digest() != digest.digest():
        fail("архив не совпал с копией после сжатия")


def _publish(gz, directory, stem):
    """Atomic: the final name appears only complete, an existing one is never replaced."""
    for attempt in range(1, 100):
        name = "%s.db.gz" % stem if attempt == 1 else "%s-%d.db.gz" % (stem, attempt)
        final = os.path.join(directory, name)
        try:
            os.link(gz, final)
        except FileExistsError:
            continue
        return final
    fail("не удалось подобрать уникальное имя файла")


def _retention(directory, keep):
    """Only after a successful publish; only predeploy-* names (nightly files are not ours)."""
    names = sorted(n for n in os.listdir(directory) if NAME_RE.match(n))
    for name in names[:-keep] if len(names) > keep else []:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass
    cutoff = time.time() - 3600
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if name.startswith(TMP_PREFIX) and path not in _tmp_paths:
            try:
                if os.lstat(path).st_mtime < cutoff:
                    os.unlink(path)
            except OSError:
                pass


def main():
    global _armed
    started = time.time()
    db = os.environ.get("BACKUP_DB", "")
    if not db:
        fail("путь базы не передан")
    timeout = _int_env("BACKUP_TIMEOUT", 300)
    keep = _int_env("BACKUP_KEEP", 10)
    sha = (os.environ.get("BACKUP_SHA") or "manual").lower()
    if not re.match(r"^[0-9a-z]{1,40}$", sha):
        sha = "manual"

    if os.environ.get("BACKUP_LIVENESS") == "1":
        threading.Thread(target=_watch_stdin, daemon=True).start()
    timer = threading.Timer(
        timeout, abort, (124, "failed (превышено время шага: %d с)" % timeout)
    )
    timer.daemon = True
    timer.start()

    try:
        info = os.stat(db)
    except FileNotFoundError:
        finish(0, "skipped (базы нет по пути %s: первая установка)" % db)
    except OSError as exc:
        fail("путь базы %s не читается: %s" % (db, exc.strerror))
    import stat

    if not stat.S_ISREG(info.st_mode):
        fail("%s не обычный файл" % db)

    directory = os.environ.get("BACKUP_DIR") or os.path.join(
        os.path.dirname(db), "backups"
    )
    os.umask(0o077)
    _ensure_dir(directory)

    raw = _new_tmp(directory, ".db.tmp")
    gz = _new_tmp(directory, ".gz.tmp")
    try:
        _copy_database(db, raw)
    except sqlite3.Error as exc:
        fail("снятие копии: %s" % exc)
    try:
        _compress(raw, gz)
    except OSError as exc:
        fail("сжатие: %s" % exc)

    stem = "%s%s-%s" % (PREFIX, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), sha)
    final = _publish(gz, directory, stem)
    _armed = False
    timer.cancel()
    _cleanup()
    _tmp_paths[:] = []
    _retention(directory, keep)
    size = os.path.getsize(final)
    seconds = int(time.time() - started)
    finish(0, "ok %s size=%d integrity=ok seconds=%d" % (final, size, seconds))


try:
    main()
except SystemExit:
    raise
except BaseException as exc:  # noqa: BLE001 - any surprise is a failed backup, not a silent pass
    fail("%s: %s" % (type(exc).__name__, exc))
