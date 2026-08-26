"""A stalled auto-release reaches the activity feed (#962).

On 26.08.2026 GitHub refused the release merge of develop into main three
poll cycles in a row — the histories had diverged after a squash release —
and the only trace was a deduplicated warning in the server log. The policy
stood still until a human read the logs and resolved it by hand. These tests
pin the behaviour that makes the next stall visible: one activity-feed entry
per persistent refusal, none for a flicker, and a sweep that survives the
feed write failing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hub import poller
from hub import repository as repo

REFUSAL = "релизный PR #40 не смержен: GitHub отказал"


@pytest.fixture(autouse=True)
def _fresh_sweep_state():
    poller._release_notices.clear()
    poller._release_stalls.clear()
    yield
    poller._release_notices.clear()
    poller._release_stalls.clear()


async def _project(db, slug: str = "default"):
    row = await repo.get_project_by_slug(db, slug)
    if row is None:
        await repo.create_project(
            db, slug=slug, name=slug, workspace_path=f"/tmp/{slug}"
        )
        row = await repo.get_project_by_slug(db, slug)
    await db.commit()
    return row


async def _stall_entries(db) -> list[dict]:
    cur = await db.execute(
        "SELECT kind, summary, detail FROM activity_log"
        " WHERE summary LIKE '%релиз стоит%' ORDER BY id"
    )
    return [dict(r) for r in await cur.fetchall()]


async def _sweep(db, merged: bool, reason: str, times: int = 1) -> None:
    """Drive the release sweep, each cycle answering (merged, reason)."""
    with patch(
        "hub.services.release.merge_ready_release",
        AsyncMock(return_value=(merged, reason)),
    ):
        for _ in range(times):
            await poller._sweep_release_policy(db)


async def test_persistent_refusal_logged_once(db):
    # AC-1: three cycles with the same refusal put exactly one entry in the
    # activity feed; further cycles with the same reason add nothing — a line
    # per cycle is how a real signal gets muted (#534).
    await _project(db)

    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES)
    entries = await _stall_entries(db)
    assert len(entries) == 1
    assert entries[0]["kind"] == "release"
    assert entries[0]["summary"] == f"default: релиз стоит — {REFUSAL}"

    await _sweep(db, False, REFUSAL, times=5)
    assert len(await _stall_entries(db)) == 1


async def test_signal_resets_on_merge_or_reason_change(db):
    # AC-2: a successful merge — or a different reason — resets the signal,
    # so the NEXT persistent stall produces a new entry instead of hiding
    # behind the old one.
    await _project(db)

    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES)
    await _sweep(db, True, "релиз PR #41 смержен в main")
    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES)
    assert len(await _stall_entries(db)) == 2

    other = "релизный PR #42 не смержен: ci_failed (mypy)"
    await _sweep(db, False, other, times=poller.RELEASE_STALL_CYCLES)
    entries = await _stall_entries(db)
    assert len(entries) == 3
    assert other in entries[-1]["summary"]


async def test_transient_refusal_not_logged(db):
    # AC-3: one or two refused cycles are a flicker (a network hiccup, a race
    # with CI), not a stall — the feed stays silent when the refusal clears
    # below the threshold, whether by a merge or by the reason vanishing.
    await _project(db)

    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES - 1)
    await _sweep(db, True, "релиз PR #41 смержен в main")
    assert await _stall_entries(db) == []

    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES - 1)
    await _sweep(db, False, "")
    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES - 1)
    assert await _stall_entries(db) == []


async def test_activity_failure_does_not_break_sweep(db):
    # AC-4: the feed write failing is absorbed — every project is still
    # swept, and the entry lands on a later cycle instead of being lost.
    await _project(db, "default")
    await _project(db, "second")

    with patch(
        "hub.poller.log_activity", AsyncMock(side_effect=RuntimeError("db locked"))
    ):
        await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES)
    assert await _stall_entries(db) == []
    assert set(poller._release_stalls) == {"default", "second"}

    await _sweep(db, False, REFUSAL)
    entries = await _stall_entries(db)
    assert {e["summary"].split(":")[0] for e in entries} == {"default", "second"}


# --- AC-4 (#970): в ленту едет диагноз, а не «GitHub отказал» ---------------


async def test_stall_notice_names_the_conflict(db):
    """Настоящий merge_ready_release, а не пересказ мока.

    #962 построил дорогу от стойла до ленты и повёз по ней ту же непрозрачную
    фразу, поэтому человек всё равно шёл смотреть в GitHub. Тест держит весь
    путь целиком: конфликтный релизный PR → причина → запись в ленте, по
    которой понятен следующий шаг.
    """
    from unittest.mock import AsyncMock as _AsyncMock

    from hub import repository as hub_repo
    from hub.integrations.protocols import MergeabilityOutcome
    from tests.test_release_policy import _git as _git_plugin

    g = _git_plugin(existing_pr=83)
    g.check_pr_mergeable = _AsyncMock(
        return_value=(MergeabilityOutcome.conflicting, "конфликт в hub/db.py")
    )
    row = await _project(db)
    await hub_repo.update_project(
        db, dict(row)["id"], gate_policy='{"release": "auto"}'
    )
    await db.commit()

    for _ in range(poller.RELEASE_STALL_CYCLES):
        await poller._sweep_release_policy(db)

    entries = await _stall_entries(db)
    assert entries, "стойло обязано дойти до ленты"
    text = " ".join((e["summary"] or "") + (e["detail"] or "") for e in entries)
    assert "hub/db.py" in text, f"в ленте нет диагноза, только шум: {entries}"
    assert "отказал" not in text, (
        f"«GitHub отказал» — это не то, что человек может починить: {entries}"
    )
    g.merge_pr.assert_not_awaited()
