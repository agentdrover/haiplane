"""The manual deploy goes through the canonical script and stops on any error (#1620).

The real scripts/manual_deploy.sh runs in a tmpdir with shims for uv, ssh and the
REMOTE rsync (the local copy uses the real rsync). The properties: a failed lock
check or export ships nothing; a tree from before #1620 (no ``build`` group)
needs an explicit build set and still gets the canonical server scripts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/manual_deploy.sh"

_UV = r"""#!/usr/bin/env bash
echo "uv $*" >>"$CALLS"
[ "$1:$UV_FAIL" = lock:check ] && exit 1
if [ "$1" = export ]; then
  out=""
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do [ "${args[$i]}" = -o ] && out="${args[$((i + 1))]}"; done
  # A failing export may still leave a (partial) file behind: only the exit code tells.
  printf 'pkg==1.0 \\\n    --hash=sha256:%s\n' "$(printf 'ab%.0s' $(seq 32))" >"$out"
  [ "$UV_FAIL" = export ] && exit 3
fi
exit 0
"""
_SSH = r"""#!/usr/bin/env bash
echo "ssh $*" >>"$CALLS"
cat >"$SSH_STDIN"
"""
_RSYNC = r"""#!/usr/bin/env bash
case "$*" in
  *@*)
    echo "rsync-remote $*" >>"$CALLS"
    for last; do :; done
    src="${*: -2:1}"
    /usr/bin/env rsync -a "$src" "$STAGED"/
    ;;
  *) exec "$REAL_RSYNC" "$@" ;;
esac
"""


def _setup(tmp: Path, *, build_group: bool):
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    for name, body in (("uv", _UV), ("ssh", _SSH), ("rsync", _RSYNC)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    tree = tmp / "tree"
    tree.mkdir()
    group = 'build = [\n    "hatchling",\n]\n' if build_group else ""
    (tree / "pyproject.toml").write_text(f"[dependency-groups]\n{group}dev = []\n")
    (tree / "uv.lock").write_text("version = 1\n")
    (tree / "deploy").mkdir()
    (tree / "deploy/remote-deploy.sh").write_text("# OLD server script\n")
    staged = tmp / "staged"
    staged.mkdir()
    return bin_dir, tree, staged


def _run(tmp: Path, bin_dir: Path, tree: Path, staged: Path, *args: str, **env: str):
    calls = tmp / "calls.log"
    calls.touch()
    # The real rsync is found BEFORE the shim directory goes first on PATH.
    real_rsync = shutil.which("rsync")
    assert real_rsync
    full = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CALLS": str(calls),
        "SSH_STDIN": str(tmp / "ssh_stdin"),
        "STAGED": str(staged),
        "REAL_RSYNC": real_rsync,
        "DEPLOY_USER": "deployer",
        "DEPLOY_HOST": "host.invalid",
        "UV_FAIL": "",
        **env,
    }
    done = subprocess.run(
        ["bash", str(SCRIPT), *args, str(tree)],
        env=full,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done, calls.read_text()


def test_a_failed_lock_check_or_export_ships_nothing(tmp_path) -> None:
    """P2 (#1620): uv lock --check or an export fails -> no rsync to the server,
    no server script; the exit code is non-zero."""
    for fail, fragment in (("check", "uv lock --check"), ("export", "uv export")):
        sub = tmp_path / fail
        sub.mkdir()
        bin_dir, tree, staged = _setup(sub, build_group=True)
        done, calls = _run(sub, bin_dir, tree, staged, UV_FAIL=fail)
        assert done.returncode != 0, (fail, done.stdout, done.stderr)
        assert fragment in calls, calls
        assert "rsync-remote" not in calls and "ssh " not in calls, calls
        assert list(staged.iterdir()) == []


def test_a_current_tree_ships_both_exports_and_runs_the_canonical_script(tmp_path):
    bin_dir, tree, staged = _setup(tmp_path, build_group=True)
    done, calls = _run(tmp_path, bin_dir, tree, staged)
    assert done.returncode == 0, (done.stdout, done.stderr)
    lines = calls.splitlines()
    assert lines[0] == "uv lock --check"
    assert any("--no-dev" in x and "requirements.runtime.txt" in x for x in lines)
    assert any("--only-group build" in x for x in lines)
    assert (staged / "requirements.runtime.txt").exists()
    assert (staged / "requirements.build.txt").exists()
    assert (staged / "uv.lock").exists()
    assert (staged / "deploy/remote-deploy.sh").read_text() == (
        ROOT / "deploy/remote-deploy.sh"
    ).read_text()
    assert "bash -s" in calls
    assert (tmp_path / "ssh_stdin").read_text() == (
        ROOT / "deploy/remote-deploy.sh"
    ).read_text()


def test_rollback_to_a_commit_before_the_build_group_needs_an_explicit_set(tmp_path):
    """P2 (#1620): an old lock has no build group: without --build-set nothing is
    shipped; with it the runtime set is the old lock's, the build set is the given
    file, and the server scripts are the CANONICAL ones, not the old tree's."""
    bin_dir, tree, staged = _setup(tmp_path, build_group=False)
    done, calls = _run(tmp_path, bin_dir, tree, staged)
    assert done.returncode != 0 and "--build-set" in done.stderr, done.stderr
    assert "rsync-remote" not in calls and "ssh " not in calls, calls

    build = tmp_path / "current-build.txt"
    build.write_text("hatchling==1.32.4 \\\n    --hash=sha256:cd\n")
    done, calls = _run(tmp_path, bin_dir, tree, staged, "--build-set", str(build))
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert "--only-group build" not in calls, "an old lock has no build group"
    assert (staged / "requirements.build.txt").read_text() == build.read_text()
    assert "pkg==1.0" in (staged / "requirements.runtime.txt").read_text()
    for name in ("remote-deploy.sh", "review-drain.sh", "predeploy-backup.py"):
        assert (staged / "deploy" / name).read_text() == (
            ROOT / "deploy" / name
        ).read_text(), f"{name} must be the canonical one, not the old tree's"
    assert (tmp_path / "ssh_stdin").read_text() == (
        ROOT / "deploy/remote-deploy.sh"
    ).read_text()
