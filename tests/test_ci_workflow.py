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
