"""#1167: исторический пакет судит поверхность и класс по сдаче, не по живой карточке."""

from __future__ import annotations

import json

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.integrations.registry import plugins
from hub.services.gate_grounds import PolicyInputs, decide
from hub.services.steward_evidence import build_historical_packet
from tests.test_steward_shadow import (
    _SHA,
    _HistoricalGitOps,
    _historical_project,
    _historical_submission,
)

_GEN1_DIFF = ["hub/services/steward_shadow.py", "hub/db.py"]
_GEN1_AREAS = ["hub/services/steward_shadow.py"]
_GEN1_CLASS = "R1"
_LATER_CLASS = "R3"


async def test_historical_packet_does_not_judge_gen1_by_todays_card(
    db: aiosqlite.Connection, monkeypatch
):
    """generation=1 без accept_areas, человек вернул; generation=2 дописал области.

    Леджер помнит sha gen-1, но _surface_fact/_risk_fact читают живую строку:
    путь, бывший вне области, попадает в расширенный набор (within_declared),
    stored-класс — от поздней сдачи. decide() читает оба флага.
    """
    project_id = await _historical_project(db, "replay-stale-card")
    monkeypatch.setattr(plugins, "git_ops", _HistoricalGitOps(_GEN1_DIFF))
    task_id = await _historical_submission(
        db, project_id, human_verdict="changes_requested", areas=_GEN1_AREAS
    )
    await repo.update_task(db, task_id, risk_class=_GEN1_CLASS)
    await repo.record_submission(
        db, task_id=task_id, generation=1, sha=_SHA, base_branch="develop"
    )

    # Как lifecycle.py:2489–2540: та же транзакция пересдачи перезаписывает
    # карточку. Путь, который на вердикте gen-1 был вне области, теперь «заявлен».
    later_areas = list(_GEN1_AREAS) + ["hub/db.py"]
    await repo.update_task(
        db,
        task_id,
        submission_generation=2,
        submission_sha="e" * 40,
        risk_class=_LATER_CLASS,
        affected_areas=json.dumps(later_areas),
    )
    await repo.record_submission(
        db, task_id=task_id, generation=2, sha="e" * 40, base_branch="develop"
    )
    await db.commit()

    rows = await fetchall(
        db,
        "SELECT created_at FROM events WHERE task_id=? AND "
        "kind='review_verdict_recorded'",
        (task_id,),
    )
    packet = await build_historical_packet(db, task_id, 1, dict(rows[0])["created_at"])

    surface = packet.fact("diff_vs_areas")
    risk = packet.fact("risk_class")
    if surface.is_present:
        assert "hub/db.py" in list(surface.value.get("undeclared") or []), (
            "путь gen-1 вне области сдачи не должен исчезнуть из undeclared "
            "из-за accept_areas поздней сдачи"
        )
    assert surface.value.get("within_declared") is not True, (
        "пути gen-1, бывшие вне области, сравнили с расширенной сегодняшней карточкой"
    )
    assert risk.value.get("stored") in (None, _GEN1_CLASS), (
        f"stored-класс должен быть сдачи gen-1 ({_GEN1_CLASS}), "
        f"а не живой карточки ({_LATER_CLASS})"
    )

    decision = decide(
        packet,
        PolicyInputs(implementer_model="claude-opus-5", reviewer_model="grok-4.6"),
    )
    assert decision.verdict != "approve", (
        "decide() прочитал within_declared/stored с чужой карточки и одобрил gen-1"
    )
