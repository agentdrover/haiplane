"""``oc-hub review-verdict``: запись вердикта и его короткий вывод (#1696).

Вынесено из ``hub/cli.py``: у того бюджет размера (#1669) и бюджет сложности
``build_parser``. REST-ответ вердикта не меняется; здесь только проекция для
человека. ``--json`` печатает тело ответа как раньше.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _api(*args: Any, **kwargs: Any) -> Any:
    # Лениво: hub.cli собирает парсеры при импорте и импортирует этот модуль.
    from hub.cli import _api as api

    return api(*args, **kwargs)


def _add_state_verdict_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expected-generation",
        dest="expected_generation",
        type=int,
        default=None,
        help=(
            "The submission generation you judged, from the review brief "
            "(required: state task #1647, commit task #1706)"
        ),
    )
    parser.add_argument(
        "--not-pair-miss",
        dest="not_pair_miss",
        metavar="NOTE",
        default=None,
        help=(
            "Human only (#2139): mark this return as NOT a miss of the steward "
            "pair (wording, not a defect the pair should have caught); NOTE "
            "says why. The return then does not put the steward in shadow"
        ),
    )
    parser.add_argument(
        "--supersedes-revision",
        dest="supersedes_revision",
        type=int,
        default=None,
        help=(
            "Human only (#1706): replace the verdict that stands, naming its "
            "revision from the task card"
        ),
    )


def _read_task(task_id: int) -> dict[str, Any] | None:
    """Задача после записи вердикта; ``None`` — не прочитана (а не «пусто»).

    ``_api`` печатает ошибку и завершает процесс; вердикт к этому моменту уже
    записан, поэтому выход не должен скрыть строку итога.
    """
    try:
        task = _api("GET", f"/api/tasks/{task_id}")
    except (SystemExit, OSError, ValueError):
        # SystemExit: _api printed the HTTP/connection error; OSError covers
        # URLError and TimeoutError while reading the body; ValueError covers
        # a broken JSON body. KeyboardInterrupt is deliberately not caught.
        return None
    return task if isinstance(task, dict) else None


def _freshness(task: dict[str, Any] | None) -> str:
    latest = (task or {}).get("latest_review")
    if not isinstance(latest, dict) or "is_current" not in latest:
        return "текущесть неизвестна"
    return "текущий" if latest["is_current"] else "устарел"


def _next_action(task: dict[str, Any]) -> str:
    # Общий расчёт, без собственных фраз: тот же, что в конверте MCP.
    from hub.mcp_envelope import build_mutation_envelope

    return str(build_mutation_envelope(task)["next_action"])


def verdict_summary(
    verdict: str, posted: dict[str, Any], task: dict[str, Any] | None
) -> list[str]:
    """Строка итога и предупреждения хаба (``lifecycle_hint``), по строке на элемент."""
    view = task or posted
    generation = posted.get("review_verdict_generation")
    line = (
        f"{verdict}, вердикт на поколение "
        f"{'?' if generation is None else generation} ({_freshness(task)}), "
        f"статус {view.get('status', '?')}, дальше: {_next_action(view)}"
    )
    hint = posted.get("lifecycle_hint")
    return [line, *str(hint).splitlines()] if hint else [line]


def cmd_review_verdict(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"verdict": args.verdict}
    if args.comments:
        body["comments"] = args.comments
    if args.agent:
        body["agent"] = args.agent
    if args.findings_json:
        try:
            findings = json.loads(args.findings_json)
        except json.JSONDecodeError as exc:
            print(f"invalid --findings-json: {exc}", file=sys.stderr)
            return 2
        body["findings"] = findings
    if getattr(args, "create_tasks_for_out_of_scope", False):
        body["create_tasks_for_out_of_scope"] = True
    if getattr(args, "expected_generation", None) is None:
        # #1706: the generation is the brief's, never the live one read here —
        # a CLI that fills it in judges whatever stands now, which is the
        # stale command this flag exists to stop.
        print(
            "--expected-generation is required: read the brief "
            f"(oc-hub review-brief {args.task_id}) and repeat the command with "
            "--expected-generation <submission_generation of the brief>",
            file=sys.stderr,
        )
        return 2
    body["expected_generation"] = args.expected_generation
    if getattr(args, "supersedes_revision", None) is not None:
        body["supersedes_revision"] = args.supersedes_revision
    if getattr(args, "not_pair_miss", None) is not None:
        body["pair_miss"] = False
        body["pair_miss_note"] = args.not_pair_miss
    result = _api("POST", f"/api/tasks/{args.task_id}/review-verdict", body)
    if getattr(args, "json", False) or not isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print("\n".join(verdict_summary(args.verdict, result, _read_task(args.task_id))))
    return 0


def add_parsers(sub: Any) -> None:
    p_review_verdict = sub.add_parser(
        "review-verdict",
        help="Record a review verdict; never completes the task",
    )
    p_review_verdict.add_argument("task_id", type=int)
    p_review_verdict.add_argument("verdict", choices=["approved", "changes_requested"])
    p_review_verdict.add_argument("--comments", default="", help="Review summary")
    p_review_verdict.add_argument("--agent", default="", help="Reviewer agent name")
    p_review_verdict.add_argument(
        "--findings-json",
        dest="findings_json",
        default="",
        help=(
            "JSON list of findings: "
            '[{"id":1,"severity":"high","message":"...",'
            '"scope":"in_scope|out_of_scope","linked_task_id":123}]. '
            "scope defaults to in_scope; changes_requested requires at "
            "least one in_scope finding; linked_task_id references the "
            "follow-up task for out_of_scope findings."
        ),
    )
    p_review_verdict.add_argument(
        "--create-tasks-for-out-of-scope",
        dest="create_tasks_for_out_of_scope",
        action="store_true",
        help=(
            "Auto-create DRAFT follow-up tasks for out_of_scope findings "
            "without linked_task_id (#436); drafts still need human approval"
        ),
    )
    p_review_verdict.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="Print the full REST response instead of the one-line summary (#1696)",
    )
    _add_state_verdict_args(p_review_verdict)
    p_review_verdict.set_defaults(func=cmd_review_verdict)
