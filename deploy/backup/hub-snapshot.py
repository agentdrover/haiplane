#!/usr/bin/env python3
"""Off-server copies of the hub (#1707): hourly database snapshot, daily settings archive.

Run by root units (deploy/backup/install.sh) as ``hub-snapshot.py hourly|daily``.
Standard library only: the host python3 is enough. Everything is encrypted HERE,
with the public age recipient from a file; the private key never exists on the
server. Plain text exists only in the tmpfs work directory (hourly) or in the
pipe to ``age`` (daily) -- never on the disk.

Published (atomically, file first and meta second) into the state directory:
  hub-<UTC>.db.age    + hub-<UTC>.meta.json     (hourly, last 3 kept)
  conf-<UTC>.tar.age  + conf-<UTC>.meta.json    (daily,  last 2 kept)

The only thing written to stdout is ONE journal line:
  ok kind=hourly name=... bytes=N
  skipped kind=hourly reason=lock_busy
  failed kind=hourly reason=<code>
Exit code: 0 ok or skipped, 1 failed. Reasons are short codes: no tool output,
no paths of secrets.

Configuration comes from the unit's environment (production defaults below);
the client of the ssh service cannot set any of it.
"""

import datetime
import errno
import fcntl
import glob
import grp
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import tarfile

HOURLY_KEEP = 3
DAILY_KEEP = 2
MANIFEST_NAME = "MANIFEST.json"
SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
ENV_FILE_RE = re.compile(r"(\S+) \(ignore_errors=(yes|no)\)")

KINDS = {
    "hourly": {"prefix": "hub", "suffix": ".db.age", "keep": HOURLY_KEEP},
    "daily": {"prefix": "conf", "suffix": ".tar.age", "keep": DAILY_KEEP},
}


class Fail(Exception):
    """A snapshot that must not be published; ``code`` goes to the journal."""

    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


class Skip(Exception):
    """Nothing to do this time (the lock is held by another run)."""

    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


class Config(object):
    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.state = e.get("SNAP_STATE_DIR") or "/var/lib/haiplane-backup"
        self.work = e.get("SNAP_WORK_DIR") or os.path.join(self.state, "work")
        self.recipient = (
            e.get("SNAP_RECIPIENT_FILE") or "/etc/haiplane-backup/recipient.txt"
        )
        self.paths_conf = e.get("SNAP_PATHS_CONF") or "/etc/haiplane-backup/paths.conf"
        self.deploy_sha = (
            e.get("SNAP_DEPLOY_SHA_FILE") or "/opt/haiplane-hub/src/.deploy-sha"
        )
        self.root = e.get("SNAP_ROOT") or "/"
        self.age = e.get("SNAP_AGE") or "age"
        self.systemctl = e.get("SNAP_SYSTEMCTL") or "systemctl"
        self.db = e.get("SNAP_DB") or ""
        self.group = e.get("SNAP_GROUP", "backup")
        self.require_tmpfs = e.get("SNAP_REQUIRE_TMPFS", "1") != "0"
        self.lock_wait = float(e.get("SNAP_LOCK_WAIT") or 120)


# Replaced by tests: time must be a parameter of the run, not a wait.
def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


_monotonic = time.monotonic
_sleep = time.sleep


def _stamp(now):
    return now.strftime("%Y%m%dT%H%M%SZ")


def _host(cfg, path):
    if cfg.root == "/":
        return path
    return os.path.join(cfg.root, path.lstrip("/"))


# ---------------------------------------------------------------- plumbing


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _set_perms(cfg, path):
    """0640 root:<group>: the ssh user reads the copies, cannot change them."""
    os.chmod(path, 0o640)
    if cfg.group:
        try:
            gid = grp.getgrnam(cfg.group).gr_gid
        except KeyError:
            raise Fail("no_group")
        os.chown(path, 0, gid)


