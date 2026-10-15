"""deploy/backup/install.sh (#1707): arguments, then a real installation on a disposable host."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.backup_support import (
    BACKUP_DIR,
    DROPIN,
    HOME_DIR,
    INSTALL,
    LIB_DIR,
    STATE_DIR,
    as_backup,
    as_root,
    installed_state,
    need_root_host,
    run_install,
    shared_keys,
    sshd_path,
    must_have,
)


def _public_key(tmp_path: Path) -> str:
    must_have(
        bool(
            subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode == 0
        ),
        "no ssh-keygen",
    )
    key = tmp_path / "k"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    return (tmp_path / "k.pub").read_text().strip()


def _install(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(INSTALL), *args], capture_output=True, text=True)


AGE_RECIPIENT = "age1" + "q" * 58  # the shape only: not a key of anybody


def test_install_refuses_bad_arguments_before_it_touches_anything(tmp_path):
    good = _public_key(tmp_path)
    cases = [
        (["--ssh-pubkey", 'command="id" ' + good], "bare public key"),
        (["--ssh-pubkey", "ssh-ed25519 not-base64-at-all x"], "not a valid public key"),
        (["--ssh-pubkey", good + "\nssh-ed25519 AAAA second"], "one line"),
        (["--age-recipient", "AGE-SECRET-KEY-1ABC"], "age1"),
        (["--age-recipient", "age1short"], "age1"),
        (["--surprise"], "unknown argument"),
        (["--ssh-pubkey"], "needs a value"),
        (["--ssh-pubkey-file", str(tmp_path / "absent.pub")], "cannot read"),
    ]
    for args, message in cases:
        done = _install(*args)
        assert done.returncode == 2, (args, done.stderr)
        assert message in done.stderr, (args, done.stderr)
        assert done.stdout == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="as root the script would really install")
def test_install_needs_root_after_the_keys_are_valid(tmp_path):
    done = _install(
        "--ssh-pubkey", _public_key(tmp_path), "--age-recipient", AGE_RECIPIENT
    )
    assert done.returncode == 1 and "run as root" in done.stderr


def test_no_key_material_is_kept_in_the_repository():
    # an ssh public key body or an age recipient in any shipped file would be a fixed identity
    for path in BACKUP_DIR.iterdir():
        text = path.read_text()
        assert not re.search(r"AAAAC3NzaC1lZDI1NTE5AAAA[A-Za-z0-9+/]{20,}", text), path
        assert not re.search(r"age1[0-9a-z]{50,}", text), path
        assert "PRIVATE KEY" not in text and "AGE-SECRET-KEY" not in text, path


def test_install_is_idempotent_and_least_privilege():
    need_root_host()
    first = run_install(lock_account=True)
    assert first.returncode == 0, first.stdout + first.stderr
    before = installed_state()
    second = run_install()
    assert second.returncode == 0, second.stdout + second.stderr
    assert second.stdout.strip() == "no changes", second.stdout
    assert installed_state() == before

    def stat_of(path):
        return as_root(["stat", "-c", "%U:%G %a", path], text=True).stdout.strip()

    assert stat_of(HOME_DIR) == "root:root 755"
    assert stat_of(HOME_DIR + "/.ssh") == "root:root 755"
    assert stat_of(HOME_DIR + "/.ssh/authorized_keys") == "root:root 644"
    assert stat_of(STATE_DIR) == "root:haiplane-backup 750"
    assert stat_of(LIB_DIR + "/snapshot-serve.sh") == "root:root 755"

    account = (
        as_root(["getent", "passwd", "haiplane-backup"], text=True)
        .stdout.strip()
        .split(":")
    )
    assert account[5] == HOME_DIR and account[6] == "/bin/sh"
    assert (
        as_root(["getent", "shadow", "haiplane-backup"], text=True).stdout.split(":")[1]
        == "*"
    )

    keys = shared_keys()
    authorized = as_root(["cat", HOME_DIR + "/.ssh/authorized_keys"], text=True).stdout
    forced = 'restrict,command="%s/snapshot-serve.sh %s" ' % (LIB_DIR, STATE_DIR)
    assert authorized == forced + keys["pub"].read_text().strip() + "\n"
    # the user reads what it needs and changes nothing of it
    assert as_backup(["cat", HOME_DIR + "/.ssh/authorized_keys"]).returncode == 0
    assert (
        as_backup(
            ["sh", "-c", "echo x >> %s/.ssh/authorized_keys" % HOME_DIR]
        ).returncode
        != 0
    )
    assert as_backup(["touch", STATE_DIR + "/planted"]).returncode != 0
    assert as_backup(["ls", STATE_DIR]).returncode == 0

    dropin = as_root(["cat", DROPIN], text=True).stdout
    for line in (
        "Match User haiplane-backup",
        "ForceCommand %s/snapshot-serve.sh %s" % (LIB_DIR, STATE_DIR),
        "PasswordAuthentication no",
        "AllowTcpForwarding no",
        "X11Forwarding no",
        "PermitTTY no",
        "AllowAgentForwarding no",
        "PermitTunnel no",
    ):
        assert line in dropin.replace("    ", ""), line
    assert as_root([sshd_path(), "-t"], text=True).returncode == 0
    effective = as_root(
        [sshd_path(), "-T", "-C", "user=haiplane-backup,host=localhost,addr=127.0.0.1"],
        text=True,
    ).stdout.splitlines()
    for line in (
        "passwordauthentication no",
        "permittty no",
        "allowtcpforwarding no",
        "permittunnel no",
    ):
        assert line in effective, line

    def unit(name):
        return as_root(["cat", "/etc/systemd/system/" + name], text=True).stdout

    hourly_timer, daily_timer = (
        unit("haiplane-snapshot-hourly.timer"),
        unit("haiplane-snapshot-daily.timer"),
    )
    assert (
        "OnCalendar=*:17:00" in hourly_timer
        and "RandomizedDelaySec=5min" in hourly_timer
    )
    assert "OnCalendar=*-*-* 03:47:00 UTC" in daily_timer
    assert "Persistent=true" in hourly_timer and "Persistent=true" in daily_timer
    hourly_service = unit("haiplane-snapshot-hourly.service")
    assert (
        "TemporaryFileSystem=%s/work:size=512M,mode=0700" % STATE_DIR in hourly_service
    )
    assert "UMask=0077" in hourly_service and "PrivateTmp=yes" in hourly_service
    assert "TimeoutStartSec=600" in hourly_service
    assert (
        "hub-snapshot.py hourly" in hourly_service
        and "hub-snapshot.py daily" in unit("haiplane-snapshot-daily.service")
    )


def test_ci_installs_the_tools_and_forbids_skips():
    text = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text()
    job_env = text.split("    env:\n", 1)[1].split("\n    steps:", 1)[0]
    assert 'CI_REQUIRE_ROOT_TESTS: "1"' in job_env, (
        "the variable must be set for the whole job"
    )
    step = text.split("- name: Install age and openssh-server (backup tests)", 1)[
        1
    ].split("      - name:", 1)[0]
    assert "apt-get install" in step and "age" in step and "openssh-server" in step
    assert (
        "command -v age-keygen" in step
        and "/usr/sbin/sshd" in step
        and "sudo -n true" in step
    )
    assert "continue-on-error" not in step and "|| true" not in step


def test_sshd_is_reloaded_on_every_run_and_before_the_key_appears():
    """A run that failed after writing the drop-in must not leave sshd on the old config.

    systemd is not available to the test, so the contract is read from the script:
    one unconditional reload, placed before the authorized_keys file is written."""
    text = (BACKUP_DIR / "install.sh").read_text()
    reload_at = text.index("systemctl reload ssh")
    key_at = text.index('put_file "$AUTH_KEYS"')
    assert reload_at < key_at
    before = text[:reload_at].rsplit("\n", 3)[-3:]
    assert any('if [ "$SERVICES" = 1 ]' in line for line in before)
    assert "CHANGED" not in "".join(before), "the reload must not depend on a change"
    assert 'command="%s/snapshot-serve.sh %s"' in text
