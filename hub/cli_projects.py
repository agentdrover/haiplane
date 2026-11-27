"""Argument parser for the projects command family."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from hub import config
from hub.models import DEFAULT_FORGE, FORGES


def add_parser(
    sub: Any,
    *,
    list_projects: Callable[[argparse.Namespace], int],
    create_project: Callable[[argparse.Namespace], int],
    project_document: Callable[[argparse.Namespace], int],
) -> None:
    p_projects = sub.add_parser("projects", help="Manage projects (#338)")
    projects_sub = p_projects.add_subparsers(dest="projects_cmd", required=True)
    pp_list = projects_sub.add_parser("list", help="List projects")
    pp_list.add_argument(
        "--include-archived", dest="include_archived", action="store_true"
    )
    pp_list.add_argument(
        "--cached-branches", action="store_true", help="Use last observed branch states"
    )
    pp_list.set_defaults(func=list_projects)
    for name, help_text in (
        ("bootstrap", "First message for a project agent"),
        ("passport", "Read a project's agent passport"),
    ):
        document = projects_sub.add_parser(name, help=help_text)
        document.add_argument("slug")
        document.add_argument("--json", action="store_true")
        document.set_defaults(func=project_document, document=f"agent-{name}")
    pp_create = projects_sub.add_parser("create", help="Create a project (human token)")
    pp_create.add_argument("slug")
    pp_create.add_argument("--name", required=True)
    pp_create.add_argument("--repo", default="")
    pp_create.add_argument("--workspace-path", dest="workspace_path", default="")
    pp_create.add_argument(
        "--default-branch",
        dest="default_branch",
        default=config.PAIR_BASE_BRANCH,
    )
    # #1114: контракт публикуется трижды — REST, CLI, MCP. Поверхность, где
    # форж задать нельзя, означает, что GitVerse-проект отсюда не завести —
    # и молча, потому что умолчание примут за выбор.
    pp_create.add_argument("--forge", choices=list(FORGES), default=DEFAULT_FORGE)
    pp_create.set_defaults(func=create_project)
