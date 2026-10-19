"""``hp-hub owner-minutes day`` и строка оценки в ``hp-hub status`` (#1874).

Вынесено из ``hub/cli.py``: у того бюджет размера (#1669). Число берётся из
объекта, который собрал хаб (``owner_minutes.line``); CLI его не пересчитывает.
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
from typing import Any


def _api(*args: Any, **kwargs: Any) -> Any:
    # Лениво: hub.cli собирает парсеры при импорте и импортирует этот модуль.
    from hub.cli import _api as api

    return api(*args, **kwargs)


def print_line(result: Any) -> None:
    """Оценка минут владельца одной строкой в stderr; JSON на stdout несёт её целиком."""
    estimate = result.get("owner_minutes") if isinstance(result, dict) else None
    if isinstance(estimate, dict) and estimate.get("line"):
        print(f"Минуты владельца: {estimate['line']}", file=sys.stderr)


def cmd_owner_minutes_day(args: argparse.Namespace) -> int:
    """Оценка минут принципала за локальный день: по принципалу и дате."""
    params = []
    if getattr(args, "principal_id", None) is not None:
        params.append(f"principal_id={int(args.principal_id)}")
    if getattr(args, "date", None):
        params.append(f"date={urllib.parse.quote(str(args.date))}")
    query = ("?" + "&".join(params)) if params else ""
    result = _api("GET", f"/api/owner-minutes/day{query}")
    print(
        f"{result['date']} ({result['timezone']}), принципал #{result['principal_id']}: "
        f"{result['line']}"
    )
    return 0


def add_parsers(sub: Any) -> None:
    parser = sub.add_parser(
        "owner-minutes",
        help="Estimated owner minutes by norms (#1874): hp-hub owner-minutes day",
    )
    inner = parser.add_subparsers(dest="owner_minutes_cmd", required=True)
    day = inner.add_parser("day", help="One principal's estimate for a local day")
    day.add_argument("--principal-id", type=int, default=None, dest="principal_id")
    day.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    day.set_defaults(func=cmd_owner_minutes_day)
