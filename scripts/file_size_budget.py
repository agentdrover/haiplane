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

Ledger дерева tests только сокращается (#1932): порог tests равен
REVIEW_FILE_LINE_CAP (сколько строк файла читает ревью), а файл тестов длиннее
него ревью дочитать не может. Поэтому против БАЗЫ (состояние репозитория до
изменения) для tests красными становятся ещё два случая: запись, которой нет в
ledger базы (новый файл или файл, переросший порог), и запись с поднятыми
measured/max. ``--update`` не поднимает записи tests. База берётся из окружения
CI (PR — ветка PR, push — ``before``, иначе origin/develop; как у
migration_steps_add_only), ``--base REF`` задаёт её явно, ``--no-base``
отключает сравнение. Базу получить не удалось — код возврата 2, не «чисто».

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
import os
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # соседний скрипт CI

from migration_steps_add_only import (  # noqa: E402
    DEFAULT_BASE,
    CompareError,
    _git,
    _snapshot,
    ci_base,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_REL = Path("docs") / "agent-context" / "file-size-budget.json"
DEFAULT_HEADROOM_PCT = 5.0
NEAR_KEY = "near_pct"
NEAR_SHOWN = 5
UNCLAIMED = "unclaimed"
_SKIP_DIRS = {"__pycache__", ".venv", "node_modules"}
# Деревья, чей ledger только сокращается (#1932): порог tests равен тому, сколько
# строк файла читает ревью; hub и scripts этого правила не получают.
RATCHET_TREES = frozenset({"tests"})
BASE_UNREADABLE = 2
ADDED_SHOWN = 5
# Всё, что может выбросить разбор ledger/pyproject базы: в любом случае rc 2.
_BASE_ERRORS = (
    CompareError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    OverflowError,
)


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
    regress: tuple[tuple[str, str], ...] = ()  # путь, причина: ledger tests вырос
    reserve_low: tuple[tuple[str, int, int], ...] = ()  # путь, строк, потолок
    base_lines: dict[str, int | None] = field(default_factory=dict)  # new -> на базе

    @property
    def ok(self) -> bool:
        return not (self.new or self.over or self.stale or self.regress)

    @property
    def near_ranked(self) -> tuple[tuple[str, int, int], ...]:
        """Файлы у порога, самые заполненные первыми."""
        return tuple(sorted(self.near, key=lambda p: (-(p[1] / p[2]), p[0])))

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "new": [
                {
                    "path": p,
                    "lines": n,
                    "threshold": t,
                    "lines_on_base": self.base_lines.get(p),
                }
                for p, n, t in self.new
            ],
            "regress": [{"path": p, "reason": r} for p, r in self.regress],
            "over": [{"path": p, "lines": n, "max": m} for p, n, m in self.over],
            "stale": [{"path": p, "reason": r} for p, r in self.stale],
            "waived_count": len(self.waived),
            "grown": [
                {"path": p, "lines": n, "measured": m, "max": x}
                for p, n, m, x in self.grown
            ],
            "reserve_low": [
                {"path": p, "lines": n, "max": x, "reserve": x - n}
                for p, n, x in self.reserve_low
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


def _tree(path: str) -> str:
    return path.split("/", 1)[0]


def _threshold(path: str, thresholds: dict[str, int]) -> int:
    return thresholds[_tree(path)]


@dataclass(frozen=True)
class BaseView:
    """Что было на базе: её ledger и размер файла на ней (None — файла не было)."""

    ledger: dict[str, dict]
    lines: Callable[[str], int | None]
    headroom_pct: float = DEFAULT_HEADROOM_PCT
    thresholds: dict[str, int] = field(default_factory=dict)  # пороги на базе

    def ratchet_in_force(self, tree: str, current: int) -> bool:
        """Порог дерева на базе не выше нынешнего: правило «только сокращается» уже
        действовало. База с более высоким порогом (до #1932 у tests 3000) его не знала:
        там ledger ещё не нёс записей tests, и вносить их можно по нынешним замерам."""
        return self.thresholds[tree] <= current


def _unknown_base_reason(on_base: int | None) -> str:
    if on_base is None:
        return "новая запись: файла не было на базе (новый файл или переименование)"
    return f"новая запись: на базе файл {on_base} строк, он перерос порог"


def _base_number(entry: dict, key: str) -> int:
    try:
        return int(entry[key])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CompareError(f"запись ledger базы {entry!r}: нет числа {key}") from exc


def _ratchet_problems(
    ledger: dict[str, dict], thresholds: dict[str, int], base: BaseView
) -> list[tuple[str, str]]:
    """Записи деревьев RATCHET_TREES, которых база не допускает."""
    found: list[tuple[str, str]] = []
    for path, entry in sorted(ledger.items()):
        if _tree(path) not in RATCHET_TREES:
            continue
        prior = base.ledger.get(path)
        if not base.ratchet_in_force(_tree(path), thresholds[_tree(path)]):
            # База правила не знала. Вносятся только файлы, что уже были на ней
            # длиннее порога (нового файла, переименования и файла, выросшего
            # из короткого, это не касается).
            on_base = base.lines(path)
            if prior is None and (
                on_base is None or on_base <= _threshold(path, thresholds)
            ):
                found.append((path, _unknown_base_reason(on_base)))
            continue
        if prior is not None:
            top_measured, top_max = (
                _base_number(prior, "measured"),
                _base_number(prior, "max"),
            )
        else:
            on_base = base.lines(path)
            if on_base is None:
                reason = "файла не было на базе"
            else:
                reason = f"на базе файл {on_base} строк, в её ledger записи нет"
            found.append((path, f"новая запись: {reason}"))
            continue
        if int(entry["measured"]) > top_measured or int(entry["max"]) > top_max:
            found.append(
                (
                    path,
                    f"measured {entry['measured']} / потолок {entry['max']} выше, чем "
                    f"допускает база ({top_measured} / {top_max})",
                )
            )
    return found


def check(
    sizes: dict[str, int],
    thresholds: dict[str, int],
    ledger: dict[str, dict],
    near_pct: float,
    base: BaseView | None = None,
) -> Report:
    """Свести замеры с ledger. Чистая функция: ни файловой системы, ни конфига."""
    new: list[tuple[str, int, int]] = []
    over: list[tuple[str, int, int]] = []
    waived: list[tuple[str, int, int]] = []
    stale: list[tuple[str, str]] = []
    grown: list[tuple[str, int, int, int]] = []
    near: list[tuple[str, int, int]] = []
    reserve_low: list[tuple[str, int, int]] = []
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
        if 0 <= allowed - lines and (allowed - lines) * 100 < allowed:
            reserve_low.append((path, lines, allowed))

    for path in sorted(ledger):
        lines = sizes.get(path)
        if lines is None:
            stale.append((path, "файла больше нет"))
        elif lines <= _threshold(path, thresholds):
            stale.append((path, f"{lines} строк, порог {_threshold(path, thresholds)}"))
    base_lines = {
        p: base.lines(p) for p, _n, _t in new if base and _tree(p) in RATCHET_TREES
    }
    regress = _ratchet_problems(ledger, thresholds, base) if base else []
    return Report(
        new=tuple(sorted(new)),
        over=tuple(sorted(over)),
        stale=tuple(stale),
        waived=tuple(sorted(waived)),
        by_reason=by_reason,
        grown=tuple(sorted(grown)),
        near=tuple(sorted(near)),
        reserve_low=tuple(sorted(reserve_low)),
        regress=tuple(regress),
        base_lines=base_lines,
    )


def _new_kind(report: Report, path: str, limit: int) -> str:
    """Для tests: новый это файл или переросший порог (по размеру на базе)."""
    if _tree(path) not in RATCHET_TREES or path not in report.base_lines:
        return ""
    on_base = report.base_lines[path]
    if on_base is None:
        return " (новый файл)"
    return f" (файл перерос порог {limit}: на базе было {on_base})"


def format_report(report: Report) -> str:
    """Отчёт печатается и на зелёном прогоне — молчание читается как «всё под контролем»."""
    lines: list[str] = []
    if report.new:
        lines.append(f"ФАЙЛ ВЫШЕ ПОРОГА ВНЕ LEDGER ({len(report.new)}):")
        for path, n, limit in report.new:
            lines.append(
                f"  {path} — {n} > порог {limit}{_new_kind(report, path, limit)}"
            )
        if any(_tree(p) not in RATCHET_TREES for p, _n, _t in report.new):
            lines.append(
                "  Разбейте файл или внесите его в "
                f"{LEDGER_REL.as_posix()} через --update с задачей, которая его снимет."
            )
        if any(_tree(p) in RATCHET_TREES for p, _n, _t in report.new):
            lines.append(
                "  Файлы tests: разбейте файл на части не длиннее порога (тесты "
                "разных AC - в разные файлы); ledger тестов не растёт, --update "
                "здесь не выход."
            )
    if report.regress:
        lines.append(f"LEDGER ТЕСТОВ ВЫРОС ({len(report.regress)}):")
        for path, reason in report.regress:
            lines.append(f"  {path} — {reason}")
        lines.append(
            "  Ledger tests только сокращается: разбейте файл на части не длиннее "
            "порога; потолок старого файла не поднимается."
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
    if report.reserve_low:
        lines.append(
            f"Файл из ledger: запас почти съеден ({len(report.reserve_low)}), CI не красит: "
            "остаток меньше 1% потолка, выносите код в модуль, не ужимайте тексты:"
        )
        for path, n, allowed in report.reserve_low:
            lines.append(f"  {path} — {n}/{allowed}, остаток {allowed - n}")
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


def _waiver_entry(
    path: str, lines: int, prior: dict | None, clamp: dict | None, headroom: float
) -> dict:
    """Запись ledger; у деревьев RATCHET_TREES запись только опускается (от ``clamp``)."""
    measured, top = lines, ceiling(lines, headroom)
    if clamp is not None and _tree(path) in RATCHET_TREES:
        measured = min(measured, int(clamp["measured"]))
        top = min(top, int(clamp["max"]))
    return {
        "path": path,
        "measured": measured,
        "max": top,
        "removed_by": (prior or {}).get("removed_by", UNCLAIMED),
    }


def write_ledger(
    sizes: dict[str, int],
    thresholds: dict[str, int],
    path: Path,
    clamp: dict[str, dict] | None = None,
) -> int:
    """Перезаписать ledger по факту; removed_by существующих записей сохраняется.

    ``clamp`` - записи, выше которых tests не поднимается: ledger базы (CI
    сверяет с ней), пустой - зажима нет (база правила не знала), None - текущий
    ledger (базу получить не удалось).
    """
    raw = _read_ledger(path)
    headroom = float(raw.get("headroom_pct", DEFAULT_HEADROOM_PCT))
    prior = {w["path"]: w for w in raw["waivers"]}
    waivers = [
        _waiver_entry(
            p, n, prior.get(p), (prior if clamp is None else clamp).get(p), headroom
        )
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


def _event_before(env: Mapping[str, str]) -> str:
    """``before`` из события push (GITHUB_EVENT_PATH); нечитаемое - CompareError.

    Откат на origin/develop тут был бы ложью: после push он равен HEAD, ledger
    сравнился бы сам с собой и проверка молча стала бы зелёной.
    """
    try:
        payload = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        before = str(payload["before"] or "")
    except (KeyError, OSError, ValueError, TypeError, AttributeError) as exc:
        raise CompareError(f"push без читаемого before события: {exc!r}") from exc
    if not before:
        raise CompareError("push: before события пуст")
    return before


def base_from_environment(env: Mapping[str, str]) -> tuple[str, bool]:
    """База сравнения из окружения CI и признак «ровно этот коммит» (push).

    PR - origin/<ветка PR>; push - состояние ДО события (HEAD уже содержит
    правку, и сверка с самой веткой молчала бы); остальное - origin/develop.
    Нулевой before читаемого события (новая ветка) - не сбой: база origin/develop.
    """
    event = env.get("GITHUB_EVENT_NAME", "")
    before = _event_before(env) if event == "push" else ""
    base = ci_base(event, before, env.get("GITHUB_BASE_REF", ""), DEFAULT_BASE)
    return base, event == "push" and base == before


def _parse_ledger(raw: object) -> dict[str, dict]:
    """Ledger базы по пути; запись без числа measured/max - CompareError."""
    try:
        entries = {w["path"]: w for w in raw["waivers"]}  # type: ignore[index]
        for entry in entries.values():
            _base_number(entry, "measured")
            _base_number(entry, "max")
    except (KeyError, TypeError) as exc:
        raise CompareError(f"ledger: нет waivers/path: {exc!r}") from exc
    return entries


def load_base(root: Path, base: str, exact: bool) -> BaseView:
    """Ledger базы и размеры файлов на ней. CompareError — сравнить нельзя."""
    snap = _snapshot(root, base, exact, _git)
    try:
        raw = json.loads(str(_git(["show", f"{snap}:{LEDGER_REL.as_posix()}"], root)))
        ledger = _parse_ledger(raw)
        headroom = float(raw.get("headroom_pct", DEFAULT_HEADROOM_PCT))
    except _BASE_ERRORS as exc:
        raise CompareError(
            f"ledger базы {base} ({snap[:12]}) не прочитан: {exc}"
        ) from exc

    def lines(path: str) -> int | None:
        if not str(_git(["ls-tree", snap, "--", path], root)).strip():
            return None
        return count_lines(bytes(_git(["show", f"{snap}:{path}"], root, text=False)))

    try:
        text = str(_git(["show", f"{snap}:pyproject.toml"], root))
        base_thresholds = {
            str(t): int(v)
            for t, v in tomllib.loads(text)["tool"]["haiplane"]["file_size"].items()
            if t != NEAR_KEY
        }
        missing = sorted(RATCHET_TREES - set(base_thresholds))
        if missing:
            raise KeyError(f"нет порога {', '.join(missing)}")
    except _BASE_ERRORS as exc:
        raise CompareError(
            f"пороги базы {base} ({snap[:12]}) не прочитаны: {exc}"
        ) from exc
    return BaseView(
        ledger=ledger, lines=lines, headroom_pct=headroom, thresholds=base_thresholds
    )


def _unaccepted_entries(
    sizes: dict[str, int],
    thresholds: dict[str, int],
    before: dict[str, dict],
    base: BaseView | None,
    forced: bool,
) -> list[str]:
    """Записи tests, что --update добавит и что CI на базе не примет.

    Читает базу целиком ДО записи ledger: сбой чтения базы здесь - rc 2 без
    изменённого файла.
    """
    paths = sorted(
        p
        for p, n in sizes.items()
        if _tree(p) in RATCHET_TREES and n > _threshold(p, thresholds)
    )
    if base is None:
        return [p for p in paths if p not in before]
    if forced:
        return [p for p in paths if p not in base.ledger]
    # База правила не знала: CI примет только файлы, длинные уже на ней.
    return [
        p
        for p in paths
        if p not in base.ledger and (base.lines(p) or 0) <= _threshold(p, thresholds)
    ]


def _update(
    sizes: dict[str, int],
    thresholds: dict[str, int],
    ledger_path: Path,
    base: BaseView | None,
) -> int:
    """--update: перезапись ledger; tests не поднимаются выше ledger базы."""
    forced = base is not None and base.ratchet_in_force("tests", thresholds["tests"])
    clamp = None if base is None else (base.ledger if forced else {})
    before = load_ledger(ledger_path)
    added = _unaccepted_entries(sizes, thresholds, before, base, forced)  # до записи
    count = write_ledger(sizes, thresholds, ledger_path, clamp)
    print(f"ledger перезаписан: {count} исключений")
    for path in added[:ADDED_SHOWN]:
        print(
            f"  внимание: новая запись {path}; CI отвергнет её, если файл не был "
            "выше порога на базе - разбейте файл"
        )
    if len(added) > ADDED_SHOWN:
        print(f"  и ещё {len(added) - ADDED_SHOWN} новых записей tests")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    parser.add_argument(
        "--update", action="store_true", help="перезаписать ledger по факту"
    )
    parser.add_argument("--root", default=str(REPO_ROOT), help="корень репозитория")
    parser.add_argument(
        "--base",
        default="",
        help="база сравнения для ledger tests (иначе из окружения CI, иначе origin/develop)",
    )
    parser.add_argument(
        "--no-base", action="store_true", help="не сравнивать ledger tests с базой"
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    thresholds = load_thresholds(root)
    near_pct = load_near_pct(root)  # до любого режима, в том числе --update
    sizes = measure(root, list(thresholds))
    ledger_path = root / LEDGER_REL

    base = None
    base_error = ""
    if not args.no_base:
        name = args.base or "из окружения CI"
        try:
            name, exact = (
                (args.base, False) if args.base else base_from_environment(os.environ)
            )
            base = load_base(root, name, exact)
        except CompareError as exc:
            base_error = f"file size: сравнить с базой {name} не удалось: {exc}"
    if base_error:  # до любой записи ledger, и для проверки, и для --update
        print(base_error, file=sys.stderr)
        return BASE_UNREADABLE
    try:
        if args.update:
            return _update(sizes, thresholds, ledger_path, base)
        report = check(sizes, thresholds, load_ledger(ledger_path), near_pct, base)
    except CompareError as exc:  # ленивое чтение базы упало уже внутри проверки
        print(f"file size: сравнить с базой не удалось: {exc}", file=sys.stderr)
        return BASE_UNREADABLE
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
