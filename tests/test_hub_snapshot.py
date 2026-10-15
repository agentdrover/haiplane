"""deploy/backup/hub-snapshot.py (#1707): hourly database snapshot, daily settings archive.

The script is loaded as a module and driven in-process: the clock, the lock wait and
the disk space are values the test sets, so nothing here waits on the wall clock.
Encryption is real age with a throwaway key.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import json
import os
import sqlite3
import stat
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.backup_support import (
    Clock,
    decrypt,
    load_snapshot,
    make_hub_db,
    make_identity,
    migrations_sha,
    must_have,
    need_age,
    sha256_of,
)

DEPLOY_SHA = "a" * 40


@pytest.fixture
def world(tmp_path, monkeypatch):
    need_age()
    snap = load_snapshot()
    clock = Clock()
    monkeypatch.setattr(snap, "_utcnow", clock)
    monkeypatch.setenv("SQLITE_TMPDIR", "")
    identity, recipient = make_identity(tmp_path)
    state = tmp_path / "state"
    work = state / "work"
    state.mkdir()
    (tmp_path / "recipient.txt").write_text(recipient + "\n")
    (tmp_path / "deploy-sha").write_text(DEPLOY_SHA + "\n")
    db = tmp_path / "hub.db"
    keeper, ghost = make_hub_db(db)
    env = {
        "SNAP_STATE_DIR": str(state),
        "SNAP_WORK_DIR": str(work),
        "SNAP_RECIPIENT_FILE": str(tmp_path / "recipient.txt"),
        "SNAP_DEPLOY_SHA_FILE": str(tmp_path / "deploy-sha"),
        "SNAP_DB": str(db),
        "SNAP_GROUP": "",
        "SNAP_REQUIRE_TMPFS": "0",
        "SNAP_LOCK_WAIT": "5",
    }
    yield SimpleNamespace(
        snap=snap,
        clock=clock,
        identity=identity,
        state=state,
        work=work,
        env=env,
        root=tmp_path,
        db=db,
    )
    ghost.close()
    keeper.close()


def run(world, kind, capsys, **env_override):
    env = dict(world.env, **env_override)
    rc = world.snap.main(["hub-snapshot.py", kind], env)
    return rc, capsys.readouterr().out.strip()


def published(world, pattern):
    return sorted(p.name for p in world.state.glob(pattern))


def meta_of(world, data_name):
    return json.loads(
        (world.state / (data_name.split(".")[0] + ".meta.json")).read_text()
    )


def test_hourly_snapshot_round_trips_through_age(world, capsys, monkeypatch, tmp_path):
    created = []
    real_connect = sqlite3.connect

    def spy(database, *args, **kwargs):
        created.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(world.snap.sqlite3, "connect", spy)
    rc, line = run(world, "hourly", capsys)
    assert rc == 0, line
    assert line.startswith("ok kind=hourly name=hub-20261015T001700Z.db.age"), line

    # every plain-text file the script opened lives in the work directory
    plain = [c for c in created if not c.startswith("file:")]
    assert plain and all(Path(c).parent == world.work for c in plain), created
    assert list(world.work.iterdir()) == [], (
        "the work directory must be empty after the run"
    )
    assert not list(world.state.glob("*.tmp"))

    # the published data is ciphertext: no SQLite header, no row text, outside the work dir
    (data,) = world.state.glob("hub-*.db.age")
    raw = data.read_bytes()
    assert not raw.startswith(b"SQLite format 3") and b"committed-1" not in raw
    for path in world.state.rglob("*"):
        if path.is_file():
            assert b"committed-1" not in path.read_bytes(), path

    # the decrypted copy is one self-contained, consistent file
    copy = tmp_path / "restored.db"
    copy.write_bytes(decrypt(world.identity, data))
    conn = sqlite3.connect(copy)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        bodies = sorted(r[0] for r in conn.execute("SELECT body FROM notes"))
    finally:
        conn.close()
    assert bodies == ["committed-1", "committed-2"]
    assert (
        not Path(str(copy) + "-wal").exists() and not Path(str(copy) + "-shm").exists()
    )

    meta = meta_of(world, data.name)
    assert meta["sha256"] == hashlib.sha256(raw).hexdigest() and meta["bytes"] == len(
        raw
    )
    assert meta["name"] == data.name and meta["kind"] == "hourly"
    assert meta["hub_commit"] == DEPLOY_SHA
    assert meta["schema_migrations"] == {"count": 3, "sha256": migrations_sha()}

    # age fails: nothing new is published, the good copy stays, the work dir is empty
    before = {
        p.name: p.read_bytes()
        for p in world.state.iterdir()
        if p.is_file() and p.name != ".lock"
    }
    failing = tmp_path / "age-fail.sh"
    failing.write_text("#!/bin/sh\ncat >/dev/null\nexit 1\n")
    failing.chmod(0o755)
    world.clock.tick()
    rc, line = run(world, "hourly", capsys, SNAP_AGE=str(failing))
    assert (rc, line) == (1, "failed kind=hourly reason=age")
    after = {
        p.name: p.read_bytes()
        for p in world.state.iterdir()
        if p.is_file() and p.name != ".lock"
    }
    assert after == before
    assert list(world.work.iterdir()) == []


def test_retention_and_rotation_during_read(world, capsys, monkeypatch):
    (world.root / "conf.txt").write_text("setting=1\n")
    (world.root / "paths.conf").write_text("required %s\n" % (world.root / "conf.txt"))
    world.env["SNAP_PATHS_CONF"] = str(world.root / "paths.conf")

    for _ in range(4):
        assert run(world, "hourly", capsys)[0] == 0
        world.clock.tick()
    for _ in range(3):
        assert run(world, "daily", capsys)[0] == 0
        world.clock.tick()
    assert len(published(world, "hub-*.db.age")) == 3

    # a reader holds the oldest kept copy open; the next snapshot rotates it away
    oldest = world.state / published(world, "hub-*.db.age")[0]
    meta = meta_of(world, oldest.name)
    handle = oldest.open("rb")
    head = handle.read(10)

    unlinked = []
    real_unlink = os.unlink
    monkeypatch.setattr(
        world.snap.os,
        "unlink",
        lambda p: (unlinked.append(Path(p).name), real_unlink(p))[1],
    )
    assert run(world, "hourly", capsys)[0] == 0
    assert not oldest.exists(), "the rotated file must be gone from the directory"
    body = head + handle.read()
    handle.close()
    assert (
        len(body) == meta["bytes"]
        and hashlib.sha256(body).hexdigest() == meta["sha256"]
    )
    # the meta is removed before its file
    base = oldest.name.split(".")[0]
    assert unlinked.index(base + ".meta.json") < unlinked.index(base + ".db.age")

    hourly = published(world, "hub-*.db.age")
    daily = published(world, "conf-*.tar.age")
    assert len(hourly) == 3 and len(daily) == 2
    assert hourly == [
        "hub-20261015T021700Z.db.age",
        "hub-20261015T031700Z.db.age",
        "hub-20261015T071700Z.db.age",
    ]
    assert published(world, "hub-*.meta.json") == [
        n.split(".")[0] + ".meta.json" for n in hourly
    ]
    assert published(world, "conf-*.meta.json") == [
        n.split(".")[0] + ".meta.json" for n in daily
    ]
    for name in hourly + daily:
        info = meta_of(world, name)
        assert (
            info["sha256"] == sha256_of(world.state / name)
            and info["bytes"] == (world.state / name).stat().st_size
        )
        assert stat.S_IMODE((world.state / name).stat().st_mode) == 0o640


def _systemctl_stub(world, props):
    script = world.root / "systemctl"
    script.write_text("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % props)
    script.chmod(0o755)
    return str(script)


def _tree(world):
    root = world.root / "sysroot"
    files = {
        "etc/haiplane/hub.env": "HAIPLANE_HUB_DB=/db\nTOKEN=hub-secret-token\n",  # pragma: allowlist secret
        "etc/systemd/system/haiplane-hub.service": "[Service]\n",
        "etc/systemd/system/haiplane-hub.service.d/10.conf": "[Service]\nNice=1\n",
        "etc/georoute/a.conf": "route-a\n",
        "etc/georoute/sub/b.conf": "route-b\n",
        "etc/fail2ban/jail.local": "[DEFAULT]\n",
        "etc/other-secret": "TOP-SECRET-OUTSIDE\n",  # pragma: allowlist secret
    }
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    os.symlink("../other-secret", root / "etc/georoute/leak")
    os.symlink("a.conf", root / "etc/georoute/alias")
    props = "\n".join(
        [
            "FragmentPath=/etc/systemd/system/haiplane-hub.service",
            "DropInPaths=/etc/systemd/system/haiplane-hub.service.d/10.conf",
            "EnvironmentFiles=/etc/haiplane/hub.env (ignore_errors=no) /etc/haiplane/absent.env (ignore_errors=yes)",
            "Environment=",
        ]
    )
    (world.root / "paths.conf").write_text(
        "unit haiplane-hub\nrequired /etc/georoute\nrequired /etc/fail2ban/jail.local\n"
        "optional /etc/runner\n# comment\n"
    )
    world.env.update(
        SNAP_ROOT=str(root),
        SNAP_PATHS_CONF=str(world.root / "paths.conf"),
        SNAP_SYSTEMCTL=_systemctl_stub(world, props),
    )
    return root


def test_daily_archive_is_exactly_the_allowlist(world, capsys):
    root = _tree(world)
    rc, line = run(world, "daily", capsys)
    assert rc == 0, line
    (data,) = world.state.glob("conf-*.tar.age")
    plain = decrypt(world.identity, data)
    with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
        names = sorted(tar.getnames())
        manifest_bytes = tar.extractfile("MANIFEST.json").read()
        contents = {
            m.name: tar.extractfile(m).read()
            for m in tar.getmembers()
            if m.isfile() and m.name != "MANIFEST.json"
        }
    assert names == sorted(
        [
            "MANIFEST.json",
            "etc/systemd/system/haiplane-hub.service",
            "etc/systemd/system/haiplane-hub.service.d/10.conf",
            "etc/haiplane/hub.env",
            "etc/georoute/a.conf",
            "etc/georoute/sub/b.conf",
            "etc/georoute/alias",
            "etc/fail2ban/jail.local",
        ]
    )
    # the stranger next to the allowlist and the target of the outside link never get in
    assert b"TOP-SECRET-OUTSIDE" not in plain and b"other-secret" not in plain
    manifest = json.loads(manifest_bytes)
    assert {f["path"]: f["sha256"] for f in manifest["files"]} == {
        p: hashlib.sha256(c).hexdigest() for p, c in contents.items()
    }
    assert manifest["skipped"] == [
        {"path": "etc/georoute/leak", "reason": "symlink_outside_allowlist"}
    ]
    meta = meta_of(world, data.name)
    assert meta["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()

    # a required path that is missing is a refusal, not a smaller archive
    (root / "etc/fail2ban/jail.local").unlink()
    world.clock.tick()
    before = published(world, "conf-*")
    rc, line = run(world, "daily", capsys)
    assert (rc, line) == (1, "failed kind=daily reason=required_missing")
    assert published(world, "conf-*") == before
    assert list(world.state.glob("*.tmp")) == []


def test_no_publish_on_enospc_or_mid_deploy(world, capsys, monkeypatch):
    for _ in range(3):
        assert run(world, "hourly", capsys)[0] == 0
        world.clock.tick()
    good = published(world, "hub-*")

    # (a) the work area is smaller than the database
    monkeypatch.setattr(
        world.snap.os, "statvfs", lambda p: SimpleNamespace(f_bavail=1, f_frsize=4096)
    )
    assert run(world, "hourly", capsys) == (1, "failed kind=hourly reason=enospc")
    monkeypatch.undo()
    monkeypatch.setattr(world.snap, "_utcnow", world.clock)
    for error in (
        OSError(errno.ENOSPC, "full"),
        sqlite3.OperationalError("database or disk is full"),
    ):

        def boom(src, dest, error=error):
            raise error

        monkeypatch.setattr(world.snap, "_copy_database", boom)
        assert run(world, "hourly", capsys) == (1, "failed kind=hourly reason=enospc")
        assert published(world, "hub-*") == good and list(world.work.iterdir()) == []
    monkeypatch.undo()
    monkeypatch.setattr(world.snap, "_utcnow", world.clock)

    # (b) the deploy marker changes while the snapshot is being taken
    real_encrypt = world.snap._encrypt

    def encrypt_then_deploy(cfg, out_path, producer):
        real_encrypt(cfg, out_path, producer)
        Path(cfg.deploy_sha).write_text("b" * 40 + "\n")

    monkeypatch.setattr(world.snap, "_encrypt", encrypt_then_deploy)
    assert run(world, "hourly", capsys) == (1, "failed kind=hourly reason=mid_deploy")
    assert published(world, "hub-*") == good
    assert list(world.state.glob("*.tmp")) == [] and list(world.work.iterdir()) == []
    monkeypatch.setattr(world.snap, "_encrypt", real_encrypt)
    (world.root / "deploy-sha").unlink()
    assert run(world, "hourly", capsys) == (
        1,
        "failed kind=hourly reason=no_deploy_sha",
    )
    assert published(world, "hub-*") == good

    # (c) the daily archive carries no migrations, only the manifest hash
    (world.root / "deploy-sha").write_text(DEPLOY_SHA + "\n")
    (world.root / "conf.txt").write_text("x\n")
    (world.root / "paths.conf").write_text("required %s\n" % (world.root / "conf.txt"))
    world.env["SNAP_PATHS_CONF"] = str(world.root / "paths.conf")
    assert run(world, "daily", capsys)[0] == 0
    (daily,) = published(world, "conf-*.tar.age")
    meta = meta_of(world, daily)
    assert meta["schema_migrations"] is None and len(meta["manifest_sha256"]) == 64
    assert (
        meta_of(world, good[-1].replace(".meta.json", ".db.age"))["manifest_sha256"]
        is None
    )


def test_work_must_be_a_tmpfs_and_leftovers_go(world, capsys):
    rc, line = run(world, "hourly", capsys, SNAP_REQUIRE_TMPFS="1")
    assert (rc, line) == (1, "failed kind=hourly reason=work_not_tmpfs")
    assert published(world, "hub-*") == []
    # a dead run's partial file and plain text are removed at the start of the next run
    world.work.mkdir(exist_ok=True)
    (world.work / "hub.db").write_text("plain text of a dead run")
    (world.state / "hub-old.db.age.tmp").write_text("partial")
    assert run(world, "hourly", capsys)[0] == 0
    assert list(world.work.iterdir()) == [] and list(world.state.glob("*.tmp")) == []


def test_busy_lock_is_a_skip_not_a_queue(world, capsys, monkeypatch):
    virtual = {"now": 0.0}
    monkeypatch.setattr(world.snap, "_monotonic", lambda: virtual["now"])
    monkeypatch.setattr(
        world.snap, "_sleep", lambda s: virtual.update(now=virtual["now"] + s)
    )
    holder = os.open(world.state / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        rc, line = run(world, "hourly", capsys)
    finally:
        os.close(holder)
    assert (rc, line) == (0, "skipped kind=hourly reason=lock_busy")
    assert virtual["now"] >= 5, "it waited out the whole (virtual) lock window"
    assert published(world, "hub-*") == []


def test_database_path_comes_from_the_unit_and_files_override(world, capsys):
    (world.root / "hub.env").write_text("HAIPLANE_HUB_DB=%s\n" % world.db)
    props = (
        "Environment=HAIPLANE_HUB_DB=/wrong/path.db\nEnvironmentFiles=%s (ignore_errors=no)\nFragmentPath=x"
        % (world.root / "hub.env")
    )
    world.env["SNAP_DB"] = ""
    world.env["SNAP_SYSTEMCTL"] = _systemctl_stub(world, props)
    assert run(world, "hourly", capsys)[0] == 0
    world.env["SNAP_SYSTEMCTL"] = _systemctl_stub(
        world, "Environment=\nEnvironmentFiles=\nFragmentPath=x"
    )
    world.clock.tick()
    assert run(world, "hourly", capsys) == (1, "failed kind=hourly reason=no_db_path")


def test_missing_recipient_publishes_nothing(world, capsys):
    (world.root / "recipient.txt").unlink()
    assert run(world, "hourly", capsys) == (1, "failed kind=hourly reason=no_recipient")
    assert published(world, "hub-*") == [] and list(world.work.iterdir()) == []


def test_unknown_kind_is_refused(world, capsys):
    assert world.snap.main(["hub-snapshot.py", "weekly"], world.env) == 1
    assert capsys.readouterr().out.strip() == "failed kind=none reason=usage"


def test_age_tools_are_required_in_ci(monkeypatch):
    monkeypatch.setenv("CI_REQUIRE_ROOT_TESTS", "1")
    with pytest.raises(pytest.fail.Exception):
        must_have(False, "age is missing")


def test_tmpfs_detection_wants_the_exact_mount_point(tmp_path):
    snap = load_snapshot()
    work = tmp_path / "work"
    work.mkdir()
    real = os.path.realpath(work)
    table = tmp_path / "mountinfo"
    head = "36 35 98:0 / %s rw,relatime shared:1 - %s tmpfs rw\n"
    table.write_text(
        head % (real, "tmpfs") + "40 35 8:1 / /var rw - ext4 /dev/sda1 rw\n"
    )
    snap.MOUNTINFO = str(table)
    assert snap._is_tmpfs(str(work)) is True
    table.write_text(head % (real, "ext4"))
    assert snap._is_tmpfs(str(work)) is False
    table.write_text(head % (os.path.dirname(real), "tmpfs"))
    assert snap._is_tmpfs(str(work)) is False, (
        "a tmpfs on a parent is not a limit on this directory"
    )
    snap.MOUNTINFO = str(tmp_path / "absent")
    assert snap._is_tmpfs(str(work)) is False
