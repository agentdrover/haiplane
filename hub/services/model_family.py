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


#: The pseudo-family :func:`family` returns for an id it does not recognise.
UNKNOWN = "unknown:"


def is_known_family(model_id: str | None) -> bool:
    """Whether this id names a family we can actually reason about (#1008).

    An id we do not recognise is not a family — it is a string. Treating it as
    one is what let the monoculture gate be walked past: see
    :func:`same_family`.
    """
    fam = family(model_id)
    return bool(fam) and not fam.startswith(UNKNOWN)


def same_family(model_a: str | None, model_b: str | None) -> bool | None:
    """True/False when both sides are known enough to compare; None otherwise.

    None means "cannot tell", and it covers two cases that used to be one and
    a half: a MISSING declaration, and an UNRECOGNISED one (#1008).

    The second case was a hole, not a subtlety. ``family()`` maps an id it does
    not know to ``unknown:<id>``, and two different unknown ids are different
    strings — so ``same_family("my-model-42", "grok-4.6")`` answered False, the
    gate read that as "diverse", and the escalation it exists to raise turned
    into a pass. An implementer declaring a garbage string got its work
    auto-approved by claiming to be a model nobody has heard of.

    Absence of data is not diversity — the same direction every unknown
    degrades in this codebase. The caller keeps the human gate instead.
    """
    if not is_known_family(model_a) or not is_known_family(model_b):
        return None
    return family(model_a) == family(model_b)
