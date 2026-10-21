#!/usr/bin/env python3
"""CI check: no new test asserts a wall-clock time under one second (#1989).

Тест, который сравнивает настоящие часы с порогом меньше секунды, падает на
загруженной машине без дефекта в коде: в #1984 `test_egress_watch` дал 0.317
при пороге 0.3. Такие места старого кода записаны в ledger
docs/agent-context/wallclock-asserts.json с владельцем (`removed_by`); новое
место вне ledger красит CI.

    uv run python scripts/wallclock_asserts.py            # проверка
    uv run python scripts/wallclock_asserts.py --update   # перезаписать ledger

Грамматика (это контракт задачи, шире и уже она не читается):

  * часы — вызовы time.monotonic / time.perf_counter / time.time через
    `import time`, `import time as X`, `from time import f [as g]`; имя,
    которому присвоена ссылка на такие часы (`m = time.monotonic`), тоже часы;
  * поток — только простые присваивания имени в теле ТОЙ ЖЕ функции (включая
    max/min и арифметику): имя выведено из часов, если выводит хоть одно
    присваивание;
  * нарушение — только ВЕРХНЯЯ граница: `x < c`, `x <= c`, `c > x`, `c >= x`
    и верхнее звено цепочки, где x выведено из часов, а c — константное
    выражение меньше 1.0 (`0.02 + 0.1` вычисляется);
  * fake-часы исключаются, только если в той же функции до первого чтения часов
    стоит monkeypatch.setattr / mock.patch ИМЕННО того объекта, через который
    читаются часы; ссылка, сохранённая до patch, не исключается;
  * прочее (часы через атрибут чужого объекта, часы, сохранённые не в имя,
    порог не вычислен) — предупреждение, rc не меняет.

Ledger: отпечаток места = файл + Класс.функция + ast.unparse сравнения +
кратность; переформатирование исходника отпечаток не меняет.
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = REPO_ROOT / "docs" / "agent-context" / "wallclock-asserts.json"
TARGET = "tests"
CLOCKS = frozenset({"monotonic", "perf_counter", "time"})
THRESHOLD = 1.0
REMOVED_BY_HINT = "назначьте task:#N, permanent: <причина> или unclaimed: <причина>"

_TASK_RE = re.compile(r"^task:#\d+(\s.*)?$")
_REASON_RE = re.compile(r"^(permanent|unclaimed)\s*[:—]\s*(\S.*)$", re.DOTALL)
_BOUND_OPS = (ast.Lt, ast.LtE)
_BOUND_OPS_REVERSED = (ast.Gt, ast.GtE)


@dataclass(frozen=True)
class Place:
    """Одно найденное место: сравнение с верхним порогом, выведенное из часов."""

    path: str
    function: str
    text: str
    line: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.function, self.text)


@dataclass(frozen=True)
class Warn:
    path: str
    line: int
    function: str
    message: str


@dataclass
class Scan:
    places: list[Place] = field(default_factory=list)
    warnings: list[Warn] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


# --- константы -------------------------------------------------------------


def _const(node: ast.AST) -> float | None:
    """Значение константного числового выражения или ``None``."""
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _const(node.operand)
        if inner is None:
            return None
        return -inner if isinstance(node.op, ast.USub) else inner
    if isinstance(node, ast.BinOp):
        left, right = _const(node.left), _const(node.right)
        if left is None or right is None:
            return None
        try:
            return _binop(node.op, left, right)
        except (ArithmeticError, ValueError):
            return None
    return None


def _binop(op: ast.operator, left: float, right: float) -> float | None:
    if isinstance(op, ast.Add):
        return left + right
    if isinstance(op, ast.Sub):
        return left - right
    if isinstance(op, ast.Mult):
        return left * right
    if isinstance(op, ast.Div):
        return left / right
    if isinstance(op, ast.FloorDiv):
        return float(left // right)
    if isinstance(op, ast.Mod):
        return left % right
    if isinstance(op, ast.Pow):
        # Ограничение степени: 9 ** 9 ** 9 не должно вешать проверку.
        if abs(right) > 64 or abs(left) > 1e6:
            return None
        result = left**right
        return result if isinstance(result, float) else None
    return None


# --- часы в модуле ---------------------------------------------------------


@dataclass
class Imports:
    """Как модуль называет time и его часы."""

    modules: set[str] = field(default_factory=set)  # import time [as X]
    funcs: dict[str, str] = field(default_factory=dict)  # alias -> имя часов


def _collect_imports(tree: ast.AST) -> Imports:
    found = Imports()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "time":
                    found.modules.add(alias.asname or "time")
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name in CLOCKS:
                    found.funcs[alias.asname or alias.name] = alias.name
    return found


def _module_clock(node: ast.AST, imp: Imports) -> str | None:
    """Имя часов, если ``node`` - ссылка ``time.monotonic`` / ``monotonic``."""
    if (
        isinstance(node, ast.Attribute)
        and node.attr in CLOCKS
        and isinstance(node.value, ast.Name)
        and node.value.id in imp.modules
    ):
        return node.attr
    if isinstance(node, ast.Name) and node.id in imp.funcs:
        return imp.funcs[node.id]
    return None


# --- одна функция ----------------------------------------------------------


@dataclass(frozen=True)
class Patch:
    clock: str
    start: int
    end: int  # последняя строка действия patch


def _own_nodes(func: ast.AST):
    """Узлы тела функции без вложенных функций и классов (у них свой разбор)."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _patched_clock(call: ast.Call, imp: Imports) -> str | None:
    """Какие часы time подменяет вызов monkeypatch.setattr / mock.patch."""
    func = call.func
    target_name = (
        func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    )
    args = call.args
    if target_name == "setattr" and isinstance(func, ast.Attribute):
        return _patched_by_args(args, imp)
    if target_name == "patch" and args:
        return _patched_by_string(args[0])
    if (
        target_name == "object"
        and isinstance(func, ast.Attribute)
        and _is_patch(func.value)
    ):
        return _patched_by_args(args, imp)
    return None


