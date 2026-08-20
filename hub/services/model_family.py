"""Model-family map for the diversity rule (#758).

Two models are "the same family" when correlated blind spots are likely:
same vendor, same lineage. The map is deliberately prefix-based and
conservative — an id nobody recognises maps to the pseudo-family
``unknown``, and ``unknown`` never matches anything except the exact same
string: when we cannot tell the families apart, the verdict goes to the
human, the same direction every unknown degrades in this codebase.
"""

from __future__ import annotations

# Ordered by PRIORITY: model-name prefixes first, platform wrappers last.
# "cursor-grok-4.6" is a grok run through Cursor — for correlated blind
# spots the underlying model is what matters, so grok (xai) must win over
# the cursor wrapper. Only Cursor's own models (composer) map to cursor.
_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude", "anthropic"),
    ("anthropic", "anthropic"),
    ("gpt", "openai"),
    ("codex", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("grok", "xai"),
    ("gemini", "google"),
    ("deepseek", "deepseek"),
    ("qwen", "alibaba"),
    ("llama", "meta"),
    ("mistral", "mistral"),
    ("composer", "cursor"),
    ("cursor", "cursor"),
)


def family(model_id: str | None) -> str:
    """Vendor family of a model id; 'unknown:<id>' when unrecognised.

    The unknown form embeds the id on purpose: two unrecognised models
    compare as the same family only when they are literally the same
    string — never merely because both are unrecognised.
    """
    raw = (model_id or "").strip().lower()
    if not raw:
        return ""
    # Ids arrive with or without wrappers (cursor-grok-4.6,
    # us.anthropic.claude-…): scan tokens per prefix, prefix priority wins.
    tokens = raw.replace("/", "-").replace(".", "-").split("-")
    for prefix, fam in _PREFIXES:
        if any(token.startswith(prefix) for token in tokens):
            return fam
    return f"unknown:{raw}"


def same_family(model_a: str | None, model_b: str | None) -> bool | None:
    """True/False when both sides are known enough to compare; None when
    either declaration is missing — absence of data is not diversity."""
    fam_a = family(model_a)
    fam_b = family(model_b)
    if not fam_a or not fam_b:
        return None
    return fam_a == fam_b
