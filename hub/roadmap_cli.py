"""CLI entry points for roadmap grants and requests."""

import argparse
from collections.abc import Callable
from typing import Any


def add_parser(
    sub: Any,
    api: Callable[..., Any],
    print_json: Callable[[Any], None],
) -> None:
    roadmap = sub.add_parser("roadmap", help="Manage task-scoped roadmap requests")
    operations = roadmap.add_subparsers(dest="roadmap_command", required=True)

    def call(args: argparse.Namespace) -> int:
        path = f"/api/tasks/{args.task_id}/roadmap-grant"
        method = args.method
        body = {
            key: getattr(args, key)
            for key in args.fields
            if getattr(args, key) is not None
        }
        if args.roadmap_command == "request":
            path = f"/api/tasks/{args.task_id}/roadmap-requests"
        print_json(api(method, path, body) if body else api(method, path))
        return 0

    for name, method, fields in (
        (
            "grant",
            "PUT",
            (
                "plan_id",
                "steward_principal_id",
                "roadmap_principal_id",
                "signal_task_id",
                "ttl_days",
            ),
        ),
        ("revoke", "DELETE", ()),
        ("signal", "GET", ()),
        ("request", "POST", ("plan_id", "request_id", "question", "source_order_id")),
    ):
        parser = operations.add_parser(name)
        parser.add_argument("task_id", type=int)
        for field in fields:
            parser.add_argument(
                "--" + field.replace("_", "-"),
                required=field not in ("signal_task_id", "source_order_id", "ttl_days"),
                type=int if field != "request_id" and field != "question" else str,
            )
        parser.set_defaults(func=call, method=method, fields=fields)
