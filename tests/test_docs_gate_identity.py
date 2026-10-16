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


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalize(text: str) -> str:
    """Collapse whitespace so a prose line-wrap cannot break a substring check."""
    return " ".join(text.split())


def _normalized(path: Path) -> str:
    return _normalize(_text(path))


def test_invariant_names_the_gate_and_its_bypass() -> None:
    text = _normalized(INVARIANTS)
    assert "Личность вливания" in text
    assert "haiplane-gate" in text
    assert "only the gate updates integration branches" in text
    assert "В обходе ruleset «green CI before merge» нет никого" in text
    assert "Ruff and pytest" in text
    assert "GitHub Actions" in text


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
