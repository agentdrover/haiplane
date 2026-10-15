"""deploy/backup/snapshot-serve.sh (#1707): the script alone, then through a real sshd."""

from __future__ import annotations

import datetime
import json
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from tests.backup_support import (
    BACKUP_DIR,
    LIB_DIR,
    STATE_DIR,
    as_root,
    need_root_host,
    SharedLock,
    run_install,
    shared_keys,
    sshd_path,
)

SERVE = BACKUP_DIR / "snapshot-serve.sh"
NOW = datetime.datetime(2026, 10, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _stamp(moment):
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _put(state: Path, kind: str, moment, payload: bytes):
    prefix, suffix = ("hub", ".db.age") if kind == "hourly" else ("conf", ".tar.age")
    base = "%s-%s" % (prefix, _stamp(moment))
    (state / (base + suffix)).write_bytes(payload)
    meta = {"name": base + suffix, "kind": kind, "bytes": len(payload)}
    (state / (base + ".meta.json")).write_text(json.dumps(meta) + "\n")
    return meta


def _serve(state: Path, command, now=NOW):
    env = {"PATH": os.environ["PATH"], "SNAP_NOW": str(int(now.timestamp()))}
    if command is not None:
        env["SSH_ORIGINAL_COMMAND"] = command
    return subprocess.run(["sh", str(SERVE), str(state)], capture_output=True, env=env)


def test_the_four_commands_serve_the_newest_copy(tmp_path):
    older = _put(tmp_path, "hourly", NOW - datetime.timedelta(minutes=70), b"OLD")
    newer = _put(
        tmp_path, "hourly", NOW - datetime.timedelta(minutes=10), b"NEW-HOURLY"
    )
    _put(tmp_path, "daily", NOW - datetime.timedelta(hours=3), b"DAILY-OLD")
    _put(tmp_path, "daily", NOW - datetime.timedelta(hours=2), b"DAILY-NEW")
    assert older["name"] != newer["name"]
    done = _serve(tmp_path, "snapshot latest")
    assert (done.returncode, done.stdout) == (0, b"NEW-HOURLY")
    done = _serve(tmp_path, "snapshot daily")
    assert (done.returncode, done.stdout) == (0, b"DAILY-NEW")
    done = _serve(tmp_path, "snapshot latest-meta")
    assert done.returncode == 0 and json.loads(done.stdout) == newer
    done = _serve(tmp_path, "snapshot daily-meta")
    assert done.returncode == 0 and json.loads(done.stdout)["bytes"] == len(
        b"DAILY-NEW"
    )


def test_missing_and_stale_copies_have_their_own_codes(tmp_path):
    for command in ("snapshot latest", "snapshot daily-meta"):
        done = _serve(tmp_path, command)
        assert (done.returncode, done.stdout) == (3, b"")
    # a meta without its file is "no copy", not an empty answer
    meta = _put(tmp_path, "hourly", NOW - datetime.timedelta(minutes=5), b"X")
    (tmp_path / meta["name"]).unlink()
    assert _serve(tmp_path, "snapshot latest").returncode == 3
    assert _serve(tmp_path, "snapshot latest-meta").returncode == 0

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _put(fresh, "hourly", NOW - datetime.timedelta(minutes=90), b"EDGE")
    _put(fresh, "daily", NOW - datetime.timedelta(hours=26), b"EDGE")
    assert _serve(fresh, "snapshot latest").returncode == 0
    assert _serve(fresh, "snapshot daily").returncode == 0
    later = NOW + datetime.timedelta(seconds=1)
    for command in (
        "snapshot latest",
        "snapshot latest-meta",
        "snapshot daily",
        "snapshot daily-meta",
    ):
        done = _serve(fresh, command, now=later)
        assert (done.returncode, done.stdout) == (4, b""), command


REFUSED = [
    None,
    "",
    "id",
    "sh",
    "snapshot",
    "snapshot latest; id",
    "snapshot latest extra",
    "snapshot  latest",
    "snapshot LATEST",
    "snapshot ../latest",
    "snapshot latest | cat",
    "snapshot $(id)",
    "snapshot latest\nid",
    "scp -f /etc/passwd",
    "internal-sftp",
    "cat /etc/passwd",
]


def test_everything_else_is_refused_without_output(tmp_path):
    _put(tmp_path, "hourly", NOW - datetime.timedelta(minutes=5), b"SECRET-COPY")
    for command in REFUSED:
        done = _serve(tmp_path, command)
        assert (done.returncode, done.stdout, done.stderr) == (2, b"", b""), repr(
            command
        )


# ---- the real sshd (AC-4) -----------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.port = _free_port()
        keys = shared_keys()
        self.key = keys["key"]
        host_key = tmp_path / "host_key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
            check=True,
        )
        self.config = tmp_path / "sshd_config"
        self.config.write_text(
            "\n".join(
                [
                    "Port %d" % self.port,
                    "ListenAddress 127.0.0.1",
                    "HostKey %s" % host_key,
                    "PidFile %s" % (tmp_path / "sshd.pid"),
                    "UsePAM no",
                    "PubkeyAuthentication yes",
                    "AuthorizedKeysFile .ssh/authorized_keys",
                    "StrictModes yes",
                    "LogLevel INFO",
                    "Include /etc/ssh/sshd_config.d/60-haiplane-backup.conf",
                    "",
                ]
            )
        )
        self.proc = None

    def effective(self) -> list[str]:
        done = as_root(
            [
                sshd_path(),
                "-T",
                "-f",
                str(self.config),
                "-C",
                "user=backup,host=127.0.0.1,addr=127.0.0.1",
            ],
            text=True,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.splitlines()

    def start(self) -> None:
        argv = [sshd_path(), "-D", "-e", "-f", str(self.config)]
        prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
        self.proc = subprocess.Popen(prefix + argv, stderr=subprocess.PIPE, text=True)
        assert self.proc.stderr is not None
        for (
            line
        ) in self.proc.stderr:  # blocks until sshd says it listens, or dies (EOF)
            if "Server listening on 127.0.0.1 port %d" % self.port in line:
                break
        else:
            pytest.fail("the test sshd did not start")
        threading.Thread(
            target=lambda: [None for _ in self.proc.stderr], daemon=True
        ).start()

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait()

    def ssh(self, *words: str, extra=(), tool="ssh"):
        if tool == "ssh":
            base = ["ssh", "-p", str(self.port)]
        else:
            base = [tool, "-P", str(self.port)]
        base += [
            "-F",
            "/dev/null",
            "-i",
            str(self.key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
        ]
        return subprocess.run(
            base + list(extra) + list(words), capture_output=True, timeout=60
        )


@pytest.fixture
def server(tmp_path):
    need_root_host()
    host = SharedLock()
    host.__enter__()  # exclusive while installing, shared while the daemon is in use
    try:
        first = run_install(held=host)
        assert first.returncode == 0, first.stdout + first.stderr
        host.downgrade()
    except BaseException:
        host.__exit__(None, None, None)
        raise
    now = datetime.datetime.now(datetime.timezone.utc)
    names = []
    payloads = {}
    for kind, payload in (
        ("hourly", os.urandom(300_000)),
        ("daily", os.urandom(50_000)),
    ):
        prefix, suffix = (
            ("hub", ".db.age") if kind == "hourly" else ("conf", ".tar.age")
        )
        base = "%s-%s" % (prefix, _stamp(now))
        data, meta = tmp_path / (base + suffix), tmp_path / (base + ".meta.json")
        data.write_bytes(payload)
        meta.write_text(
            json.dumps({"name": base + suffix, "kind": kind, "bytes": len(payload)})
            + "\n"
        )
        for src in (data, meta):
            done = as_root(
                [
                    "install",
                    "-m",
                    "0640",
                    "-o",
                    "root",
                    "-g",
                    "backup",
                    str(src),
                    STATE_DIR + "/" + src.name,
                ]
            )
            assert done.returncode == 0, done.stderr
            names.append(src.name)
        payloads[kind] = (payload, meta.read_bytes())
    srv = Server(tmp_path)
    srv.payloads = payloads
    srv.start()
    yield srv
    srv.stop()
    for name in names:
        as_root(["rm", "-f", STATE_DIR + "/" + name])
    host.__exit__(None, None, None)


def test_only_the_four_commands_are_served_over_real_sshd(server, tmp_path):
    # the daemon under test really applies the installed drop-in to the user
    effective = server.effective()
    assert "forcecommand %s/snapshot-serve.sh %s" % (LIB_DIR, STATE_DIR) in effective
    for line in (
        "passwordauthentication no",
        "permittty no",
        "allowtcpforwarding no",
        "x11forwarding no",
    ):
        assert line in effective, line
    assert "allowagentforwarding no" in effective and "permittunnel no" in effective

    hourly, hourly_meta = server.payloads["hourly"]
    daily, daily_meta = server.payloads["daily"]
    for command, expected in (
        ("snapshot latest", hourly),
        ("snapshot latest-meta", hourly_meta),
        ("snapshot daily", daily),
        ("snapshot daily-meta", daily_meta),
    ):
        done = server.ssh("backup@127.0.0.1", command)
        assert (done.returncode, done.stdout) == (0, expected), (command, done.stderr)

    refused = [
        "id",
        "sh",
        "snapshot latest; id",
        "snapshot latest && id",
        "snapshot latest extra",
        "snapshot ../latest",
        "snapshot latest | cat",
        "cat /etc/passwd",
    ]
    for command in refused:
        done = server.ssh("backup@127.0.0.1", command)
        assert done.returncode == 2 and done.stdout == b"", (
            command,
            done.stdout,
            done.stderr,
        )
    shell = server.ssh(
        "-T", "backup@127.0.0.1"
    )  # no command: a login shell is asked for
    assert shell.returncode == 2 and shell.stdout == b""

    target = tmp_path / "pulled"
    scp = server.ssh("backup@127.0.0.1:/etc/passwd", str(target), tool="scp")
    assert scp.returncode != 0 and not target.exists()
    sftp = subprocess.run(
        [
            "sftp", "-P", str(server.port), "-F", "/dev/null", "-i", str(server.key),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-b", "-", "backup@127.0.0.1",
        ],
        input=b"ls /\n",
        capture_output=True,
        timeout=60,
    )  # fmt: skip
    assert sftp.returncode != 0 and b"passwd" not in sftp.stdout

    # no password login either
    password = subprocess.run(
        ["ssh", "-p", str(server.port), "-F", "/dev/null", "-o", "PubkeyAuthentication=no",
         "-o", "PreferredAuthentications=password", "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "backup@127.0.0.1", "snapshot latest-meta"],
        capture_output=True,
        timeout=60,
    )  # fmt: skip
    assert password.returncode == 255 and password.stdout == b""

    # channels, checked apart from the command's own return code
    pty = server.ssh("-tt", "backup@127.0.0.1", "snapshot latest-meta")
    assert b"PTY allocation request failed" in pty.stderr

    remote = server.ssh(
        "backup@127.0.0.1",
        "snapshot latest-meta",
        extra=["-o", "ExitOnForwardFailure=yes", "-R", "0:127.0.0.1:9"],
    )
    assert remote.returncode == 255 and b"forwarding failed" in remote.stderr

    target_side = socket.socket()
    target_side.bind(("127.0.0.1", 0))
    target_side.listen(1)
    target_side.setblocking(False)
    local = _free_port()
    control = str(tmp_path / "ctl")
    opened = server.ssh(
        "backup@127.0.0.1",
        extra=[
            "-f", "-N", "-M", "-S", control, "-o", "ExitOnForwardFailure=yes",
            "-L", "%d:127.0.0.1:%d" % (local, target_side.getsockname()[1]),
        ],
    )  # fmt: skip
    assert opened.returncode == 0, opened.stderr
    try:
        with socket.create_connection(("127.0.0.1", local), timeout=30) as client:
            client.settimeout(30)
            try:
                got = client.recv(1)
            except ConnectionResetError:
                got = b""
        assert got == b"", "the forwarded connection must be closed by the server"
        with pytest.raises(BlockingIOError):
            target_side.accept()  # nothing ever reached the target through the tunnel
    finally:
        subprocess.run(
            ["ssh", "-S", control, "-O", "exit", "backup@127.0.0.1"],
            capture_output=True,
            timeout=30,
        )
        target_side.close()
