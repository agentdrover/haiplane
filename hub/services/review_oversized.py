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

Диапазон, который дифф показывает не целиком (функция обрывается на границе
ханка, а файл не новый), помечен ``partial``: «нашли начало» не выдаётся за
«нашли функцию».
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from hub.services.test_locator import PYTEST, runner_of

#: Префикс строки ``lost_dimensions``, которой ревьюер называет непрочитанный тест AC.
UNREAD_PREFIX = "ac_test_unread: "

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(")
_CLASS = re.compile(r"^(\s*)class\s+(\w+)\b")
_PARAM = re.compile(r"\[.*\]$")

NOT_FOUND = "не найден в диффе"
PARTIAL = "в диффе виден не весь тест"


class Row(NamedTuple):
    """Строка новой стороны диффа: номер в файле, текст, заголовок её ханка."""

    no: int
    text: str
    ctx: str  # то, что git пишет после ``@@``: ближайшая строка без отступа
    eof: bool  # ханк нового файла: конец ханка — конец файла


@dataclass(frozen=True)
class AcTestSpan:
    """Где в диффе лежит тест AC: ``start``/``end`` — строки файла, или ``None``."""

    ref: str
    path: str
    start: int | None = None
    end: int | None = None
    partial: bool = False
    lines: tuple[tuple[int, str], ...] = ()

    @property
    def found(self) -> bool:
        return self.start is not None

    def label(self) -> str:
        """Строка блока «ЧИТАТЬ ПЕРВЫМИ» для этого теста."""
        if not self.found:
            return f"ЧИТАТЬ ПЕРВЫМИ: {self.ref} — {NOT_FOUND}"
        tail = f" — {PARTIAL}" if self.partial else ""
        return f"ЧИТАТЬ ПЕРВЫМИ: {self.path}:{self.start}–{self.end} ({self.ref}){tail}"


def ac_test_refs(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """``test_ref`` AC с ``verifiable_by=test``, в порядке AC, без повторов."""
    seen: list[str] = []
    for row in rows:
        ref = (row.get("test_ref") or "").strip()
        if str(row.get("verifiable_by") or "") == "test" and ref and ref not in seen:
            seen.append(ref)
    return seen


def _new_side(section: str) -> list[Row]:
    """Строки новой стороны секции файла.

    Тело ханка читается по его счётчикам, как у ``diff_scan``: строка ``+++``
    внутри тела — добавленная строка, а не заголовок.
    """
    out: list[Row] = []
    lines = section.splitlines()
    i = 0
    while i < len(lines):
        m = _HUNK.match(lines[i])
        i += 1
        if not m:
            continue
        old = int(m.group(2)) if m.group(2) is not None else 1
        new = int(m.group(4)) if m.group(4) is not None else 1
        eof = m.group(1) == "0" and old == 0
        at, ctx = int(m.group(3)), m.group(5)
        while i < len(lines) and (old > 0 or new > 0):
            line = lines[i]
            i += 1
            tag, text = line[:1], line[1:]
            if tag == "+":
                out.append(Row(at, text, ctx, eof))
                at += 1
                new -= 1
            elif tag == "-":
                old -= 1
            elif tag != "\\":
                out.append(Row(at, text, ctx, eof))
                at += 1
                old -= 1
                new -= 1
    return out


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip())


def _scan(text: str, depth: int) -> tuple[int, bool]:
    """``(глубина скобок в конце строки, был ли двоеточие вне скобок)``.

    Строки в одинарных кавычках и ``#``-комментарий скобок и двоеточий не дают.
    """
    quote, colon = "", False
    prev = ""
    for ch in text:
        if quote:
            if ch == quote and prev != "\\":
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#":
            break
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth <= 0:
            colon = True
        prev = ch
    return depth, colon


def _header_end(rows: Sequence[Row], at: int) -> int:
    """Индекс последней строки заголовка ``def``/``class``, начатого в ``at``."""
    depth = 0
    for k in range(at, len(rows)):
        if k > at and rows[k].no != rows[k - 1].no + 1:
            return k - 1
        depth, colon = _scan(rows[k].text, depth)
        if depth <= 0 and colon:
            return k
    return len(rows) - 1


def _balanced(rows: Sequence[Row]) -> bool:
    depth = 0
    for row in rows:
        depth, _ = _scan(row.text, depth)
    return depth == 0


def _first_line(rows: Sequence[Row], at: int) -> int:
    """Индекс первой строки блока с его декораторами (в том числе многострочными)."""
    base, start, k = _indent(rows[at].text), at, at - 1
    while k >= 0 and rows[k].no == rows[k + 1].no - 1:
        text = rows[k].text
        if _indent(text) == base and text.lstrip().startswith("@"):
            if _balanced(rows[k:at]):
                start = k
        elif (
            text.strip() and _indent(text) <= base and not text.lstrip().startswith(")")
        ):
            break
        elif not text.strip():
            break
        k -= 1
    return start


def _block_end(rows: Sequence[Row], at: int, module_level: bool) -> tuple[int, bool]:
    """``(индекс последней строки блока, показан ли он дифф-ом не целиком)``.

    Блок кончается строкой без отступа глубже его заголовка. Разрыв между ханками
    пересекается только у функции верхнего уровня, когда git назвал её же
    заголовком следующего ханка; иначе блок обрывается и считается частичным.
    Целым он считается, когда его закрыла видимая строка с меньшим отступом или
    когда ханк — целый новый файл.
    """
    base = _indent(rows[at].text)
    name = _DEF.match(rows[at].text)
    last = _header_end(rows, at)
    own = rows[at].text.strip().split("(")[0]
    for k in range(last + 1, len(rows)):
        row = rows[k]
        if row.no != rows[k - 1].no + 1:
            if not (module_level and row.ctx.startswith(own) and name):
                return last, True
        if row.text.strip():
            if _indent(row.text) <= base:
                return last, False
            last = k
    return last, not rows[last].eof


def _body_indent(rows: Sequence[Row], header_last: int) -> int:
    for row in rows[header_last + 1 :]:
        if row.text.strip():
            return _indent(row.text)
    return 0


def _find_def(
    rows: Sequence[Row], lo: int, hi: int, name: str, indent: int | None
) -> int | None:
    for k in range(lo, hi):
        m = _DEF.match(rows[k].text)
        if m and m.group(2) == name and (indent is None or len(m.group(1)) == indent):
            return k
    return None


def _find(rows: Sequence[Row], owner: str, name: str) -> tuple[int, int, bool] | None:
    """``(индекс начала с декораторами, индекс конца, частично ли)`` или ``None``."""
    if not owner:
        at = _find_def(rows, 0, len(rows), name, 0)
        if at is None:
            return None
        end, partial = _block_end(rows, at, True)
        return _first_line(rows, at), end, partial
    for k, row in enumerate(rows):
        m = _CLASS.match(row.text)
        if m and m.group(2) == owner and not m.group(1):
            head = _header_end(rows, k)
            hi = len(rows)
            for j in range(head + 1, len(rows)):
                if rows[j].text.strip() and _indent(rows[j].text) == 0:
                    hi = j
                    break
            at = _find_def(rows, head + 1, hi, name, _body_indent(rows, head))
            break
    else:
        # Класса в диффе нет, но git назвал его заголовком ханка (-U0).
        cls = [
            j
            for j, row in enumerate(rows)
            if (m := _CLASS.match(row.ctx)) and m.group(2) == owner and not m.group(1)
        ]
        at = next(
            (
                found
                for j in cls
                if (found := _find_def(rows, j, j + 1, name, None)) is not None
                and _indent(rows[found].text) > 0
            ),
            None,
        )
    if at is None:
        return None
    end, partial = _block_end(rows, at, False)
    return _first_line(rows, at), end, partial


def _parse_ref(ref: str) -> tuple[str, str, str] | None:
    """``(файл, класс или "", функция)`` pytest-ссылки; параметр ``[id]`` отброшен."""
    if runner_of(ref) != PYTEST:
        return None
    path, _, node = ref.partition("::")
    parts = [part for part in _PARAM.sub("", node).split("::") if part]
    if not parts or len(parts) > 2:
        return None
    return path, (parts[0] if len(parts) == 2 else ""), parts[-1]


def ac_test_spans(
    sections: Sequence[tuple[str, str]], refs: Sequence[str]
) -> list[AcTestSpan]:
    """Диапазоны тестов AC в диффе, в порядке ``refs``; не найденное — без диапазона.

    ``sections`` — ``[(путь, текст файла в диффе)]``. Ссылка не pytest (vitest) и
    функция, которой в диффе нет, остаются в ответе без диапазона: молчание
    выглядело бы как «ревьюеру читать нечего».
    """
    by_path = {path: text for path, text in sections if path}
    out: list[AcTestSpan] = []
    for ref in refs:
        parsed = _parse_ref(ref)
        text = by_path.get(parsed[0]) if parsed else None
        rows = _new_side(text) if parsed and text else []
        hit = _find(rows, parsed[1], parsed[2]) if parsed and rows else None
        if hit is None or parsed is None:
            out.append(AcTestSpan(ref, parsed[0] if parsed else ""))
            continue
        begin, end, partial = hit
        body = tuple((r.no, r.text) for r in rows[begin : end + 1])
        out.append(
            AcTestSpan(ref, parsed[0], rows[begin].no, rows[end].no, partial, body)
        )
    return out


def first_block(spans: Sequence[AcTestSpan]) -> str:
    """Блок «ЧИТАТЬ ПЕРВЫМИ»: по строке на тест AC; пусто, если читать нечего."""
    return "\n".join(span.label() for span in spans)


def cutout(span: AcTestSpan) -> str:
    """Функция теста AC вырезкой: строки файла с номерами, разрывы названы."""
    out: list[str] = []
    prev = None
    for number, text in span.lines:
        if prev is not None and number != prev + 1:
            out.append("…")
        out.append(f"{number}: {text}")
        prev = number
    tail = f" [{PARTIAL}]" if span.partial else ""
    return (
        f"=== ВЫРЕЗКА ТЕСТА AC {span.ref} ({span.path}:{span.start}–{span.end})"
        f"{tail} ===\n" + "\n".join(out) + "\n=== КОНЕЦ ВЫРЕЗКИ ===\n"
    )


def cutouts_within(
    spans: Sequence[AcTestSpan], absent: Collection[str], budget: int
) -> tuple[list[str], list[str]]:
    """``(вырезки, непрочитанные ref)`` для тестов в файлах, которых нет в промте.

    ``absent`` — файлы, что не вошли в потолок или выпали из дельты. Тест, чей
    файл в промте есть целиком, вырезки не просит.
    """
    taken: list[str] = []
    unread: list[str] = []
    for span in spans:
        if not span.found or span.path not in absent:
            continue
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


async def attach_ac_tests_unread(db: Any, view: Any) -> None:
    """Заполнить ``view.ac_tests_unread`` из ``lost_dimensions`` и AC задачи.

    Запрос к БД делается только когда в отчёте есть строка с префиксом: у
    остальных отчётов поле честно пустое, и очередь ревью не платит за него.
    """
    if not names_unread_ac_test(view.lost_dimensions):
        view.ac_tests_unread = []
        return
    from hub import repository as repo

    rows = await repo.list_acceptance_criteria(db, view.task_id)
    view.ac_tests_unread = unread_ac_tests(
        view.lost_dimensions, ac_test_refs([dict(r) for r in rows])
    )
