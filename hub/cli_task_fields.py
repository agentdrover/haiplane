"""Флаги полей постановки для propose и refine: ``--plain-summary`` (#1811),
``--result-kind``/``--rollback`` (#1647).

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


def _add_result_kind_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--result-kind",
        dest="result_kind",
        choices=("commit", "state"),
        default=None,
        help=(
            "commit (default) or state (#1647): the result is a state of the "
            "world, submitted with evidence and accepted by a human; leaf "
            "task/subtask only, changes only in draft"
        ),
    )
    parser.add_argument(
        "--rollback",
        dest="rollback",
        default=None,
        help="How to undo a state result (required by DoR for state tasks)",
    )
