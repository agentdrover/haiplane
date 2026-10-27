"""``hp-hub outcome-retire preview|apply|cancel|history`` (#2091).

Вынесено из ``hub/cli.py``: у того бюджет размера (#1669). Действия записи
доступны только человеку (REST за ``require_human_or_admin``).
"""

from __future__ import annotations

import argparse
from typing import Any


def _api(*args: Any, **kwargs: Any) -> Any:
    from hub.cli import _api as api

    return api(*args, **kwargs)


def _print_json(value: Any) -> None:
    from hub.cli import _print_json as printer

    printer(value)


def cmd_outcome_retire(args: argparse.Namespace) -> int:
    """Retire old outcome promises by the owner's decision (#2091)."""
    action = args.retire_cmd
    if action == "preview":
        body = {"cutoff": args.cutoff, "projects": args.project}
        result = _api("POST", "/api/outcome-retirements/preview", body)
    elif action == "apply":
        body = {"token": args.token, "reason": args.reason}
        result = _api("POST", "/api/outcome-retirements/apply", body)
    elif action == "cancel":
        body = {"reason": args.reason}
        result = _api(
            "POST", f"/api/tasks/{args.task_id}/outcome-retirement/cancel", body
        )
    else:
        query = f"?task_id={args.task_id}" if args.task_id else ""
        result = _api("GET", f"/api/outcome-retirements{query}")
    _print_json(result)
    return 0


def add_parsers(sub: Any) -> None:
    """``outcome-retire preview|apply|cancel|history`` (#2091), human-only writes."""
    p_retire = sub.add_parser(
        "outcome-retire",
        help="Retire old outcome promises by the owner's decision (#2091)",
    )
    retire_sub = p_retire.add_subparsers(dest="retire_cmd", required=True)
    p_prev = retire_sub.add_parser("preview", help="Show the cohort, write nothing")
    p_prev.add_argument(
        "--cutoff",
        required=True,
        help="Instant WITH time zone, e.g. 2026-10-01T00:00:00Z",
    )
    p_prev.add_argument(
        "--project", action="append", required=True, help="Project slug; repeatable"
    )
    p_apply = retire_sub.add_parser("apply", help="Retire the previewed cohort")
    p_apply.add_argument("--token", required=True, help="Token from the preview")
    p_apply.add_argument("--reason", required=True)
    p_cancel = retire_sub.add_parser("cancel", help="Take a retirement back")
    p_cancel.add_argument("task_id", type=int)
    p_cancel.add_argument("--reason", required=True)
    p_hist = retire_sub.add_parser("history", help="Decisions, cancelled included")
    p_hist.add_argument("--task-id", dest="task_id", type=int, default=None)
    p_retire.set_defaults(func=cmd_outcome_retire)