def _is_patch(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "patch"
    return isinstance(node, ast.Name) and node.id == "patch"


def _patched_by_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        module, _, name = node.value.partition(".")
        if module == "time" and name in CLOCKS:
            return name
    return None


def _patched_by_args(args: list[ast.expr], imp: Imports) -> str | None:
    if args and isinstance(args[0], ast.Constant):
        return _patched_by_string(args[0])
    if (
        len(args) >= 2
        and isinstance(args[0], ast.Name)
        and args[0].id in imp.modules
        and isinstance(args[1], ast.Constant)
        and args[1].value in CLOCKS
    ):
        return str(args[1].value)
    return None


def _patches(func: ast.AST, imp: Imports, end_line: int) -> list[Patch]:
    """Подмены часов в функции, которые ДЕЙСТВУЮТ, с областью действия.

    Действует: monkeypatch.setattr с момента вызова; ``with patch(...)`` в теле
    with; декоратор @patch на всю функцию; ``p = patch(...)`` / ``patch(...)``
    только вместе с ``.start()``. Голый вызов ``patch(...)`` ничего не
    подменяет.
    """
    in_with: dict[int, int] = {}
    decorators = {
        id(d) for d in getattr(func, "decorator_list", []) if isinstance(d, ast.Call)
    }
    nodes = list(_own_nodes(func))
    for node in nodes:
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                in_with[id(item.context_expr)] = node.end_lineno or end_line
    pending: dict[str, str] = {}  # имя -> часы из ``p = patch(...)``
    for node in nodes:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and (clock := _patched_clock(node.value, imp))
            and not _is_setattr(node.value)
        ):
            pending[node.targets[0].id] = clock
    found: list[Patch] = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        clock = _patched_clock(node, imp)
        if clock is not None:
            if _is_setattr(node):
                found.append(Patch(clock, node.lineno, end_line))
            elif id(node) in in_with:
                found.append(Patch(clock, node.lineno, in_with[id(node)]))
            elif id(node) in decorators:
                found.append(Patch(clock, getattr(func, "lineno", 0), end_line))
            continue
        started = _started_clock(node, imp, pending)
        if started is not None:
            found.append(Patch(started, node.lineno, end_line))
    return found


