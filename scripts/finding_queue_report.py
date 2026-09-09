"""Разбор находок: снимок очереди, отчёт и слепая перепроверка (#1171).

Три команды, и ни одна из них ничего не судит — суждение вводит человек через
очередь ``/findings`` (#876, правило 1). Скрипт только считает.

* ``snapshot`` фиксирует ЗНАМЕНАТЕЛЬ приёмки: список uid и число на момент
  старта. Очередь пополняется каждым новым отчётом, и «разобрано 95% очереди»
  без снимка меряло бы гонку разбора с конвейером.
* ``report`` печатает сток и поток ОТДЕЛЬНЫМИ строками, precision и
  resolution_rate по профилям и моделям с размером выборки у каждой цифры, а
  ниже порога — слово «недобор» вместо числа, и долю находок, судить которые
  не по чему, с причинами.
* ``recheck`` печатает выборку для слепого второго суждения, а с
  ``--answers`` — число расхождений с первым.

Запуск:

    uv run python scripts/finding_queue_report.py snapshot
    uv run python scripts/finding_queue_report.py report --since-days 60
    uv run python scripts/finding_queue_report.py recheck --salt 2026-09-08
    uv run python scripts/finding_queue_report.py recheck --salt 2026-09-08 \\
        --answers /tmp/second-pass.csv

База берётся тем же путём, что и хаб (``HAIPLANE_HUB_DB``), или задаётся
``--db``. Файл ответов перепроверки — ``uid,disposition`` по строке.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

import aiosqlite

from hub.db import connect
from hub.services.finding_report import (
    compare_recheck,
    disposition_report,
    first_judgements,
    queue_snapshot,
    recheck_sample,
)


async def _open(dsn: str | None) -> aiosqlite.Connection:
    return await connect(dsn)


def _print_snapshot(snapshot: dict) -> None:
    print(f"Снимок очереди: {snapshot['total']} подтверждённых находок без диспозиции")
    print("(сток за всё время — не оконное число)")
    for item in snapshot["items"]:
        print(
            f"  {item['uid'] or '—':16} #{item['task_id']:<5} отчёт {item['review_id']:<5} "
            f"{item['category']:<24} {item['reported_at'][:10]} {item['title'][:70]}"
        )
    print()
    print("uid одной строкой:")
    print(" ".join(item["uid"] for item in snapshot["items"] if item["uid"]))


def _print_slice(label: str, row: dict) -> None:
    if row["shortfall"]:
        print(f"  {label:<28} {row['shortfall']}")
        return
    print(
        f"  {label:<28} precision {row['precision']} · resolution "
        f"{row['resolution_rate']} · разобрано {row['judged']} "
        f"(исправлено {row['fixed']}, ложняк {row['false_positive']}, "
        f"неважно {row['wont_fix']})"
    )


def _print_report(report: dict) -> None:
    stock = report["stock"]
    flow = report["flow"]
    print("СТОК (за всё время, окно не применяется)")
    print(f"  в очереди {stock['findings']} находок в {stock['reports']} отчётах")
    print()
    print(f"ПОТОК (окно {flow['since_days']} дней)")
    print(f"  покрытие отчётов: {flow['reports_judged']} из {flow['reports_counted']}")
    print("  сток и поток отвечают на разные вопросы и в одну дробь не сводятся")
    print()
    minimum = report["minimum_per_slice"]
    _print_judged(
        f"РАЗБОР ЗА ОКНО ({flow['since_days']} дней по дате ОТЧЁТА; "
        f"порог ответа по срезу — {minimum} находок)",
        report["judged_window"],
    )
    print()
    _print_judged(
        f"РАЗБОР ЗА ВСЁ ВРЕМЯ (окно не применяется; порог — {minimum} находок)",
        report["judged_all_time"],
    )
    print(
        "  суждение о находке из отчёта старше окна в оконный срез не входит: "
        "разбор накопленного виден здесь, а не выше"
    )
    unknown = report["unknown"]
    print()
    if unknown is None:
        # #762: пустота не чистота. Секция, исчезнувшая молча, читается как
        # «unknown нет», а её здесь не считали — по явному ключу.
        print("СУДИТЬ НЕ ПО ЧЕМУ: не считали (ключ --no-evidence, нужны клоны задач)")
        return
    print("СУДИТЬ НЕ ПО ЧЕМУ (остаются неразобранными, дефолт не назначается)")
    print(
        f"  {unknown['unknown']} из {unknown['queued']} "
        f"(доля {unknown['share']}; выше 0.4 — условие пересмотра)"
    )
    for reason in unknown["reasons"]:
        print(f"    {reason['reason']:<24} {reason['findings']}")


def _print_judged(header: str, slices: dict) -> None:
    print(header)
    _print_slice("всего", slices["overall"])
    for row in slices["by_profile"]:
        _print_slice(f"профиль {row['profile']}", row)
    for row in slices["by_model"]:
        _print_slice(f"модель {row['model']}", row)


def _read_answers(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as handle:
        return {
            row[0].strip(): row[1].strip()
            for row in csv.reader(handle)
            if len(row) >= 2 and row[0].strip() and not row[0].startswith("#")
        }


async def _run(args: argparse.Namespace) -> int:
    db = await _open(args.db)
    try:
        if args.command == "snapshot":
            snapshot = await queue_snapshot(db)
            if args.json:
                print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            else:
                _print_snapshot(snapshot)
            return 0
        if args.command == "report":
            report = await disposition_report(
                db,
                since_days=args.since_days,
                with_evidence=not args.no_evidence,
            )
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                _print_report(report)
            return 0
        first = await first_judgements(db)
        sample = recheck_sample(list(first), args.salt)
        if not args.answers:
            print(
                f"Выборка на слепую перепроверку: {len(sample)} из {len(first)} "
                "разобранных"
            )
            print(
                "(первое суждение здесь НЕ печатается — иначе перепроверка не слепая)"
            )
            for uid in sample:
                print(f"  {uid}")
            return 0
        result = compare_recheck(first, _read_answers(Path(args.answers)), sample)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="путь к базе хаба")
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="зафиксировать очередь на старте")
    snap.add_argument("--json", action="store_true")

    rep = sub.add_parser("report", help="сток, поток, precision по срезам")
    rep.add_argument("--since-days", type=int, default=60)
    rep.add_argument(
        "--no-evidence",
        action="store_true",
        help="не считать долю unknown (требует клонов задач)",
    )
    rep.add_argument("--json", action="store_true")

    check = sub.add_parser("recheck", help="слепая перепроверка 10% разобранного")
    check.add_argument("--salt", required=True, help="соль выборки, например дата")
    check.add_argument("--answers", default="", help="файл uid,disposition")

    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
