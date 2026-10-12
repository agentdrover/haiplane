from __future__ import annotations

from httpx import AsyncClient

from tests.web_support import (
    _form_carries_policy_version,  # noqa: F401
)


# ---------------------------------------------------------------------------
# The stylesheet cannot reference what does not exist (#622)
# ---------------------------------------------------------------------------

# The debt list that used to live here held seven --clr-* names plus
# --radius-xl, and it was self-cleaning: the test asserted each entry was STILL
# undeclared. #624 fixed all seven, so that assertion failed and named every one
# of them — which is the whole point of writing an exception list by name and
# forcing it to be revisited. The list is gone because it is empty; no exception
# survives here silently.


def _stylesheet() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "hub" / "static" / "style.css"
    ).read_text()


async def test_every_css_token_and_utility_class_exists():
    # AC-1 (#622). This is the check whose ABSENCE let the defect exist: .muted
    # and .small were used 28 times across six templates while no rule defined
    # them, so every "quiet" caption rendered at body colour and size — the least
    # important text on the screen louder than the most important. And I myself
    # wrote var(--muted, #999), inventing a token that was never declared.
    import re
    from pathlib import Path

    css = _stylesheet()
    used = set(re.findall(r"var\(\s*(--[a-zA-Z0-9-]+)", css))
    declared = set(re.findall(r"^\s*(--[a-zA-Z0-9-]+)\s*:", css, re.M))
    assert used and declared, "guard the guard: an empty parse agrees with anything"

    # No allowlist any more (#624 emptied it). A var() that resolves to nothing
    # invalidates the whole declaration, so an undeclared token does not shift a
    # colour — it deletes the border, the background, the radius.
    undeclared = used - declared
    assert not undeclared, (
        f"CSS references tokens that do not exist: {sorted(undeclared)}"
    )

    # Utility classes used in templates must have a rule. Only the ones this task
    # is about; a general class linter is a different job.
    templates = Path(__file__).resolve().parent.parent / "hub" / "templates"
    for utility in ("muted", "small"):
        used_in = [
            p.name
            for p in templates.rglob("*.html")
            if re.search(rf'class="[^"]*\b{utility}\b', p.read_text())
        ]
        assert used_in, f"precondition: .{utility} is used somewhere"
        assert re.search(rf"^\s*\.{utility}\b[^{{]*{{", css, re.M), (
            f".{utility} is used in {used_in} but no CSS rule defines it"
        )


async def test_workspace_error_shows_its_reason_visibly(client: AsyncClient, db):
    # AC-3 (#622). #568 kept the failure in the header, which was right; what it
    # missed is that the CAUSE lived only in a title attribute — invisible on a
    # phone, unread by screen readers on touch. And the green "ws ok" left: a
    # state that needs no action is not news.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    broken = await repo_module.create_project(
        db, slug="broken", name="Broken", repo_name="owner/x"
    )
    healthy = await repo_module.create_project(
        db, slug="healthy", name="Healthy", repo_name="owner/y"
    )
    await db.execute(
        "UPDATE projects SET provision_status='error', provision_detail='клон не удался: нет ключа' WHERE id=?",
        (broken,),
    )
    await db.execute(
        "UPDATE projects SET provision_status='ok', provision_detail='всё на месте' WHERE id=?",
        (healthy,),
    )
    await db.commit()

    page = (await client.get("/projects")).text
    assert "workspace: ошибка" in page
    assert "клон не удался: нет ключа" in page, (
        "the cause must be visible text, not a title attribute"
    )
    assert "ws&nbsp;ok" not in page and "ws ok" not in page, (
        "'everything is fine' is not news and does not belong in the header"
    )
    assert "всё на месте" not in page, "a healthy workspace needs no explanation"


async def test_projects_page_speaks_one_language(client: AsyncClient, db):
    # AC-4 (#622): a Russian body with English chrome makes the reader translate
    # while scanning. And the footnote used to sit ABOVE the h1 — the first tab
    # stop answered a question the arriving reader does not have yet.
    from hub.db import seed_default_project

    await seed_default_project(db)
    page = (await client.get("/projects")).text

    assert '<h1 class="page-title">Проекты' in page
    for english in (
        "Provision<",
        "Activate<",
        ">Archive<",
        ">edit<",
        ">Save<",
        "New Project<",
    ):
        assert english not in page, f"English chrome left on a Russian page: {english}"

    body = page.split("<main", 1)[1]
    h1_at = body.index("page-title")
    note_at = body.index("Без эпика") if "Без эпика" in body else h1_at + 1
    assert h1_at < note_at, "the section title must come before its footnote"


