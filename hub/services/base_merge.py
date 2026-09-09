"""Мерж базы в ветку: что он стоит человеку, и что гейт вправе решить сам (#1233).

09.09.2026 #1204 была одобрена в 05:48 и через четырнадцать секунд получила
merge_failed: конфликт с только что доставленной #1205 в трёх файлах. Задача
ушла в needs_decision, исполнитель слил базу, разрешил конфликты и пересдался —
и тот же человек должен одобрить ту же работу второй раз. Один мерж стоил двух
вердиктов.

Здесь живут два ПРАВИЛА, оба механические, и оба нарочно узкие.

1. ВЕРДИКТ ПЕРЕЖИВАЕТ МЕРЖ БАЗЫ, если авторская работа не изменилась. Признак —
   ``git diff <base>...<tip>`` до мержа и после совпадает байт в байт. Никакого
   доверия к сообщению коммита: сообщение — это заявление, а дифф — наблюдение.

2. АВТОМЕРЖ ТОЛЬКО ОДНОГО КЛАССА КОНФЛИКТА: обе стороны лишь ДОБАВИЛИ строки в
   конец одного файла, ничего общего не тронув. Признак берётся у самого git:
   в разметке diff3 у такого конфликта ПУСТАЯ секция общего предка — значит,
   ни одна сторона не переписала ни одной существующей строки. Всё остальное,
   включая смысловой случай #1204, остаётся человеку.

Порядок разрешения — «сначала наше, потом привезённое» — не косметика: авторский
блок остаётся на своём месте, а привезённое дописывается за ним. Ничего чужого
поверх авторского кода не ложится.

Правило 1 НЕ покрывает случай правила 2, и это проверено делом
(tests/test_delivery_gate.py, настоящий git): после мержа базы в тот же файл
``git diff base...tip`` меняется даже при неизменной авторской работе — другой
блоб базы даёт другую строку ``index``, а вставка базы сдвигает заголовки
ханков. Поэтому вердикт после АВТОмержа держится не на сравнении диффов, а на
том, что коммит сделал сам гейт: он перезакрепляет коммит сдачи и говорит об
этом вслух. Сравнение диффов остаётся для чужих мержей, где ручаться не за что.
"""

from __future__ import annotations

# Классы конфликта. ``unresolvable`` — это не «плохо», а «не наш класс»: гейт
# зовёт человека и называет причину, как и раньше.
TAIL_ADDITIONS = "tail_additions"
UNRESOLVABLE = "unresolvable"

_OURS = "<<<<<<<"
_BASE = "|||||||"
_THEIRS = "======="
_END = ">>>>>>>"


def author_diff_unchanged(before: str | None, after: str | None) -> tuple[bool, str]:
    """Осталась ли авторская работа той же после мержа базы (#1233, AC-2).

    ``before`` и ``after`` — ``git diff <base>...<tip>`` до мержа и после.
    Возвращает ``(сохранён, причина)``.

    ``None`` — это «посмотреть не удалось», и оно НЕ засчитывается за совпадение.
    Иначе недоступный git молча продлевал бы вердикт на код, которого никто не
    видел, — ровно та подмена молчания наблюдением, ради которой #572 вообще
    завёл закрепление коммита.
    """
    if before is None or after is None:
        return False, "дифф ветки к базе прочитать не удалось — сверять нечего"
    if before != after:
        return False, "дифф ветки к базе изменился — в ветке есть авторская правка"
    return True, "дифф ветки к базе не изменился: мерж привёз только базу"


def _regions(text: str) -> list[tuple[int, int]] | None:
    """Границы конфликтных участков (индексы строк ``<<<<<<<`` и ``>>>>>>>``).

    ``None``, если разметка не разобралась: вложенный или оборванный маркер —
    это повод позвать человека, а не угадывать.
    """
    lines = text.splitlines()
    out: list[tuple[int, int]] = []
    start = -1
    for i, line in enumerate(lines):
        if line.startswith(_OURS):
            if start >= 0:
                return None
            start = i
        elif line.startswith(_END):
            if start < 0:
                return None
            out.append((start, i))
            start = -1
    if start >= 0:
        return None
    return out


