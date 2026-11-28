"""CLI projection of the plan endpoints."""

import json
from typing import Any


def add_parser(sub: Any, api: Any) -> None:
    def run(args: Any) -> int:
        path = f"/api/tasks/{args.epic_id}/epic-plan"
        match args.action:
            case "show":
                data = api("GET", path)
            case "approve":
                data = api("POST", path + "/approve", {"version": args.version})
            case state:
                data = api(
                    "POST",
                    path + "/terminate",
                    {"version": args.version, "state": state, "reason": args.reason},
                )
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    plan = sub.add_parser(
        "epic-plan",
        help="Read subscription terms and blockers, or sign this pilot plan version",
    )
    actions = plan.add_subparsers(dest="action", required=True)
    for name in ("show", "approve", "stopped", "revoked"):
        action = actions.add_parser(name)
        action.add_argument("epic_id", type=int)
        if name != "show":
            action.add_argument("--version", required=True)
        if name in {"stopped", "revoked"}:
            action.add_argument("--reason", required=True)
        action.set_defaults(func=run)
