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

_BLOB_MODES = {"100644", "100755"}
_REF_RE = re.compile(r"(?<![\w&])#(\d{2,6})\b")
_CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
# Директивы сравниваются сегментом целиком: регистр и список кодов — как есть.
_WHOLE_RE = re.compile(
    r"^(?:noqa\b|type:|pragma:|fmt:|nosec\b|pylint:|mypy:|isort:|ruff:|flake8:|yapf:"
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
_DOC_READERS = {"getdoc", "get_docstring"}
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
    col: int = -1  # отступ отдельной строки значим (``fmt: off``); у inline — нет
    before_doc: bool = False  # стоит прямо перед исключаемым docstring-оператором


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
        asked = [p for p in paths if _plain_path(p)]  # протокол построчный
        query = "".join(f"{sha}:{p}\n" for p in asked).encode()
        out = self._run("cat-file", "--batch", data=query) if asked else b""
        result: dict[str, bytes | None] = {p: None for p in paths}
        pos = 0
        for path in asked:
            end = out.index(b"\n", pos)
            header = out[pos:end].split()
            pos = end + 1
            if len(header) == 3 and header[1] == b"blob":
                size = int(header[2])
                result[path] = out[pos : pos + size]
                pos += size + 1
        return result

    def modes(self, sha: str, paths: Sequence[str]) -> dict[str, str]:
        """Режим записи git (100644, 100755, 120000 …) файлов ревизии."""
        if not paths:
            return {}
        out = self._run("ls-tree", "-z", sha, "--", *paths)
        found: dict[str, str] = {}
        for entry in out.decode("utf-8", "surrogateescape").split("\0"):
            meta, _, name = entry.partition("\t")
            if name:
                found[name] = meta.split()[0]
        return found

    def list_py(self, sha: str) -> list[tuple[str, str]]:
        """Все ``*.py`` ревизии: (путь, режим записи git)."""
        out = self._run("ls-tree", "-r", "-z", sha)
        found: list[tuple[str, str]] = []
        for entry in out.decode("utf-8", "surrogateescape").split("\0"):
            meta, _, name = entry.partition("\t")
            if name.endswith(".py"):
                found.append((name, meta.split()[0]))
        return found


def _plain_path(path: str) -> bool:
    """Имя без управляющих символов: ``cat-file --batch`` читает его построчно."""
    return all(ord(ch) >= 0x20 and ch != "\x7f" for ch in path)


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
            key = name if name not in docs else f"{name}#{len(docs)}"  # property.setter
            docs[key] = DocSlot(name, expr, str(expr.value.value))
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
        if tok.type == tokenize.NEWLINE:
            return {j}
        if tok.type == tokenize.OP and tok.string == ";":
            return {j, *_separator_after(toks, j, removed)}  # ``"doc";`` + NEWLINE
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
        if _WHOLE_RE.match(seg):
            found.append(_norm_text(seg))
    return found


def _scan_tokens(
    toks: list[tokenize.TokenInfo], removed: set[int]
) -> tuple[list[Tok], list[Directive]]:
    kept: list[Tok] = []
    directives: list[Directive] = []
    last_row = 0  # строка конца последнего «настоящего» токена
    counted = 0  # токены без INDENT/DEDENT: они «переезжают» вместе с docstring
    for i, tok in enumerate(toks):
        if tok.type == tokenize.COMMENT:
            inline = last_row == tok.start[0]
            before_doc = _next_real(toks, i) in removed
            for text in _directives_of(tok.string, tok.start[0], last_row == 0):
                directives.append(
                    Directive(
                        counted,
                        inline,
                        text,
                        tok.start[0],
                        -1 if inline else tok.start[1],
                        before_doc,
                    )
                )
            continue
        if tok.type not in _LAYOUT:
            last_row = tok.end[0]
        if i in removed or tok.type in _SKIPPED:
            continue
        string = "\n" if tok.type == tokenize.NEWLINE and not tok.string else tok.string
        kept.append(Tok(tok.type, string, tok.start[0]))
        if tok.type not in (tokenize.INDENT, tokenize.DEDENT):
            counted += 1
    return kept, directives


def _next_real(toks: list[tokenize.TokenInfo], i: int) -> int:
    """Индекс ближайшего за комментарием токена, не являющегося разметкой."""
    for j in range(i + 1, len(toks)):
        if toks[j].type not in _LAYOUT:
            return j
    return -1


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

UNRESOLVED = "·"  # голова цепочки не импорт: переменная, self, фикстура


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
    """Имя класса и модуль, откуда файл его взял (``""`` — определён в файле)."""

    name: str
    module: str = ""


@dataclass
class Imports:
    """Все привязки имён к импортам файла, в том числе в локальных областях.

    Имя может быть связано по-разному в разных областях; разбор не угадывает
    нужную, а возвращает все варианты, и решение принимается по худшему.
    """

    names: dict[str, set[Ref]] = field(default_factory=dict)
    modules: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def of(cls, tree: ast.Module, path: str = "") -> Imports:
        found = cls()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = _absolute_module(path, node.level, node.module)
                for alias in node.names:
                    ref = Ref(alias.name, module)
                    found.names.setdefault(alias.asname or alias.name, set()).add(ref)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    target = alias.name if alias.asname else local
                    found.modules.setdefault(local, set()).add(target)
        return found

    def dotted(self, chain: tuple[str, ...]) -> set[tuple[str, ...]]:
        """Варианты цепочки с развёрнутым первым звеном; нераскрытое — с ``·``."""
        head, rest = chain[0], chain[1:]
        variants: set[tuple[str, ...]] = set()
        for ref in self.names.get(head, ()):
            prefix = (*ref.module.split("."), ref.name) if ref.module else (ref.name,)
            variants.add((*prefix, *rest))
        for module in self.modules.get(head, ()):
            variants.add((*module.split("."), *rest))
        if variants:
            return variants
        return {chain if len(chain) == 1 else (UNRESOLVED, *chain)}

    def refs(self, chain: tuple[str, ...]) -> set[Ref]:
        out: set[Ref] = set()
        if len(chain) == 1:
            out.add(Ref(chain[0]))  # определён в этом же файле
            out |= self.names.get(chain[0], set())
            return out
        for full in self.dotted(chain):
            if full[0] != UNRESOLVED:
                out.add(Ref(full[-1], ".".join(full[:-1])))
        return out

    def last_names(self, chain: tuple[str, ...]) -> set[str]:
        """Оригинальные имена конца цепочки: ``cmd`` из ``command as cmd`` тоже."""
        return {chain[-1]} | {v[-1] for v in self.dotted(chain)}


def _absolute_module(path: str, level: int, module: str | None) -> str:
    """``from ..x import y`` в файле ``path`` -> абсолютное имя модуля."""
    if not level:
        return module or ""
    parts = _dotted_module(path).split(".") if path else []
    if not path.endswith("__init__.py"):
        parts = parts[:-1]
    parts = parts[: max(len(parts) - (level - 1), 0)]
    if module:
        parts = [*parts, *module.split(".")]
    return ".".join(parts)


def _base_refs(cls: ast.ClassDef, imports: Imports) -> set[Ref]:
    refs: set[Ref] = set()
    for base in cls.bases:
        node = base.value if isinstance(base, ast.Subscript) else base
        chain = _chain(node)
        if chain:
            refs |= imports.refs(chain)
    return refs


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


def _decorator_reason(dec: ast.expr, imports: Imports, index: RepoIndex) -> str:
    call = dec if isinstance(dec, ast.Call) else None
    chain = _chain(call.func if call else dec)
    if not chain:
        return ""
    names = index.names_of(imports, chain)
    label = ".".join(chain)
    if call and _has_text(call, "description"):
        return ""  # описание задано явно: docstring наружу не уходит
    if names & _TOOL_DECORATORS:
        return f"@{label}: описание в tools/list"
    if names & _ROUTE_DECORATORS and call and (call.args or call.keywords):
        hidden = _kw(call, "include_in_schema")
        if isinstance(hidden, ast.Constant) and hidden.value is False:
            return ""
        return f"маршрут @{label}: описание в OpenAPI"
    if names & _CLI_DECORATORS and not (call and _has_text(call, "help")):
        return f"@{label}: справка CLI"
    return ""


def _def_paths(tree: ast.Module) -> dict[str, list[tuple[str, ...]]]:
    """Имя определения -> его полные квалифицированные имена в файле."""
    found: dict[str, list[tuple[str, ...]]] = {}

    def visit(node: ast.AST, prefix: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEF_NODES):
                qual = (*prefix, child.name)
                found.setdefault(child.name, []).append(qual)
                visit(child, qual)
            else:
                visit(child, prefix)

    visit(tree, ())
    return found


def _doc_targets(
    tree: ast.Module,
    imports: Imports,
    module: tuple[str, ...],
    index: RepoIndex,
) -> set[tuple[str, ...]]:
    """Цепочки имён, чей docstring читают снаружи: ``x.__doc__``, ``getdoc(x)``,
    аргументы ``add_tool``/``add_api_route`` (позиционные и по ключу).

    Имя, определённое в самом файле (в том числе вложенное), получает путь
    модуля и область; чужая переменная (``svc.method``) — маркер ``·``.
    """
    found: set[tuple[str, ...]] = set()
    defs = _def_paths(tree)

    def add(node: ast.AST | None) -> None:
        chain = _chain(node) if node is not None else None
        if not chain:
            return
        for full in imports.dotted(chain):
            plain = full == chain or full == (UNRESOLVED, *chain)
            if plain and chain[0] in defs:
                found.update((*module, *q, *chain[1:]) for q in defs[chain[0]])
            elif plain and len(chain) == 1:
                found.add((UNRESOLVED, chain[0]))
            else:
                found.add(full)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "__doc__":
            add(node.value)
        elif isinstance(node, ast.Call):
            chain = _chain(node.func)
            names = index.names_of(imports, chain) if chain else set()
            if names & _DOC_READERS or (names & _REGISTRARS and _publishes(node)):
                for arg in (*node.args, *(kw.value for kw in node.keywords)):
                    add(arg)
    return found


def _publishes(call: ast.Call) -> bool:
    """Регистрация выставляет docstring наружу (нет явного описания и не скрыта)."""
    hidden = _kw(call, "include_in_schema")
    if isinstance(hidden, ast.Constant) and hidden.value is False:
        return False
    return not _has_text(call, "description")


def _uses_module_doc(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "__doc__" for n in ast.walk(tree))


def _dotted_module(path: str) -> str:
    stem = path[:-3] if path.endswith(".py") else path
    return stem.removesuffix("/__init__").replace("/", ".")


def _target_hits(symbol: tuple[str, ...], target: tuple[str, ...]) -> bool:
    """Читатель ``target`` относится к символу ``symbol`` (полный путь с модулем)."""
    if not target or target[-1] != symbol[-1]:
        return False
    if target[0] != UNRESOLVED:
        # Путь известен: он обязан быть хвостом пути символа (корень пакета в
        # импорте мог быть опущен), но не длиннее его: ``pkg.m.f`` — не ``m.f``.
        return len(target) <= len(symbol) and symbol[-len(target) :] == target
    rest = target[1:]
    if len(rest) == 1:
        return True
    owner_t = rest[-2]
    owner_s = symbol[-2] if len(symbol) >= 2 else ""
    # ``svc.method.__doc__``: экземпляр неизвестен — подходит метод любого класса.
    return owner_t == owner_s or (owner_t[:1].islower() and owner_s[:1].isupper())


@dataclass
class RepoIndex:
    """Что известно о репозитории целиком: базы классов и читатели docstring."""

    classes: dict[str, dict[str, set[Ref]]] = field(default_factory=dict)
    aliases: dict[str, dict[str, set[Ref]]] = field(default_factory=dict)
    mod_aliases: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    origin: dict[str, set[str]] = field(default_factory=dict)
    doc_targets: set[tuple[str, ...]] = field(default_factory=set)
    trees: list[tuple[str, ast.Module]] = field(default_factory=list)

    def add(self, path: str, tree: ast.Module) -> None:
        imports = Imports.of(tree, path)
        local = self.classes.setdefault(path, {})
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                local.setdefault(node.name, set()).update(_base_refs(node, imports))
        self.aliases[path] = imports.names
        self.mod_aliases[path] = imports.modules
        self.trees.append((path, tree))

    def names_of(self, imports: Imports, chain: tuple[str, ...]) -> set[str]:
        """Имена конца цепочки с учётом переименований импортом по всему репозиторию."""
        names = imports.last_names(chain)
        for name in tuple(names):
            names |= self.origin.get(name, set())
        return names

    def _build_origin(self) -> None:
        origin: dict[str, set[str]] = {}
        for aliases in self.aliases.values():
            for local, refs in aliases.items():
                origin.setdefault(local, set()).update(r.name for r in refs)
        grown = True
        while grown:  # конечное множество имён: замыкание сходится
            grown = False
            for names in origin.values():
                extra = {o for n in names for o in origin.get(n, ())} - names
                if extra:
                    names |= extra
                    grown = True
        self.origin = origin

    def finalize(self) -> None:
        """Достроить читателей: реэкспорт, затем наследование."""
        self._build_origin()
        targets: set[tuple[str, ...]] = set()
        for path, tree in self.trees:
            targets |= _doc_targets(
                tree,
                Imports.of(tree, path),
                tuple(_dotted_module(path).split(".")),
                self,
            )
        while True:
            fresh = {x for t in targets for x in self._reexported(t)} - targets
            if not fresh:
                break
            targets |= fresh
        self.doc_targets = targets | self._inherited(targets)
        self.trees.clear()

    def _reexported(self, target: tuple[str, ...]) -> set[tuple[str, ...]]:
        out: set[tuple[str, ...]] = set()
        if target[0] == UNRESOLVED:
            return out
        for cut in range(1, len(target)):
            module, name = ".".join(target[:-cut]), target[-cut]
            tail = target[len(target) - cut + 1 :]
            for path in self._module_files(module):
                for ref in self.aliases.get(path, {}).get(name, ()):
                    where = ref.module or _dotted_module(path)
                    out.add((*where.split("."), ref.name, *tail))
                for mod in self.mod_aliases.get(path, {}).get(name, ()):
                    out.add((*mod.split("."), *tail))
        return out

    def resolve_class(
        self, ref: Ref, path: str, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> set[tuple[str, str]]:
        """Определения класса (файл, имя) за ссылкой, с проходом по реэкспортам."""
        files = [path] if not ref.module else self._module_files(ref.module)
        out: set[tuple[str, str]] = set()
        for p in files:
            key = (p, ref.name)
            if key in seen:
                continue
            if ref.name in self.classes.get(p, {}):
                out.add(key)
                continue
            for alias in self.aliases.get(p, {}).get(ref.name, ()):
                out |= self.resolve_class(alias, p, seen | {key})
        return out

    def _inherited(self, targets: set[tuple[str, ...]]) -> set[tuple[str, ...]]:
        """``getdoc(B)``/``getdoc(B.f)`` читают docstring предка: предки тоже."""
        reads: dict[tuple[str, str], set[str]] = {}
        symbols = {
            key: (*_dotted_module(key[0]).split("."), key[1])
            for key in ((p, n) for p, cs in self.classes.items() for n in cs)
        }
        for key, symbol in symbols.items():
            for t in targets:
                if _target_hits(symbol, t):
                    reads.setdefault(key, set())
                elif len(t) >= 2 and _target_hits(symbol, t[:-1]):
                    reads.setdefault(key, set()).add(t[-1])
        parents = {
            key: set().union(*(self.resolve_class(b, key[0]) for b in bases))
            for key in reads
            for bases in [self.classes[key[0]][key[1]]]
        }
        grown = True
        while grown:
            grown = False
            for key, members in list(reads.items()):
                for parent in parents.get(key) or self._parents_of(key):
                    if parent not in reads:
                        reads[parent] = set()
                        grown = True
                    if not members <= reads[parent]:
                        reads[parent] |= members
                        grown = True
        out: set[tuple[str, ...]] = set()
        for key, members in reads.items():
            symbol = symbols[key]
            out.add(symbol)
            out |= {(*symbol, m) for m in members}
        return out

    def _parents_of(self, key: tuple[str, str]) -> set[tuple[str, str]]:
        bases = self.classes[key[0]][key[1]]
        return set().union(*(self.resolve_class(b, key[0]) for b in bases))

    def _module_files(self, module: str) -> list[str]:
        mod = module.lstrip(".")
        return [
            p
            for p in self.classes
            if mod
            and (_dotted_module(p) == mod or _dotted_module(p).endswith("." + mod))
        ]

    def is_schema(
        self, ref: Ref, path: str, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> bool:
        files = [path] if not ref.module else self._module_files(ref.module)
        if ref.name in _SCHEMA_BASES and not any(
            ref.name in self.classes[p] or ref.name in self.aliases.get(p, {})
            for p in files
        ):
            return True  # библиотечный класс, в репозитории своего нет
        return any(self._file_defines_schema(ref, p, seen) for p in files)

    def _file_defines_schema(
        self, ref: Ref, path: str, seen: frozenset[tuple[str, str]]
    ) -> bool:
        key = (path, ref.name)
        if key in seen:
            return False
        seen = seen | {key}
        bases = self.classes.get(path, {}).get(ref.name)
        if bases is not None:
            return any(self.is_schema(b, path, seen) for b in bases)
        # Реэкспорт: имя в этом модуле — импорт из другого (``X as Y``).
        return any(
            self.is_schema(alias, path, seen)
            for alias in self.aliases.get(path, {}).get(ref.name, ())
        )


def find_contracts(tree: ast.Module, path: str, index: RepoIndex) -> dict[str, str]:
    """Символ файла -> причина, по которой его docstring читают снаружи."""
    out: dict[str, str] = {}
    imports = Imports.of(tree, path)
    module = tuple(_dotted_module(path).split("."))
    if _uses_module_doc(tree):
        out[MODULE] = "модуль использует __doc__ (argparse description=__doc__)"
    if any(_target_hits(module, t) for t in index.doc_targets):
        out.setdefault(MODULE, "docstring модуля читают снаружи (__doc__/getdoc)")

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEF_NODES):
                qual = f"{prefix}.{child.name}" if prefix else child.name
                symbol = (*module, *qual.split("."))
                reason = _symbol_reason(child, symbol, path, imports, index)
                if reason:
                    out[qual] = reason
                visit(child, qual)
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _symbol_reason(
    node: ast.AST,
    symbol: tuple[str, ...],
    path: str,
    imports: Imports,
    index: RepoIndex,
) -> str:
    if isinstance(node, ast.ClassDef):
        for base in _base_refs(node, imports):
            if index.is_schema(base, path):
                return f"наследник {base.name}: описание уходит в схему"
    else:
        for dec in getattr(node, "decorator_list", []):
            reason = _decorator_reason(dec, imports, index)
            if reason:
                return reason
    for target in index.doc_targets:
        if _target_hits(symbol, target):
            return f"docstring читают снаружи ({'.'.join(target)})"
    return ""


# ---------------------------------------------------------------------- сравнение


def _tok_text(tok: Tok | None) -> str:
    if tok is None:
        return "<нет>"
    text = tok.string if tok.type != tokenize.NEWLINE else "NEWLINE"
    return f"{tokenize.tok_name.get(tok.type, tok.type)}:{text[:40]!r}"


def _common_prefix(base: Parsed, head: Parsed) -> int:
    """Общее начало потоков в единицах якоря директив (без INDENT/DEDENT)."""
    a, b = base.tokens, head.tokens
    n = counted = 0
    while n < min(len(a), len(b)) and a[n].key == b[n].key:
        if a[n].type not in (tokenize.INDENT, tokenize.DEDENT):
            counted += 1
        n += 1
    return counted


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
    # Положение ``type: ignore`` держит проверка директив; здесь — только набор
    # реально действующих (Python создаёт TypeIgnore лишь для комментария целиком).
    tree.type_ignores = [ast.TypeIgnore(0, i.tag) for i in tree.type_ignores]
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

    def key(d: Directive) -> tuple[int, bool, int, bool, str]:
        return (d.anchor, d.inline, d.col, d.before_doc, d.text)

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
    key: Callable[[Directive], tuple[int, bool, int, bool, str]],
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
        owner = qual.split("#")[0]
        reason = base.contracts.get(owner) or head.contracts.get(owner)
        old = base.docs.get(qual)
        new = head.docs.get(qual)
        if not reason or (old and new and old.text == new.text):
            continue
        if f"{path}::{owner}" in allow:
            continue
        line = (old or new).expr.lineno if (old or new) else 1  # type: ignore[union-attr]
        what = "удалён" if new is None else "добавлен" if old is None else "изменён"
        out.append(
            Violation(
                path,
                line,
                "contract_docstring",
                f"{path}::{owner}: docstring {what}, это контракт ({reason}); "
                f"допустимо только через --allow-docstring {path}::{owner}",
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
    """Индекс контрактов по всему репозиторию. Неполный индекс — ошибка, не «чисто»:
    непрочитанный файл мог оказаться тем, что читает docstring или задаёт схему."""
    index = RepoIndex()
    seen: set[tuple[str, bytes]] = set()  # тот же файл по тому же пути — раз
    incomplete: list[str] = []
    for sha in shas:
        entries = repo.list_py(sha)
        plain = [p for p, mode in entries if mode in _BLOB_MODES and _plain_path(p)]
        incomplete += [p for p, mode in entries if p not in plain]
        for path, data in repo.read_many(sha, plain).items():
            if data is None:
                incomplete.append(path)
                continue
            key = (path, hashlib.blake2b(data).digest())
            if key in seen:
                continue
            seen.add(key)
            try:  # байты, а не текст: разбор сам учитывает coding-cookie
                index.add(path, ast.parse(data))
            except (SyntaxError, ValueError, RecursionError):
                incomplete.append(path)
    if incomplete:
        shown = ", ".join(sorted(set(incomplete))[:5])
        raise CheckError(
            f"индекс контрактов неполон ({len(set(incomplete))} файл(ов): {shown}): "
            "не разобрано, ссылка или имя с управляющими символами"
        )
    index.finalize()
    return index


def _normalize_paths(files: Sequence[str]) -> list[str]:
    out: list[str] = []
    for raw in files:
        path = posixpath.normpath(raw.replace("\\", "/"))
        if not _plain_path(path):
            raise CheckError(f"{raw!r}: управляющие символы в пути не поддержаны")
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
    notes: list[str] = field(default_factory=list)
    files: int = 0

    @property
    def rc(self) -> int:
        return 2 if self.errors else (1 if self.violations else 0)


def _mode_problem(
    path: str, modes: tuple[dict[str, str], dict[str, str]], report: Report
) -> bool:
    """Символьная ссылка и подмодуль — не исходник; смена режима — не комментарий."""
    before, after = modes[0].get(path), modes[1].get(path)
    if {before, after} & {"120000", "160000"}:
        report.errors.append(f"{path}: символьная ссылка или подмодуль не поддержаны")
        return True
    if before and after and before != after:
        report.violations.append(
            Violation(path, 0, "file_mode", f"режим файла изменён: {before} -> {after}")
        )
    return False


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
    modes = (repo.modes(base, listed), repo.modes(head, listed))
    todo: list[str] = []
    for path in listed:
        report.files += 1
        if _mode_problem(path, modes, report):
            continue
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
        try:
            index = build_index(repo, [base, head])
        except CheckError as exc:
            report.errors.append(str(exc))
            todo = []
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
    for note in report.notes:
        print(f"предупреждение: {note}")
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
