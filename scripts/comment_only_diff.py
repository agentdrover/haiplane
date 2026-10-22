#!/usr/bin/env python3
"""Проверка «изменены только комментарии и неконтрактные docstring-и» (#2032).

Серия чистки истории в комментариях правит сотни строк в файлах ядра. Ревью не
отличает смену текста от смены кода, а потеря ``# type: ignore`` или правка
контрактного docstring ломает инструменты и публичные поверхности. Скрипт для
двух ревизий (SHA) и списка .py-файлов проверяет ОГРАНИЧЕННЫЙ класс правок.
Это не доказательство отсутствия изменения поведения и не смысловое ревью.

    uv run python scripts/comment_only_diff.py --base SHA --head SHA FILE...
    uv run python scripts/comment_only_diff.py --base SHA --head SHA \\
        --allow-docstring hub/services/lifecycle.py::record_review_verdict FILE...

Что проверяется (каждое нарушение — строка ``файл:строка: [вид] причина``):
  * [tokens] последовательность (тип токена, точное написание) без координат
    совпала после исключения COMMENT/NL и docstring-узлов. Docstring — только
    ПЕРВЫЙ ``Expr(Constant(str))`` модуля, класса, функции; f-строки, bytes,
    SQL, промпты и все прочие строки — защищённый код. NEWLINE/INDENT/DEDENT
    сохраняются; скобочный и склеенный docstring исключается целиком;
  * [ast] нормализованный AST (``type_comments=True``, без docstring-ов) равен;
  * [directive] директивы (``type:``, ``noqa``, ``pragma:`` в т. ч. allowlist
    secret, ``fmt:``, ``nosec``, ``pylint:``/``mypy:``/…, shebang, coding)
    сохранены текстом И привязкой: тот же токен-предшественник и та же форма
    (в строке оператора или отдельной строкой). Перенос на соседний оператор
    или соседнюю строку оператора — нарушение;
  * [contract_docstring] изменён docstring символа, который читают снаружи:
    ``@mcp.tool``/``prompt``/``resource``, маршруты FastAPI без явного
    ``description`` и не ``include_in_schema=False``, команды click/typer,
    наследники Pydantic/Enum/TypedDict (в том числе через другие файлы),
    модуль с ``__doc__`` в коде (argparse ``description=__doc__``), символы,
    чей docstring читают тесты (``inspect.getdoc(x)``, ``x.__doc__``), и
    регистрации ``add_tool``/``add_api_route``. Признаки — не ручной список.
    Допустимое сокращение называется явно: ``--allow-docstring путь::имя``
    (``<module>`` — docstring модуля);
  * [ref_lost] ссылка ``#NNNN`` из комментария или docstring базы не осталась
    ни в файле, ни в ``docs/agent-context/invariants.md`` ревизии head
    (вспомогательная проверка: сохранность номера не равна сохранности причины);
  * [unlisted_file] вне списка изменён другой .py (список не прячет правки);
  * [file_changed] файл из списка добавлен или удалён.

Коды возврата: 0 — нарушений нет; 1 — есть нарушения; 2 — проверить не удалось
(ревизия не читается, синтаксическая ошибка, не .py, не UTF-8): «чисто» не
выводится никогда.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import difflib
import io
import json
import posixpath
import re
import subprocess
import sys
import tokenize
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

INVARIANTS_MD = "docs/agent-context/invariants.md"
MODULE = "<module>"
# Предел точной разметки расхождений: больше — одно расхождение «от и до».
_DIFF_CELLS = 4_000_000
DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# Токены, которые правка комментариев законно меняет, в сравнение не входят.
_SKIPPED = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.ENCODING,
    tokenize.ENDMARKER,
}
# Токены разметки блоков не «стоят в строке» оператора.
_LAYOUT = _SKIPPED | {tokenize.INDENT, tokenize.DEDENT}

_REF_RE = re.compile(r"(?<![\w&])#(\d{2,6})\b")
_CODING_RE = re.compile(r"coding[:=][ \t]*([-\w.]+)")
_DIRECTIVES = (
    re.compile(r"^(noqa)\b(?:\s*:\s*((?:[A-Za-z]+\d+[\s,]*)+))?", re.I),
    re.compile(r"^(type:\s*ignore)\s*(\[[^\]]*\])?"),
    re.compile(r"^(type:)\s*(.+)$"),
    re.compile(r"^(pragma:)\s*(.+)$", re.I),
    re.compile(r"^(fmt:)\s*(\S+)"),
    re.compile(r"^(nosec)\b(?:\s+([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*))?", re.I),
    re.compile(
        r"^(pylint|mypy|isort|ruff|flake8|yapf|pyright|pytype|nosemgrep|cython"
        r"|pyre-ignore|pyre-fixme)\b(.*)$",
        re.I,
    ),
)

# Признаки «docstring читают снаружи» (см. модульный docstring).
_TOOL_DECORATORS = {"tool", "prompt", "resource"}
_ROUTE_DECORATORS = {
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "head",
    "options",
    "trace",
    "api_route",
    "websocket",
    "route",
}
_CLI_DECORATORS = {"command", "group", "callback"}
_REGISTRARS = {
    "add_tool",
    "add_api_route",
    "add_route",
    "add_api_websocket_route",
    "register_tool",
}
_SCHEMA_BASES = {
    "BaseModel",
    "BaseSettings",
    "RootModel",
    "Enum",
    "StrEnum",
    "IntEnum",
    "Flag",
    "IntFlag",
    "ReprEnum",
    "TypedDict",
}


class CheckError(Exception):
    """Проверить не удалось: исход «не смог», а не «нарушений нет»."""


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    kind: str
    message: str

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: [{self.kind}] {self.message}"


@dataclass(frozen=True)
class Tok:
    type: int
    string: str
    row: int

    @property
    def key(self) -> tuple[int, str]:
        return (self.type, self.string)


@dataclass
class Directive:
    anchor: int
    inline: bool
    text: str
    row: int


@dataclass
class DocSlot:
    qualname: str
    expr: ast.Expr
    text: str


@dataclass
class Parsed:
    src: str
    tree: ast.Module
    tokens: list[Tok]
    directives: list[Directive]
    docs: dict[str, DocSlot]
    refs: dict[str, int]  # номер -> первая строка в комментарии или docstring
    contracts: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- git


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _run(self, *args: str, data: bytes | None = None) -> bytes:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.root,
                input=data,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise CheckError(f"git не запускается: {exc}") from exc
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip()
            raise CheckError(f"git {' '.join(args[:2])} rc={proc.returncode}: {tail}")
        return proc.stdout

    def resolve(self, rev: str) -> str:
        try:
            out = self._run("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        except CheckError as exc:
            raise CheckError(f"ревизия {rev!r} не читается ({exc})") from exc
        return out.decode().strip()

    def changed_py(self, base: str, head: str) -> list[str]:
        out = self._run("diff", "--name-only", "--no-renames", "-z", base, head)
        names = out.decode("utf-8", "surrogateescape").split("\0")
        return sorted(n for n in names if n.endswith(".py"))

    def read_many(self, sha: str, paths: Sequence[str]) -> dict[str, bytes | None]:
        """Содержимое файлов ревизии одним ``cat-file --batch``; нет файла — None."""
        if not paths:
            return {}
        query = "".join(f"{sha}:{p}\n" for p in paths).encode()
        out = self._run("cat-file", "--batch", data=query)
        result: dict[str, bytes | None] = {}
        pos = 0
        for path in paths:
            end = out.index(b"\n", pos)
            header = out[pos:end].split()
            pos = end + 1
            if len(header) == 3 and header[1] == b"blob":
                size = int(header[2])
                result[path] = out[pos : pos + size]
                pos += size + 1
            else:
                result[path] = None
        return result

    def list_py(self, sha: str) -> list[str]:
        out = self._run("ls-tree", "-r", "--name-only", "-z", sha)
        names = out.decode("utf-8", "surrogateescape").split("\0")
        return [n for n in names if n.endswith(".py")]


# ------------------------------------------------------------------ разбор файла


def _decode(data: bytes, path: str) -> str:
    try:
        src = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckError(f"{path}: файл не в UTF-8 ({exc})") from exc
    if re.search(r"\r(?!\n)", src):
        raise CheckError(f"{path}: одиночный CR в переводах строк не поддержан")
    return src


def _tokenize(src: str, path: str) -> list[tokenize.TokenInfo]:
    try:
        return list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError) as exc:
        raise CheckError(f"{path}: синтаксис не разобран tokenize ({exc})") from exc


def _parse_tree(src: str, path: str) -> ast.Module:
    try:
        compile(src, path, "exec")
        return ast.parse(src, path, type_comments=True)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise CheckError(f"{path}: синтаксическая ошибка ({exc})") from exc


def _docstring_expr(node: ast.AST) -> ast.Expr | None:
    """Первый ``Expr(Constant(str))`` тела: f-строка и bytes — не docstring."""
    body = getattr(node, "body", None)
    if not isinstance(body, list) or not body:
        return None
    first = body[0]
    if not isinstance(first, ast.Expr):
        return None
    value = first.value
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return first
    return None


def _collect_docs(tree: ast.Module) -> dict[str, DocSlot]:
    docs: dict[str, DocSlot] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        expr = _docstring_expr(node)
        if expr is not None and isinstance(expr.value, ast.Constant):
            name = prefix or MODULE
            docs[name] = DocSlot(name, expr, str(expr.value.value))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEF_NODES):
                visit(child, f"{prefix}.{child.name}" if prefix else child.name)
            else:
                visit(child, prefix)

    visit(tree, "")
    return docs


def _byte_col(lines: list[str], row: int, col: int) -> int:
    """Колонка tokenize (символы) -> колонка AST (байты UTF-8)."""
    if row - 1 >= len(lines):
        return col
    return len(lines[row - 1][:col].encode("utf-8"))


def _removed_indexes(
    toks: list[tokenize.TokenInfo], docs: Iterable[DocSlot], lines: list[str]
) -> set[int]:
    """Индексы токенов docstring-операторов вместе с их разделителем."""
    spans = [
        (_byte_pos(lines, t.start), _byte_pos(lines, t.end)) for t in toks
    ]  # токены идут по возрастанию начала: ищем окно бисекцией
    starts = [s for s, _ in spans]
    removed: set[int] = set()
    for doc in docs:
        expr = doc.expr
        lo = (expr.lineno, expr.col_offset)
        hi = (expr.end_lineno or expr.lineno, expr.end_col_offset or 0)
        last = -1
        i = bisect.bisect_left(starts, lo)
        while i < len(toks) and starts[i] < hi:
            if spans[i][1] <= hi and toks[i].type not in _SKIPPED:
                removed.add(i)
                last = i
            i += 1
        removed |= _separator_after(toks, last, removed)
    return removed


def _byte_pos(lines: list[str], pos: tuple[int, int]) -> tuple[int, int]:
    return (pos[0], _byte_col(lines, pos[0], pos[1]))


def _separator_after(
    toks: list[tokenize.TokenInfo], last: int, removed: set[int]
) -> set[int]:
    """NEWLINE или ``;`` сразу за исключённым оператором (мимо COMMENT/NL)."""
    if last < 0:
        return set()
    for j in range(last + 1, len(toks)):
        tok = toks[j]
        if tok.type in (tokenize.COMMENT, tokenize.NL):
            continue
        if tok.type == tokenize.NEWLINE or (
            tok.type == tokenize.OP and tok.string == ";"
        ):
            return {j}
        return set()
    return set()


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\s*,\s*", ",", text.strip()))


def _directives_of(comment: str, row: int) -> list[str]:
    """Тексты директив инструментов в одном комментарии (разделитель — ``#``)."""
    if row == 1 and comment.startswith("#!"):
        return [f"shebang {comment}"]
    found: list[str] = []
    if row <= 2:
        cookie = _CODING_RE.search(comment)
        if cookie and re.match(r"^#.*coding[:=]", comment):
            found.append(f"coding {cookie.group(1).lower()}")
    for segment in comment.split("#")[1:]:
        seg = segment.strip()
        for pattern in _DIRECTIVES:
            m = pattern.match(seg)
            if m:
                parts = [p for p in m.groups() if p]
                found.append(_norm_text(" ".join(parts)).lower())
                break
    return found


def _scan_tokens(
    toks: list[tokenize.TokenInfo], removed: set[int]
) -> tuple[list[Tok], list[Directive]]:
    kept: list[Tok] = []
    directives: list[Directive] = []
    last_row = 0  # строка конца последнего «настоящего» токена
    for i, tok in enumerate(toks):
        if tok.type == tokenize.COMMENT:
            inline = last_row == tok.start[0]
            for text in _directives_of(tok.string, tok.start[0]):
                directives.append(Directive(len(kept), inline, text, tok.start[0]))
            continue
        if tok.type not in _LAYOUT:
            last_row = tok.end[0]
        if i in removed or tok.type in _SKIPPED:
            continue
        string = "\n" if tok.type == tokenize.NEWLINE else tok.string
        kept.append(Tok(tok.type, string, tok.start[0]))
    return kept, directives


def _refs(toks: list[tokenize.TokenInfo], docs: Iterable[DocSlot]) -> dict[str, int]:
    refs: dict[str, int] = {}
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            for m in _REF_RE.finditer(tok.string):
                refs.setdefault(m.group(1), tok.start[0])
    for doc in docs:
        for m in _REF_RE.finditer(doc.text):
            refs.setdefault(m.group(1), doc.expr.lineno)
    return refs


def parse_source(data: bytes, path: str) -> Parsed:
    src = _decode(data, path)
    tree = _parse_tree(src, path)
    toks = _tokenize(src, path)
    docs = _collect_docs(tree)
    removed = _removed_indexes(toks, docs.values(), src.split("\n"))
    kept, directives = _scan_tokens(toks, removed)
    return Parsed(src, tree, kept, directives, docs, _refs(toks, docs.values()))


# ------------------------------------------------------------- контрактные имена


def _chain(node: ast.AST) -> tuple[str, ...] | None:
    """``a.b.c`` -> (a, b, c); любое другое выражение — None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    return None


def _base_names(cls: ast.ClassDef) -> list[str]:
    names: list[str] = []
    for base in cls.bases:
        node = base.value if isinstance(base, ast.Subscript) else base
        chain = _chain(node)
        if chain:
            names.append(chain[-1])
    return names


def _call_name(call: ast.Call) -> str:
    chain = _chain(call.func)
    return chain[-1] if chain else ""


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _first_is_path(call: ast.Call) -> bool:
    if call.args:
        arg = call.args[0]
        return isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    return _kw(call, "path") is not None


def _decorator_reason(dec: ast.expr) -> str:
    call = dec if isinstance(dec, ast.Call) else None
    chain = _chain(call.func if call else dec)
    if not chain:
        return ""
    name = chain[-1]
    if call and _kw(call, "description") is not None:
        return ""  # описание задано явно: docstring наружу не уходит
    if name in _TOOL_DECORATORS:
        return f"@{'.'.join(chain)}: описание в tools/list"
    if name in _ROUTE_DECORATORS and call and _first_is_path(call):
        hidden = _kw(call, "include_in_schema")
        if isinstance(hidden, ast.Constant) and hidden.value is False:
            return ""
        return f"маршрут @{'.'.join(chain)}: описание в OpenAPI"
    if name in _CLI_DECORATORS and not (call and _kw(call, "help") is not None):
        return f"@{'.'.join(chain)}: справка CLI"
    return ""


@dataclass
class RepoIndex:
    """Что известно о репозитории целиком: базы классов и ссылки тестов на docstring."""

    class_bases: dict[str, set[str]] = field(default_factory=dict)
    doc_targets: set[tuple[str, ...]] = field(default_factory=set)

    def add(self, path: str, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self.class_bases.setdefault(node.name, set()).update(_base_names(node))
        if Path(path).name.startswith("test_") or path.startswith("tests/"):
            self.doc_targets |= _doc_targets(tree)

    def is_schema(self, name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in _SCHEMA_BASES:
            return True
        if name in seen:
            return False
        bases = self.class_bases.get(name, set())
        return any(self.is_schema(b, seen | {name}) for b in bases)


def _doc_targets(tree: ast.Module) -> set[tuple[str, ...]]:
    """Цепочки имён, чей docstring читает тест: ``x.__doc__``, ``getdoc(x)``."""
    found: set[tuple[str, ...]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "__doc__":
            chain = _chain(node.value)
            if chain:
                found.add(chain)
        elif isinstance(node, ast.Call) and _call_name(node) in (
            "getdoc",
            "get_docstring",
        ):
            chain = _chain(node.args[0]) if node.args else None
            if chain:
                found.add(chain)
    return found


def _matches_target(parts: list[str], chain: tuple[str, ...]) -> bool:
    if chain[-1] != parts[-1]:
        return False
    return len(chain) < 2 or len(parts) < 2 or chain[-2] == parts[-2]


def _in_file_references(tree: ast.Module) -> tuple[bool, set[str]]:
    """``__doc__`` в коде модуля и имена символов, переданных в регистраторы."""
    module_doc = False
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "__doc__":
            module_doc = True
        elif isinstance(node, ast.Attribute) and node.attr == "__doc__":
            chain = _chain(node.value)
            if chain:
                names.add(chain[-1])
        elif isinstance(node, ast.Call) and _call_name(node) in _REGISTRARS:
            for arg in node.args:
                chain = _chain(arg)
                if chain:
                    names.add(chain[-1])
    return module_doc, names


def find_contracts(tree: ast.Module, path: str, index: RepoIndex) -> dict[str, str]:
    """Символ файла -> причина, по которой его docstring читают снаружи."""
    out: dict[str, str] = {}
    module_doc, ref_names = _in_file_references(tree)
    if module_doc:
        out[MODULE] = "модуль использует __doc__ (argparse description=__doc__)"

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEF_NODES):
                qual = f"{prefix}.{child.name}" if prefix else child.name
                reason = _symbol_reason(child, qual, ref_names, index)
                if reason:
                    out[qual] = reason
                visit(child, qual)
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _symbol_reason(
    node: ast.AST, qual: str, ref_names: set[str], index: RepoIndex
) -> str:
    parts = qual.split(".")
    if isinstance(node, ast.ClassDef):
        for base in _base_names(node):
            if index.is_schema(base):
                return f"наследник {base}: описание уходит в схему"
    else:
        for dec in getattr(node, "decorator_list", []):
            reason = _decorator_reason(dec)
            if reason:
                return reason
    if parts[-1] in ref_names:
        return "docstring читается кодом (__doc__ / регистрация)"
    for chain in index.doc_targets:
        if _matches_target(parts, chain):
            return f"docstring читает тест ({'.'.join(chain)})"
    return ""


# ---------------------------------------------------------------------- сравнение


def _tok_text(tok: Tok | None) -> str:
    if tok is None:
        return "<нет>"
    text = tok.string if tok.type != tokenize.NEWLINE else "NEWLINE"
    return f"{tokenize.tok_name.get(tok.type, tok.type)}:{text[:40]!r}"


def _common_prefix(base: Parsed, head: Parsed) -> int:
    a, b = base.tokens, head.tokens
    n = 0
    while n < min(len(a), len(b)) and a[n].key == b[n].key:
        n += 1
    return n


def compare_tokens(path: str, base: Parsed, head: Parsed) -> list[Violation]:
    a = [t.key for t in base.tokens]
    b = [t.key for t in head.tokens]
    if a == b:
        return []
    lo = 0
    while lo < min(len(a), len(b)) and a[lo] == b[lo]:
        lo += 1
    hi = 0
    while hi < min(len(a), len(b)) - lo and a[-1 - hi] == b[-1 - hi]:
        hi += 1
    mid_a, mid_b = a[lo : len(a) - hi], b[lo : len(b) - hi]
    if len(mid_a) * len(mid_b) > _DIFF_CELLS:
        opcodes = [("replace", 0, len(mid_a), 0, len(mid_b))]  # слишком велико
    else:
        opcodes = difflib.SequenceMatcher(
            None, mid_a, mid_b, autojunk=False
        ).get_opcodes()
    out: list[Violation] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal" or len(out) >= 20:
            continue
        bt = base.tokens[lo + i1] if lo + i1 < len(base.tokens) else None
        ht = head.tokens[lo + j1] if lo + j1 < len(head.tokens) else None
        line = bt.row if bt else (base.tokens[-1].row if base.tokens else 1)
        out.append(
            Violation(
                path,
                line,
                "tokens",
                f"изменён код: base {_tok_text(bt)} (-{i2 - i1}) -> head "
                f"{_tok_text(ht)} (+{j2 - j1}, строка {ht.row if ht else '-'})",
            )
        )
    return out


def _strip_docs(tree: ast.Module) -> str:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, *DEF_NODES)) and _docstring_expr(node):
            node.body = node.body[1:]  # type: ignore[attr-defined]
    tree.type_ignores = []  # привязку директив держит отдельная проверка
    return ast.dump(tree)


def compare_ast(path: str, base: Parsed, head: Parsed) -> list[Violation]:
    if _strip_docs(base.tree) == _strip_docs(head.tree):
        return []
    return [
        Violation(path, 1, "ast", "нормализованный AST (type_comments) различается")
    ]


def _directives_before(parsed: Parsed, limit: int) -> Parsed:
    clone = Parsed(
        parsed.src,
        parsed.tree,
        parsed.tokens,
        [d for d in parsed.directives if d.anchor <= limit],
        parsed.docs,
        parsed.refs,
    )
    return clone


def compare_directives(
    path: str, base: Parsed, head: Parsed, limit: int | None = None
) -> list[Violation]:
    """``limit`` — число общих начальных токенов, если потоки уже разошлись."""
    if limit is not None:
        base = _directives_before(base, limit)
        head = _directives_before(head, limit)

    def key(d: Directive) -> tuple[int, bool, str]:
        return (d.anchor, d.inline, d.text)

    a = Counter(key(d) for d in base.directives)
    b = Counter(key(d) for d in head.directives)
    out: list[Violation] = []
    for d in base.directives:
        if a[key(d)] > b[key(d)]:
            a[key(d)] -= 1
            out.append(
                Violation(
                    path,
                    d.row,
                    "directive",
                    f"директива {d.text!r} потеряна, изменена или перенесена "
                    "на другой оператор",
                )
            )
    for d in head.directives:
        if b[key(d)] > a[key(d)]:
            b[key(d)] -= 1
            out.append(
                Violation(
                    path,
                    d.row,
                    "directive",
                    f"директива {d.text!r} появилась или перенесена (строка {d.row} head)",
                )
            )
    return out


def compare_contract_docs(
    path: str, base: Parsed, head: Parsed, allow: set[str]
) -> list[Violation]:
    out: list[Violation] = []
    for qual in sorted(set(base.docs) | set(head.docs)):
        reason = base.contracts.get(qual) or head.contracts.get(qual)
        old = base.docs.get(qual)
        new = head.docs.get(qual)
        if not reason or (old and new and old.text == new.text):
            continue
        if f"{path}::{qual}" in allow:
            continue
        line = (old or new).expr.lineno if (old or new) else 1  # type: ignore[union-attr]
        what = "удалён" if new is None else "добавлен" if old is None else "изменён"
        out.append(
            Violation(
                path,
                line,
                "contract_docstring",
                f"{path}::{qual}: docstring {what}, это контракт ({reason}); "
                f"допустимо только через --allow-docstring {path}::{qual}",
            )
        )
    return out


def compare_refs(
    path: str, base: Parsed, head: Parsed, invariants: str
) -> list[Violation]:
    kept = set(_REF_RE.findall(head.src)) | set(_REF_RE.findall(invariants))
    return [
        Violation(
            path,
            line,
            "ref_lost",
            f"ссылка #{num} из комментария или docstring базы не осталась ни в "
            f"файле, ни в {INVARIANTS_MD} ревизии head",
        )
        for num, line in sorted(base.refs.items(), key=lambda kv: kv[1])
        if num not in kept
    ]


def analyze_pair(
    path: str,
    base_data: bytes,
    head_data: bytes,
    index: RepoIndex,
    allow: set[str],
    invariants: str,
) -> list[Violation]:
    base = parse_source(base_data, path)
    head = parse_source(head_data, path)
    base.contracts = find_contracts(base.tree, path, index)
    head.contracts = find_contracts(head.tree, path, index)
    out = compare_tokens(path, base, head)
    # Индекс токена-якоря значим лишь там, где потоки ещё совпадают.
    same = _common_prefix(base, head) if out else None
    out += compare_directives(path, base, head, same)
    if not out:
        out += compare_ast(path, base, head)
    out += compare_contract_docs(path, base, head, allow)
    out += compare_refs(path, base, head, invariants)
    return out


# ------------------------------------------------------------------------ запуск


def build_index(repo: Repo, shas: Sequence[str]) -> RepoIndex:
    index = RepoIndex()
    seen: set[int] = set()  # один и тот же файл в двух ревизиях разбираем раз
    for sha in shas:
        paths = repo.list_py(sha)
        for path, data in repo.read_many(sha, paths).items():
            if data is None or hash(data) in seen:
                continue
            seen.add(hash(data))
            text = data.decode("utf-8", "replace")
            if "class " not in text and "__doc__" not in text and "getdoc" not in text:
                continue
            try:
                index.add(path, ast.parse(text))
            except (SyntaxError, ValueError, RecursionError):
                continue  # чужой файл вне списка: на проверку списка не влияет
    return index


def _normalize_paths(files: Sequence[str]) -> list[str]:
    out: list[str] = []
    for raw in files:
        path = posixpath.normpath(raw.replace("\\", "/"))
        if path.startswith("/") or path.startswith(".."):
            raise CheckError(f"{raw}: путь должен быть относительным к репозиторию")
        if not path.endswith(".py"):
            raise CheckError(f"{raw}: не .py, файл не поддержан")
        if path not in out:
            out.append(path)
    return out


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    files: int = 0

    @property
    def rc(self) -> int:
        return 2 if self.errors else (1 if self.violations else 0)


def run_check(
    repo: Repo, base_rev: str, head_rev: str, files: Sequence[str], allow: set[str]
) -> Report:
    report = Report()
    listed = _normalize_paths(files)
    base = repo.resolve(base_rev)
    head = repo.resolve(head_rev)
    for extra in repo.changed_py(base, head):
        if extra not in listed:
            report.violations.append(
                Violation(extra, 0, "unlisted_file", "изменён .py вне списка файлов")
            )
    old = repo.read_many(base, listed)
    new = repo.read_many(head, listed)
    todo: list[str] = []
    for path in listed:
        report.files += 1
        a, b = old[path], new[path]
        if a is None and b is None:
            report.errors.append(f"{path}: нет ни в base, ни в head")
        elif a is None or b is None:
            side = "добавлен в head" if a is None else "удалён в head"
            report.violations.append(Violation(path, 0, "file_changed", side))
        elif a != b:
            todo.append(path)
    if todo:
        index = build_index(repo, [base, head])
        inv = repo.read_many(head, [INVARIANTS_MD]).get(INVARIANTS_MD) or b""
        for path in todo:
            try:
                report.violations += analyze_pair(
                    path,
                    old[path] or b"",
                    new[path] or b"",
                    index,
                    allow,
                    inv.decode("utf-8", "replace"),
                )
            except CheckError as exc:
                report.errors.append(str(exc))
    return report


def _print(report: Report, as_json: bool) -> None:
    if as_json:
        payload = {
            "rc": report.rc,
            "files": report.files,
            "violations": [vars(v) for v in report.violations],
            "errors": report.errors,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for v in sorted(report.violations, key=lambda v: (v.path, v.line, v.kind)):
        print(v.render())
    for err in report.errors:
        print(f"{err} [error]")
    if report.errors:
        print(
            f"НЕ ПРОВЕРЕНО: {len(report.errors)} ошибок; результат «чисто» невозможен"
        )
    elif report.violations:
        print(f"НАРУШЕНИЙ: {len(report.violations)}")
    else:
        print(
            f"OK: {report.files} файл(ов) — изменены только комментарии и "
            "неконтрактные docstring-и (проверка ограниченного класса правок; "
            "поведение она не доказывает)"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", required=True, help="ревизия до правки (SHA)")
    parser.add_argument("--head", required=True, help="ревизия сдачи (SHA)")
    parser.add_argument("--repo", default=".", help="каталог репозитория")
    parser.add_argument(
        "--allow-docstring",
        action="append",
        default=[],
        metavar="ПУТЬ::ИМЯ",
        help="контрактный docstring, сокращение которого допустимо",
    )
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    parser.add_argument("files", nargs="+", help=".py-файлы, заявленные в сдаче")
    args = parser.parse_args(argv)
    try:
        report = run_check(
            Repo(Path(args.repo)),
            args.base,
            args.head,
            args.files,
            set(args.allow_docstring),
        )
    except CheckError as exc:
        report = Report(errors=[str(exc)])
    _print(report, args.json)
    return report.rc


if __name__ == "__main__":
    sys.exit(main())