def _acquire_lock(cfg):
    fd = os.open(os.path.join(cfg.state, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = _monotonic() + cfg.lock_wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                os.close(fd)
                raise
        if _monotonic() >= deadline:
            os.close(fd)
            raise Skip("lock_busy")
        _sleep(1)


MOUNTINFO = "/proc/self/mountinfo"


def _is_tmpfs(path):
    """True only when ``path`` itself is the mount point of a tmpfs."""
    target = os.path.realpath(path)
    try:
        with open(MOUNTINFO) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False
    for line in lines:
        left, sep, right = line.partition(" - ")
        fields = left.split(" ")
        if sep and len(fields) > 4 and fields[4] == target:
            return right.split(" ")[0] == "tmpfs"
    return False


def _clean_leftovers(cfg):
    """Start of a run: partial files and plain text of dead runs go away."""
    for path in glob.glob(os.path.join(cfg.state, "*.tmp")):
        os.unlink(path)
    _empty_dir(cfg.work)


def _empty_dir(path):
    if not os.path.isdir(path):
        return
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            try:
                os.unlink(full)
            except OSError:
                pass


def _read_deploy_sha(cfg, required):
    try:
        with open(cfg.deploy_sha) as fh:
            text = fh.read(128).strip()
    except OSError:
        if required:
            raise Fail("no_deploy_sha")
        return None
    if not SHA_RE.match(text):
        if required:
            raise Fail("bad_deploy_sha")
        return None
    return text


def _recipient_ready(cfg):
    if not os.path.isfile(cfg.recipient):
        raise Fail("no_recipient")


def _age_process(cfg, out):
    try:
        return subprocess.Popen(
            [cfg.age, "-R", cfg.recipient],
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise Fail("age_missing")


def _encrypt(cfg, out_path, producer):
    """Feed ``producer(stdin)`` through ``age`` into ``out_path`` (fsynced)."""
    with open(out_path, "wb") as out:
        proc = _age_process(cfg, out)
        broken = False
        try:
            producer(proc.stdin)
        except BrokenPipeError:
            broken = True
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            try:
                proc.stdin.close()
            except OSError:
                broken = True
        rc = proc.wait()
        if rc != 0 or broken:
            raise Fail("age")
        out.flush()
        os.fsync(out.fileno())


def _publish(cfg, kind, stamp, tmp_data, extra_meta):
    """File first, meta second; the pair is complete only when the meta exists."""
    spec = KINDS[kind]
    base = "%s-%s" % (spec["prefix"], stamp)
    data_name = base + spec["suffix"]
    final_data = os.path.join(cfg.state, data_name)
    final_meta = os.path.join(cfg.state, base + ".meta.json")
    if os.path.exists(final_data):
        raise Fail("name_exists")
    digest, size = _sha256_file(tmp_data)
    meta = {
        "name": data_name,
        "utc": stamp,
        "bytes": size,
        "sha256": digest,
        "kind": kind,
        "hub_commit": None,
        "schema_migrations": None,
        "manifest_sha256": None,
    }
    meta.update(extra_meta)
    tmp_meta = final_meta + ".tmp"
    with open(tmp_meta, "w") as fh:
        json.dump(meta, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    _set_perms(cfg, tmp_data)
    _set_perms(cfg, tmp_meta)
    os.rename(tmp_data, final_data)
    os.rename(tmp_meta, final_meta)
    _fsync_dir(cfg.state)
    return data_name, size


def _rotate(cfg, kind):
    """Keep the newest copies; the meta goes before its file."""
    spec = KINDS[kind]
    prefix = spec["prefix"]
    metas = sorted(glob.glob(os.path.join(cfg.state, prefix + "-*.meta.json")))
    keep = set(
        os.path.basename(m)[: -len(".meta.json")] for m in metas[-spec["keep"] :]
    )
    for meta in metas:
        if os.path.basename(meta)[: -len(".meta.json")] not in keep:
            os.unlink(meta)
    for data in glob.glob(os.path.join(cfg.state, prefix + "-*" + spec["suffix"])):
        if os.path.basename(data)[: -len(spec["suffix"])] not in keep:
            os.unlink(data)
    _fsync_dir(cfg.state)


# ---------------------------------------------------------------- systemd


def _unit_props(cfg, unit):
    try:
        proc = subprocess.run(
            [
                cfg.systemctl,
                "show",
                "-p",
                "FragmentPath",
                "-p",
                "DropInPaths",
                "-p",
                "EnvironmentFiles",
                "-p",
                "Environment",
                unit,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
    except OSError:
        raise Fail("systemctl")
    if proc.returncode != 0:
        raise Fail("systemctl")
    props = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    return props


def _env_files(props):
    return [
        (m.group(1).lstrip("-"), m.group(2) == "yes")
        for m in ENV_FILE_RE.finditer(props.get("EnvironmentFiles", ""))
    ]


def _env_assignments(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def _discover_db(cfg):
    """HAIPLANE_HUB_DB of the unit: Environment, then EnvironmentFiles override."""
    props = _unit_props(cfg, "haiplane-hub")
    try:
        values = _env_assignments("\n".join(shlex.split(props.get("Environment", ""))))
    except ValueError:
        raise Fail("no_db_path")
    for path, soft in _env_files(props):
        try:
            with open(_host(cfg, path)) as fh:
                values.update(_env_assignments(fh.read()))
        except OSError:
            if not soft:
                raise Fail("no_db_path")
    if not values.get("HAIPLANE_HUB_DB"):
        raise Fail("no_db_path")
    return values["HAIPLANE_HUB_DB"]


# ---------------------------------------------------------------- hourly


def _check_space(cfg, db):
    need = 1 << 20
    for suffix in ("", "-wal"):
        try:
            need += os.path.getsize(db + suffix)
        except OSError:
            pass
    stat = os.statvfs(cfg.work)
    if stat.f_bavail * stat.f_frsize < need:
        raise Fail("enospc")


def _copy_database(src, dest):
    """SQLite backup API from a read-only (NOT immutable: it ignores the WAL) handle."""
    source = sqlite3.connect(
        "file:%s?mode=ro" % urllib.parse.quote(src), uri=True, timeout=30
    )
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _verify_copy(dest):
    """Make the copy one self-contained file; return (count, sha256) of _migrations."""
    conn = sqlite3.connect(dest)
    try:
        if conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise Fail("journal_mode")
        if [r[0] for r in conn.execute("PRAGMA integrity_check")] != ["ok"]:
            raise Fail("integrity")
        try:
            names = sorted(r[0] for r in conn.execute("SELECT name FROM _migrations"))
        except sqlite3.OperationalError:
            raise Fail("no_migrations")
    finally:
        conn.close()
    if not names:
        raise Fail("no_migrations")
    return len(names), hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


def _feed_file(path):
    def produce(stdin):
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                stdin.write(chunk)

    return produce


def run_hourly(cfg):
    sha_before = _read_deploy_sha(cfg, True)
    _recipient_ready(cfg)
    db = cfg.db or _discover_db(cfg)
    if not os.path.isfile(db):
        raise Fail("no_db")
    stamp = _stamp(_utcnow())
    copy = os.path.join(cfg.work, "hub.db")
    tmp_data = os.path.join(cfg.state, "hub-%s.db.age.tmp" % stamp)
    _check_space(cfg, db)
    os.environ["SQLITE_TMPDIR"] = cfg.work
    try:
        _copy_database(db, copy)
    except sqlite3.OperationalError as exc:
        raise Fail("enospc" if "full" in str(exc).lower() else "sqlite")
    count, migrations_sha = _verify_copy(copy)
    _encrypt(cfg, tmp_data, _feed_file(copy))
    if _read_deploy_sha(cfg, False) != sha_before:
        raise Fail("mid_deploy")
    meta = {
        "hub_commit": sha_before,
        "schema_migrations": {"count": count, "sha256": migrations_sha},
    }
    return _publish(cfg, "hourly", stamp, tmp_data, meta)


# ---------------------------------------------------------------- daily


def _parse_paths(cfg):
    entries = []
    try:
        with open(cfg.paths_conf) as fh:
            lines = fh.read().splitlines()
    except OSError:
        raise Fail("no_paths_conf")
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or parts[0] not in (
            "required",
            "optional",
            "unit",
            "optional-unit",
        ):
            raise Fail("bad_paths_conf")
        kind, value = parts
        if kind in ("required", "optional") and (
            not os.path.isabs(value) or os.path.normpath(value) != value.rstrip("/")
        ):
            raise Fail("bad_paths_conf")
        entries.append((kind, value))
    return entries


def _expand(cfg, entries):
    """[(path, required)] with the files of the listed units added."""
    out = []
    for kind, value in entries:
        if kind in ("required", "optional"):
            out.append((value, kind == "required"))
            continue
        props = _unit_props(cfg, value)
        required = kind == "unit"
        if not props.get("FragmentPath"):
            if required:
                raise Fail("unit_missing")
            continue
        out.append((props["FragmentPath"], required))
        out.extend((p, required) for p in props.get("DropInPaths", "").split())
        for path, soft in _env_files(props):
            out.append((path, required and not soft))
    return out


def _arcname(path):
    return path.lstrip("/")


def _inside(target, roots):
    return any(target == r or target.startswith(r + os.sep) for r in roots)


def _walk(cfg, entries):
    """(files, links, skipped) under the allowlist; links are never followed out of it."""
    roots = [os.path.realpath(_host(cfg, p)) for p, _ in entries]
    files, links, skipped, seen = [], [], [], set()
    for path, required in entries:
        host = _host(cfg, path)
        if not os.path.lexists(host):
            if required:
                raise Fail("required_missing")
            continue
        found = []
        if os.path.isdir(host) and not os.path.islink(host):
            for top, dirs, names in os.walk(host):
                dirs.sort()
                found.extend(os.path.join(top, n) for n in sorted(names))
                found.extend(
                    os.path.join(top, d)
                    for d in dirs
                    if os.path.islink(os.path.join(top, d))
                )
        else:
            found.append(host)
        usable = 0
        for item in found:
            arc = _arcname(os.path.relpath(item, cfg.root) if cfg.root != "/" else item)
            if arc in seen:
                usable += 1
                continue
            if os.path.islink(item):
                if _inside(
                    os.path.realpath(item),
                    [r for r in roots if r != os.path.realpath(item)],
                ):
                    links.append((arc, item))
                    seen.add(arc)
                    usable += 1
                else:
                    skipped.append({"path": arc, "reason": "symlink_outside_allowlist"})
            elif os.path.isfile(item):
                files.append((arc, item))
                seen.add(arc)
                usable += 1
            else:
                skipped.append({"path": arc, "reason": "not_a_regular_file"})
        if required and not usable:
            raise Fail("required_unusable")
    return files, links, skipped


class _Hashing(object):
    def __init__(self, fileobj):
        self.fileobj = fileobj
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        data = self.fileobj.read(size)
        self.digest.update(data)
        return data


def _add_file(tar, arc, host):
    fd = os.open(host, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        info = tarfile.TarInfo(arc)
        info.size, info.mtime = st.st_size, int(st.st_mtime)
        info.mode, info.uid, info.gid = st.st_mode & 0o7777, st.st_uid, st.st_gid
        reader = _Hashing(fh)
        tar.addfile(info, reader)
    return {
        "path": arc,
        "sha256": reader.digest.hexdigest(),
        "size": st.st_size,
        "mode": st.st_mode & 0o7777,
        "uid": st.st_uid,
        "gid": st.st_gid,
    }


def _write_archive(tar, files, links, skipped, stamp, box):
    manifest = {
        "version": 1,
        "created": stamp,
        "files": [],
        "links": [],
        "skipped": skipped,
    }
    for arc, host in files:
        manifest["files"].append(_add_file(tar, arc, host))
    for arc, host in links:
        info = tarfile.TarInfo(arc)
        info.type, info.linkname = tarfile.SYMTYPE, os.readlink(host)
        tar.addfile(info)
        manifest["links"].append({"path": arc, "target": info.linkname})
    blob = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    info = tarfile.TarInfo(MANIFEST_NAME)
    info.size, info.mode, info.mtime = len(blob), 0o600, int(_utcnow().timestamp())
    tar.addfile(info, io.BytesIO(blob))
    box["manifest_sha256"] = hashlib.sha256(blob).hexdigest()


def run_daily(cfg):
    _recipient_ready(cfg)
    entries = _expand(cfg, _parse_paths(cfg))
    files, links, skipped = _walk(cfg, entries)
    stamp = _stamp(_utcnow())
    tmp_data = os.path.join(cfg.state, "conf-%s.tar.age.tmp" % stamp)
    box = {}

    def produce(stdin):
        tar = tarfile.open(fileobj=stdin, mode="w|", format=tarfile.PAX_FORMAT)
        _write_archive(tar, files, links, skipped, stamp, box)
        tar.close()

    _encrypt(cfg, tmp_data, produce)
    meta = {
        "hub_commit": _read_deploy_sha(cfg, False),
        "manifest_sha256": box["manifest_sha256"],
    }
    return _publish(cfg, "daily", stamp, tmp_data, meta)


# ---------------------------------------------------------------- entry


def _execute(cfg, kind):
    os.makedirs(cfg.work, mode=0o700, exist_ok=True)
    if kind == "hourly" and cfg.require_tmpfs and not _is_tmpfs(cfg.work):
        raise Fail("work_not_tmpfs")
    lock = _acquire_lock(cfg)
    try:
        _clean_leftovers(cfg)
        try:
            name, size = (run_hourly if kind == "hourly" else run_daily)(cfg)
        finally:
            _empty_dir(cfg.work)
            for path in glob.glob(os.path.join(cfg.state, "*.tmp")):
                os.unlink(path)
        _rotate(cfg, kind)
        return name, size
    finally:
        os.close(lock)


def main(argv, env=None):
    if len(argv) != 2 or argv[1] not in KINDS:
        sys.stdout.write("failed kind=none reason=usage\n")
        return 1
    kind = argv[1]
    cfg = Config(env)
    old_umask = os.umask(0o077)
    try:
        name, size = _execute(cfg, kind)
        line, rc = "ok kind=%s name=%s bytes=%d" % (kind, name, size), 0
    except Skip as exc:
        line, rc = "skipped kind=%s reason=%s" % (kind, exc.code), 0
    except Fail as exc:
        line, rc = "failed kind=%s reason=%s" % (kind, exc.code), 1
    except OSError as exc:
        reason = "enospc" if exc.errno == errno.ENOSPC else "io_error"
        line, rc = "failed kind=%s reason=%s" % (kind, reason), 1
    except Exception:
        line, rc = "failed kind=%s reason=unexpected" % kind, 1
    finally:
        os.umask(old_umask)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
