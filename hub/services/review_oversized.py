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
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hub.services.test_locator import PYTEST, runner_of

#: Префикс строки ``lost_dimensions``, которой ревьюер называет непрочитанный тест AC.
UNREAD_PREFIX = "ac_test_unread: "

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(")
_CLASS = re.compile(r"^(\s*)class\s+(\w+)\b")
_PARAM = re.compile(r"\[.*\]$")

NOT_FOUND = "не найден в диффе"


@dataclass(frozen=True)
class AcTestSpan:
    """Где в диффе лежит тест AC: ``start``/``end`` — строки файла, или ``None``."""

    ref: str
    path: str
    start: int | None = None
    end: int | None = None
    lines: tuple[tuple[int, str], ...] = ()

    @property
    def found(self) -> bool:
        return self.start is not None

    def label(self) -> str:
        """Строка блока «ЧИТАТЬ ПЕРВЫМИ» для этого теста."""
        if not self.found:
            return f"ЧИТАТЬ ПЕРВЫМИ: {self.ref} — {NOT_FOUND}"
        return f"ЧИТАТЬ ПЕРВЫМИ: {self.path}:{self.start}–{self.end} ({self.ref})"


def ac_test_refs(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """``test_ref`` AC с ``verifiable_by=test``, в порядке AC, без повторов."""
    seen: list[str] = []
    for row in rows:
        ref = (row.get("test_ref") or "").strip()
        if str(row.get("verifiable_by") or "") == "test" and ref and ref not in seen:
            seen.append(ref)
    return seen


def _new_side(section: str) -> list[tuple[int, str]]:
    """Строки новой стороны секции файла: ``[(номер строки файла, текст)]``.

    Тело ханка читается по его счётчикам, как у ``diff_scan``: строка ``+++``
    внутри тела — добавленная строка, а не заголовок.
    """
    out: list[tuple[int, str]] = []
    lines = section.splitlines()
    i = 0
    while i < len(lines):
        m = _HUNK.match(lines[i])
        i += 1
        if not m:
            continue
        old = int(m.group(1)) if m.group(1) is not None else 1
        new = int(m.group(3)) if m.group(3) is not None else 1
        at = int(m.group(2))
        while i < len(lines) and (old > 0 or new > 0):
            line = lines[i]
            i += 1
            tag, text = line[:1], line[1:]
            if tag == "+":
                out.append((at, text))
                at += 1
                new -= 1
            elif tag == "-":
                old -= 1
            elif tag == "\\":
                continue
            else:
                out.append((at, text))
                at += 1
                old -= 1
                new -= 1
    return out


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip())


def _signature_end(rows: Sequence[tuple[int, str]], at: int) -> int:
    """Индекс последней строки сигнатуры, начатой в ``at`` (скобки могут быть многострочные)."""
    depth = 0
    for k in range(at, len(rows)):
        if k > at and rows[k][0] != rows[k - 1][0] + 1:
            return k - 1
        text = rows[k][1]
        depth += sum(text.count(c) for c in "([{") - sum(text.count(c) for c in ")]}")
        if depth <= 0 and text.rstrip().endswith(":"):
            return k
    return len(rows) - 1


def _block_end(rows: Sequence[tuple[int, str]], at: int) -> int:
    """Индекс последней непустой строки блока, чей заголовок стоит в ``at``."""
    base = _indent(rows[at][1])
    last = _signature_end(rows, at)
    for k in range(last + 1, len(rows)):
        number, text = rows[k]
        if number != rows[k - 1][0] + 1:
            break  # дальше дифф не видел строк: конец известного
        if text.strip() and _indent(text) <= base:
            break
        if text.strip():
            last = k
    return last


def _find(
    rows: Sequence[tuple[int, str]], owner: str, name: str
) -> tuple[int, int] | None:
    """``(индекс def, индекс конца)`` функции ``name`` (в классе ``owner``, если задан)."""
    lo, hi, floor = 0, len(rows), -1
    if owner:
        for k, (_, text) in enumerate(rows):
            m = _CLASS.match(text)
            if m and m.group(2) == owner:
                lo, floor = k + 1, len(m.group(1))
                hi = len(rows)
                for j in range(k + 1, len(rows)):
                    if rows[j][1].strip() and _indent(rows[j][1]) <= floor:
                        hi = j
                        break
                break
        else:
            return None
    for k in range(lo, hi):
        m = _DEF.match(rows[k][1])
        if m and m.group(2) == name and (not owner or len(m.group(1)) > floor):
            return k, _block_end(rows, k)
    return None


def _parse_ref(ref: str) -> tuple[str, str, str] | None:
    """``(файл, класс или "", функция)`` pytest-ссылки; параметр ``[id]`` отброшен."""
    if runner_of(ref) != PYTEST:
        return None
    path, _, node = ref.partition("::")
    parts = [_PARAM.sub("", part) for part in node.split("::") if part]
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
        found = None
        if parsed and text is not None:
            rows = _new_side(text)
            hit = _find(rows, parsed[1], parsed[2])
            if hit is not None:
                begin, end = hit
                found = AcTestSpan(
                    ref,
                    parsed[0],
                    rows[begin][0],
                    rows[end][0],
                    tuple(rows[begin : end + 1]),
                )
        out.append(found or AcTestSpan(ref, parsed[0] if parsed else ""))
    return out


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
    spans: Sequence[AcTestSpan], omitted: Collection[str], budget: int
) -> tuple[list[str], list[str]]:
    """``(вырезки, непрочитанные ref)`` для тестов в файлах, что не вошли в промт."""
    taken: list[str] = []
    unread: list[str] = []
    for span in spans:
        if not span.found or span.path not in omitted:
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
