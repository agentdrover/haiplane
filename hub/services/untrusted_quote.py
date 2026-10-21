"""Недоверенная цитата: ОДНА функция санитизации выжимки чужого хоста (#1953).

Выжимку критика пишет не хаб: она доезжает в ленту задачи и карточку, где её
читают люди и агенты. Поэтому цитата проходит одно и то же сито везде, где хаб
вставляет чужой текст в свою запись: управляющие символы, форматирующие
(bidi, нулевой ширины, TAG), суррогаты и частная область вычищаются; HTML и
Markdown экранируются, обратная кавычка (code span) нейтрализуется; размер
выхода ограничен без разрыва экранирования посередине. Набор тестовых векторов
общий с #1911 и #1952: ``tests/fixtures/untrusted_quote_vectors.json``.
"""

from __future__ import annotations

import re
import unicodedata

#: Потолок исходного текста: больше хаб не читает (REST отвечает 422 раньше).
RAW_LIMIT = 4000
#: Потолок вывода после экранирования.
OUT_LIMIT = 600

_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_HTML = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;"}
_MARKDOWN = frozenset("\\[]()*_~|#!")
_HOST_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,79}")


def valid_host_ref(value: str) -> bool:
    """Ссылка хоста на свой прогон: короткий токен без пробелов и управляющих."""
    return isinstance(value, str) and _HOST_REF.fullmatch(value) is not None


def _escaped(ch: str) -> str:
    if ch == "`":
        return "&#96;"
    if ch in _HTML:
        return _HTML[ch]
    if ch in _MARKDOWN:
        return "\\" + ch
    return ch


def sanitize_untrusted_quote(
    text: object, *, raw_limit: int = RAW_LIMIT, out_limit: int = OUT_LIMIT
) -> str:
    """Безопасный для ленты вид чужого текста; не-строка и пустое дают ''."""
    if not isinstance(text, str) or not text:
        return ""
    pieces: list[str] = []
    size = 0
    reserve = 1  # место под многоточие
    for ch in text[:raw_limit]:
        if ch in "\r\n\t\v\f":
            ch = " "
        elif unicodedata.category(ch) in _DROPPED_CATEGORIES:
            continue
        if ch == " " and (not pieces or pieces[-1] == " "):
            continue
        piece = _escaped(ch)
        if size + len(piece) > out_limit - reserve:
            pieces.append("…")
            break
        pieces.append(piece)
        size += len(piece)
    return "".join(pieces).strip()
