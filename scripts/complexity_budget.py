#!/usr/bin/env python3
"""CI check: no function grows past the complexity budget unnoticed (#1066).

Пока в линте не было ни одного правила сложности, `ruff check hub tests` был
зелёным всё время, пока submit_for_review рос до 173 операторов и цикломатики
46. Функция дорастает до такого размера не одним решением, а двадцатью, и ни
одно из них по отдельности не выглядит тем, на чём надо остановиться. Это
проверка ровно про этот случай: тридцать седьмое нарушение не появится молча.

    uv run python scripts/complexity_budget.py            # проверка
    uv run python scripts/complexity_budget.py --json     # машиночитаемо
    uv run python scripts/complexity_budget.py --update   # перезаписать ledger

Пороги живут в pyproject.toml ([tool.ruff.lint.mccabe] и
[tool.ruff.lint.pylint]) — здесь их копии нет. Правила C901 и PLR0915 не
включены в lint.select: выразить пофункциональное исключение в конфиге ruff
нечем — per-file-ignores гасит файл целиком (и вместе с сегодняшним
нарушением прячет все будущие в том же файле), а `# noqa` пришлось бы
расставить по hub/, чего задача не делает. Поэтому правила селектит эта
проверка, а ledger исключений лежит в docs/agent-context/complexity-budget.json.

Три способа покраснеть, и все три названы поимённо:
  * нарушение, которого нет в ledger — новая сложность;
  * запись ledger без нарушения — исключение пережило свою причину и должно
    быть снято (иначе список превращается в унаследованный мусор, который
    никто не решается тронуть);
  * нарушение выше собственного потолка — waived-функция продолжила расти.

Потолок с запасом, а не точная заморозка — урок #829. Точная заморозка
означала бы, что любая правка внутри waived-функции обязана редактировать
ledger, и две такие ветки конфликтуют по построению. Запас оплачивается
видимостью: каждый прогон печатает съеденную долю, а на 90% говорит отдельной
строкой. Записи адресуются `путь::функция`, а не строкой файла: правка выше по
файлу не должна двигать ledger.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = REPO_ROOT / "docs" / "agent-context" / "complexity-budget.json"
TARGET = "hub"
RULES = ("C901", "PLR0915")
DEFAULT_HEADROOM_PCT = 15.0
# Доля потолка, после которой запас называется вслух. Запас, за которым никто
# не следит, — это способ тихо перестать проверять (#829).
LOUD_AT_PCT = 90.0


@dataclass(frozen=True)
class Violation:
    """Одно нарушение бюджета, адресованное функцией, а не строкой."""

    path: str
    function: str
    rule: str
    value: int
    limit: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.function}"

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "function": self.function,
            "rule": self.rule,
            "value": self.value,
            "limit": self.limit,
        }


def _run_ruff(target: str = TARGET) -> list[dict]:
    """Прогнать ruff с правилами бюджета и вернуть разобранный JSON.

    Через ``sys.executable -m ruff``, а не через имя в PATH: скрипт запускают
    и из CI, и локально, и путь до бинаря в этих двух случаях разный.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            target,
            "--select",
            ",".join(RULES),
            "--output-format",
            "json",
            "--no-cache",
            # Бюджет не глушится комментарием. Без этого флага `# noqa: C901`
            # на новой функции прятал бы её от проверки целиком — то есть
            # ровно тот тихий обход, ради которого ledger и заводился. Уже
            # внесённую запись noqa тоже не спасает: нарушение исчезает из
            # прогона, запись становится stale и красит проверку с другой
            # стороны. Здесь этот путь закрыт с обеих.
            "--ignore-noqa",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # ruff отдаёт 1 при найденных нарушениях — это ожидаемый путь, а не сбой.
    # Сбоем считается всё остальное: пустой stdout при ненулевом коде значит,
    # что упал сам ruff, и молча считать это «нарушений нет» нельзя.
    if not proc.stdout.strip():
        if proc.returncode not in (0, 1):
            raise SystemExit(
                f"ruff failed (exit {proc.returncode}):\n{proc.stderr.strip()}"
            )
        return []
    return json.loads(proc.stdout)


