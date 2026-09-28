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
import re
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


def _posted_body(run: str, values: dict[str, str]) -> dict:
    """The JSON the curl ``-d`` sends, with shell variables filled in."""
    match = re.search(r'-d "((?:[^"\\]|\\.)*)"', run)
    assert match, "the callback must post a JSON body with -d"
    template = match.group(1).replace('\\"', '"')
    for name, value in values.items():
        template = template.replace(f"${name}", value)
    assert "$" not in template, f"unfilled shell variable in body: {template}"
    return json.loads(template)


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
