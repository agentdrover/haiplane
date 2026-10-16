"""deploy/backup/install.sh (#1707): arguments, then a real installation on a disposable host."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.backup_support import (
    BACKUP_DIR,
    DROPIN,
    HUB_NAME,
    HOME_DIR,
    INSTALL,
    LIB_DIR,
    PATHS_CONF,
    SHARED,
    STATE_DIR,
    SharedLock,
    as_backup,
    as_root,
    installed_state,
    need_root_host,
    run_install,
    shared_keys,
    sshd_path,
    must_have,
    write_sites,
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
    # shipped files only: a __pycache__ directory (an import elsewhere) is not one
    shipped = [p for p in BACKUP_DIR.iterdir() if p.is_file() and p.suffix != ".pyc"]
    assert {p.name for p in shipped} >= {"install.sh", "hub-snapshot.py", "README.md"}
    for path in shipped:
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


def _paths_lines() -> list[str]:
    return as_root(["cat", PATHS_CONF], text=True).stdout.splitlines()


def test_nginx_site_is_found_by_server_name(tmp_path):
    need_root_host()
    sites = tmp_path / "sites"
    with SharedLock() as host:
        try:
            write_sites(sites)
            as_root(["rm", "-f", PATHS_CONF])
            done = run_install(["--sites-dir", str(sites)], held=host)
            assert done.returncode == 0, done.stdout + done.stderr
            lines = _paths_lines()
            # exactly the file whose server_name lists the host: not a name that merely contains it
            assert "required %s/%s" % (sites, HUB_NAME) in lines
            assert [x for x in lines if str(sites) in x] == [
                "required %s/%s" % (sites, HUB_NAME)
            ]
            assert "required /etc/" + HUB_NAME in lines
            assert "optional /etc/haiplane-backup" in lines
            assert "required /etc/nginx/sites-available/agenthai.ru" not in lines

            # another host name picks another file
            as_root(["rm", "-f", PATHS_CONF])
            done = run_install(
                ["--sites-dir", str(sites), "--site-host", "haiplane.com"], held=host
            )
            assert done.returncode == 0, done.stderr
            assert "required %s/haiplane" % sites in _paths_lines()

            # no file, or two files: refused before any change
            for name, files in (
                ("none", {"haiplane": "server {\n    server_name haiplane.com;\n}\n"}),
                (
                    "two",
                    {
                        "a": "server {\n    server_name agenthai.ru;\n}\n",
                        "b": "server {\n    server_name x.example agenthai.ru;\n}\n",
                    },
                ),
            ):
                write_sites(sites, files)
                as_root(["rm", "-f", PATHS_CONF])
                before = installed_state()
                done = run_install(["--sites-dir", str(sites)], held=host)
                assert done.returncode != 0, name
                assert "nothing was changed" in done.stderr, (name, done.stderr)
                assert installed_state() == before, name
                if name == "two":
                    assert (
                        str(sites / "a") in done.stderr
                        and str(sites / "b") in done.stderr
                    )
        finally:
            as_root(["rm", "-f", PATHS_CONF])


def test_nginx_site_search_reads_directives_links_and_explicit_path(tmp_path):
    need_root_host()
    nginx = tmp_path / "nginx"
    sites = nginx / "sites-available"
    real_dir = nginx / "real"
    outside = tmp_path / "outside"
    site_text = "server {\n    server_name agenthai.ru;\n}\n"

    def prepare(files, links=()):
        write_sites(sites, files)
        for directory in (real_dir, outside):
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True)
        (real_dir / "site").write_text(site_text)
        (outside / "site").write_text(site_text)
        for name, target in links:
            (sites / name).symlink_to(target)

    def attempt(host, extra=()):
        as_root(["rm", "-f", PATHS_CONF])
        before = installed_state()
        done = run_install(["--sites-dir", str(sites), *extra], held=host)
        if done.returncode != 0:
            assert "nothing was changed" in done.stderr, done.stderr
            assert installed_state() == before
        return done

    with SharedLock() as host:
        try:
            # a name in a comment is not a name
            prepare(
                {"c": "server {\n    server_name other.example # agenthai.ru ;\n}\n"}
            )
            assert attempt(host).returncode != 0
            # a directive over several lines, and one right after '{'
            prepare(
                {
                    "m": "server {\n  server_name\n    x.example\n    agenthai.ru\n  ;\n}\n"
                }
            )
            assert attempt(host).returncode == 0
            assert "required %s/m" % sites in _paths_lines()
            prepare({"b": "server {server_name agenthai.ru;}\n"})
            assert attempt(host).returncode == 0
            assert "required %s/b" % sites in _paths_lines()

            # a link: the real file goes into the allowlist; a link out of nginx's tree is refused
            prepare({}, links=[("link", real_dir / "site")])
            assert attempt(host).returncode == 0
            assert "required %s" % os.path.realpath(real_dir / "site") in _paths_lines()
            assert not [x for x in _paths_lines() if x.endswith("/link")]
            prepare({}, links=[("link", outside / "site")])
            done = attempt(host)
            assert done.returncode != 0 and "outside" in done.stderr

            # --nginx-site replaces the search and must be an existing regular file
            prepare({})
            assert attempt(host).returncode != 0
            for bad in (str(sites / "absent"), str(sites)):
                assert attempt(host, ["--nginx-site", bad]).returncode != 0, bad
            done = attempt(host, ["--nginx-site", str(real_dir / "site")])
            assert done.returncode == 0, done.stderr
            assert "required %s" % (real_dir / "site") in _paths_lines()
        finally:
            as_root(["rm", "-f", PATHS_CONF])


def test_relative_nginx_paths_reach_paths_conf_as_absolute_ones(tmp_path):
    need_root_host()
    from tests.backup_support import load_snapshot

    site_text = "server {\n    server_name agenthai.ru;\n}\n"
    (tmp_path / "hub.conf").write_text(site_text)
    write_sites(tmp_path / "rel" / "sites-available", {"hub": site_text})
    snap = load_snapshot()
    with SharedLock() as host:
        try:
            for extra, expected in (
                (["--nginx-site", "./hub.conf"], tmp_path / "hub.conf"),
                (
                    ["--sites-dir", "rel/sites-available"],
                    tmp_path / "rel/sites-available/hub",
                ),
            ):
                as_root(["rm", "-f", PATHS_CONF])
                done = run_install(extra, held=host, cwd=tmp_path)
                assert done.returncode == 0, (extra, done.stderr)
                want = "required %s" % os.path.realpath(expected)
                assert want in _paths_lines(), (extra, _paths_lines())
                # the file the daily run reads: every entry must be an absolute path
                conf = tmp_path / "paths.conf"
                conf.write_text("\n".join(_paths_lines()) + "\n")
                cfg = snap.Config({"SNAP_PATHS_CONF": str(conf)})
                assert snap._parse_paths(cfg), extra
        finally:
            as_root(["rm", "-f", PATHS_CONF])


# paths.conf as the installer of origin/develop wrote it
OLD_PATHS_CONF = """# Allowlist of the daily settings archive (#1707). Nothing outside it is read.
unit haiplane-hub
required /etc/georoute
required /etc/systemd/networkd.conf.d
required /etc/fail2ban/jail.local
required /etc/nginx/sites-available/agenthai.ru
# optional-unit haiplane-hub-poller
# optional /etc/haiplane-review-runner
"""


def test_reinstall_keeps_paths_conf_and_names_missing_defaults(tmp_path):
    need_root_host()
    from tests.backup_support import decrypt, load_snapshot

    sites = tmp_path / "sites"
    with SharedLock() as host:
        try:
            write_sites(sites)
            as_root(["install", "-d", "-m", "0755", "/etc/haiplane-backup"], check=True)
            as_root(["tee", PATHS_CONF], input=OLD_PATHS_CONF, text=True, check=True)
            done = run_install(["--sites-dir", str(sites)], held=host)
            assert done.returncode == 0, done.stdout + done.stderr
            assert _paths_lines() == OLD_PATHS_CONF.splitlines(), "not rewritten"
            site = "%s/%s" % (sites, HUB_NAME)
            warned = done.stderr
            assert (
                "required /etc/nginx/sites-available/agenthai.ru does not exist"
                in warned.replace("required path ", "required ")
            )
            assert "replace the entry with 'required %s'" % site in warned
            missing = [
                line.split("default entry: ", 1)[1]
                for line in warned.splitlines()
                if "lacks the default entry: " in line
            ]
            assert missing == [
                "required " + site,
                "required /etc/" + HUB_NAME,
                "optional /etc/haiplane-backup",
            ]
            recipient = as_root(["cat", "/etc/haiplane-backup/recipient.txt"]).stdout

            # the owner's correction by the README: replace the old entry, add the missing ones
            corrected = (
                OLD_PATHS_CONF.replace(
                    "required /etc/nginx/sites-available/agenthai.ru\n", ""
                )
                + "\n".join(missing)
                + "\n"
            )
            root = tmp_path / "sysroot"
            for rel, text in {
                "etc/georoute/a.conf": "r\n",
                "etc/fail2ban/jail.local": "[DEFAULT]\n",
                "etc/%s/%s.env" % (HUB_NAME, HUB_NAME): "HAIPLANE_HUB_DB=/db\n",
                "etc/systemd/system/haiplane-hub.service": "[Service]\n",
                site.lstrip("/"): "server {\n    server_name agenthai.ru;\n}\n",
                "etc/haiplane-backup/paths.conf": corrected,
            }.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)
            (root / "etc/systemd/networkd.conf.d").mkdir(parents=True)
            (root / "etc/haiplane-backup/recipient.txt").write_bytes(recipient)
            systemctl = tmp_path / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\necho FragmentPath=/etc/systemd/system/haiplane-hub.service\n"
            )
            systemctl.chmod(0o755)
            (tmp_path / "state").mkdir()
            env = {
                "SNAP_ROOT": str(root),
                "SNAP_PATHS_CONF": str(root / "etc/haiplane-backup/paths.conf"),
                "SNAP_RECIPIENT_FILE": str(root / "etc/haiplane-backup/recipient.txt"),
                "SNAP_STATE_DIR": str(tmp_path / "state"),
                "SNAP_WORK_DIR": str(tmp_path / "state" / "work"),
                "SNAP_SYSTEMCTL": str(systemctl),
                "SNAP_GROUP": "",
                "SNAP_REQUIRE_TMPFS": "0",
            }
            import io
            import tarfile

            snap = load_snapshot()
            # the old entry alone leaves the archive without the site: daily refuses
            old = dict(env, SNAP_PATHS_CONF=str(tmp_path / "old.conf"))
            (tmp_path / "old.conf").write_text(OLD_PATHS_CONF)
            assert snap.main(["hub-snapshot.py", "daily"], old) == 1
            assert snap.main(["hub-snapshot.py", "daily"], env) == 0
            (data,) = (tmp_path / "state").glob("conf-*.tar.age")
            plain = decrypt(SHARED / "identity.txt", data)
            with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
                names = set(tar.getnames())
            assert "etc/haiplane-backup/paths.conf" in names
            assert "etc/haiplane-backup/recipient.txt" in names
        finally:
            as_root(["rm", "-f", PATHS_CONF])


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


def test_the_scan_survives_a_bytecode_directory_left_by_an_import(
    tmp_path, monkeypatch
):
    """Order independence: the module imported first, this scan second."""
    import sys

    from tests.backup_support import load_snapshot

    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    cache = BACKUP_DIR / "__pycache__"
    existed = cache.exists()
    load_snapshot()
    cache.mkdir(exist_ok=True)
    try:
        test_no_key_material_is_kept_in_the_repository()
    finally:
        if not existed:
            import shutil

            shutil.rmtree(cache, ignore_errors=True)
