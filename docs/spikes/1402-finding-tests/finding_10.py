"""#1167: пустой origin-дифф после мержа — дыра, а не «всё в заявленном»."""

from __future__ import annotations

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.integrations.registry import plugins
from hub.services.gate_grounds import PolicyInputs, decide
from hub.services.steward_evidence import (
    build_historical_packet,
    diff_recovered,
)
from tests.test_steward_shadow import (
    _SHA,
    _HistoricalGitOps,
    _historical_project,
    _historical_submission,
)


class _OriginMergedLocalLags(_HistoricalGitOps):
    """Как живой git_ops: трёхточечный дифф — против origin/<base>, предок — нет.

    branch_diff_paths после fetch резолвит origin/develop...sha и после мержа
    отдаёт []. is_ancestor ходит в cat-file/merge-base по сырому имени
    «develop» — без fetch и без origin/; на общем клоне локальный develop
    отстаёт, ответ False. delivery_state для того же вопроса уже пишет
    origin/{base}.
    """

    def __init__(self) -> None:
        super().__init__(
            ["hub/services/steward_shadow.py"],
            diff_empty=True,
            ancestor=False,
        )
        self.asked_descendants: list[str] = []

    async def is_ancestor(self, repo, ancestor, descendant):
        self.asked_descendants.append(descendant)
        if str(descendant).startswith("origin/"):
            return True
        return False


async def test_collapsed_origin_diff_is_not_within_declared_when_local_base_lags(
    db: aiosqlite.Connection, monkeypatch
):
    """Мерж generation не бампает: карточка текущая, дифф origin уже пуст."""
    project_id = await _historical_project(db, "replay-origin-lag")
    git = _OriginMergedLocalLags()
    monkeypatch.setattr(plugins, "git_ops", git)
    task_id = await _historical_submission(db, project_id, human_verdict="approved")
    await repo.record_submission(
        db, task_id=task_id, generation=1, sha=_SHA, base_branch="develop"
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
    assert surface.is_absent, (
        "пустой origin-дифф после мержа выдан за измеренную поверхность "
        f"(within_declared={surface.value.get('within_declared')})"
    )
    assert surface.reason == "historical_diff_collapsed"
    assert risk.is_absent and risk.reason == "historical_diff_collapsed"
    assert diff_recovered(packet) is False, "схлопнувшийся дифф засчитан успехом реконструкции"
    assert any(str(name).startswith("origin/") for name in git.asked_descendants), (
        "is_ancestor спросили не у той вершины, против которой считался дифф "
        f"(спросили {git.asked_descendants!r}, а не origin/develop)"
    )

    decision = decide(
        packet,
        PolicyInputs(implementer_model="claude-opus-5", reviewer_model="grok-4.6"),
    )
    assert decision.verdict != "approve", (
        "чистый отчёт и зелёный CI при пустом undeclared дали approve на схлопнувшемся диффе"
    )
