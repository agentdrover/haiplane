"""Shared helpers of the off-server copy tests (#1707): the script under test, age, a hub database."""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

BACKUP_DIR = Path(__file__).resolve().parents[1] / "deploy" / "backup"
MIGRATIONS = ("001_init", "002_tasks", "003_reviews")


def must_have(condition: bool, reason: str) -> None:
    """A missing tool is a skip on a laptop and a failure in CI (the job installs it)."""
    if condition:
        return
    if os.environ.get("CI_REQUIRE_ROOT_TESTS") == "1" or os.environ.get("CI"):
        pytest.fail("CI must provide this, a skip would hide the test: " + reason)
    pytest.skip(reason)


def need_age() -> None:
    must_have(
        bool(shutil.which("age")) and bool(shutil.which("age-keygen")),
        "age and age-keygen are not installed",
    )


def load_snapshot() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hub_snapshot_under_test", BACKUP_DIR / "hub-snapshot.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    """The snapshot clock as a value: a test moves it, nothing waits."""

    def __init__(self) -> None:
        self.now = datetime.datetime(
            2026, 10, 15, 0, 17, 0, tzinfo=datetime.timezone.utc
        )

    def __call__(self) -> datetime.datetime:
        return self.now

    def tick(self, hours: int = 1) -> None:
        self.now += datetime.timedelta(hours=hours)


def make_identity(directory: Path) -> tuple[Path, str]:
    """A throwaway age key pair; returns (identity file, public recipient)."""
    identity = directory / "identity.txt"
    done = subprocess.run(
        ["age-keygen", "-o", str(identity)], capture_output=True, text=True, check=True
    )
    match = re.search(r"(age1[0-9a-z]+)", done.stderr + done.stdout)
    assert match, "age-keygen printed no public key"
    return identity, match.group(1)


def decrypt(identity: Path, path: Path) -> bytes:
    done = subprocess.run(
        ["age", "-d", "-i", str(identity), str(path)], capture_output=True, check=True
    )
    return done.stdout


def make_hub_db(path: Path, rows=("committed-1", "committed-2")):
    """WAL database: committed rows still in the WAL and one open uncommitted write."""
    keeper = sqlite3.connect(path, isolation_level=None)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute("CREATE TABLE _migrations (name TEXT PRIMARY KEY, applied_at TEXT)")
    for name in MIGRATIONS:
        keeper.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))
    keeper.execute("CREATE TABLE notes (body TEXT)")
    for body in rows:
        keeper.execute("INSERT INTO notes VALUES (?)", (body,))
    ghost = sqlite3.connect(path, isolation_level=None)
    ghost.execute("BEGIN")
    ghost.execute("INSERT INTO notes VALUES ('uncommitted-ghost')")
    assert os.path.getsize(str(path) + "-wal") > 0, "the rows must still be in the WAL"
    return keeper, ghost


def migrations_sha() -> str:
    return hashlib.sha256("\n".join(sorted(MIGRATIONS)).encode()).hexdigest()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- a real installation on a disposable Linux host (AC-4, AC-5) ----------------

INSTALL = BACKUP_DIR / "install.sh"
HOME_DIR = "/var/lib/haiplane-backup-home"
STATE_DIR = "/var/lib/haiplane-backup"
LIB_DIR = "/usr/local/lib/haiplane-backup"
DROPIN = "/etc/ssh/sshd_config.d/60-haiplane-backup.conf"
SHARED = Path(tempfile.gettempdir()) / "haiplane-backup-test-material"


def root_prefix() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


