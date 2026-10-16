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

REPO_ROOT = Path(__file__).resolve().parent.parent
INVARIANTS = REPO_ROOT / "docs" / "agent-context" / "invariants.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalized(path: Path) -> str:
    """Collapse whitespace so a prose line-wrap cannot break a substring check."""
    return " ".join(_text(path).split())


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


def test_no_false_gate_claims_remain() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {phrase!r}"
        for path, phrase in FORBIDDEN_PHRASES
        if phrase in _text(path)
    ]
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


def test_guard_names_file_and_phrase_on_regression(tmp_path: Path) -> None:
    """Mutation check for AC-5: a reintroduced false phrase is named, not swallowed."""
    target = REPO_ROOT / "hub" / "integrations" / "forge" / "github.py"
    original = _text(target)
    mutated = original.replace(
        "сам токен хаба умеет и писать", "PAT хаба read-only (мутация теста)"
    )
    assert mutated != original, "fixture text to mutate was not found in github.py"
    target.write_text(mutated, encoding="utf-8")
    try:
        offenders = [
            f"{path.relative_to(REPO_ROOT)}: {phrase!r}"
            for path, phrase in FORBIDDEN_PHRASES
            if phrase in _text(path)
        ]
        assert offenders == [f"{target.relative_to(REPO_ROOT)}: 'PAT хаба read-only'"]
    finally:
        target.write_text(original, encoding="utf-8")
