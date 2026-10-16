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


# (path, forbidden substring) — each pair is a claim retired by #1708.
FORBIDDEN_PHRASES: list[tuple[Path, str]] = [
    (REPO_ROOT / "deploy" / "CD.md", "PAT только на чтение"),
    (REPO_ROOT / "deploy" / "CD.md", "(токен только на чтение)"),
    (
        REPO_ROOT / "docs" / "workspace-safety-policy.md",
        "Branch protection недоступна",
    ),
    (REPO_ROOT / "docs" / "workspace-safety-policy.md", "rulesets недоступны"),
    (REPO_ROOT / "docs" / "hub-workflows-map.html", "rulesets недоступны"),
    (REPO_ROOT / "hub" / "services" / "drift_guard.py", "rulesets are unavailable"),
    (REPO_ROOT / "hub" / "integrations" / "forge" / "github.py", "PAT хаба read-only"),
    (
        REPO_ROOT / "docs" / "specs" / "orchestrator-executor-environment.md",
        "rulesets недоступны",
    ),
    (
        REPO_ROOT / "docs" / "specs" / "orchestrator-executor-environment.md",
        "rulesets are unavailable",
    ),
]


def _forbidden_offenders(overrides: dict[Path, str] | None = None) -> list[str]:
    """Shared guard check: one list of forbidden claims, read normalized.

    ``overrides`` lets a caller substitute an in-memory text for a path
    instead of the file on disk — used by the mutation test below so no
    test ever writes into the shared checkout (which would be visible to
    sibling workers under ``pytest -n auto``).
    """
    overrides = overrides or {}
    offenders = []
    for path, phrase in FORBIDDEN_PHRASES:
        text = _normalize(overrides[path]) if path in overrides else _normalized(path)
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


# One representative (file, phrase) per AC-3 file, in FORBIDDEN_PHRASES' own
# order, so every file named by AC-3 gets a mutation case — not just one of
# the six.
_AC3_FILES = list(dict.fromkeys(path for path, _ in FORBIDDEN_PHRASES))
_AC3_REPRESENTATIVE: list[tuple[Path, str]] = [
    next((path, phrase) for path, phrase in FORBIDDEN_PHRASES if path == target)
    for target in _AC3_FILES
]


@pytest.mark.parametrize(
    "target,phrase",
    _AC3_REPRESENTATIVE,
    ids=[str(path.relative_to(REPO_ROOT)) for path, _ in _AC3_REPRESENTATIVE],
)
def test_guard_names_file_and_phrase_on_regression(target: Path, phrase: str) -> None:
    """Mutation check for AC-5: a reintroduced false phrase is named, not swallowed,
    for every file listed in AC-3 — not just one of the six.

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
