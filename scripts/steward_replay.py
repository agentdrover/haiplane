#!/usr/bin/env python3
"""Стенд политики гейтов: прогнать её по УЖЕ СЛУЧИВШИМСЯ сдачам (#1167).

Фаза тени стюарда наполняется только вперёд, человеческими возвратами, и при
их доле 0.119 десять штук набираются месяцами. За то же окно в базе уже лежат
сотни вердиктов, каждый привязан к submission_sha с сохранённым отчётом. Этот
скрипт читает ту разметку.

    uv run python scripts/steward_replay.py --days 60
    uv run python scripts/steward_replay.py --days 60 --backfill-cap 5

Без ``--backfill-cap`` не тратится ничего: оффлайн-реплей прогоняет
детерминированный слой политики и не делает ни одного обращения к провайдеру.
С потолком печатается ПЛАН бэкфилла и его цена — план, а не запуск: деньги
тратит человек, прочитавший число.

Вывод побайтово одинаков на одном корпусе и одной политике — иначе стендом
нельзя доказывать ничего про правку порога.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hub.db import connect
from hub.services import gate_grounds as grounds
from hub.services import steward_shadow as shadow


async def _run(days: int, cap: int, budget: int, require_raw: bool) -> int:
    db = await connect()
    try:
        entries = await shadow.collect_corpus(db, days=days)
        cases, excluded = await shadow.build_cases(db, entries)
        policy = grounds.GatePolicy(
            name=f"budget={budget},raw={'on' if require_raw else 'off'}",
            token_budget=budget,
            require_raw_count=require_raw,
        )
        report = shadow.replay(
            cases, policy, window_days=days, excluded=excluded
        )
        sys.stdout.write(shadow.render_report(report))
        if cap:
            plan = shadow.plan_backfill(cases, cap)
            sys.stdout.write("\nПЛАН БЭКФИЛЛА\n")
            if plan.refused:
                sys.stdout.write(f"  отказ: {plan.refusal}\n")
                return 1
            sys.stdout.write(f"  прогонов: {plan.runs}\n")
            sys.stdout.write(
                f"  оценка цены: {plan.tokens_estimate} provider-токенов\n"
            )
            sys.stdout.write(
                "  строки пойдут под kind=" + shadow.KIND_VERDICT_HISTORICAL + ", "
                "в отдельный счётчик: защитный отказ act они не снимают\n"
            )
        return 0
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="окно корпуса, дней")
    parser.add_argument(
        "--backfill-cap",
        type=int,
        default=0,
        help="потолок числа прогонов бэкфилла; без него печатается только реплей",
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=0,
        help="порог токен-бюджета ревью в примеряемой политике (0 — проверка выключена)",
    )
    parser.add_argument(
        "--no-raw-count",
        action="store_true",
        help="примерить политику, которая НЕ требует хотя бы одного кандидата",
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(args.days, args.backfill_cap, args.token_budget, not args.no_raw_count)
    )


if __name__ == "__main__":
    raise SystemExit(main())
