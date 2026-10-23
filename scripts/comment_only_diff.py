#!/usr/bin/env python3
"""Строгая проверка «изменены только комментарии» (#2032).

Серия чистки истории в комментариях правит сотни строк в файлах ядра. Ревью не
отличает смену текста от смены кода, а потеря ``# type: ignore`` или правка
docstring ломает инструменты и публичные поверхности. Скрипт для двух ревизий
(SHA) и списка .py-файлов проверяет ОГРАНИЧЕННЫЙ класс правок. Это не
доказательство отсутствия изменения поведения и не смысловое ревью.

    uv run python scripts/comment_only_diff.py --base SHA --head SHA FILE...
    uv run python scripts/comment_only_diff.py --base SHA --head SHA \\
        --allow-docstring hub/services/lifecycle.py::blocker_note FILE...

Строгий режим (сменил распознавание «контрактных» docstring-ов, #2032 круг 5):
статический поиск потребителей docstring-а не сходится — каждая форма
(присваивание декоратора, звёздочный реэкспорт, вложенный класс) находилась
заново. Поэтому разрешено только то, что названо явно.
  * [code] строки кода до и после совпадают ЦЕЛИКОМ (отступ, разбиение на
    строки). Из сравнения выпадают пустые строки, строки из одних
    комментариев, хвостовой комментарий-не-директива и docstring-операторы.
    Любой перенос части выражения на другую строку — нарушение;
  * комментарий, где встречается имя инструмента (``noqa``, ``type:``,
    ``pragma``, ``fmt:``, ``nosec``, ``pylint``/``mypy``/…, shebang, coding),
    входит в строку кода ДОСЛОВНО (пробелы, регистр, префикс, хвост) и
    остаётся на своей строке и в своём порядке; число вычеркнутых строк между
    ``nextline``-директивой и её целью тоже сравнивается. Правка пояснения в
    таком комментарии тоже красная;
  * [docstring] любое изменение, добавление или удаление docstring-а (первый
    ``Expr(Constant(str))`` модуля, класса, функции; f-строки, bytes и прочие
    строки — код) — нарушение, пока символ не назван в
    ``--allow-docstring путь::имя`` (``<module>`` — docstring модуля). Каждый
    разрешённый символ сдача сопровождает проверкой его публикуемой
    поверхности (tools/list, OpenAPI, схема, тест). В сообщении — подсказка
    (декораторы, базы класса), но не разрешение;
  * [ast] нормализованный AST (``type_comments=True``, без docstring-ов) равен;
  * [ref_lost] ссылка ``#NNNN`` из комментария или docstring базы осталась в
    файле или в ``docs/agent-context/invariants.md`` ревизии head
    (вспомогательная проверка: номер не равен сохранённой причине);
  * [unlisted_file] вне списка изменён другой .py; [file_changed] файл списка
    добавлен или удалён; [file_mode] сменился режим файла.

Коды возврата: 0 — нарушений нет; 1 — есть нарушения; 2 — проверить не удалось
(ревизия не читается, синтаксическая ошибка, не .py, не UTF-8, ссылка, docstring
на одной строке с кодом): «чисто» не выводится никогда.
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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

INVARIANTS_MD = "docs/agent-context/invariants.md"
MODULE = "<module>"
# Предел точной разметки расхождений: больше — одно расхождение «от и до».
_DIFF_CELLS = 4_000_000
DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

_SKIPPED = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING, tokenize.ENDMARKER}
# Токены, которые вправе делить строку с docstring-оператором.
_LAYOUT = _SKIPPED | {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE}

_REF_RE = re.compile(r"(?<![\w&])#(\d{2,6})\b")
_CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
# Комментарий, в котором встречается имя инструмента, сравнивается ЦЕЛИКОМ и
# дословно: действие директивы зависит от пробелов, регистра, префикса и хвоста
# (``pragma:  x``, ``note # mypy: …``, ``nosecB101``), а не только от «ключевого»
# сегмента. Цена — правка пояснения в таком комментарии тоже красная.
_TOOL_RE = re.compile(
    r"noqa|type:|pragma|fmt:|nosem|nosec|pylint|mypy|isort|ruff|flake8|yapf|pyright"
    r"|pytype|cython|pyre-|nextline|skipcq|lgtm|codeql|sonar|noinspection"
    r"|autopep8|vulture|bandit|trivy|black|pyflakes|pycodestyle|pydocstyle|pep8"
    r"|coverage|pytest|hadolint|semgrep|ignore-errors|disable",
    re.I,
)


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


@dataclass
class DocSlot:
    qualname: str
    expr: ast.Expr
    text: str
    hint: str  # декораторы и базы владельца: подсказка, не разрешение


@dataclass
class Parsed:
    src: str
    tree: ast.Module
    code: list[tuple[int, str]]  # (строка, нормализованный текст строки кода)
    docs: dict[str, DocSlot]
    refs: dict[str, int]  # номер -> первая строка в комментарии или docstring


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


def _hint(node: ast.AST) -> str:
    parts = [f"@{ast.unparse(d)}" for d in getattr(node, "decorator_list", [])]
    parts += [f"база {ast.unparse(b)}" for b in getattr(node, "bases", [])]
    return ", ".join(parts)


def _collect_docs(tree: ast.Module) -> dict[str, DocSlot]:
    """Docstring-и по владельцам. Одноимённые определения нумеруются все — и с
    docstring-ом, и без: перенос docstring-а между ними виден."""
    docs: dict[str, DocSlot] = {}
    seen: dict[str, int] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        name = prefix or MODULE
        if isinstance(node, (ast.Module, *DEF_NODES)):
            index = seen.get(name, 0)
            seen[name] = index + 1
            key = name if index == 0 else f"{name}#{index}"
            expr = _docstring_expr(node)
            if expr is not None and isinstance(expr.value, ast.Constant):
                docs[key] = DocSlot(name, expr, str(expr.value.value), _hint(node))
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


def _check_docstring_rows(
    toks: list[tokenize.TokenInfo], docs: dict[str, DocSlot], lines: list[str]
) -> None:
    """Docstring-оператор не делит строку с кодом: иначе строку не вычесть."""
    starts = [(t.start[0], _byte_col(lines, t.start[0], t.start[1])) for t in toks]
    for doc in docs.values():
        expr = doc.expr
        lo = (expr.lineno, expr.col_offset)
        hi = (expr.end_lineno or expr.lineno, expr.end_col_offset or 0)
        first = bisect.bisect_left([s[0] for s in starts], lo[0])
        for i in range(first, len(toks)):
            if starts[i][0] > hi[0]:
                break
            end = (toks[i].end[0], _byte_col(lines, toks[i].end[0], toks[i].end[1]))
            inside = starts[i] >= lo and end <= hi
            if toks[i].type not in _LAYOUT and not inside:
                raise CheckError(
                    f"строка {starts[i][0]}: docstring {doc.qualname!r} делит "
                    "строку с кодом — не поддержано"
                )


def _directive_of(comment: str, row: int, leading: bool) -> str:
    """Дословный текст комментария-директивы или пустая строка.

    Shebang и coding-cookie действуют только до первого токена кода: после него
    это обычный комментарий.
    """
    text = comment.rstrip(" \t\r")  # NBSP и \f в хвосте значимы
    if leading and row == 1 and text.startswith("#!"):
        return text
    if leading and row <= 2 and _CODING_RE.match(text):
        return text
    return text if _TOOL_RE.search(text) else ""


def _multiline_rows(toks: list[tokenize.TokenInfo]) -> tuple[set[int], set[int]]:
    """Строки внутри многострочных литералов: (хвост+конец, начало).

    Содержимое таких строк — часть значения: пробелы и пустые строки значимы.
    """
    inner: set[int] = set()
    opening: set[int] = set()
    fstart: list[int] = []
    for tok in toks:
        kind = tokenize.tok_name.get(tok.type, "")
        if kind == "FSTRING_START":
            fstart.append(tok.start[0])
        elif kind == "FSTRING_END" and fstart:
            begin = fstart.pop()
            if tok.end[0] > begin:
                opening.add(begin)
                inner.update(range(begin + 1, tok.end[0] + 1))
        elif tok.type not in _LAYOUT and tok.end[0] > tok.start[0]:
            opening.add(tok.start[0])
            inner.update(range(tok.start[0] + 1, tok.end[0] + 1))
    return inner, opening


def doc_rows_of(doc: DocSlot) -> range:
    return range(doc.expr.lineno, (doc.expr.end_lineno or doc.expr.lineno) + 1)


def _gap_mark(lines: list[str], row: int, doc_first: set[int]) -> str:
    """Что между директивой и ближайшей строкой с содержимым.

    ``nextline``-директивы действуют на соседнюю строку: число вычеркнутых
    пустых и комментарных строк между ними входит в сравнение. Если за ними
    начало docstring-а — это тоже отмечается.
    """
    gap = 0
    for nxt in range(row + 1, len(lines) + 1):
        stripped = lines[nxt - 1].strip()
        if stripped and not stripped.startswith("#"):
            doc = "\x00docstring" if nxt in doc_first else ""
            return f"\x00gap={gap}{doc}" if gap or doc else ""
        if stripped.startswith("#") and _TOOL_RE.search(stripped):
            return f"\x00gap={gap}" if gap else ""
        gap += 1
    return f"\x00gap={gap}" if gap else ""


def _code_lines(
    src: str, toks: list[tokenize.TokenInfo], docs: dict[str, DocSlot]
) -> list[tuple[int, str]]:
    """Строки кода без комментариев-не-директив, пустых строк и docstring-ов."""
    lines = src.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    comments = {t.start[0]: t for t in toks if t.type == tokenize.COMMENT}
    first_real = next((t.start[0] for t in toks if t.type not in _LAYOUT), 10**9)
    inner, opening = _multiline_rows(toks)
    doc_rows = {r for d in docs.values() for r in doc_rows_of(d)}
    doc_first = {d.expr.lineno for d in docs.values()}
    doc_start = {r: d.expr.lineno for d in docs.values() for r in doc_rows_of(d)}
    out: list[tuple[int, str]] = []
    for row, text in enumerate(lines, start=1):
        eol = "\r" if text.endswith("\r") else ""
        com = comments.get(row)
        cut = text[: com.start[1]] if com else text
        directive = _directive_of(com.string, row, row < first_real) if com else ""
        verbatim = (row in inner or row in opening) and not com
        body = cut if verbatim else cut.rstrip(" \t\r")
        if row in doc_rows:
            if not directive:
                continue
            offset = row - doc_start[row]
            where = "inline" if cut.strip() else f"col{len(cut)}"
            body = f"\x00docstring+{offset}\x00{where}"
        elif not verbatim and not body.strip():
            if directive:  # комментарий-директива на своей строке: отступ значим
                mark = _gap_mark(lines, row, doc_first)
                out.append((row, f"{cut}{directive}{mark}{eol}"))
            continue
        suffix = f"  {directive}" if directive and body.strip() else ""
        out.append((row, f"{body}{suffix}" + ("" if verbatim else eol)))
    return out


def _refs(toks: list[tokenize.TokenInfo], docs: dict[str, DocSlot]) -> dict[str, int]:
    refs: dict[str, int] = {}
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            for m in _REF_RE.finditer(tok.string):
                refs.setdefault(m.group(1), tok.start[0])
    for doc in docs.values():
        for m in _REF_RE.finditer(doc.text):
            refs.setdefault(m.group(1), doc.expr.lineno)
    return refs


def parse_source(data: bytes, path: str) -> Parsed:
    src = _decode(data, path)
    tree = _parse_tree(src, path)
    toks = _tokenize(src, path)
    docs = _collect_docs(tree)
    _check_docstring_rows(toks, docs, src.split("\n"))
    return Parsed(src, tree, _code_lines(src, toks, docs), docs, _refs(toks, docs))


# ---------------------------------------------------------------------- сравнение


def compare_code(path: str, base: Parsed, head: Parsed) -> list[Violation]:
    a = [text for _, text in base.code]
    b = [text for _, text in head.code]
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
        opcodes = [("replace", 0, len(mid_a), 0, len(mid_b))]
    else:
        matcher = difflib.SequenceMatcher(None, mid_a, mid_b, autojunk=False)
        opcodes = matcher.get_opcodes()
    out: list[Violation] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal" or len(out) >= 20:
            continue
        old = base.code[lo + i1][0] if lo + i1 < len(base.code) else None
        new = head.code[lo + j1][0] if lo + j1 < len(head.code) else None
        was = " | ".join(t.strip()[:50] for t in mid_a[i1:i2][:2]) or "<нет>"
        now = " | ".join(t.strip()[:50] for t in mid_b[j1:j2][:2]) or "<нет>"
        out.append(
            Violation(
                path,
                old or (base.code[-1][0] if base.code else 1),
                "code",
                f"строки кода изменены (head: строка {new or '-'}): было "
                f"{was!r} (-{i2 - i1}), стало {now!r} (+{j2 - j1})",
            )
        )
    return out


def _strip_docs(tree: ast.Module) -> str:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, *DEF_NODES)) and _docstring_expr(node):
            node.body = node.body[1:]  # type: ignore[attr-defined]
    # Положение ``type: ignore`` держит сравнение строк; здесь — набор реально
    # действующих (Python создаёт TypeIgnore лишь для комментария целиком).
    tree.type_ignores = [ast.TypeIgnore(0, i.tag) for i in tree.type_ignores]
    return ast.dump(tree)


def compare_ast(path: str, base: Parsed, head: Parsed) -> list[Violation]:
    if _strip_docs(base.tree) == _strip_docs(head.tree):
        return []
    return [
        Violation(path, 1, "ast", "нормализованный AST (type_comments) различается")
    ]


def compare_docstrings(
    path: str, base: Parsed, head: Parsed, allow: set[str]
) -> list[Violation]:
    out: list[Violation] = []
    for key in sorted(set(base.docs) | set(head.docs)):
        old, new = base.docs.get(key), head.docs.get(key)
        owner = key.split("#")[0]
        if (old and new and old.text == new.text) or f"{path}::{owner}" in allow:
            continue
        slot = old or new
        assert slot is not None
        what = "удалён" if new is None else "добавлен" if old is None else "изменён"
        hint = f" ({slot.hint})" if slot.hint else ""
        out.append(
            Violation(
                path,
                slot.expr.lineno,
                "docstring",
                f"{path}::{owner}: docstring {what}{hint}; разрешено только "
                f"явно: --allow-docstring {path}::{owner}",
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
    allow: set[str],
    invariants: str,
) -> list[Violation]:
    base = parse_source(base_data, path)
    head = parse_source(head_data, path)
    out = compare_code(path, base, head)
    out += compare_docstrings(path, base, head, allow)
    if not out:
        out += compare_ast(path, base, head)
    out += compare_refs(path, base, head, invariants)
    return out


# ------------------------------------------------------------------------ запуск


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
    inv = repo.read_many(head, [INVARIANTS_MD]).get(INVARIANTS_MD) or b""
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
        elif a == b:
            _validate(a, path, report)
        else:
            try:
                report.violations += analyze_pair(
                    path, a, b, allow, inv.decode("utf-8", "replace")
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
            "docstring-и, названные в --allow-docstring (проверка ограниченного "
            "класса правок; поведение она не доказывает)"
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
        help="docstring, изменение которого разрешено явно",
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