def classify_conflict(text: str) -> tuple[str, str]:
    """Класс конфликта одного файла по разметке diff3 (#1233, AC-3/AC-4).

    Возвращает ``(класс, причина)``. ``TAIL_ADDITIONS`` — единственный класс,
    который гейт разрешает сам: ровно один участок, пустая секция общего предка
    (обе стороны только добавляли) и участок стоит в конце файла. Причина
    заполнена всегда, в том числе у разрешимого класса: «что именно сложено»
    обязано быть видно в карточке, а не выводиться читателем.
    """
    regions = _regions(text)
    if regions is None:
        return UNRESOLVABLE, "разметку конфликта не удалось разобрать"
    if len(regions) != 1:
        return UNRESOLVABLE, (
            f"участков конфликта {len(regions)}, а автомерж умеет только один"
        )
    lines = text.splitlines()
    start, end = regions[0]
    body = lines[start + 1 : end]
    if not any(ln.startswith(_BASE) for ln in body):
        # Без секции общего предка нельзя доказать, что ничего не переписано.
        return UNRESOLVABLE, "конфликт записан без общего предка (нужна разметка diff3)"
    ancestor_at = next(i for i, ln in enumerate(body) if ln.startswith(_BASE))
    theirs_at = next((i for i, ln in enumerate(body) if ln.startswith(_THEIRS)), -1)
    if theirs_at < ancestor_at:
        return UNRESOLVABLE, "разметку конфликта не удалось разобрать"
    ancestor = body[ancestor_at + 1 : theirs_at]
    if [ln for ln in ancestor if ln.strip()]:
        return UNRESOLVABLE, (
            "обе стороны правят одни и те же строки — это разрешает человек"
        )
    if [ln for ln in lines[end + 1 :] if ln.strip()]:
        return UNRESOLVABLE, "конфликт не в конце файла — это разрешает человек"
    ours = body[:ancestor_at]
    theirs = body[theirs_at + 1 :]
    return TAIL_ADDITIONS, (
        f"хвостовые добавления: {len(ours)} строк из ветки и {len(theirs)} "
        f"из базы, ничего не переписано"
    )


def resolve_tail_additions(text: str) -> str | None:
    """«Оставить оба» для класса ``TAIL_ADDITIONS``; ``None`` для всех прочих.

    Наше идёт первым — см. модульную строку: этот порядок сохраняет смещение
    авторского блока, а с ним и вердикт.
    """
    if classify_conflict(text)[0] != TAIL_ADDITIONS:
        return None
    lines = text.splitlines()
    regions = _regions(text) or []
    start, end = regions[0]
    body = lines[start + 1 : end]
    ancestor_at = next(i for i, ln in enumerate(body) if ln.startswith(_BASE))
    theirs_at = next(i for i, ln in enumerate(body) if ln.startswith(_THEIRS))
    merged = (
        lines[:start] + body[:ancestor_at] + body[theirs_at + 1 :] + lines[end + 1 :]
    )
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(merged) + trailing


def plan_resolution(files: dict[str, str]) -> tuple[dict[str, str], str]:
    """Разрешение всего конфликта целиком, или отказ с названной причиной.

    Возвращает ``(разрешения, причина)``. Непустая причина при пустых
    разрешениях — это отказ: класс не наш, задача идёт к человеку. Правило
    «целиком»: хватает ОДНОГО файла вне класса, чтобы не трогать ни один, —
    полуразрешённый мерж хуже неразрешённого.
    """
    if not files:
        return {}, "конфликтующих файлов не названо"
    resolutions: dict[str, str] = {}
    notes: list[str] = []
    for path in sorted(files):
        kind, why = classify_conflict(files[path])
        if kind != TAIL_ADDITIONS:
            return {}, f"{path}: {why}"
        merged = resolve_tail_additions(files[path])
        if merged is None:  # pragma: no cover - classify уже сказал «да»
            return {}, f"{path}: разрешение не собралось"
        resolutions[path] = merged
        notes.append(f"{path} — {why}")
    return resolutions, "; ".join(notes)