def _is_setattr(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr == "setattr"


def _started_clock(call: ast.Call, imp: Imports, pending: dict[str, str]) -> str | None:
    """Часы, подмену которых включает ``p.start()`` / ``patch(...).start()``."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "start"):
        return None
    if isinstance(func.value, ast.Name):
        return pending.get(func.value.id)
    if isinstance(func.value, ast.Call):
        return _patched_clock(func.value, imp)
    return None


class _Flow:
    """Какие имена функции выведены из часов (строго / возможно)."""

    def __init__(self, func: ast.AST, imp: Imports) -> None:
        self.imp = imp
        end = getattr(func, "end_lineno", None) or 10**9
        self.patches = _patches(func, imp, end)
        # имя -> (часы, строка присваивания, взяты ли с модуля time)
        self.aliases: dict[str, tuple[str, int, bool]] = {}
        self.warnings: list[tuple[int, str]] = []
        self._own = list(_own_nodes(func))
        self._collect_aliases()
        self.first_read = self._first_read()
        self.sure: set[str] = set()
        self.maybe: set[str] = set()
        self._propagate()

    # ссылки на часы вида ``m = time.monotonic``
    def _collect_aliases(self) -> None:
        for node in self._own:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                clock = _module_clock(node.value, self.imp)
                if clock:
                    on_module = isinstance(node.value, ast.Attribute)
                    self.aliases.setdefault(
                        node.targets[0].id, (clock, node.lineno, on_module)
                    )

    def _read_kind(self, call: ast.Call) -> str | None:
        """``"module"`` - чтение через time.X / from-импорт; ``"alias"`` - через
        сохранённую ссылку; ``"object"`` - часы чужого объекта."""
        func = call.func
        if _module_clock(func, self.imp):
            return "module"
        if isinstance(func, ast.Name) and func.id in self.aliases:
            return "alias"
        if (
            isinstance(func, ast.Attribute)
            and func.attr in CLOCKS
            and not (
                isinstance(func.value, ast.Name) and func.value.id in self.imp.modules
            )
        ):
            return "object"
        return None

    def _first_read(self) -> int:
        lines = [
            n.lineno
            for n in self._own
            if isinstance(n, ast.Call) and self._read_kind(n) in ("module", "alias")
        ]
        return min(lines, default=10**9)

    def _is_faked(self, call: ast.Call) -> bool:
        func = call.func
        # Ссылка, сохранённая ПОСЛЕ подмены, указывает на fake; сохранённая до
        # неё - на настоящие часы.
        if isinstance(func, ast.Name) and func.id in self.aliases:
            clock, saved_at, on_module = self.aliases[func.id]
            return on_module and any(
                p.clock == clock
                and p.start < self.first_read
                and p.start < saved_at <= p.end
                for p in self.patches
            )
        # Только чтение через сам объект time: from-импорт не затронут
        # подменой атрибута модуля.
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in self.imp.modules
        ):
            return False
        return any(
            p.clock == func.attr
            and p.start < self.first_read
            and p.start <= call.lineno <= p.end
            for p in self.patches
        )

    def level(self, expr: ast.AST) -> str | None:
        """``"sure"`` | ``"maybe"`` | ``None`` для выражения."""
        result: str | None = None
        for node in ast.walk(expr):
            if isinstance(node, ast.Call):
                kind = self._read_kind(node)
                if kind in ("module", "alias") and not self._is_faked(node):
                    return "sure"
                if kind == "object":
                    result = "maybe"
            elif isinstance(node, ast.Name):
                if node.id in self.sure:
                    return "sure"
                if node.id in self.maybe:
                    result = "maybe"
        return result

    def _assignments(self):
        for node in self._own:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    yield target, node.value, node
            elif isinstance(node, ast.AugAssign):
                yield node.target, node.value, node
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                yield node.target, node.value, node

    def _propagate(self) -> None:
        pairs = list(self._assignments())
        changed = True
        while changed:
            changed = False
            for target, value, _ in pairs:
                if not isinstance(target, ast.Name):
                    continue
                level = self.level(value)
                if level == "sure" and target.id not in self.sure:
                    self.sure.add(target.id)
                    self.maybe.discard(target.id)
                    changed = True
                elif (
                    level == "maybe"
                    and target.id not in self.sure
                    and target.id not in self.maybe
                ):
                    self.maybe.add(target.id)
                    changed = True
        for target, value, node in pairs:
            if not isinstance(target, ast.Name) and self.level(value) == "sure":
                self.warnings.append(
                    (node.lineno, "часы сохранены не в простое имя: поток не прослежен")
                )


# --- сравнения -------------------------------------------------------------


def _upper_bound_pairs(compare: ast.Compare):
    """Пары (измеряемое, порог) по верхним границам звеньев цепочки."""
    operands = [compare.left, *compare.comparators]
    for i, op in enumerate(compare.ops):
        left, right = operands[i], operands[i + 1]
        if isinstance(op, _BOUND_OPS):
            yield left, right
        elif isinstance(op, _BOUND_OPS_REVERSED):
            yield right, left


def _params(func: ast.AST) -> set[str]:
    args = func.args  # type: ignore[attr-defined]
    names = [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
    for extra in (args.vararg, args.kwarg):
        if extra:
            names.append(extra.arg)
    return set(names)


def _scan_function(
    func: ast.AST, qualname: str, path: str, imp: Imports, scan: Scan
) -> None:
    flow = _Flow(func, imp)
    params = _params(func)
    for line, message in flow.warnings:
        scan.warnings.append(Warn(path, line, qualname, message))
    for node in _own_nodes(func):
        if not isinstance(node, ast.Assert):
            continue
        for compare in ast.walk(node.test):
            if isinstance(compare, ast.Compare):
                _scan_compare(compare, node, flow, params, (path, qualname), scan)


def _scan_compare(
    compare: ast.Compare,
    stmt: ast.Assert,
    flow: _Flow,
    params: set[str],
    where: tuple[str, str],
    scan: Scan,
) -> None:
    path, qualname = where
    for subject, bound in _upper_bound_pairs(compare):
        level = flow.level(subject)
        if level is None:
            continue
        value = _const(bound)
        if value is None:
            names = {n.id for n in ast.walk(bound) if isinstance(n, ast.Name)}
            if names and names <= params:
                continue  # порог пришёл параметром - вне области
            scan.warnings.append(
                Warn(path, stmt.lineno, qualname, "порог не вычислен: не константа")
            )
        elif value >= THRESHOLD:
            continue
        elif level == "sure":
            text = _fingerprint(compare)
            scan.places.append(Place(path, qualname, text, stmt.lineno))
            break
        else:
            scan.warnings.append(
                Warn(
                    path,
                    stmt.lineno,
                    qualname,
                    "часы через атрибут объекта: не прослежены",
                )
            )


class _FStrings(ast.NodeTransformer):
    """f-строки -> имя с ast.dump: unparse f-строк различается между версиями."""

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        return ast.Name(id="f<" + ast.dump(node) + ">", ctx=ast.Load())


def _fingerprint(compare: ast.Compare) -> str:
    """Текст сравнения для отпечатка, одинаковый на Python 3.11 и 3.12+."""
    clone = _FStrings().visit(copy.deepcopy(compare))
    return ast.unparse(clone)


def _walk_scopes(node: ast.AST, prefix: str = ""):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = f"{prefix}{child.name}"
            yield child, name
            yield from _walk_scopes(child, f"{name}.")
        elif isinstance(child, ast.ClassDef):
            yield from _walk_scopes(child, f"{prefix}{child.name}.")
        else:
            yield from _walk_scopes(child, prefix)


def scan_source(source: str, path: str, scan: Scan | None = None) -> Scan:
    """Разобрать один исходник; ``path`` попадает в отпечаток и в сообщения."""
    scan = scan if scan is not None else Scan()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        scan.unparsed.append(path)
        return scan
    imp = _collect_imports(tree)
    for func, qualname in _walk_scopes(tree):
        _scan_function(func, qualname, path, imp, scan)
    return scan


def scan_tree(root: Path, target: str) -> Scan:
    scan = Scan()
    base = root / target
    files = [base] if base.is_file() else sorted(base.rglob("*.py"))
    for file in files:
        rel = file.relative_to(root).as_posix()
        scan_source(file.read_text(encoding="utf-8"), rel, scan)
    scan.places.sort(key=lambda p: (p.path, p.line))
    return scan


# --- ledger ----------------------------------------------------------------


def validate_removed_by(text: object) -> str | None:
    """Текст ошибки формата ``removed_by`` или ``None``, если формат верен."""
    raw = str(text or "").strip()
    if not raw:
        return "пусто"
    if _TASK_RE.match(raw) or _REASON_RE.match(raw):
        return None
    if raw.startswith("task:#"):
        return "task без номера"
    if raw.split(":", 1)[0].strip() in ("permanent", "unclaimed"):
        return "причина обязательна"
    return f"неизвестный формат {raw!r}"


def _entry_key(entry: dict) -> tuple[str, str, str]:
    return (str(entry["path"]), str(entry["function"]), str(entry["assert"]))


def load_ledger(path: Path) -> tuple[list[dict], list]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return list(raw.get("entries", [])), list(raw.get("_comment", []))


@dataclass(frozen=True)
class Report:
    new: tuple[tuple[Place, int, int], ...]  # место, найдено, в ledger
    stale: tuple[tuple[tuple[str, str, str], int, int], ...]  # ключ, найдено, в ledger
    invalid: tuple[tuple[str, str], ...]
    duplicates: tuple[tuple[str, str, str], ...]
    by_reason: dict[str, int]
    total: int

    @property
    def ok(self) -> bool:
        return not (self.new or self.stale or self.invalid or self.duplicates)


def _label(key: tuple[str, str, str]) -> str:
    return f"{key[0]}::{key[1]}: {key[2]}"


def check(places: list[Place], entries: list[dict]) -> Report:
    """Свести найденные места с ledger. Чистая функция."""
    found = Counter(p.key for p in places)
    first_place: dict[tuple[str, str, str], Place] = {}
    for p in places:
        first_place.setdefault(p.key, p)
    seen: Counter[tuple[str, str, str]] = Counter()
    invalid: list[tuple[str, str]] = []
    allowed: dict[tuple[str, str, str], int] = {}
    by_reason: dict[str, int] = {}
    for entry in entries:
        try:
            key = _entry_key(entry)
        except KeyError as exc:
            invalid.append((str(entry), f"нет поля {exc}"))
            continue
        seen[key] += 1
        count = entry.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            invalid.append((_label(key), f"count должен быть целым >= 1: {count!r}"))
            continue
        allowed.setdefault(key, count)
        problem = validate_removed_by(entry.get("removed_by"))
        if problem:
            invalid.append((_label(key), problem))
        else:
            kind = str(entry["removed_by"]).strip().split(":", 1)[0]
            kind = "task" if kind.startswith("task") else kind.split("—")[0].strip()
            by_reason[kind] = by_reason.get(kind, 0) + 1
    duplicates = tuple(sorted(k for k, n in seen.items() if n > 1))
    new = tuple(
        (first_place[k], n, allowed.get(k, 0))
        for k, n in sorted(found.items())
        if n > allowed.get(k, 0)
    )
    stale = tuple(
        (k, found.get(k, 0), n)
        for k, n in sorted(allowed.items())
        if found.get(k, 0) < n
    )
    return Report(
        new, stale, tuple(invalid), duplicates, by_reason, sum(found.values())
    )


def format_report(report: Report, scan: Scan) -> str:
    lines: list[str] = []
    if report.new:
        lines.append(
            f"НОВОЕ МЕСТО С ПОРОГОМ НАСТЕННОГО ВРЕМЕНИ < 1 с ({len(report.new)}):"
        )
        for place, n, allowed in report.new:
            extra = f" (кратность {n} при {allowed} в ledger)" if allowed else ""
            lines.append(
                f"  {place.path}:{place.line} {place.function}: "
                f"assert {place.text}{extra}"
            )
        lines.append(
            "  Настоящие часы на загруженном CI дают больше порога без дефекта: "
            "подмените часы (monkeypatch) или проверьте счётчик, а не секунды."
        )
    if report.stale:
        lines.append(f"ЗАПИСЬ LEDGER ПЕРЕЖИЛА СВОЁ МЕСТО ({len(report.stale)}):")
        for key, n, allowed in report.stale:
            lines.append(
                f"  {_label(key)} - найдено {n}, в ledger {allowed}; "
                "удалите запись или уменьшите count"
            )
    if report.duplicates:
        lines.append(f"ДУБЛЬ ЗАПИСИ LEDGER ({len(report.duplicates)}):")
        lines.extend(f"  {_label(key)}" for key in report.duplicates)
    if report.invalid:
        lines.append(f"REMOVED_BY ИЛИ ЗАПИСЬ НЕ ПО ФОРМАТУ ({len(report.invalid)}):")
        lines.extend(
            f"  {key} - {problem}; {REMOVED_BY_HINT}" for key, problem in report.invalid
        )
    for warn in scan.warnings:
        lines.append(
            f"ПРЕДУПРЕЖДЕНИЕ {warn.path}:{warn.line} {warn.function}: {warn.message}"
        )
    for path in scan.unparsed:
        lines.append(f"ПРЕДУПРЕЖДЕНИЕ {path}: файл не разобран, не проверен")
    lines.append(f"Мест в силе: {report.total}")
    for kind, count in sorted(report.by_reason.items()):
        lines.append(f"  {kind}: {count}")
    return "\n".join(lines)


def build_entries(places: list[Place], existing: list[dict]) -> list[dict]:
    """Записи ledger по факту; removed_by существующих сохраняется."""
    prior: dict[tuple[str, str, str], str] = {}
    for entry in existing:
        try:
            prior.setdefault(_entry_key(entry), str(entry.get("removed_by", "")))
        except KeyError:
            continue
    counts = Counter(p.key for p in places)
    return [
        {
            "path": path,
            "function": function,
            "assert": text,
            "count": n,
            "removed_by": prior.get((path, function, text), ""),
        }
        for (path, function, text), n in sorted(counts.items())
    ]


def write_ledger(path: Path, entries: list[dict], comment: list) -> None:
    path.write_text(
        json.dumps(
            {"_comment": comment, "entries": entries}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="перезаписать ledger по факту; removed_by существующих сохраняется",
    )
    parser.add_argument("--target", default=TARGET, help="каталог или файл от --root")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    args = parser.parse_args(argv)

    scan = scan_tree(args.root, args.target)
    if args.update:
        entries, comment = (
            load_ledger(args.ledger) if args.ledger.exists() else ([], [])
        )
        merged = build_entries(scan.places, entries)
        write_ledger(args.ledger, merged, comment)
        empty = sum(1 for e in merged if not e["removed_by"])
        print(f"ledger перезаписан: {len(merged)} записей, без removed_by: {empty}")
        return 0

    entries, _ = load_ledger(args.ledger)
    report = check(scan.places, entries)
    print(format_report(report, scan))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
