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
import hashlib
import io
import json
import posixpath
import re
import subprocess
import sys
import tokenize
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
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
_CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
_NOQA_RE = re.compile(r"^noqa\b(?:\s*:\s*(?:[A-Za-z]+\d+[\s,]*)+)?", re.I)
# Остальные директивы сравниваются сегментом целиком, регистр и список — как есть.
_WHOLE_RE = re.compile(
    r"^(?:type:|pragma:|fmt:|nosec\b|pylint:|mypy:|isort:|ruff:|flake8:|yapf:"
    r"|pyright:|pytype:|nosemgrep|cython:|pyre-ignore|pyre-fixme)",
    re.I,
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
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except (SyntaxError, LookupError) as exc:
        raise CheckError(f"{path}: кодировка файла не определена ({exc})") from exc
    if encoding != "utf-8":
        raise CheckError(f"{path}: кодировка {encoding}, поддержан только utf-8")
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
        expr = (
            _docstring_expr(node)
            if isinstance(node, (ast.Module, *DEF_NODES))
            else None
        )
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


def _directives_of(comment: str, row: int, leading: bool) -> list[str]:
    """Тексты директив инструментов в одном комментарии (разделитель — ``#``).

    Shebang и coding-cookie действуют только до первого токена кода: после него
    это обычный комментарий, и такой «перенос» cookie директивой не считается.
    """
    if leading and row == 1 and comment.startswith("#!"):
        return [f"shebang {comment}"]
    found: list[str] = []
    cookie = _CODING_RE.match(comment) if leading and row <= 2 else None
    if cookie:
        found.append(f"coding {cookie.group(1)}")
    for segment in comment.split("#")[1:]:
        seg = segment.strip()
        noqa = _NOQA_RE.match(seg)
        if noqa:
            found.append(_norm_text(noqa.group(0)))
        elif _WHOLE_RE.match(seg):
            found.append(_norm_text(seg))
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
            for text in _directives_of(tok.string, tok.start[0], last_row == 0):
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


@dataclass(frozen=True)
class Ref:
    """Имя класса и модуль, из которого файл его импортировал (``""`` — не импорт)."""

    name: str
    module: str = ""


@dataclass
class Imports:
    names: dict[str, Ref] = field(default_factory=dict)  # локальное имя -> оригинал
    modules: dict[str, str] = field(default_factory=dict)  # псевдоним -> модуль

    @classmethod
    def of(cls, tree: ast.Module) -> Imports:
        found = cls()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    found.names[alias.asname or alias.name] = Ref(
                        alias.name, node.module or ""
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    found.modules[local] = alias.name if alias.asname else local
        return found

    def dotted(self, chain: tuple[str, ...]) -> tuple[str, ...]:
        """Цепочка имён с развёрнутым первым звеном (импорт и псевдоним)."""
        head, rest = chain[0], chain[1:]
        if head in self.names:
            ref = self.names[head]
            return (
                (*ref.module.split("."), ref.name, *rest)
                if ref.module
                else (
                    ref.name,
                    *rest,
                )
            )
        if head in self.modules:
            return (*self.modules[head].split("."), *rest)
        return chain

    def ref(self, chain: tuple[str, ...]) -> Ref:
        full = self.dotted(chain)
        if len(chain) == 1 and chain[0] not in self.names:
            return Ref(chain[0])
        return Ref(full[-1], ".".join(full[:-1]))


def _base_refs(cls: ast.ClassDef, imports: Imports) -> set[Ref]:
    refs: set[Ref] = set()
    for base in cls.bases:
        node = base.value if isinstance(base, ast.Subscript) else base
        chain = _chain(node)
        if chain:
            refs.add(imports.ref(chain))
    return refs


def _call_name(call: ast.Call) -> str:
    chain = _chain(call.func)
    return chain[-1] if chain else ""


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _has_text(call: ast.Call, name: str) -> bool:
    """Ключ задан непустой строкой: тогда docstring наружу не уходит."""
    value = _kw(call, name)
    return (
        isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and bool(value.value.strip())
    )


def _decorator_reason(dec: ast.expr) -> str:
    call = dec if isinstance(dec, ast.Call) else None
    chain = _chain(call.func if call else dec)
    if not chain:
        return ""
    name = chain[-1]
    label = ".".join(chain)
    if call and _has_text(call, "description"):
        return ""  # описание задано явно: docstring наружу не уходит
    if name in _TOOL_DECORATORS:
        return f"@{label}: описание в tools/list"
    if name in _ROUTE_DECORATORS and call and (call.args or _kw(call, "path")):
        hidden = _kw(call, "include_in_schema")
        if isinstance(hidden, ast.Constant) and hidden.value is False:
            return ""
        return f"маршрут @{label}: описание в OpenAPI"
    if name in _CLI_DECORATORS and not (call and _has_text(call, "help")):
        return f"@{label}: справка CLI"
    return ""


def _doc_targets(tree: ast.Module, imports: Imports) -> set[tuple[str, ...]]:
    """Цепочки имён, чей docstring читают снаружи: ``x.__doc__``, ``getdoc(x)``,
    аргументы ``add_tool``/``add_api_route`` (позиционные и по ключу)."""
    found: set[tuple[str, ...]] = set()

    def add(node: ast.AST | None) -> None:
        chain = _chain(node) if node is not None else None
        if chain:
            found.add(imports.dotted(chain))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "__doc__":
            add(node.value)
        elif isinstance(node, ast.Call):
            name = imports.dotted(_chain(node.func) or ("",))[-1]
            if name in ("getdoc", "get_docstring") and node.args:
                add(node.args[0])
            elif name in _REGISTRARS:
                for arg in (*node.args, *(kw.value for kw in node.keywords)):
                    add(arg)
    return found


def _uses_module_doc(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "__doc__" for n in ast.walk(tree))


def _dotted_module(path: str) -> str:
    stem = path[:-3] if path.endswith(".py") else path
    return stem.removesuffix("/__init__").replace("/", ".")


@dataclass
class RepoIndex:
    """Что известно о репозитории целиком: базы классов и читатели docstring."""

    classes: dict[str, dict[str, set[Ref]]] = field(default_factory=dict)
    doc_targets: set[tuple[str, ...]] = field(default_factory=set)

    def add(self, path: str, tree: ast.Module) -> None:
        imports = Imports.of(tree)
        local = self.classes.setdefault(path, {})
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                local.setdefault(node.name, set()).update(_base_refs(node, imports))
        self.doc_targets |= _doc_targets(tree, imports)

    def _candidates(self, ref: Ref, path: str) -> list[tuple[str, str]]:
        if not ref.module:
            return [(path, ref.name)] if ref.name in self.classes.get(path, {}) else []
        defined = [(p, ref.name) for p, c in self.classes.items() if ref.name in c]
        mod = ref.module.lstrip(".")
        exact = [
            (p, n)
            for p, n in defined
            if _dotted_module(p) == mod or _dotted_module(p).endswith("." + mod)
        ]
        return exact or defined  # реэкспорт из пакета: путь модуля не совпал

    def is_schema(
        self, ref: Ref, path: str, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> bool:
        if ref.name in _SCHEMA_BASES:
            return True
        for key in self._candidates(ref, path):
            if key in seen:
                continue
            bases = self.classes[key[0]][key[1]]
            if any(self.is_schema(b, key[0], seen | {key}) for b in bases):
                return True
        return False


def _matches_target(parts: list[str], chain: tuple[str, ...]) -> bool:
    return chain[-1] == parts[-1]


def find_contracts(tree: ast.Module, path: str, index: RepoIndex) -> dict[str, str]:
    """Символ файла -> причина, по которой его docstring читают снаружи."""
    out: dict[str, str] = {}
    imports = Imports.of(tree)
    stem = Path(path).stem
    if _uses_module_doc(tree):
        out[MODULE] = "модуль использует __doc__ (argparse description=__doc__)"
    for chain in index.doc_targets:
        if chain[-1] == stem and MODULE not in out:
            out[MODULE] = f"docstring модуля читают снаружи ({'.'.join(chain)})"

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEF_NODES):
                qual = f"{prefix}.{child.name}" if prefix else child.name
                reason = _symbol_reason(child, qual, path, imports, index)
                if reason:
                    out[qual] = reason
                visit(child, qual)
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _symbol_reason(
    node: ast.AST, qual: str, path: str, imports: Imports, index: RepoIndex
) -> str:
    parts = qual.split(".")
    if isinstance(node, ast.ClassDef):
        for base in _base_refs(node, imports):
            if index.is_schema(base, path):
                return f"наследник {base.name}: описание уходит в схему"
    else:
        for dec in getattr(node, "decorator_list", []):
            reason = _decorator_reason(dec)
            if reason:
                return reason
    for chain in index.doc_targets:
        if _matches_target(parts, chain):
            return f"docstring читают снаружи ({'.'.join(chain)})"
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
    return out or _directive_order(path, base, head, key)


def _directive_order(
    path: str,
    base: Parsed,
    head: Parsed,
    key: Callable[[Directive], tuple[int, bool, str]],
) -> list[Violation]:
    """Набор директив тот же, а порядок нет: ``fmt: off``/``fmt: on`` зависят от него."""
    a = [key(d) for d in base.directives]
    b = [key(d) for d in head.directives]
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return [
                Violation(
                    path,
                    base.directives[i].row,
                    "directive",
                    f"порядок директив изменён: {base.directives[i].text!r} "
                    f"заменена на {head.directives[i].text!r} в той же позиции",
                )
            ]
    return []


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


_INDEX_MARKERS = ("class ", "__doc__", "getdoc", "get_docstring", *_REGISTRARS)


def build_index(repo: Repo, shas: Sequence[str]) -> RepoIndex:
    index = RepoIndex()
    seen: set[tuple[str, bytes]] = set()  # тот же файл по тому же пути — раз
    for sha in shas:
        paths = repo.list_py(sha)
        for path, data in repo.read_many(sha, paths).items():
            if data is None:
                continue
            key = (path, hashlib.blake2b(data).digest())
            if key in seen:
                continue
            seen.add(key)
            text = data.decode("utf-8", "replace")
            if not any(marker in text for marker in _INDEX_MARKERS):
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


def _validate(data: bytes, path: str, report: Report) -> None:
    """Не изменённый файл списка тоже должен разбираться: иначе «чисто» ложно."""
    try:
        parse_source(data, path)
    except CheckError as exc:
        report.errors.append(str(exc))


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
        else:
            _validate(a, path, report)
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
