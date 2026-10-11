#!/usr/bin/env python3
"""CI check: .py-файл не растёт молча выше порога размера (#1669).

Крупный файл даёт крупный дифф, а крупный дифф deep-ревью не дочитывает
(REVIEW_FILE_LINE_CAP). complexity_budget (#1066) смотрит только функции; эта
проверка — тот же приём для файлов, по образцу scripts/complexity_budget.py.

    uv run python scripts/file_size_budget.py            # проверка
    uv run python scripts/file_size_budget.py --json     # машиночитаемо
    uv run python scripts/file_size_budget.py --update   # перезаписать ledger

Пороги по деревьям (hub/, scripts/, tests/) живут в pyproject.toml
([tool.haiplane.file_size]) — здесь их копии нет. Ledger исключений —
docs/agent-context/file-size-budget.json, записи адресуются путём файла.

Три способа покраснеть, и все три названы поимённо:
  * новый файл выше порога без записи в ledger;
  * файл из ledger выше собственного потолка (замер + headroom_pct);
  * запись ledger, чей файл удалён или не выше порога, — её надо снять.

Потолок с запасом, а не точная заморозка — урок #829: две ветки, правящие один
большой файл, не должны конфликтовать по ledger. Запас оплачивается
видимостью: на 90% потолка проверка говорит отдельной строкой.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_REL = Path("docs") / "agent-context" / "file-size-budget.json"
DEFAULT_HEADROOM_PCT = 5.0
# Доля потолка, после которой запас называется вслух (#829).
LOUD_AT_PCT = 90.0
UNCLAIMED = "unclaimed"
_SKIP_DIRS = {"__pycache__", ".venv", "node_modules"}


def load_thresholds(root: Path) -> dict[str, int]:
    """Пороги по деревьям из pyproject.toml корня."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    try:
        section = data["tool"]["haiplane"]["file_size"]
    except KeyError:
        raise SystemExit("pyproject.toml: нет секции [tool.haiplane.file_size]")
    return {str(tree): int(limit) for tree, limit in section.items()}


def measure(root: Path, trees: list[str]) -> dict[str, int]:
    """Число строк каждого .py под деревьями, ключ — путь от корня."""
    sizes: dict[str, int] = {}
    for tree in trees:
        for path in sorted((root / tree).rglob("*.py")):
            if _SKIP_DIRS & set(path.relative_to(root).parts):
                continue
            text = path.read_text(encoding="utf-8")
            sizes[path.relative_to(root).as_posix()] = len(text.splitlines())
    return sizes


def ceiling(value: int, headroom_pct: float = DEFAULT_HEADROOM_PCT) -> int:
    """Потолок waived-файла: замер плюс объявленный запас."""
    return int(math.ceil(value * (1 + headroom_pct / 100)))


@dataclass(frozen=True)
class Report:
    """Что бюджет говорит о текущем дереве."""

    new: tuple[tuple[str, int, int], ...]  # путь, строк, порог
    over: tuple[tuple[str, int, int], ...]  # путь, строк, потолок
    stale: tuple[tuple[str, str], ...]  # путь, причина
    waived: tuple[tuple[str, int, int], ...]  # путь, строк, потолок
    by_reason: dict[str, int]

    @property
    def ok(self) -> bool:
        return not (self.new or self.over or self.stale)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "new": [{"path": p, "lines": n, "threshold": t} for p, n, t in self.new],
            "over": [{"path": p, "lines": n, "max": m} for p, n, m in self.over],
            "stale": [{"path": p, "reason": r} for p, r in self.stale],
            "waived_count": len(self.waived),
            "by_reason": dict(self.by_reason),
        }


def _threshold(path: str, thresholds: dict[str, int]) -> int:
    return thresholds[path.split("/", 1)[0]]