def as_root(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(root_prefix() + list(args), capture_output=True, **kwargs)


def as_backup(args: list[str]) -> subprocess.CompletedProcess:
    prefix = (
        ["runuser", "-u", "haiplane-backup", "--"]
        if os.geteuid() == 0
        else ["sudo", "-n", "-u", "haiplane-backup"]
    )
    return subprocess.run(prefix + list(args), capture_output=True, text=True)


def need_root_host() -> None:
    """Real user, real sshd drop-in: only on a disposable Linux host, never by accident."""
    must_have(
        sys.platform.startswith("linux"),
        "the test installs a real user and sshd drop-in: Linux only",
    )
    can_root = os.geteuid() == 0 or (
        bool(shutil.which("sudo"))
        and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0
    )
    must_have(can_root, "needs root or passwordless sudo")
    must_have(
        os.environ.get("CI_REQUIRE_ROOT_TESTS") == "1"
        or os.environ.get("HAIPLANE_DISPOSABLE_HOST") == "1",
        "installs a real user and sshd drop-in: set HAIPLANE_DISPOSABLE_HOST=1 on a disposable host only",
    )
    sshd = shutil.which("sshd") or (
        "/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else ""
    )
    must_have(bool(sshd), "openssh-server (sshd) is not installed")
    must_have(
        bool(shutil.which("ssh-keygen")) and bool(shutil.which("ssh")),
        "openssh-client is not installed",
    )
    need_age()


def sshd_path() -> str:
    return shutil.which("sshd") or "/usr/sbin/sshd"


class SharedLock:
    """One lock per host for every xdist worker: the installation is machine-wide.

    The installer takes it exclusively; a test that is using the installed service
    (the real sshd) downgrades to a shared hold, so no other test reinstalls under it.
    """

    def __init__(self, shared: bool = False):
        self.shared = shared

    def __enter__(self):
        SHARED.mkdir(parents=True, exist_ok=True)
        self.handle = open(SHARED / "lock", "w")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def downgrade(self) -> None:
        fcntl.flock(self.handle, fcntl.LOCK_SH)

    def __exit__(self, *exc):
        self.handle.close()


def shared_keys() -> dict[str, Path]:
    """Client ssh key and age recipient file, generated once per host."""
    key = SHARED / "client_key"
    recipient = SHARED / "recipient.txt"
    if not key.exists():
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
        )
    if not recipient.exists():
        _, public = make_identity(SHARED)
        recipient.write_text(public + "\n")
    return {"key": key, "pub": Path(str(key) + ".pub"), "recipient": recipient}


def run_install(
    extra: list[str] | None = None,
    held: SharedLock | None = None,
    lock_account: bool = False,
) -> subprocess.CompletedProcess:
    """Run the real install.sh under the host lock (``held``: the caller already has it).

    ``lock_account`` first puts the account into the '!' state that useradd leaves,
    which the installer must repair (sshd without PAM refuses such an account)."""
    if held is None:
        with SharedLock() as taken:
            return run_install(extra, taken, lock_account)
    if (
        lock_account
        and as_root(["getent", "passwd", "haiplane-backup"]).returncode == 0
    ):
        as_root(["usermod", "-L", "haiplane-backup"], check=True)
    keys = shared_keys()
    args = [
        "sh",
        str(INSTALL),
        "--ssh-pubkey-file",
        str(keys["pub"]),
        "--age-recipient-file",
        str(keys["recipient"]),
        "--no-services",
    ] + (extra or [])
    return as_root(args, text=True)


def installed_state() -> str:
    """Owners, modes, contents and account fields of everything the installer owns."""
    paths = [
        HOME_DIR,
        HOME_DIR + "/.ssh",
        HOME_DIR + "/.ssh/authorized_keys",
        STATE_DIR,
        STATE_DIR + "/work",
        "/etc/haiplane-backup",
        "/etc/haiplane-backup/recipient.txt",
        "/etc/haiplane-backup/paths.conf",
        LIB_DIR + "/hub-snapshot.py",
        LIB_DIR + "/snapshot-serve.sh",
        DROPIN,
    ] + [
        "/etc/systemd/system/haiplane-snapshot-%s.%s" % (a, b)
        for a in ("hourly", "daily")
        for b in ("service", "timer")
    ]
    parts = []
    for path in paths:
        info = as_root(["stat", "-c", "%U:%G %a %F %n", path], text=True)
        parts.append(info.stdout + info.stderr)
        body = as_root(
            ["sh", "-c", "[ -f '%s' ] && sha256sum '%s'" % (path, path)], text=True
        )
        parts.append(body.stdout)
    parts.append(as_root(["getent", "passwd", "haiplane-backup"], text=True).stdout)
    parts.append(as_root(["getent", "shadow", "haiplane-backup"], text=True).stdout)
    return "".join(parts)
