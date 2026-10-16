"""Regression guard for gate-identity claims in docs/comments (#1708).

After the W4/Л1 gate-identity change (16.10.2026) ``agentdrover/haiplane``
runs three GitHub rulesets and the gate merges to ``main``/``develop`` under
the ``haiplane-gate`` machine account. Several docs, comments and docstrings
still claimed the opposite (hub token read-only, rulesets unavailable,
bypass open to any Write-role writer). This file has no behavior under
test: it scans the retired-claim files as text so a later edit cannot
silently reintroduce the false claims.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INVARIANTS = REPO_ROOT / "docs" / "agent-context" / "invariants.md"

# Two separate events, not one: the Write-role bypass on "green CI before
# merge" was removed on 07.10.2026 (#1426); the new "only the gate updates
# integration branches" ruleset was added later and separately, on
# 16.10.2026 (#1708). Review on 218ae4c8 found these dates conflated into a
# single event; review on 0021fa3e found that checking only "both dates
# appear somewhere in the file" let a place attach the WRONG date to an
# event (deploy/CD.md:364 dated the no-bypass state 16.10 instead of 07.10;
# the spec file was missing from this check entirely). So each entry below
# is a (file, exact phrase pairs) map: every pair's two phrases must both be
# present, each anchoring its own date to its own event at that one place in
# the text — not just "both date strings occur somewhere in the file".
DATE_PLACES: dict[Path, list[tuple[str, str]]] = {
    INVARIANTS: [
        (
            "ruleset «green CI before merge» снят 07.10.2026 (#1426)",
            "ruleset «only the gate updates integration branches» (id "
            "25216047, названный выше) добавлен позже, 16.10.2026.",
        ),
    ],
    REPO_ROOT / "docs" / "workspace-safety-policy.md": [
        (
            "07.10.2026 роль Write убрали из bypass «green CI before merge» без замены",
            "16.10.2026, позже и отдельно от снятия роли Write 07.10.2026, "
            "на `agentdrover/haiplane` добавился третий ruleset",
        ),
    ],
    REPO_ROOT / "docs" / "hub-workflows-map.html": [
        (
            "07.10.2026 роль Write убрали из этого bypass без замены",
            "16.10.2026, добавился третий ruleset",
        ),
    ],
    REPO_ROOT / "deploy" / "CD.md": [
        (
            "до 07.10.2026: роль Write убрали из bypass без замены (#1426)",
            "позже, 16.10.2026, добавился новый ruleset «only the gate "
            "updates integration",
        ),
        (
            "с 07.10.2026 не имеет обхода вовсе (роль Write убрали из bypass, #1426)",
            "Отдельно, 16.10.2026, добавился ruleset «only the gate "
            "updates integration",
        ),
    ],
    REPO_ROOT / "hub" / "integrations" / "git_ops.py": [
        (
            "before 07.10.2026, when the Write role was dropped from the bypass",
            "on 16.10.2026 (#1708), a narrower ruleset was added",
        ),
    ],
    REPO_ROOT / "hub" / "services" / "release.py": [
        (
            "before 07.10.2026, when the Write role was dropped from the bypass",
            "on 16.10.2026 (#1708), a narrower ruleset was added",
        ),
    ],
    REPO_ROOT / "docs" / "specs" / "orchestrator-executor-environment.md": [
        (
            "до #1426, до 07.10.2026) обход первого ruleset хаб использовал сам",
            "Отдельно, 16.10.2026, добавился новый ruleset 25216047 "
            "«only the gate updates in",
        ),
        (
            "#1426 выполнена 07.10.2026: возврат main→develop идёт PR-ом",
            "Отдельно, 16.10.2026, добавился новый ruleset 25216047 "
            "«only the gate updates in",
        ),
    ],
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalize(text: str) -> str:
    """Collapse whitespace and strip markdown emphasis markers.

    A line-wrap must not break a substring check, and neither must bold/italic
    markup: the original false claim was literally ``rulesets **недоступны**``
    (bold only around the second word), which a plain ``rulesets недоступны``
    substring check does not match with the ``**`` still in place — found by
    the Codex critic on f8710589.
    """
    return " ".join(text.replace("**", "").replace("*", "").split())


def _normalized(path: Path) -> str:
    return _normalize(_text(path))


def test_invariant_names_the_gate_and_its_bypass() -> None:
    """Checks exclusivity wording, not just keyword presence.

    A prior version of this test only checked that "haiplane-gate" and
    "GitHub Actions" appear somewhere in the text — an edit that widened the
    bypass to every writer, or accepted status checks from any application,
    would still pass as long as those two words stayed in the doc elsewhere.
    Found by the Codex critic on f8710589. Each assertion below now anchors
    on the word that makes the claim exclusive ("единственная" / "только"),
    not just on the named actor.
    """
    text = _normalized(INVARIANTS)
    assert "Личность вливания" in text
    assert "haiplane-gate" in text
    assert "only the gate updates integration branches" in text
    assert "Она единственная в обходе ruleset «only the gate updates" in text
    assert "В обходе ruleset «green CI before merge» нет никого" in text
    assert "Ruff and pytest" in text
    assert "принимается только от GitHub Actions" in text


def test_invariant_names_executors_drift_guard_and_rotation() -> None:
    text = _normalized(INVARIANTS)
    assert "App на k8" in text
    assert "Cursor" in text
    assert "`gh` на Mac" in text
    assert "без обхода" in text
    assert "drift_guard" in text or "Drift-guard" in text
    assert "не актора" in text
    assert "ротируется раз в 90 дней" in text
    assert "gh auth switch" in text


# AC-3 names these six files and says NONE of them may carry ANY of the six
# retired claims below — not "this file may carry only its originally-cited
# phrase". So the check is a full cross product: every file against every
# phrase, not a fixed list of (file, phrase) pairs (a fixed-pair list missed
# e.g. "PAT хаба read-only" reappearing in deploy/CD.md, since that pairing
# was never listed — found by review on eebc0180).
AC3_FILES: list[Path] = [
    REPO_ROOT / "deploy" / "CD.md",
    REPO_ROOT / "docs" / "workspace-safety-policy.md",
    REPO_ROOT / "docs" / "hub-workflows-map.html",
    REPO_ROOT / "hub" / "services" / "drift_guard.py",
    REPO_ROOT / "hub" / "integrations" / "forge" / "github.py",
    REPO_ROOT / "docs" / "specs" / "orchestrator-executor-environment.md",
]

FORBIDDEN_SUBSTRINGS: list[str] = [
    "PAT только на чтение",
    "(токен только на чтение)",
    "Branch protection недоступна",
    "rulesets недоступны",
    "rulesets are unavailable",
    "PAT хаба read-only",
    # AC-3's own wording is "«токен хаба … только на чтение»" (ellipsis =
    # a wildcard claim family), not just the two phrasings above — missing
    # found by the Codex critic on f8710589 (injecting this exact phrase
    # into each AC-3 file produced no offenders).
    "токен хаба только на чтение",
]

# Kept only for the ids of the main guard test's failure message / parametrize
# below: the full cross product of AC3_FILES x FORBIDDEN_SUBSTRINGS.
FORBIDDEN_PHRASES: list[tuple[Path, str]] = [
    (path, phrase) for path in AC3_FILES for phrase in FORBIDDEN_SUBSTRINGS
]


def _forbidden_offenders(overrides: dict[Path, str] | None = None) -> list[str]:
    """Shared guard check: every AC-3 file against every forbidden claim.

    ``overrides`` lets a caller substitute an in-memory text for a path
    instead of the file on disk — used by the mutation test below so no
    test ever writes into the shared checkout (which would be visible to
    sibling workers under ``pytest -n auto``).
    """
    overrides = overrides or {}
    offenders = []
    for path in AC3_FILES:
        text = _normalize(overrides[path]) if path in overrides else _normalized(path)
        for phrase in FORBIDDEN_SUBSTRINGS:
            if phrase in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {phrase!r}")
    return offenders


def test_no_false_gate_claims_remain() -> None:
    offenders = _forbidden_offenders()
    assert not offenders, "false gate-identity claim(s) found: " + "; ".join(offenders)


def test_write_bypass_removal_and_gate_ruleset_dated_separately() -> None:
    """Each named place must name 07.10 and 16.10 as two separate events.

    Checks per PLACE (an exact phrase anchoring each date to its own
    event), not just that both date strings occur somewhere in the file:
    that weaker check passed even when review on 0021fa3e found
    deploy/CD.md:364 attaching 16.10.2026 to the no-PR/CI-bypass state that
    actually dates from 07.10.2026 (the Write-role bypass removal, #1426;
    the "only the gate updates integration branches" ruleset 25216047 is
    the separate, later, 16.10.2026 event). The spec file
    docs/specs/orchestrator-executor-environment.md carried the same
    conflation at two places and was missing from this check entirely.
    """
    for path, places in DATE_PLACES.items():
        text = _normalized(path)
        rel = path.relative_to(REPO_ROOT)
        for bypass_removal_phrase, ruleset_addition_phrase in places:
            assert bypass_removal_phrase in text, (
                f"{rel}: missing or misdated Write-bypass-removal (07.10.2026) "
                f"phrase: {bypass_removal_phrase!r}"
            )
            assert ruleset_addition_phrase in text, (
                f"{rel}: missing or misdated gate-ruleset-addition (16.10.2026) "
                f"phrase: {ruleset_addition_phrase!r}"
            )


def test_merge_branches_mention_is_marked_historical() -> None:
    spec = REPO_ROOT / "docs" / "specs" / "orchestrator-executor-environment.md"
    for para in _text(spec).split("\n\n"):
        if (
            "merge_branches" in para
            or "POST /repos/{repo}/merges" in para
            or "POST /merges" in para
        ):
            assert "историческ" in para.lower(), (
                "a paragraph mentions the retired merge_branches/POST merges path "
                f"without a nearby 'историческ…' marker: {para[:120]!r}"
            )


def test_bypass_wording_names_pr_and_ci_requirements() -> None:
    cd_md = _normalized(REPO_ROOT / "deploy" / "CD.md")
    git_ops = _normalized(REPO_ROOT / "hub" / "integrations" / "git_ops.py")
    release = _normalized(REPO_ROOT / "hub" / "services" / "release.py")

    assert "обход требований PR/CI" in cd_md
    assert "обход не нужен никому" not in cd_md

    assert "bypass of the PR/CI requirements" in git_ops
    assert "needs no bypass at all" not in git_ops

    assert "bypass the PR/CI requirements" in release
    assert "Nothing needs to bypass anything." not in release


@pytest.mark.parametrize(
    "target,phrase",
    FORBIDDEN_PHRASES,
    ids=[
        f"{path.relative_to(REPO_ROOT)}::{phrase}" for path, phrase in FORBIDDEN_PHRASES
    ],
)
def test_guard_names_file_and_phrase_on_regression(target: Path, phrase: str) -> None:
    """Mutation check for AC-5: a reintroduced false phrase is named, not swallowed,
    for every (file, phrase) pair AC-3 covers — the full cross product of the
    six files and the six retired claims, not just the pairing each phrase
    happened to start in.

    The mutated text lives only in a local variable, never written to the
    file on disk: writing into the real checkout would be visible to
    sibling workers under ``pytest -n auto``. The same ``_forbidden_offenders``
    helper that the main guard test (``test_no_false_gate_claims_remain``)
    calls is reused here via its ``overrides`` argument, so this test proves
    the guard catches a regression instead of re-implementing its own search
    with a separate, driftable set of anchors.
    """
    original = _text(target)
    mutated = f"{original}\n<!-- regression check: {phrase} -->\n"
    offenders = _forbidden_offenders({target: mutated})
    assert offenders == [f"{target.relative_to(REPO_ROOT)}: {phrase!r}"]
