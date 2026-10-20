"""Тесты AC в файле с диффом выше потолка: где они лежат и что ревьюер не прочёл (#1933).

Файл выше ``REVIEW_FILE_LINE_CAP`` строк не помещается в один проход, и раньше
ревьюеру говорили «прочитай сколько успеешь». Непрочитанным оказывался хвост
файла, а в хвосте — именно тесты, доказывающие AC (#1874, #1862). Здесь две
чистые вещи, без БД и сети:

* :func:`ac_test_spans` — по диффу находит функции ``test_ref`` задачи и
  отдаёт диапазоны строк ФАЙЛА на коммите сдачи (новая сторона диффа);
* :func:`unread_ac_tests` — из ``lost_dimensions`` отчёта берёт тесты AC,
  которые ревьюер назвал непрочитанными, точным совпадением со списком.

Второй — не разбор прозы (#1238): строка сверяется с ЗАКРЫТЫМ списком из
карточки задачи, после фиксированного префикса, по равенству. Подстрока,
другой регистр префикса и чужое имя не засчитываются.

Границы функций берутся НЕ из текста ханков (это чёрный список случаев:
литералы, вложенность, переносы между ханками), а из ``ast`` файла теста на
ЗАКРЕПЛЁННОМ sha сдачи: ``lineno``, ``end_lineno``, ``decorator_list``, класс —
по дереву. Файл не прочитан или не разобран — «не найден», без отката на ветку.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hub.services.test_locator import PYTEST, runner_of

#: Префикс строки ``lost_dimensions``, которой ревьюер называет непрочитанный тест AC.
UNREAD_PREFIX = "ac_test_unread: "

_PARAM = re.compile(r"\[.*\]$")

NOT_FOUND = "не найден в диффе"
UNREADABLE = "не найден: файл на закреплённом коммите не прочитан или имя определено неоднозначно"
READ_LIMIT = 2_000_000


@dataclass(frozen=True)
class AcTestSpan:
    """Где лежит тест AC: ``start``/``end`` — строки файла на sha сдачи, или ``None``."""

    ref: str
    path: str
    start: int | None = None
    end: int | None = None
    unreadable: bool = False
    lines: tuple[tuple[int, str], ...] = ()

    @property
    def found(self) -> bool:
        return self.start is not None

    def label(self) -> str:
        """Строка блока «ЧИТАТЬ ПЕРВЫМИ» для этого теста."""
        if self.found:
            return f"ЧИТАТЬ ПЕРВЫМИ: {self.path}:{self.start}–{self.end} ({self.ref})"
        why = UNREADABLE if self.unreadable else NOT_FOUND
        return f"ЧИТАТЬ ПЕРВЫМИ: {self.ref} — {why}"


def ac_test_refs(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """``test_ref`` AC с ``verifiable_by=test``, в порядке AC, без повторов."""
    seen: list[str] = []
    for row in rows:
        ref = (row.get("test_ref") or "").strip()
        if str(row.get("verifiable_by") or "") == "test" and ref and ref not in seen:
            seen.append(ref)
    return seen


def _parse_ref(ref: str) -> tuple[str, tuple[str, ...], str] | None:
    """``(файл, цепочка классов, функция)`` pytest-ссылки; параметр ``[id]`` отброшен."""
    if runner_of(ref) != PYTEST:
        return None
    path, _, node = ref.partition("::")
    parts = [part for part in _PARAM.sub("", node).split("::") if part]
    if not parts:
        return None
    return path, tuple(parts[:-1]), parts[-1]


def ac_test_paths(refs: Iterable[str]) -> list[str]:
    """Файлы pytest-ссылок, без повторов, в порядке ссылок."""
    out: list[str] = []
    for ref in refs:
        parsed = _parse_ref(ref)
        if parsed and parsed[0] not in out:
            out.append(parsed[0])
    return out


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPOUND = (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)
_NOT_DEFINED = object()


def _rebinds(stmt: ast.stmt, name: str) -> bool:
    """Связывает ли оператор ``name`` иначе, чем прямым ``def``/``class`` в этом теле."""
    if isinstance(stmt, ast.Assign):
        return any(
            isinstance(n, ast.Name) and n.id == name
            for t in stmt.targets
            for n in ast.walk(t)
        )
    if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        return isinstance(stmt.target, ast.Name) and stmt.target.id == name
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return any((a.asname or a.name).split(".")[0] == name for a in stmt.names)
    return False


def _nested_binding(body: Sequence[ast.stmt], name: str) -> bool:
    """Связано ли ``name`` внутри if/try/with/for/while этого тела (pytest выполнит ЛЮБОЕ)."""
    for stmt in body:
        if isinstance(stmt, _COMPOUND):
            parts = [stmt.body, getattr(stmt, "orelse", [])]
            parts += [getattr(stmt, "finalbody", [])]
            parts += [h.body for h in getattr(stmt, "handlers", [])]
            for part in parts:
                for inner in part:
                    named = (
                        isinstance(inner, (*_DEFS, ast.ClassDef)) and inner.name == name
                    )
                    if named or _rebinds(inner, name):
                        return True
                if _nested_binding(part, name):
                    return True
    return False


def _locate(
    tree: ast.Module, owners: Sequence[str], name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None | object:
    """Функция ``name`` в теле класса(ов) ``owners`` от верха модуля, прямой потомок.

    Как Python: при повторном определении действует последнее. Имя, связанное ещё
    и иначе (присваивание, импорт, def внутри if/try), — неоднозначно:
    ``_NOT_DEFINED`` вместо достоверного диапазона.
    """
    scope: list[ast.stmt] = tree.body
    for owner in owners:
        classes = [n for n in scope if isinstance(n, ast.ClassDef) and n.name == owner]
        if not classes:
            return None
        if len(classes) > 1 or _nested_binding(scope, owner):
            return _NOT_DEFINED
        scope = classes[-1].body
    if any(_rebinds(s, name) for s in scope) or _nested_binding(scope, name):
        return _NOT_DEFINED
    found = [n for n in scope if isinstance(n, _DEFS) and n.name == name]
    return found[-1] if found else None


def _tree_of(text: str | None) -> tuple[ast.Module, list[str]] | None:
    if text is None:
        return None
    try:
        # Нумерация строк ast — по \n, \r\n, \r; splitlines() режет ещё и по \x0c.
        return ast.parse(text), re.split(r"\r\n|\r|\n", text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def ac_test_spans(
    sources: Mapping[str, str | None] | None, refs: Sequence[str]
) -> list[AcTestSpan]:
    """Диапазоны тестов AC по файлам на закреплённом sha, в порядке ``refs``.

    ``sources`` — ``{путь: текст файла на sha}``; ``None`` вместо текста (или
    ``sources=None``) — файл не прочитан, это «неизвестно», а не «теста нет».
    Ссылка не pytest (vitest) и функция, которой в файле нет, — без диапазона:
    молчание выглядело бы как «ревьюеру читать нечего».
    """
    trees: dict[str, tuple[ast.Module, list[str]] | None] = {}
    out: list[AcTestSpan] = []
    for ref in refs:
        parsed = _parse_ref(ref)
        if parsed is None:
            out.append(AcTestSpan(ref, ""))
            continue
        path, owners, name = parsed
        if path not in trees:
            trees[path] = _tree_of((sources or {}).get(path))
        got = trees[path]
        if got is None:
            out.append(AcTestSpan(ref, path, unreadable=True))
            continue
        node = _locate(got[0], owners, name)
        if node is _NOT_DEFINED:
            out.append(AcTestSpan(ref, path, unreadable=True))
            continue
        if not isinstance(node, _DEFS):
            out.append(AcTestSpan(ref, path))
            continue
        decorated = [d.lineno for d in node.decorator_list]
        start, end = min([node.lineno, *decorated]), node.end_lineno or node.lineno
        body = tuple((n, got[1][n - 1]) for n in range(start, end + 1))
        out.append(AcTestSpan(ref, path, start, end, lines=body))
    return out


def covered_lines(section: str) -> list[tuple[int, int]]:
    """Диапазоны строк НОВОЙ стороны, которые текст диффа файла показывает (по ``@@ +c,d``)."""
    out = []
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", section, re.M):
        start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        if count:
            out.append((start, start + count - 1))
    return out


def shown_in(section: str | None, span: AcTestSpan) -> bool:
    """Показан ли тест AC в тексте диффа файла ЦЕЛИКОМ (иначе нужна вырезка)."""
    if section is None or span.start is None or span.end is None:
        return False
    return any(a <= span.start and span.end <= b for a, b in covered_lines(section))


async def read_sources(
    git: Any, repo: str | None, sha: str, refs: Iterable[str]
) -> dict[str, str | None] | None:
    """Тексты файлов тестов AC на закреплённом sha; ``None`` вместо текста — не прочитан.

    Нет sha или клона — ``None`` целиком. Усечённое чтение не разбирается: обрезанный
    файл мог бы дать чужие границы.
    """
    paths = ac_test_paths(refs)
    if not paths:
        return {}
    if not sha or not repo:
        return None
    out: dict[str, str | None] = {}
    for path in paths:
        try:
            got = await git.read_file_at_ref(repo, sha, path, limit_chars=READ_LIMIT)
        except Exception:  # noqa: BLE001 - "could not look" is an answer, not a crash
            got = {}
        ok = got.get("state") == "present" and not got.get("truncated")
        out[path] = got.get("content", "") if ok else None
    return out


def oversized_paragraph(
    oversized: list[tuple[str, int]], has_ac_tests: bool, cap: int
) -> str:
    """Абзац про файлы выше потолка: порядок чтения и что называть непрочитанным (#1933)."""
    named = ", ".join(f"{path} ({count} строк)" for path, count in oversized)
    order = (
        "Порядок: 1) тесты AC, названные выше строками «ЧИТАТЬ ПЕРВЫМИ» "
        "(номера — строки файла на коммите сдачи; «не найден в диффе» — "
        "найди сам); 2) остальные файлы диффа; 3) остаток большого файла. "
        if has_ac_tests
        else "Порядок: 1) остальные файлы диффа; 2) остаток большого файла. "
    )
    unread = (
        f"; непрочитанный тест AC — отдельной строкой «{UNREAD_PREFIX}"
        "<test_ref>» (test_ref ровно как в скобках блока)"
        if has_ac_tests
        else ""
    )
    return (
        f"НЕ ПОМЕСТЯТСЯ В ОДИН ПРОХОД (потолок {cap} "
        f"строк на файл): {named}. Потолок — порядок чтения, а не предел. "
        f"{order}Второго прохода хаб не заказывает. Непрочитанный остаток "
        f"перечисли в lost_dimensions и сдай incomplete=true{unread}. "
        "Один большой файл не имеет права съесть бюджет, которого ждали "
        "остальные."
    )


def first_block(spans: Sequence[AcTestSpan]) -> str:
    """Блок «ЧИТАТЬ ПЕРВЫМИ»: по строке на тест AC; пусто, если читать нечего."""
    return "\n".join(span.label() for span in spans)


def cutout(span: AcTestSpan) -> str:
    """Функция теста AC вырезкой: строки файла с номерами."""
    body = "\n".join(f"{number}: {text}" for number, text in span.lines)
    return (
        f"=== ВЫРЕЗКА ТЕСТА AC {span.ref} ({span.path}:{span.start}–{span.end}) ===\n"
        f"{body}\n=== КОНЕЦ ВЫРЕЗКИ ===\n"
    )


def cutouts_within(
    spans: Sequence[AcTestSpan], budget: int
) -> tuple[list[str], list[str]]:
    """``(вырезки, непрочитанные ref)`` для тестов, которых нет в промте целиком."""
    taken: list[str] = []
    unread: list[str] = []
    for span in spans:
        text = cutout(span)
        if len(text) <= budget:
            taken.append(text)
            budget -= len(text)
        else:
            unread.append(span.ref)
    return taken, unread


def unread_ac_tests(
    lost_dimensions: Iterable[str], test_refs: Sequence[str]
) -> list[str]:
    """Тесты AC, названные ревьюером непрочитанными, — точным совпадением (#1933).

    Строка ``lost_dimensions`` засчитывается, если она начинается ровно с
    ``UNREAD_PREFIX`` и остаток (без пробелов по краям) равен одному из
    ``test_refs`` задачи. Ответ — в порядке ``test_refs``, без повторов.
    """
    named = {
        line[len(UNREAD_PREFIX) :].strip()
        for line in lost_dimensions
        if isinstance(line, str) and line.startswith(UNREAD_PREFIX)
    }
    return [ref for ref in test_refs if ref in named]


def names_unread_ac_test(lost_dimensions: Iterable[str]) -> bool:
    """Назвал ли ревьюер хоть один непрочитанный тест AC (по префиксу)."""
    return any(
        isinstance(line, str) and line.startswith(UNREAD_PREFIX)
        for line in lost_dimensions
    )


async def unread_for_task(
    db: Any, task_id: int, lost_dimensions: Iterable[str]
) -> list[str]:
    """Тесты AC задачи, названные непрочитанными: точная сверка с её test_ref (#1933).

    Запрос к БД делается только когда в отчёте есть строка с префиксом.
    """
    lost = list(lost_dimensions)
    if not names_unread_ac_test(lost):
        return []
    from hub import repository as repo

    rows = await repo.list_acceptance_criteria(db, task_id)
    return unread_ac_tests(lost, ac_test_refs([dict(r) for r in rows]))


async def attach_ac_tests_unread(db: Any, view: Any) -> None:
    """Заполнить ``view.ac_tests_unread`` из ``lost_dimensions`` и AC задачи."""
    view.ac_tests_unread = await unread_for_task(db, view.task_id, view.lost_dimensions)
