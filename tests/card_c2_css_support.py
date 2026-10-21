"""Статическая проверка тем карточки (#1812, AC-2): разбор CSS без браузера.

Каскад четырёх контекстов вычисляется здесь, а не угадывается: тёмная тема -
умолчание (``:root``), светлая приходит из ``prefers-color-scheme: light`` (если
страница не закрепила тёмную через ``data-theme``) и из ``data-theme="light"``.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "hub" / "static"
COLOR = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|color-mix\(", re.IGNORECASE
)
LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", re.IGNORECASE)
DECL = re.compile(r"(--[a-zA-Z0-9-]+)\s*:\s*([^;{}]+);")
DECL_NAME = re.compile(r"(--[a-zA-Z0-9-]+)\s*:")
CONTEXTS = ("prefers-light", "prefers-dark", "data-theme-light", "data-theme-dark")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _block(css: str, opener: str) -> str:
    """Тело блока, открытого точной строкой ``opener`` (с учётом вложенных скобок)."""
    at = css.find(opener)
    assert at >= 0, f"в style.css нет блока {opener!r}"
    start = css.index("{", at)
    depth, i = 0, start
    while True:
        depth += css[i] == "{"
        depth -= css[i] == "}"
        if depth == 0:
            return css[start + 1 : i]
        i += 1


def theme_blocks(css: str) -> dict[str, dict[str, str]]:
    css = _strip_comments(css)
    dark = _block(css, "\n:root {")
    media = _block(css, "@media (prefers-color-scheme: light)")
    light_pref = _block(media, ':root:not([data-theme="dark"])')
    light_attr = _block(css, ':root[data-theme="light"]')
    return {
        name: dict(DECL.findall(body))
        for name, body in (
            ("dark", dark),
            ("light-pref", light_pref),
            ("light-attr", light_attr),
        )
    }


def resolve(blocks: dict[str, dict[str, str]], context: str) -> dict[str, str]:
    """Значения токенов в одном из четырёх контекстов (последнее слово - каскад)."""
    out = dict(blocks["dark"])
    if context == "prefers-light":
        out.update(blocks["light-pref"])
    elif context == "data-theme-light":
        out.update(blocks["light-attr"])
    return out


def color_tokens(tokens: dict[str, str]) -> set[str]:
    return {k for k, v in tokens.items() if COLOR.search(v)}


def check_style_tokens_both_themes() -> None:
    css = (STATIC / "style.css").read_text()
    blocks = theme_blocks(css)
    dark_colors = color_tokens(blocks["dark"])
    assert len(dark_colors) >= 30, "guard: токены тёмной темы не разобраны"
    for name in ("light-pref", "light-attr"):
        light_colors = color_tokens(blocks[name])
        only_dark = dark_colors - light_colors
        assert not only_dark, f"цвет только в тёмной теме ({name}): {sorted(only_dark)}"
        only_light = light_colors - dark_colors
        assert not only_light, f"цвет только в светлой ({name}): {sorted(only_light)}"
    assert blocks["light-pref"] == blocks["light-attr"], "две светлые темы расходятся"
    # Четыре контекста дают полный набор, и светлые значения не равны тёмным.
    sets = {c: resolve(blocks, c) for c in CONTEXTS}
    assert sets["prefers-dark"] == sets["data-theme-dark"] == blocks["dark"]
    assert sets["prefers-light"] == sets["data-theme-light"]
    differing = [
        k for k in dark_colors if sets["prefers-light"][k] != sets["prefers-dark"][k]
    ]
    assert len(differing) >= len(dark_colors) * 0.8, "светлая тема почти не отличается"
    # Правила карточки: цвет только через токен, литерала нет.
    card = _strip_comments((STATIC / "card_c2.css").read_text())
    rules_only = re.sub(r"(--[a-zA-Z0-9-]+)\s*:[^;{}]+;", "", card)
    assert not LITERAL.search(rules_only), "в card_c2.css цветовой литерал вне токена"
    used = set(re.findall(r"var\(\s*(--[a-zA-Z0-9-]+)", card))
    declared = set(blocks["dark"]) | set(DECL_NAME.findall(card))
    assert used <= declared, (
        f"card_c2.css берёт необъявленные токены: {sorted(used - declared)}"
    )
