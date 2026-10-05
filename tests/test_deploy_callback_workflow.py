"""The deploy job tells the Hub what is running (#496).

The Hub knew when it MERGED a change and read that as delivery. On 21.08.2026
task #823 sat ``completed`` with its PR merged into develop while this very job
was marked ``skipped`` — deployment runs from main. Nobody could see that from
the Hub; it took reading GitHub's logs.

These tests read the workflow itself, because that is where the property
lives. They cannot prove a callback reaches the Hub — only a real release does
that — and they are written to fail for the reasons that would matter: the step
disappearing, being moved before the rollout, hard-coding success, or being
allowed to turn a completed deploy into a red job.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
CALLBACK_STEP = "Report deploy to Hub"


@pytest.fixture
def deploy_steps() -> list[dict]:
    if not WORKFLOW.exists():  # pragma: no cover - only in a partial checkout
        pytest.skip(f"workflow not present at {WORKFLOW}")
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return workflow["jobs"]["deploy"]["steps"]


def _callback(steps: list[dict]) -> dict:
    for step in steps:
        if step.get("name") == CALLBACK_STEP:
            return step
    raise AssertionError(f"no {CALLBACK_STEP!r} step in the deploy job")


def test_callback_step_runs_after_deploy_even_on_failure(deploy_steps: list[dict]):
    # AC-1 (#496): after the rollout, and not conditional on it succeeding —
    # a deploy that FELL OVER is a fact the Hub needs too. Reporting only the
    # good ones would show a pipeline that never breaks.
    names = [step.get("name") for step in deploy_steps]
    assert CALLBACK_STEP in names

    rollout_at = names.index("Deploy and health check")
    callback_at = names.index(CALLBACK_STEP)
    assert rollout_at < callback_at, "nothing to report before the rollout ran"
    assert _callback(deploy_steps).get("if") == "always()"


def test_status_follows_the_deploy_outcome(deploy_steps: list[dict]):
    # AC-2 (#496): the status is derived, never asserted. A hard-coded
    # "success" would make the Hub's record of production a formality.
    step = _callback(deploy_steps)
    env = step.get("env", {})

    assert env.get("ROLLOUT_OUTCOME") == "${{ steps.rollout.outcome }}", (
        "the rollout step must be the source of the reported status"
    )
    assert "STATUS=failed" in step["run"], "a failed rollout must be reportable"
    assert "STATUS=success" in step["run"]

    rollout = next(
        s for s in deploy_steps if s.get("name") == "Deploy and health check"
    )
    assert rollout.get("id") == "rollout", "the outcome is read through this id"


def test_callback_reports_the_deployed_sha(deploy_steps: list[dict]):
    # AC-3 (#496): the commit that shipped, to the endpoint that records it.
    step = _callback(deploy_steps)
    env = step.get("env", {})

    assert env.get("DEPLOYED_SHA") == "${{ github.sha }}"
    assert env.get("DEPLOYED_REF") == "${{ github.ref_name }}"
    assert "/api/deploys" in step["run"]
    assert "$DEPLOYED_SHA" in step["run"]


def test_reporting_failure_does_not_fail_the_deploy(deploy_steps: list[dict]):
    # AC-4 (#496): a red job here would claim the deploy did not happen when
    # it did. The failure is still printed — the Hub reads a missing record as
    # "unknown", never as "not deployed" (#839), so a quiet gap stays honest.
    step = _callback(deploy_steps)

    assert step.get("continue-on-error") is True
    assert "::warning::" in step["run"], "a swallowed failure must still be visible"
    assert "::notice::" in step["run"], "a fork without secrets skips, and says so"


# ---- #1590: итог бэкапа доезжает до отчёта о деплое -------------------------

_SSH = """#!/usr/bin/env bash
cat >/dev/null
cat "$SSH_OUT"
exit "$SSH_RC"
"""

_CURL = """#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in
    --data-binary) cp "${2#@}" "$CURL_BODY"; shift ;;
    -o) printf '{}' >"$2"; shift ;;
  esac
  shift
done
printf 200
"""


def _run(
    step: dict, tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess:
    shell = tmp_path / "step.sh"
    shell.write_text(step["run"])
    return subprocess.run(
        ["bash", str(shell)],
        cwd=tmp_path,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("ssh_out", "ssh_rc", "expected"),
    [
        (
            "deploy ok\nbackup: ok /b/predeploy-1.db.gz size=10 integrity=ok seconds=1\n",
            0,
            "ok /b/predeploy-1.db.gz size=10 integrity=ok seconds=1",
        ),
        (
            'backup: failed (cannot open "hub.db": it\'s locked)\n',
            1,
            'failed (cannot open "hub.db": it\'s locked)',
        ),
        (
            "backup: skipped (обход DEPLOY_SKIP_BACKUP=1)\ndeploy ok\n",
            0,
            "skipped (обход DEPLOY_SKIP_BACKUP=1)",
        ),
        ("старый скрипт, строки backup нет\n", 1, None),
    ],
)
def test_the_backup_verdict_reaches_the_report_at_any_exit_code(
    deploy_steps: list[dict], tmp_path: Path, ssh_out: str, ssh_rc: int, expected
):
    # AC-3 (#1590): rollout захватывает вывод ssh, пишет строку backup в
    # GITHUB_OUTPUT ДАЖЕ при ненулевом коде и возвращает исходный код; отчёт
    # собирает JSON сериализатором — кавычки в причине его не ломают.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", _SSH), ("curl", _CURL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/remote-deploy.sh").write_text("echo remote\n")
    out_file = tmp_path / "ssh.out"
    out_file.write_text(ssh_out)
    github_output = tmp_path / "github_output"
    github_output.write_text("")
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(github_output),
        "SSH_OUT": str(out_file),
        "SSH_RC": str(ssh_rc),
        "DEPLOY_USER": "u",
        "DEPLOY_HOST": "h",
    }

    rollout = next(s for s in deploy_steps if s.get("id") == "rollout")
    done = _run(rollout, tmp_path, base)
    assert done.returncode == ssh_rc, "исходный код rollout потерян: " + done.stderr
    outputs = dict(
        line.split("=", 1)
        for line in github_output.read_text().splitlines()
        if "=" in line
    )
    assert outputs.get("backup") == expected

    body_file = tmp_path / "body.json"
    report = _run(
        _callback(deploy_steps),
        tmp_path,
        {
            **base,
            "HAIPLANE_HUB_URL": "http://hub.invalid",
            "HAIPLANE_HUB_CI_TOKEN": "t",
            "ROLLOUT_OUTCOME": "success" if ssh_rc == 0 else "failure",
            "DEPLOYED_SHA": "abcdef1",
            "DEPLOYED_REF": "main",
            "DEPLOYED_PROJECT": "default",
            "BACKUP_LINE": outputs.get("backup", ""),
            "CURL_BODY": str(body_file),
        },
    )
    assert report.returncode == 0, report.stderr
    body = json.loads(body_file.read_text())
    assert body["sha"] == "abcdef1"
    assert body["status"] == ("success" if ssh_rc == 0 else "failed")
    assert body.get("backup") == expected


def test_the_sha_travels_to_the_server_as_a_file_of_the_tree(deploy_steps: list[dict]):
    # Ключ CI под forced command: ни аргументов, ни переменных — sha едет файлом.
    step = next(
        s for s in deploy_steps if s.get("name") == "Sync working tree to staging"
    )
    assert step["env"]["DEPLOYED_SHA"] == "${{ github.sha }}"
    assert "> .deploy-sha" in step["run"]
    assert step["run"].index(".deploy-sha") < step["run"].index("rsync")
