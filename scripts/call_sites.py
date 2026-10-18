#!/usr/bin/env python3
"""Карта мест вызова для постановки: кандидаты по имени (#1853).

hub/services/call_sites.py (#601) строит индекс «короткое имя → места вызова»,
но используется только на сдаче. При постановке L-задачи карту мест собирают
руками и критиком; этот скрипт даёт её одной командой.

    uv run python scripts/call_sites.py trust_of ci_trust.pass_gap
    uv run python scripts/call_sites.py hub/services/ci_trust.py --markdown
    uv run python scripts/call_sites.py --root <каталог снимка> <модуль> --json

Это НОМИНАЛЬНЫЙ поиск: результат — «кандидаты по имени». Разрешения импортов,
алиасов (``from x import f as g``), модуля определения и чтений ``ref = f`` нет;
вызов и передача аргументом не различаются. Шапка вывода говорит об этом сама.

``--root`` — анализируемое дерево (по умолчанию корень checkout). Код
индексатора и самого скрипта берётся из текущей ветки.

Коды возврата: 0 — ответ получен (в том числе «вызовов нет» и «публичных
функций нет»); 2 — «не проанализировано» (пустой индекс) или запрошенный
модуль отсутствует, нечитаем, не разбирается. Пустой результат анализа — не
ответ «вызовов нет».
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# The indexer comes from THIS checkout, whatever PYTHONPATH or an editable
# install of another checkout says: our root goes first, older entries go.
sys.path[:] = [p for p in sys.path if p not in ("", str(REPO_ROOT))]
sys.path.insert(0, str(REPO_ROOT))

from hub.services import call_sites as _indexer  # noqa: E402
from hub.services.call_sites import CallSite, build_call_index  # noqa: E402

RC_OK = 0
RC_FAIL = 2

BLIND_SPOTS: tuple[str, ...] = (
    "кандидаты по имени: совпадение по короткому имени, разрешения импортов нет",
    "вызовы через registry, getattr или имя-строку не видны",
    "другие языки (не .py) не разбираются",
    "алиасы импорта (from x import f as g) не находятся",
    "одноимённые символы (функции и методы разных модулей) делят одну корзину",
    "вызов и передача аргументом не различаются",
    "индексируются только hub/ и tests/ анализируемого дерева (scripts/ и прочее — нет)",
    "чтения вида ref = f не видны",
    "из модуля раскрываются публичные функции верхнего уровня (def, async def); "
    "методы, вложенные функции и определения внутри if/try не входят",
)

Site = dict[str, object]


def _site_dict(site: CallSite) -> Site:
    return {"file": site.file, "line": site.line, "caller": site.caller}


def _sites_for(index: dict[str, list[CallSite]], name: str) -> list[Site]:
    seen: set[tuple[str, int, str]] = set()
    out: list[Site] = []
    for site in sorted(index.get(name, []), key=lambda s: (s.file, s.line)):
        key = (site.file, site.line, site.caller)
        if key not in seen:
            seen.add(key)
            out.append(_site_dict(site))
    return out


def _is_module(target: str) -> bool:
    return target.endswith(".py")


def _public_functions(root: Path, target: str) -> list[str]:
    """Top-level public def / async def names of a module. Raises ValueError."""
    path = Path(target)
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise ValueError(f"модуль вне --root: {target}")
        source = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"модуль отсутствует или нечитаем: {target} ({exc})") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"модуль нечитаем: {target} ({exc})") from exc
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"модуль не разбирается: {target} ({exc})") from exc
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    ]


def _symbol_entry(index: dict[str, list[CallSite]], label: str, name: str) -> dict:
    return {"symbol": name, "label": label, "sites": _sites_for(index, name)}


def _answer_target(
    root: Path, index: dict[str, list[CallSite]], target: str
) -> dict[str, object]:
    if not _is_module(target):
        short = target.rsplit(".", 1)[-1]
        if not short.isidentifier():
            return _error_target(target, "symbol", f"не имя символа: {target}")
        return {
            "target": target,
            "kind": "symbol",
            "error": None,
            "note": None,
            "symbols": [_symbol_entry(index, target, short)],
        }
    try:
        names = _public_functions(root, target)
    except ValueError as exc:
        return _error_target(target, "module", str(exc))
    return {
        "target": target,
        "kind": "module",
        "error": None,
        "note": None if names else "публичных функций нет",
        "symbols": [_symbol_entry(index, f"{target}:{n}", n) for n in names],
    }


def _error_target(target: str, kind: str, message: str) -> dict[str, object]:
    return {
        "target": target,
        "kind": kind,
        "error": message,
        "note": None,
        "symbols": [],
    }


def run_query(root: Path, targets: list[str]) -> dict[str, object]:
    base: dict[str, object] = {
        "root": str(root),
        "blind_spots": list(BLIND_SPOTS),
        "unparsed": [],
        "targets": [],
    }
    if not root.is_dir():
        base["status"] = "error"
        base["message"] = f"каталог --root не найден: {root}"
        return base
    index, unparsed = build_call_index(str(root))
    base["unparsed"] = sorted(unparsed)
    if not index:
        base["status"] = "not_analysed"
        base["message"] = (
            "не проанализировано: в индексе нет ни одного вызова "
            f"(каталог {root}, деревья hub/ и tests/)"
        )
        return base
    answers = [_answer_target(root, index, t) for t in targets]
    base["targets"] = answers
    failed = any(a["error"] for a in answers)
    base["status"] = "error" if failed else "ok"
    return base


def _rows(result: dict[str, object]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for target in result["targets"]:  # type: ignore[attr-defined]
        for sym in target["symbols"]:
            for site in sym["sites"]:
                rows.append(
                    (
                        sym["label"],
                        f"{site['file']}:{site['line']}",
                        str(site["caller"]),
                    )
                )
    return rows


def _header_lines(result: dict[str, object], markdown: bool) -> list[str]:
    mark = "- " if markdown else "  * "
    lines = ["Кандидаты по имени (номинальный поиск). Слепые зоны:"]
    lines += [f"{mark}{spot}" for spot in result["blind_spots"]]  # type: ignore[attr-defined]
    unparsed = result["unparsed"]
    if unparsed:
        lines.append(f"Не разобраны ({len(unparsed)}): " + ", ".join(unparsed))  # type: ignore[arg-type]
    return lines


def _target_lines(target: dict, markdown: bool) -> list[str]:
    if target["error"]:
        return [f"ОШИБКА {target['target']}: {target['error']}"]
    lines: list[str] = []
    if target["note"]:
        lines.append(f"{target['target']}: {target['note']}")
    for sym in target["symbols"]:
        if not sym["sites"]:
            lines.append(f"{sym['label']}: вызовов нет")
        elif not markdown:
            lines.append(f"{sym['label']}: {len(sym['sites'])}")
            lines += [
                f"  {s['file']}:{s['line']} ({s['caller']})" for s in sym["sites"]
            ]
    return lines


def _table(result: dict[str, object]) -> list[str]:
    rows = _rows(result)
    if not rows:
        return []
    lines = ["", "| символ | файл:строка | функция |", "|---|---|---|"]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return lines


def render(result: dict[str, object], markdown: bool) -> str:
    if result["status"] == "error" and "message" in result:
        return str(result["message"])
    lines = _header_lines(result, markdown)
    if result["status"] == "not_analysed":
        return "\n".join([str(result["message"]), "", *lines])
    lines.append("")
    for target in result["targets"]:  # type: ignore[attr-defined]
        lines += _target_lines(target, markdown)
    if markdown:
        lines += _table(result)
    return "\n".join(lines)


def _indexer_file() -> str:
    return str(getattr(_indexer, "__file__", "") or "")


def _indexer_problem() -> str | None:
    """Say so when the indexer was loaded from outside this checkout."""
    path = _indexer_file()
    try:
        inside = Path(path).resolve().is_relative_to(REPO_ROOT)
    except (OSError, ValueError):
        inside = False
    if inside:
        return None
    return f"индексатор загружен не из этой ветки: {path or 'путь неизвестен'}"


class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _ArgError(message)


def _fail(message: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"status": "error", "message": message}, ensure_ascii=False))
    else:
        print(f"ОШИБКА: {message}", file=sys.stderr)
    return RC_FAIL


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in raw
    problem = _indexer_problem()
    if problem:
        return _fail(problem, as_json)
    parser = _Parser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "targets", nargs="+", help="символ (trust_of, mod.f) или модуль .py"
    )
    parser.add_argument("--root", default=str(REPO_ROOT), help="анализируемое дерево")
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--markdown", action="store_true", help="таблица markdown")
    fmt.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    try:
        args = parser.parse_args(raw)
    except _ArgError as exc:
        return _fail(str(exc), as_json)

    result = run_query(Path(args.root), list(args.targets))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result, args.markdown))
    return RC_OK if result["status"] == "ok" else RC_FAIL


if __name__ == "__main__":
    sys.exit(main())
