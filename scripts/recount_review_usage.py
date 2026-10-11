#!/usr/bin/env python3
"""Пересчёт provider_tokens исторических заказов ревью по счёту агента (#1413).

До #1413 хаб записывал счёт ОДНОГО прогона ревьюера, а провайдер начисляет
все прогоны агента: для deep это 13–43% настоящего счёта. Поллер историю не
трогает — её пересчитывает владелец этой командой, выборочно и в пределах
хранения провайдера (удалённый агент счёта не отдаёт — строка остаётся как
была и печатается с причиной).

    uv run python scripts/recount_review_usage.py --ids 415 416 420
    uv run python scripts/recount_review_usage.py --since 2026-09-20 --until 2026-09-26
    uv run python scripts/recount_review_usage.py --ids 420 --apply

Без ``--apply`` ничего не пишется: печатается «было → станет». С ``--apply``
пересчитанные строки получают ``usage_scope='recounted'``, счёт ложится и на
отчёт заказа (machine_reviews.provider_tokens).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hub.db import connect
from hub.services.review_dispatch import recount_review_usage


def _render(rows: list[dict], apply: bool) -> str:
    lines = []
    for r in rows:
        after = "не назван провайдером" if r["after"] is None else r["after"]
        mark = "записан" if r["applied"] else ("пропущен" if apply else "план")
        lines.append(
            f"#{r['id']} task #{r['task_id']} {r['profile'] or '-'} "
            f"{r['agent_id']}: {r['before']} -> {after} [{mark}]"
        )
    lines.append(f"строк: {len(rows)}, записано: {sum(r['applied'] for r in rows)}")
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> int:
    db = await connect()
    try:
        rows = await recount_review_usage(
            db,
            dispatch_ids=args.ids,
            since=args.since,
            until=args.until,
            apply=args.apply,
        )
    finally:
        await db.close()
    sys.stdout.write(_render(rows, args.apply))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ids", type=int, nargs="*", help="id заказов review_dispatches"
    )
    parser.add_argument("--since", default="", help="created_at >= (YYYY-MM-DD)")
    parser.add_argument("--until", default="", help="created_at < (YYYY-MM-DD)")
    parser.add_argument("--apply", action="store_true", help="записать пересчёт")
    args = parser.parse_args()
    if not (args.ids or args.since or args.until):
        parser.error("нужна выборка: --ids или --since/--until")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
