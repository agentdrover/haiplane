"""Read deployment history through the same view and formatter as the service."""

from __future__ import annotations

import argparse
from typing import Any
from urllib.parse import urlencode

from hub.services.deploys_view import DEFAULT_LIMIT, DeploysView, format_deploys_view


def cmd_deploys(args: argparse.Namespace) -> int:
    from hub.cli import _api

    query = urlencode({"project": args.project, "limit": args.limit})
    payload = _api("GET", f"/api/deploys?{query}")
    print(format_deploys_view(DeploysView.model_validate(payload)))
    return 0


def add_parsers(sub: Any) -> None:
    parser = sub.add_parser("deploys", help="Deployment pipeline and recent releases")
    parser.add_argument("--project", default="default", help="Project slug")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Recent rows")
    parser.set_defaults(func=cmd_deploys)