def _function_at(path: Path, row: int) -> str:
    """Имя функции, объявленной на строке ``row``.

    ruff указывает строку ``def``, поэтому достаточно точного совпадения;
    вложенные функции получают квалифицированное имя ``outer.inner``, иначе
    два одноимённых внутренних помощника в одном файле неразличимы.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef) and child.lineno == row:
                    found.append(name)
                walk(child, f"{name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found[0] if found else f"<line {row}>"


def _parse_limits(message: str) -> tuple[int, int]:
    """Вытащить (значение, порог) из текста ruff.

    C901: "`f` is too complex (46 > 15)"; PLR0915: "Too many statements
    (108 > 60)". Разбирается последняя скобка, так что имя функции со скобками
    внутри разбор не ломает.
    """
    body = message.rsplit("(", 1)[-1].rstrip(")")
    left, _, right = body.partition(">")
    return int(left.strip()), int(right.strip())


def collect_violations(target: str = TARGET) -> list[Violation]:
    """Текущие нарушения бюджета, отсортированные для стабильного вывода."""
    out: list[Violation] = []
    for item in _run_ruff(target):
        path = Path(item["filename"])
        # Путь вне репозитория — законный случай: так проверку прогоняют
        # тесты по временному дереву. Абсолютный путь в этом случае честнее,
        # чем упасть на relative_to.
        rel = (
            path.relative_to(REPO_ROOT).as_posix()
            if path.is_relative_to(REPO_ROOT)
            else path.as_posix()
        )
        row = item["location"]["row"]
        value, limit = _parse_limits(item["message"])
        out.append(
            Violation(
                path=rel,
                function=_function_at(path, row),
                rule=item["code"],
                value=value,
                limit=limit,
            )
        )
    return sorted(out, key=lambda v: (v.path, v.function, v.rule))


def ceiling(value: int, headroom_pct: float = DEFAULT_HEADROOM_PCT) -> int:
    """Потолок для waived-функции: замер плюс объявленный запас."""
    return int(math.ceil(value * (1 + headroom_pct / 100)))


def load_ledger(path: Path = LEDGER_PATH) -> dict[str, dict]:
    """Записи ledger по ключу ``путь::функция::правило``."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {f"{w['path']}::{w['function']}::{w['rule']}": w for w in raw["waivers"]}


@dataclass(frozen=True)
class Report:
    """Что бюджет говорит о текущем дереве."""

    new: tuple[Violation, ...]
    stale: tuple[str, ...]
    over: tuple[tuple[Violation, int], ...]
    waived: tuple[tuple[Violation, int], ...]
    # Сколько исключений за какой причиной: task:#NNNN / permanent / unclaimed.
    by_reason: dict[str, int]

    @property
    def ok(self) -> bool:
        return not (self.new or self.stale or self.over)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "new": [v.as_dict() for v in self.new],
            "stale": list(self.stale),
            "over": [dict(v.as_dict(), max=m) for v, m in self.over],
            "waived_count": len(self.waived),
            "by_reason": dict(self.by_reason),
        }


def check(violations: list[Violation], ledger: dict[str, dict]) -> Report:
    """Свести нарушения с ledger. Чистая функция: ни ruff, ни файловой системы."""
    new: list[Violation] = []
    over: list[tuple[Violation, int]] = []
    waived: list[tuple[Violation, int]] = []
    seen: set[str] = set()
    by_reason: dict[str, int] = {}

    for violation in violations:
        key = f"{violation.key}::{violation.rule}"
        entry = ledger.get(key)
        if entry is None:
            new.append(violation)
            continue
        seen.add(key)
        # "task:#1067 — ..." / "permanent — ..." / "unclaimed — ..."
        kind = str(entry.get("removed_by", "")).split("—")[0].strip() or "?"
        by_reason[kind] = by_reason.get(kind, 0) + 1
        allowed = int(entry["max"])
        if violation.value > allowed:
            over.append((violation, allowed))
        else:
            waived.append((violation, allowed))

    stale = tuple(sorted(set(ledger) - seen))
    return Report(
        new=tuple(new),
        stale=stale,
        over=tuple(over),
        waived=tuple(waived),
        by_reason=by_reason,
    )


