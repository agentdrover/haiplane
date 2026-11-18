"""CLI projection of the role outbox HTTP contract."""

import argparse
import json
import sys
from collections.abc import Callable
from urllib.parse import quote


def add_parser(sub: argparse._SubParsersAction, api: Callable) -> None:
    outbox = sub.add_parser(
        "agent-outbox", help="Publish, read or answer a role delivery"
    )
    commands = outbox.add_subparsers(required=True)
    for action in ("publish", "status", "scope"):
        parser = commands.add_parser(action)
        parser.add_argument("host")
        parser.add_argument("order_id", type=int)
        if action == "status":
            parser.add_argument("delivery_id", type=int)
        parser.set_defaults(func=lambda args: _run(api, args), outbox_action=action)
    answer = commands.add_parser("answer")
    answer.add_argument("task_id", type=int)
    answer.add_argument("message_id", type=int)
    answer.set_defaults(func=lambda args: _run(api, args), outbox_action="answer")


def _run(api: Callable, args: argparse.Namespace) -> int:
    if args.outbox_action == "answer":
        path = f"/api/tasks/{args.task_id}/outbox-questions/{args.message_id}/answer"
        result = api("POST", path, {"body": sys.stdin.read().strip()})
    else:
        path = f"/api/executor-hosts/{quote(args.host, safe='')}/role-orders/{args.order_id}/outbox"
        if args.outbox_action == "publish":
            try:
                payload = json.load(sys.stdin)
            except ValueError:
                print("Invalid JSON input", file=sys.stderr)
                return 2
            result = api("POST", path, payload)
        elif args.outbox_action == "status":
            result = api("GET", f"{path}/{args.delivery_id}")
        else:
            result = api("GET", path)
    print(json.dumps(result, ensure_ascii=False))
    return 0
