"""Workflow templates provisioning lays into a project's repository (#476).

The hub merges a pull request only after reading the outcome of a GitHub
Actions job (#605/#606). Until this package existed, ``.github/workflows``
lived in the hub's own repository and nowhere else, so a freshly provisioned
project produced no run at all: the probe answered ``absent``, the delivery
gate refused with ``ci_absent``, and approved, green work had no supported
path to delivery. A repository the hub can create but cannot deliver from is
not provisioned.

The files here are TEMPLATES, not workflows: they carry ``@@NAME@@``
placeholders that :mod:`hub.services.workflow_seed` substitutes per project.
The placeholder syntax is deliberately not Jinja's ``{{ }}`` — GitHub Actions
spells its own expressions ``${{ }}``, and two languages sharing one set of
braces is how a renderer starts eating the workflow it renders.

Which branch goes into a placeholder is never decided here. It comes from
``hub.services.project_policy`` — the one reader of ``project.default_branch``
(#475) — because the hub integrates on ``develop``, calc-kids on ``master``
and spike-bo on ``main``, and a template that named any of them would be
correct for exactly one project.

Shipped as package data (see ``[tool.hatch.build.targets.wheel] artifacts`` in
pyproject.toml) and read through ``importlib.resources``, the same way
``hub/cli_templates/work_types`` is, so editable installs and wheels behave
identically.
"""
