"""Workflow templates of a satellite's trusted CI report (#1848).

Two files. Provisioning of a new GitHub project lays them down itself
(hub.services.external_ci_seed); for an existing project the owner copies them.
Either way the environment, keys and ci_contract are the owner's
(docs/satellite-ci-report.md):

* the PRODUCER (``satellite-ci-producer.yml``): the tests job. It runs the
  branch's code, so it has no ``environment`` and no report key, only the read
  key; it uploads the results as the artifact ``hub-ci-results-<attempt>``;
* the SENDER (``satellite-ci-report.yml``): a ``workflow_run`` workflow of the
  default branch, bound to an environment, that posts the report.

The workflow file the hub confirms is the project's ``ci_contract``; the sender
is told the same file through its ``workflow-path`` input.
"""

from __future__ import annotations

import re

from hub import brand
from hub.services.ci_contract import validated_ci_contract
from hub.services.workflow_seed import DEFAULT_AC_RUNNER, read_template

SENDER_TEMPLATE = "satellite-ci-report.yml"
PRODUCER_TEMPLATE = "satellite-ci-producer.yml"
_PLACEHOLDER = re.compile(r"@@([A-Z_]+)@@")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}$")


def _render(name: str, values: dict[str, str]) -> str:
    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError(f"{name}: unknown placeholder {key}")
        return values[key]

    return _PLACEHOLDER.sub(sub, read_template(name))


def _checked(workflow_name: str, workflow_path: str) -> str:
    if not _NAME.match(workflow_name or ""):
        raise ValueError(f"workflow name {workflow_name!r} is not a plain name")
    # The same validator as the contract: a path the hub would refuse is refused here.
    return validated_ci_contract(
        {"workflow_path": workflow_path, "required_checks": ["tests"]}
    )["workflow_path"]


def render_sender(workflow_name: str, workflow_path: str) -> str:
    """The sender workflow for the tests workflow ``workflow_name`` (file ``workflow_path``)."""
    path = _checked(workflow_name, workflow_path)
    return _render(
        SENDER_TEMPLATE,
        {
            "WORKFLOW_NAME": workflow_name,
            "WORKFLOW_PATH": path,
            "CI_SEND_ACTION": brand.ci_send_action(),
        },
    )


def render_producer(
    workflow_name: str,
    workflow_path: str,
    *,
    push_branches: str = '["main"]',
    ac_runner: str = DEFAULT_AC_RUNNER,
) -> str:
    """The tests workflow skeleton with the collector and the artifact upload."""
    _checked(workflow_name, workflow_path)
    return _render(
        PRODUCER_TEMPLATE,
        {
            "WORKFLOW_NAME": workflow_name,
            "PUSH_BRANCHES": push_branches,
            "AC_RUNNER": ac_runner,
            "CI_REPORT_ACTION": brand.ci_report_action(),
        },
    )
