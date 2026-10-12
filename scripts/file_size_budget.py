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
видимостью, и CI она не красит (#1676):
  * «вырос сверх замера» — файл из ledger длиннее своего замера;
  * «у порога» — файл вне ledger не короче near_pct% порога своего дерева
    (near_pct живёт в pyproject.toml рядом с порогами и деревом не является).
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
NEAR_KEY = "near_pct"
NEAR_SHOWN = 5
UNCLAIMED = "unclaimed"
_SKIP_DIRS = {"__pycache__", ".venv", "node_modules"}


def _file_size_section(root: Path) -> dict:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    try:
        return data["tool"]["haiplane"]["file_size"]
    except KeyError:
        raise SystemExit("pyproject.toml: нет секции [tool.haiplane.file_size]")


def load_thresholds(root: Path) -> dict[str, int]:
    """Пороги по деревьям из pyproject.toml корня (near_pct — не дерево)."""
    section = _file_size_section(root)
    return {
        str(tree): int(limit) for tree, limit in section.items() if tree != NEAR_KEY
    }


def load_near_pct(root: Path) -> float:
    """Доля порога, с которой файл вне ledger называется «у порога»."""
    section = _file_size_section(root)
    if NEAR_KEY not in section:
        raise SystemExit(
            f"pyproject.toml: нет ключа {NEAR_KEY} в [tool.haiplane.file_size]"
        )
    return float(section[NEAR_KEY])


def count_lines(data: bytes) -> int:
    """Физические строки, как `wc -l` плюс последняя строка без перевода.

    str.splitlines() считает разделителями U+2028, form feed и другие, и файл
    из 1500 физических строк с таким символом в литерале меряется как 1501.
    CRLF — одна строка.
    """
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def measure(root: Path, trees: list[str]) -> dict[str, int]:
    """Число строк каждого .py под деревьями, ключ — путь от корня."""
    sizes: dict[str, int] = {}
    for tree in trees:
        for path in sorted((root / tree).rglob("*.py")):
            if _SKIP_DIRS & set(path.relative_to(root).parts):
                continue
            sizes[path.relative_to(root).as_posix()] = count_lines(path.read_bytes())
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
    grown: tuple[tuple[str, int, int, int], ...] = ()  # путь, строк, замер, потолок
    near: tuple[tuple[str, int, int], ...] = ()  # путь, строк, порог

    @property
    def ok(self) -> bool:
        return not (self.new or self.over or self.stale)

    @property
    def near_ranked(self) -> tuple[tuple[str, int, int], ...]:
        """Файлы у порога, самые заполненные первыми."""
        return tuple(sorted(self.near, key=lambda p: (-(p[1] / p[2]), p[0])))

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "new": [{"path": p, "lines": n, "threshold": t} for p, n, t in self.new],
            "over": [{"path": p, "lines": n, "max": m} for p, n, m in self.over],
            "stale": [{"path": p, "reason": r} for p, r in self.stale],
            "waived_count": len(self.waived),
            "grown": [
                {"path": p, "lines": n, "measured": m, "max": x}
                for p, n, m, x in self.grown
            ],
            "near": [
                {
                    "path": p,
                    "lines": n,
                    "threshold": t,
                    "used_pct": round(100.0 * n / t, 1),
                }
                for p, n, t in self.near_ranked
            ],
            "by_reason": dict(self.by_reason),
        }


def _threshold(path: str, thresholds: dict[str, int]) -> int:
    return thresholds[path.split("/", 1)[0]]


def check(
    sizes: dict[str, int],
    thresholds: dict[str, int],
    ledger: dict[str, dict],
    near_pct: float,
) -> Report:
    """Свести замеры с ledger. Чистая функция: ни файловой системы, ни конфига."""
    new: list[tuple[str, int, int]] = []
    over: list[tuple[str, int, int]] = []
    waived: list[tuple[str, int, int]] = []
    stale: list[tuple[str, str]] = []
    grown: list[tuple[str, int, int, int]] = []
    near: list[tuple[str, int, int]] = []
    by_reason: dict[str, int] = {}

    for path, lines in sizes.items():
        limit = _threshold(path, thresholds)
        entry = ledger.get(path)
        if entry is None:
            if lines > limit:
                new.append((path, lines, limit))
            elif lines * 100 >= near_pct * limit:
                near.append((path, lines, limit))
            continue
        if lines <= limit:
            continue  # запись stale, ниже
        kind = str(entry.get("removed_by", "")).split("—")[0].strip() or "?"
        by_reason[kind] = by_reason.get(kind, 0) + 1
        allowed = int(entry["max"])
        (over if lines > allowed else waived).append((path, lines, allowed))
        measured = int(entry["measured"])
        if measured < lines <= allowed:
            grown.append((path, lines, measured, allowed))

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
        grown=tuple(sorted(grown)),
        near=tuple(sorted(near)),
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
    if report.grown:
        lines.append(
            f"Файл из ledger вырос сверх замера ({len(report.grown)}), CI не красит:"
        )
        for path, n, measured, allowed in report.grown:
            lines.append(f"  {path} — {n} > замер {measured} (потолок {allowed})")
    if report.near:
        lines.append(
            f"Файл у порога ({len(report.near)}), CI не красит: разделите по темам "
            "до того, как файл упрётся в порог:"
        )
        ranked = report.near_ranked
        for path, n, limit in ranked[:NEAR_SHOWN]:
            lines.append(f"  {path} — {n}/{limit} ({100.0 * n / limit:.0f}%)")
        if len(ranked) > NEAR_SHOWN:
            lines.append(
                f"  и ещё {len(ranked) - NEAR_SHOWN} файлов у порога (полный список: --json)"
            )
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
    near_pct = load_near_pct(root)  # до любого режима, в том числе --update
    sizes = measure(root, list(thresholds))
    ledger_path = root / LEDGER_REL

    if args.update:
        count = write_ledger(sizes, thresholds, ledger_path)
        print(f"ledger перезаписан: {count} исключений")
        return 0

    report = check(sizes, thresholds, load_ledger(ledger_path), near_pct)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