def format_report(report: Report) -> str:
    """Отчёт печатается и на зелёном прогоне — молчание читается как «всё под контролем»."""
    lines: list[str] = []
    if report.new:
        lines.append(f"НОВАЯ СЛОЖНОСТЬ ({len(report.new)}):")
        for v in report.new:
            lines.append(f"  {v.path}::{v.function} — {v.rule} {v.value} > {v.limit}")
        lines.append(
            "  Разбейте функцию или внесите её в "
            f"{LEDGER_PATH.relative_to(REPO_ROOT)} с задачей, которая её снимет."
        )
    if report.over:
        lines.append(f"WAIVED-ФУНКЦИЯ ВЫРОСЛА ({len(report.over)}):")
        for v, allowed in report.over:
            lines.append(
                f"  {v.path}::{v.function} — {v.rule} {v.value} > потолок {allowed}"
            )
    if report.stale:
        lines.append(f"ИСКЛЮЧЕНИЕ ПЕРЕЖИЛО ПРИЧИНУ ({len(report.stale)}):")
        for key in report.stale:
            lines.append(f"  {key} — нарушения больше нет, снимите запись")

    lines.append(f"Исключений в силе: {len(report.waived)}")
    if report.by_reason:
        # Молчание о непокрытом читается как «покрыто всё»: долг без задачи
        # называется числом на каждом прогоне, а не всплывает через полгода.
        for kind, count in sorted(report.by_reason.items()):
            lines.append(f"  {kind}: {count}")
    for v, allowed in sorted(report.waived, key=lambda p: -(p[0].value / p[1]))[:5]:
        used = 100.0 * v.value / allowed
        mark = "  <-- запас почти съеден" if used >= LOUD_AT_PCT else ""
        lines.append(
            f"  {v.path}::{v.function} {v.rule} {v.value}/{allowed} ({used:.0f}%){mark}"
        )
    return "\n".join(lines)


def write_ledger(
    violations: list[Violation],
    path: Path = LEDGER_PATH,
    headroom_pct: float = DEFAULT_HEADROOM_PCT,
) -> None:
    """Перезаписать ledger по факту. Осознанный акт: правка видна в диффе."""
    existing = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        comment = raw.get("_comment", [])
        existing = {
            f"{w['path']}::{w['function']}::{w['rule']}": w for w in raw["waivers"]
        }
    else:
        comment = []

    waivers = []
    for v in violations:
        key = f"{v.key}::{v.rule}"
        prior = existing.get(key, {})
        waivers.append(
            {
                "path": v.path,
                "function": v.function,
                "rule": v.rule,
                "measured": v.value,
                "max": ceiling(v.value, headroom_pct),
                # Причина не выводится машиной: её пишет человек, и именно
                # она отличает долг от постоянного исключения.
                "removed_by": prior.get("removed_by", "TODO: задача или причина"),
            }
        )

    path.write_text(
        json.dumps(
            {"_comment": comment, "headroom_pct": headroom_pct, "waivers": waivers},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    parser.add_argument(
        "--update", action="store_true", help="перезаписать ledger по факту"
    )
    parser.add_argument("--target", default=TARGET, help="что проверять")
    args = parser.parse_args()

    violations = collect_violations(args.target)

    if args.update:
        write_ledger(violations)
        print(f"ledger перезаписан: {len(violations)} исключений")
        return 0

    report = check(violations, load_ledger())
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