async def test_card_uses_the_dashboard_chip_pattern(client: AsyncClient, db):
    # AC-2 (#622): the same visual language the owner already reads on the
    # dashboard, not a second one invented for the same meaning. And zero is
    # dimmed rather than shouted.
    from hub.db import seed_default_project

    await seed_default_project(db)
    page = (await client.get("/projects")).text

    assert "topbar-stat-label" in page and "topbar-stat-value" in page
    assert "is-zero" in page, "a nothing must be dimmed, not equal to a number"
    assert "·" not in page.split("topbar-stats", 1)[1][:600], (
        "separators and nested brackets are what made the line unreadable"
    )
    # Screen readers list links out of context: five identical "в работе: 0" told
    # the user nothing about which project they belonged to.
    assert 'aria-label="Ждёт вашего решения:' in page


# ---------------------------------------------------------------------------
# A dark interface carries no light-theme patches, and a rule that never
# applies is not a rule (#624)
# ---------------------------------------------------------------------------

# Backgrounds are what gives a stolen palette away. This stylesheet
# legitimately hardcodes vivid accents for :hover — #00d2a4 at 0.49 relative
# luminance, #fbb034 at 0.52 — and white text on accent fills. Those are
# decisions. What it must not carry is a light SURFACE: the five blocks this
# task fixed sat at 0.74–0.90, i.e. nearly white, copied out of a light theme
# into a dark interface. 0.6 separates the two groups with room on both sides,
# so the threshold is not a knob tuned until the test passed.
_MAX_BACKGROUND_LUMINANCE = 0.6


# The five rules whose colours came from a light palette instead of the token
# system. Named, not counted: the statement said "one such block" because I had
# looked at one, and a number nobody can check is how that error survived.
_ONCE_LIGHT_THEME_RULES = (
    ".admin-warning",
    ".admin-notice",
    ".badge-active",
    ".badge-disabled",
    ".stat-card-warn",
)


# AC-3 forbids inventing tokens to fix these blocks — the semantic pairs
# (--orange/--orange-dim and friends) already exist. If a later task adds a
# token on purpose, update this number and say why; the point is that nobody
# adds one by accident while recolouring a warning.
_DECLARED_TOKEN_COUNT = 50


