"""The deploy callback names the project it deployed (#1453).

``POST /api/deploys`` accepts a project slug, but the workflow sent only
``{sha, ref, status}``, so every row in ``releases`` on production came in with
an empty ``project_id``. #915 papers over that with "no project means default";
that holds only while one project deploys.

The test reads the workflow, because that is where the property lives: the body
the step actually builds, the variable it comes from, and the fallback that
keeps an unset or empty repository variable on ``default``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
CALLBACK_STEP = "Report deploy to Hub"
PROJECT_EXPR = "${{ vars.HAIPLANE_HUB_PROJECT || 'default' }}"


def _callback_step() -> dict:
    if not WORKFLOW.exists():  # pragma: no cover - only in a partial checkout
        pytest.skip(f"workflow not present at {WORKFLOW}")
    workflow = yaml.safe_load(WORKFLOW.read_text())
    for step in workflow["jobs"]["deploy"]["steps"]:
        if step.get("name") == CALLBACK_STEP:
            return step
    raise AssertionError(f"no {CALLBACK_STEP!r} step in the deploy job")


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


def _posted_body(run: str, values: dict[str, str]) -> dict:
    """The JSON the step really POSTs: the step runs with a curl shim (#1590
    moved the body to a serializer, so the text of the step is no longer a template)."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        (tmp / "bin").mkdir()
        shim = tmp / "bin/curl"
        shim.write_text(_CURL)
        shim.chmod(0o755)
        script = tmp / "step.sh"
        script.write_text(run)
        body = tmp / "body.json"
        env = {
            **os.environ,
            **values,
            "PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "HAIPLANE_HUB_URL": "http://hub.invalid",
            "HAIPLANE_HUB_CI_TOKEN": "t",
            "ROLLOUT_OUTCOME": "success",
            "CURL_BODY": str(body),
        }
        done = subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60
        )
        assert done.returncode == 0, done.stderr
        return json.loads(body.read_text())


def _render_project(expr: str, var_value: str | None) -> str:
    """Evaluate the one expression form GitHub allows here: ``vars.X || 'lit'``."""
    match = re.fullmatch(
        r"\$\{\{\s*vars\.HAIPLANE_HUB_PROJECT(?:\s*\|\|\s*'([^']*)')?\s*\}\}", expr
    )
    assert match, f"project must come from vars.HAIPLANE_HUB_PROJECT, got {expr!r}"
    fallback = match.group(1) or ""
    return var_value or fallback


def test_deploy_callback_names_the_project():
    # AC-1 (#1453): the body carries project, taken from the repository
    # variable, and an unset OR empty variable still reports default.
    step = _callback_step()
    env = step.get("env", {})
    expr = env.get("DEPLOYED_PROJECT")
    assert expr == PROJECT_EXPR

    for var_value, expected in ((None, "default"), ("", "default"), ("snip", "snip")):
        project = _render_project(expr, var_value)
        assert project == expected
        body = _posted_body(
            step["run"],
            {
                "DEPLOYED_SHA": "abc123",
                "DEPLOYED_REF": "main",
                "STATUS": "success",
                "DEPLOYED_PROJECT": project,
            },
        )
        assert body == {
            "sha": "abc123",
            "ref": "main",
            "status": "success",
            "project": expected,
        }

    # The callback must stay unable to turn a completed deploy red, and a
    # rejected slug (404) must stay visible as a warning.
    assert step.get("continue-on-error") is True
    assert "::warning::Hub did not record the deploy (HTTP $code)" in step["run"]


# ---- #1620: the lock exports are complete and really install ------------------


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, f"{argv}\n{done.stdout}\n{done.stderr}"
    return done.stdout


def test_the_lock_exports_install_into_a_clean_python311(tmp_path) -> None:
    """AC-4 (#1620): the deploy job's OWN export step runs on a copy of the lock;
    both sets install into a clean Python 3.11 exactly as the server does it
    (hashes, wheels only, no resolution), then the app, then it imports. A build
    set missing what the editable hook needs, a wheel-less pin or a duplicate pin
    between the two sets fails HERE, in CI, and not on the server."""
    import shutil

    uv = shutil.which("uv")
    assert uv, "uv is required to run the repository tests"
    root = WORKFLOW.parents[2]
    export_step = next(
        s
        for s in yaml.safe_load(WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
        if s.get("name") == "Export hashed dependency sets from uv.lock"
    )
    work = tmp_path / "tree"
    work.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(root / name, work / name)
    _run(["bash", "-eo", "pipefail", "-c", export_step["run"]], work)
    runtime, build = work / "requirements.runtime.txt", work / "requirements.build.txt"
    assert "--hash=sha256:" in runtime.read_text()
    assert "hatchling==" in build.read_text()
    assert "editables==" in build.read_text(), "the editable hook needs editables"
    assert "hatchling==" not in runtime.read_text()

    venv = tmp_path / "venv"
    _run([uv, "venv", "--python", "3.11", "--seed", str(venv)], work)
    python = venv / "bin" / "python"
    assert (
        _run(
            [str(python), "-c", "import sys; print(sys.version_info[:2])"], work
        ).strip()
        == "(3, 11)"
    )
    pip = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    _run(
        [
            *pip,
            "--require-hashes",
            "--only-binary=:all:",
            "--no-deps",
            "-r",
            str(build),
            "-r",
            str(runtime),
        ],
        work,
    )
    _run([*pip, "--no-deps", "--no-build-isolation", "-e", str(root)], work)
    # From a directory that is not the repo, so the checkout is not on sys.path.
    out = _run(
        [
            str(python),
            "-c",
            "import hub, hub.app, hub.mcp_server, hub.cli; print(hub.__file__)",
        ],
        work,
    )
    assert out.strip() == str(root / "hub" / "__init__.py"), out
    _run([str(python), "-m", "pip", "check"], work)
