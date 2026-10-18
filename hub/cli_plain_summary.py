"""Флаг ``--plain-summary`` для propose и refine (#1811).

Вынесено из ``hub/cli.py``: у того бюджет размера (#1669).
"""

from __future__ import annotations

import argparse
from typing import Any


def add_plain_summary_option(*parsers: argparse.ArgumentParser) -> None:
    for parser in parsers:
        parser.add_argument(
            "--plain-summary",
            dest="plain_summary",
            default=None,
            help="What the task is about in plain words, up to 300 chars",
        )


def put_plain_summary(body: dict[str, Any], args: argparse.Namespace) -> None:
    if getattr(args, "plain_summary", None) is not None:
        body["plain_summary"] = args.plain_summary


def put_plain_summary_after(
    filed: dict[str, Any], args: argparse.Namespace, api: Any
) -> None:
    """Прод-дефект создаёт отдельный маршрут без поля: дописываем refine-ом."""
    if getattr(args, "plain_summary", None) is not None:
        body = {"plain_summary": args.plain_summary}
        api("POST", f"/api/tasks/{filed['task']['id']}/refine", body)