def _relative_luminance(colour: str) -> float:
    """WCAG relative luminance of a #rgb / #rrggbb colour."""
    digits = colour.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    channels = [int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _css_rules(css: str) -> list[tuple[str, str]]:
    """Flat (selector, body) pairs. @media wrappers surface as their own
    pseudo-rule, and the rules inside them are still seen — enough for a
    stylesheet with no deeper nesting."""
    import re

    return [
        (m.group(1).strip(), m.group(2))
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
    ]


async def test_no_light_theme_hardcodes_in_a_dark_interface():
    # AC-3 (#624). The statement claimed ONE light-theme block. Counting by
    # luminance instead of by memory found FIVE, every one of them rendered on a
    # real page: an admin warning, an admin notice, two key badges and the warning
    # stat card. A near-white panel in a dark interface does not read as
    # "attention" — it reads as a rendering failure.
    import re

    css = _stylesheet()
    rules = _css_rules(css)
    assert rules, "guard the guard: an empty parse agrees with anything"

    too_light = []
    for selector, body in rules:
        for declaration in re.finditer(r"\bbackground(?:-color)?\s*:\s*([^;}]*)", body):
            for colour in re.findall(
                r"#[0-9a-fA-F]{3}\b|#[0-9a-fA-F]{6}\b", declaration.group(1)
            ):
                if _relative_luminance(colour) > _MAX_BACKGROUND_LUMINANCE:
                    too_light.append(
                        f"{selector} → {colour} ({_relative_luminance(colour):.2f})"
                    )
    assert not too_light, "light-theme surfaces inside a dark interface: " + "; ".join(
        too_light
    )

    # The fix had to come from the token system, not from a better-looking hex.
    for selector in _ONCE_LIGHT_THEME_RULES:
        bodies = [
            b
            for sel, b in rules
            if re.search(rf"(^|,|\s){re.escape(selector)}(\s|,|$)", sel)
        ]
        assert bodies, f"precondition: {selector} still exists to be checked"
        for body in bodies:
            leftovers = re.findall(r"#[0-9a-fA-F]{3,6}\b", body)
            assert not leftovers, (
                f"{selector} still hardcodes {leftovers} — the token pairs "
                "(--orange/--orange-dim and friends) are what makes it a theme"
            )

    root = re.search(r":root\s*\{([^}]*)\}", css, re.S)
    assert root, "precondition: the token block exists"
    declared = re.findall(r"^\s*(--[a-zA-Z0-9-]+)\s*:", root.group(1), re.M)
    assert len(declared) == _DECLARED_TOKEN_COUNT, (
        f"token count changed to {len(declared)}: recolouring five blocks must not "
        "invent tokens — if a token was added on purpose, update _DECLARED_TOKEN_COUNT"
    )


async def test_small_badge_size_actually_applies():
    # AC-4 (#624). .badge--xs asked for 9px and every one of its 17 uses rendered
    # at 11px. Not specificity — both selectors are a single class, so the FILE
    # ORDER decided it and .badge, sitting 1200 lines lower, won. The fix is the
    # order; raising specificity would have left the same trap for the next
    # person, who would also read the rule and believe it.
    import re
    from pathlib import Path

    css = _stylesheet()
    base = [m.start() for m in re.finditer(r"^\.badge\s*\{", css, re.M)]
    modifier = [m.start() for m in re.finditer(r"^\.badge--xs\s*\{", css, re.M)]
    assert len(base) == 1, f"precondition: one .badge rule, found {len(base)}"
    assert len(modifier) == 1, (
        f"precondition: one .badge--xs rule, found {len(modifier)}"
    )

    base_body = re.match(r"^\.badge\s*\{([^}]*)\}", css[base[0] :], re.S)
    assert base_body and "font-size" in base_body.group(1), (
        "precondition: .badge is the rule that sets the competing font-size"
    )
    assert modifier[0] > base[0], (
        "equal specificity means source order decides: .badge--xs must come "
        "AFTER .badge or its font-size is decoration in a text file"
    )
    modifier_body = re.match(r"^\.badge--xs\s*\{([^}]*)\}", css[modifier[0] :], re.S)
    assert modifier_body and "font-size: 9px" in modifier_body.group(1), (
        "the compact badge keeps the size its author wrote"
    )

    # Dead classes are removed, not fixed: a class no template uses cannot be
    # verified by looking at the product, so it rots into a second answer to a
    # question that already has one.
    templates = Path(__file__).resolve().parent.parent / "hub" / "templates"
    markup = "\n".join(p.read_text() for p in templates.rglob("*.html"))
    for dead in (".btn--xs", ".badge-locked"):
        # re.escape already escapes the leading dot; adding another backslash made
        # this pattern hunt for a literal backslash and match nothing, so the
        # assertion passed no matter what. A mutation putting .btn--xs back is what
        # exposed it — the test had been agreeing with everything.
        assert not re.search(rf"^{re.escape(dead)}\s*[,{{]", css, re.M), (
            f"{dead} is a second answer to a question .btn-xs already answers — "
            "delete it instead of styling it"
        )
    assert re.search(r"^\.btn-xs\s*\{", css, re.M), (
        "the surviving small button keeps its rule"
    )
    assert 'class="btn btn-xs' in markup or "btn-xs" in markup, (
        "precondition: .btn-xs is the one templates actually use"
    )


async def test_the_keyboard_reaches_the_content_before_the_menu(
    client: AsyncClient, db
):
    # AC-4 (#623). Measured before this task: the first focus stop on EVERY page
    # was "Dashboard", and six sidebar links stood between the keyboard and the
    # content. The fix is a link that is invisible until focused — it costs a
    # mouse user nothing and saves everyone else six tab presses per page.
    import re

    from hub.db import seed_default_project

    await seed_default_project(db)
    page = (await client.get("/projects")).text

    focusable = re.findall(r"<(?:a\s[^>]*href|button|summary|input)[^>]*>", page)
    assert focusable, "precondition: the page has focusable elements at all"
    assert "skip-link" in focusable[0], (
        f"the skip link must be the FIRST focus stop, not somewhere after the "
        f"menu; first was {focusable[0][:80]}"
    )

    target = re.search(r'class="skip-link" href="#([\w-]+)"', page)
    assert target, "the skip link must point somewhere"
    assert f'id="{target.group(1)}"' in page, (
        f"#{target.group(1)} does not exist on the page — a skip link that lands "
        "nowhere is worse than none, because the keyboard user loses their place"
    )
