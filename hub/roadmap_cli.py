"""CLI entry points for roadmap grants and requests."""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _read_question(filename: str) -> str:
    try:
        if filename == "-":
            question = sys.stdin.read(2001)
        else:
            with Path(filename).open(encoding="utf-8") as stream:
                question = stream.read(2001)
    except (OSError, UnicodeError):
        raise SystemExit("Could not read the roadmap question as UTF-8") from None
    if len(question) > 2000 or not question.strip():
        raise SystemExit("Roadmap questions must contain 1–2000 characters")
    return question


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
            body["question"] = _read_question(args.question_file)
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
        ("request", "POST", ("plan_id", "grant_id", "request_id", "source_order_id")),
    ):
        parser = operations.add_parser(name)
        parser.add_argument("task_id", type=int)
        if name == "request":
            parser.add_argument(
                "--question-file",
                required=True,
                help="UTF-8 question file or - for stdin",
            )
        for field in fields:
            parser.add_argument(
                "--" + field.replace("_", "-"),
                required=field not in ("signal_task_id", "source_order_id", "ttl_days"),
                type=str if field == "request_id" else int,
            )
        parser.set_defaults(func=call, method=method, fields=fields)