def check(
    sizes: dict[str, int], thresholds: dict[str, int], ledger: dict[str, dict]
) -> Report:
    """Свести замеры с ledger. Чистая функция: ни файловой системы, ни конфига."""
    new: list[tuple[str, int, int]] = []
    over: list[tuple[str, int, int]] = []
    waived: list[tuple[str, int, int]] = []
    stale: list[tuple[str, str]] = []
    by_reason: dict[str, int] = {}

    for path, lines in sizes.items():
        limit = _threshold(path, thresholds)
        entry = ledger.get(path)
        if entry is None:
            if lines > limit:
                new.append((path, lines, limit))
            continue
        if lines <= limit:
            continue  # запись stale, ниже
        kind = str(entry.get("removed_by", "")).split("—")[0].strip() or "?"
        by_reason[kind] = by_reason.get(kind, 0) + 1
        allowed = int(entry["max"])
        (over if lines > allowed else waived).append((path, lines, allowed))

    for path in sorted(ledger):
        lines = sizes.get(path)
        if lines is None:
            stale.append((path, "файла больше нет"))
        elif lines <= _threshold(path, thresholds):
            stale.append((path, f"{lines} строк, порог {_threshold(path, thresholds)}"))
    return Report(
        new=tuple(sorted(new)),
        over=tuple(sorted(over)),
        stale=tuple(stale),
        waived=tuple(sorted(waived)),
        by_reason=by_reason,
    )


def format_report(report: Report) -> str:
    """Отчёт печатается и на зелёном прогоне — молчание читается как «всё под контролем»."""
    lines: list[str] = []
    if report.new:
        lines.append(f"НОВЫЙ ФАЙЛ ВЫШЕ ПОРОГА ({len(report.new)}):")
        for path, n, limit in report.new:
            lines.append(f"  {path} — {n} > порог {limit}")
        lines.append(
            "  Разбейте файл или внесите его в "
            f"{LEDGER_REL.as_posix()} через --update с задачей, которая его снимет."
        )
    if report.over:
        lines.append(f"WAIVED-ФАЙЛ ВЫРОС ({len(report.over)}):")
        for path, n, allowed in report.over:
            lines.append(f"  {path} — {n} > потолок {allowed}")
    if report.stale:
        lines.append(f"ИСКЛЮЧЕНИЕ ПЕРЕЖИЛО ПРИЧИНУ ({len(report.stale)}):")
        for path, reason in report.stale:
            lines.append(f"  {path} — {reason}, снимите запись")

    lines.append(f"Исключений в силе: {len(report.waived) + len(report.over)}")
    for kind, count in sorted(report.by_reason.items()):
        lines.append(f"  {kind}: {count}")
    for path, n, allowed in sorted(report.waived, key=lambda p: -(p[1] / p[2]))[:5]:
        used = 100.0 * n / allowed
        mark = "  <-- запас почти съеден" if used >= LOUD_AT_PCT else ""
        lines.append(f"  {path} {n}/{allowed} ({used:.0f}%){mark}")
    return "\n".join(lines)


def _read_ledger(path: Path) -> dict:
    if not path.exists():
        return {"_comment": [], "headroom_pct": DEFAULT_HEADROOM_PCT, "waivers": []}
    return json.loads(path.read_text(encoding="utf-8"))


def load_ledger(path: Path) -> dict[str, dict]:
    """Записи ledger по пути файла."""
    return {w["path"]: w for w in _read_ledger(path)["waivers"]}


def write_ledger(sizes: dict[str, int], thresholds: dict[str, int], path: Path) -> int:
    """Перезаписать ledger по факту; removed_by существующих записей сохраняется."""
    raw = _read_ledger(path)
    headroom = float(raw.get("headroom_pct", DEFAULT_HEADROOM_PCT))
    prior = {w["path"]: w for w in raw["waivers"]}
    waivers = [
        {
            "path": p,
            "measured": n,
            "max": ceiling(n, headroom),
            "removed_by": prior.get(p, {}).get("removed_by", UNCLAIMED),
        }
        for p, n in sorted(sizes.items())
        if n > _threshold(p, thresholds)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "_comment": raw.get("_comment", []),
                "headroom_pct": headroom,
                "waivers": waivers,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return len(waivers)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    parser.add_argument(
        "--update", action="store_true", help="перезаписать ledger по факту"
    )
    parser.add_argument("--root", default=str(REPO_ROOT), help="корень репозитория")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    thresholds = load_thresholds(root)
    sizes = measure(root, list(thresholds))
    ledger_path = root / LEDGER_REL

    if args.update:
        count = write_ledger(sizes, thresholds, ledger_path)
        print(f"ledger перезаписан: {count} исключений")
        return 0

    report = check(sizes, thresholds, load_ledger(ledger_path))
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
