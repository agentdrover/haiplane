"""``base_merge`` at ``state=clean`` names what is actually going on (#1935).

Measured 20.10: ``mergeable`` from the provider always became ``state=clean``
with ``reason=detail`` — and ``detail`` is GitHub's raw ``mergeStateStatus``
(#475: it answers "no obligatory checks" the same way whether checks passed
or never ran). The steward's advisor read ``clean`` next to a raw ``blocked``
as a contradiction three times (judgements 346, 351, 354): "clean" and
"blocked" sound opposite, but both mean "no conflict with the base" — one
with nothing left to wait for, the other still waiting on checks or review.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hub import services as hub_services
from hub.integrations.noop import NoopGitOps
from hub.integrations.protocols import MergeabilityOutcome
from hub.integrations.registry import plugins
from hub.services import review_brief


def _task_view(pr_number: int = 42) -> SimpleNamespace:
    return SimpleNamespace(id=1, pr_number=pr_number, submission_sha="deadbeef")


def _rig(monkeypatch, outcome: MergeabilityOutcome, detail: str) -> None:
    g = NoopGitOps()
    g.check_pr_mergeable = AsyncMock(return_value=(outcome, detail))
    plugins.git_ops = g
    monkeypatch.setattr(
        hub_services,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )


# ---- AC-2: every raw GitHub mergeStateStatus explains itself in words ----


@pytest.mark.parametrize(
    "detail,expected_fragment",
    [
        ("clean", "мерж будет чистым"),
        ("blocked", "ждёт обязательных проверок"),
        ("behind", "отстаёт от базы"),
        ("unstable", "необязательные проверки красные"),
        ("draft", "в черновике"),
        ("has_hooks", "мерж будет чистым"),
    ],
)
async def test_blocked_is_explained_not_passed_raw(
    monkeypatch, detail, expected_fragment
):
    _rig(monkeypatch, MergeabilityOutcome.mergeable, detail)

    state = await review_brief._ask_base_merge(db=None, task_view=_task_view())

    assert state.state == "clean"
    assert state.reason != detail, (
        f"the raw mergeStateStatus {detail!r} must not pass through as the "
        "human reason — that is exactly what read as a contradiction"
    )
    assert expected_fragment in state.reason, state.reason


async def test_an_unknown_detail_still_says_clean_and_names_the_raw_value(
    monkeypatch,
):
    """A mergeStateStatus not in the table, or a non-GitHub forge's own text,
    must stay honest about state=clean while naming what it got."""
    _rig(monkeypatch, MergeabilityOutcome.mergeable, "мерж пройдёт")

    state = await review_brief._ask_base_merge(db=None, task_view=_task_view())

    assert state.state == "clean"
    assert "мерж будет чистым" in state.reason
    assert "мерж пройдёт" in state.reason


async def test_a_conflict_is_unaffected_by_the_clean_reason_table(monkeypatch):
    _rig(
        monkeypatch,
        MergeabilityOutcome.conflicting,
        "конфликт с базовой веткой: hub/a.py",
    )

    state = await review_brief._ask_base_merge(db=None, task_view=_task_view())

    assert state.state == "conflicting"
    assert state.files == ["hub/a.py"]
