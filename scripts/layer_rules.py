#!/usr/bin/env python3
"""CI check: the import directions between hub layers stay as declared (#1852).

Слои хаба описаны прозой (docs/architecture.md, system-map.md), и то, что
services не импортирует entry, а integrations — services, держалось на
внимании. Обратные рёбра (models, repository, db → services) не были учтены
нигде. Эта проверка превращает прозу в красный CI, по образцу
complexity_budget: таблица, запреты и ledger сегодняшних исключений лежат в
docs/agent-context/layer-rules.json.

    uv run python scripts/layer_rules.py            # проверка
    uv run python scripts/layer_rules.py --update   # дописать новые рёбра в ledger

Ребро — уникальная пара «модуль → модуль» внутри hub. Рёбра собирает обход AST
всех .py: импорты уровня модуля и внутри функций, относительные, под
TYPE_CHECKING. Разрешение ``from a import b``: существующий модуль ``a.b``,
иначе ``a`` (b — символ). Неявных рёбер к родительским пакетам нет:
``import hub.services.x`` — это ребро к ``hub.services.x`` и только к нему.

Способы покраснеть, все названы поимённо:
  * запрещённое ребро, которого нет в ledger — новое нарушение слоя;
  * запись ledger без ребра — исключение пережило причину, удалите строку;
  * модуль без слоя или в двух слоях — таблица неполна или двусмысленна;
  * файл, который не разбирается — тихо считать его «без импортов» нельзя;
  * removed_by не по формату (или плейсхолдер ``--update``).

Чего проверка не видит: динамические импорты (``importlib.import_module``,
``__import__``) и обращения через атрибут (``import hub; hub.services.x``) — в AST
это не ребро между модулями. Сегодня в hub/ их нет; появятся — расширяйте обход.

``--update`` дописывает в ledger только новые запрещённые рёбра с плейсхолдером
и существующих записей не меняет; причину пишет человек.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from complexity_budget import REMOVED_BY_HINT, validate_removed_by  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "docs" / "agent-context" / "layer-rules.json"
TARGET = REPO_ROOT / "hub"
PLACEHOLDER = "unclaimed — <нужна причина>"

Edge = tuple[str, str]


@dataclass(frozen=True)
class Config:
    layers: dict[str, dict]
    forbidden: dict[str, list[str]]
    ledger: dict[Edge, dict]
    duplicates: tuple[Edge, ...] = ()


@dataclass(frozen=True)
class Scan:
    modules: frozenset[str]
    edges: frozenset[Edge]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    forbidden: bool
    src_layer: str = ""
    dst_layer: str = ""


def load_config(path: Path = CONFIG_PATH) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    keys = [(e["from"], e["to"]) for e in raw.get("ledger", [])]
    ledger = dict(zip(keys, raw.get("ledger", []), strict=True))
    dupes = tuple(sorted({k for k in keys if keys.count(k) > 1}))
    return Config(raw["layers"], raw["forbidden"], ledger, dupes)


def _pattern(glob: str) -> re.Pattern[str]:
    """``hub.web_*``: звёздочка не пересекает точку, потомки пакета — через trees."""
    return re.compile(re.escape(glob).replace(r"\*", "[^.]*") + r"\Z")


def layers_of(module: str, layers: dict[str, dict]) -> list[str]:
    """Все слои, в которые таблица отнесла модуль (верно — ровно один)."""
    found = []
    for name, spec in layers.items():
        by_module = any(_pattern(g).match(module) for g in spec.get("modules", []))
        by_tree = any(
            module == t or module.startswith(t + ".") for t in spec.get("trees", [])
        )
        if by_module or by_tree:
            found.append(name)
    return found


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = [root.name, *rel.parts]
    return ".".join(p for p in parts if p != "__init__")


def _resolve(base: str, names: list[str], modules: frozenset[str]) -> list[str]:
    """``from base import names``: base.name, если такой модуль есть, иначе base."""
    out = []
    for name in names:
        full = f"{base}.{name}" if base else name
        out.append(full if full in modules else base)
    return out


def _import_targets(node: ast.AST, package: str, modules: frozenset[str]) -> list[str]:
    if isinstance(node, ast.Import):
        found = []
        for alias in node.names:
            parts = alias.name.split(".")
            while parts and ".".join(parts) not in modules:
                parts.pop()
            if parts:
                found.append(".".join(parts))
        return found
    if not isinstance(node, ast.ImportFrom):
        return []
    base = node.module or ""
    if node.level:
        anchor = package.split(".")
        anchor = anchor[: len(anchor) - (node.level - 1)]
        base = ".".join([*anchor, *([base] if base else [])])
    return [t for t in _resolve(base, [a.name for a in node.names], modules) if t]


def collect_edges(root: Path = TARGET) -> Scan:
    """Уникальные рёбра между модулями дерева ``root`` и ошибки разбора."""
    root = root.resolve()
    files = sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    names = {p: _module_name(root, p) for p in files}
    modules = frozenset(names.values())
    edges: set[Edge] = set()
    errors: list[str] = []
    for path, module in names.items():
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            errors.append(
                f"{path.relative_to(root.parent).as_posix()}: не разобран ({exc})"
            )
            continue
        for node in ast.walk(tree):
            for target in _import_targets(node, package, modules):
                if target != module and target in modules:
                    edges.add((module, target))
    return Scan(modules, frozenset(edges), tuple(errors))


def is_forbidden(edge: Edge, config: Config) -> Verdict:
    src = layers_of(edge[0], config.layers)
    dst = layers_of(edge[1], config.layers)
    if len(src) != 1 or len(dst) != 1:
        return Verdict(False)
    banned = dst[0] in config.forbidden.get(src[0], [])
    return Verdict(banned, src[0], dst[0])


def _layer_problems(modules: frozenset[str], config: Config) -> list[str]:
    problems = []
    for module in sorted(modules):
        found = layers_of(module, config.layers)
        if not found:
            problems.append(f"{module} — без слоя: добавьте в таблицу слоёв")
        elif len(found) > 1:
            problems.append(f"{module} — в двух слоях: {', '.join(found)}")
    return problems


def _config_problems(config: Config) -> list[str]:
    names = set(config.layers)
    problems = [f"ledger: ребро записано дважды: {_fmt(e)}" for e in config.duplicates]
    for src, dsts in config.forbidden.items():
        for layer in [src, *dsts]:
            if layer not in names:
                problems.append(f"запрет называет неизвестный слой {layer!r}")
    return problems


@dataclass(frozen=True)
class Report:
    new: tuple[tuple[Edge, Verdict], ...]
    stale: tuple[Edge, ...]
    invalid: tuple[tuple[Edge, str], ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (self.new or self.stale or self.invalid or self.problems)


def check(scan: Scan, config: Config) -> Report:
    problems = [*scan.errors, *_config_problems(config)]
    problems += _layer_problems(scan.modules, config)
    banned = {
        e: v for e in sorted(scan.edges) if (v := is_forbidden(e, config)).forbidden
    }
    new = tuple((e, v) for e, v in banned.items() if e not in config.ledger)
    stale = tuple(sorted(set(config.ledger) - set(banned)))
    invalid = []
    for edge, entry in sorted(config.ledger.items()):
        problem = validate_removed_by(entry.get("removed_by"))
        if str(entry.get("removed_by", "")).strip() == PLACEHOLDER:
            problem = "плейсхолдер --update: впишите причину"
        if problem:
            invalid.append((edge, problem))
    return Report(new, stale, tuple(invalid), tuple(problems))


def _fmt(edge: Edge) -> str:
    return f"{edge[0]} -> {edge[1]}"


def format_report(report: Report, ledger_size: int) -> str:
    lines: list[str] = []
    if report.problems:
        lines.append(
            f"ТАБЛИЦА СЛОЁВ ИЛИ ДЕРЕВО НЕ РАЗБИРАЮТСЯ ({len(report.problems)}):"
        )
        lines += [f"  {p}" for p in report.problems]
    if report.new:
        lines.append(f"ЗАПРЕЩЁННОЕ НАПРАВЛЕНИЕ ИМПОРТА ({len(report.new)}):")
        for edge, verdict in report.new:
            lines.append(f"  {_fmt(edge)} ({verdict.src_layer} → {verdict.dst_layer})")
        lines.append(
            "  Уберите импорт или внесите ребро в docs/agent-context/layer-rules.json "
            "(ledger) с задачей, которая его снимет."
        )
    if report.invalid:
        lines.append(f"REMOVED_BY НЕ ПО ФОРМАТУ ({len(report.invalid)}):")
        for edge, problem in report.invalid:
            lines.append(f"  {_fmt(edge)} — {problem}; {REMOVED_BY_HINT}")
    if report.stale:
        lines.append(f"ИСКЛЮЧЕНИЕ ПЕРЕЖИЛО ПРИЧИНУ ({len(report.stale)}):")
        lines += [
            f"  {_fmt(e)} — ребра больше нет, удалите строку" for e in report.stale
        ]
    lines.append(f"Исключений в ledger слоёв: {ledger_size}")
    return "\n".join(lines)


def update_ledger(scan: Scan, config: Config, path: Path) -> int:
    """Дописать новые запрещённые рёбра с плейсхолдером; остальное не трогать."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = {(e["from"], e["to"]) for e in raw.get("ledger", [])}
    fresh = sorted(
        e for e in scan.edges if is_forbidden(e, config).forbidden and e not in known
    )
    raw.setdefault("ledger", []).extend(
        {"from": s, "to": d, "removed_by": PLACEHOLDER} for s, d in fresh
    )
    path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return len(fresh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="дописать новые рёбра")
    parser.add_argument("--root", type=Path, default=TARGET, help="каталог пакета hub")
    parser.add_argument(
        "--config", type=Path, default=CONFIG_PATH, help="layer-rules.json"
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    scan = collect_edges(args.root)
    if args.update:
        added = update_ledger(scan, config, args.config)
        print(f"ledger слоёв: дописано {added} рёбер, существующие не тронуты")
        return 0
    report = check(scan, config)
    print(format_report(report, len(config.ledger)))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
