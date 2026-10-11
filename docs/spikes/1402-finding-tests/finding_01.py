"""#1167: подмена базы названа в пакете и должна доехать до отчёта реплея."""

from __future__ import annotations

import aiosqlite
import pytest

from hub import repository as repo
from hub.config import PAIR_BASE_BRANCH
from hub.db import fetchall
from hub.integrations.git_ops import _resolve_base
from hub.integrations.registry import plugins
from hub.services import steward_shadow as sh
from hub.services.steward_evidence import build_historical_packet
from tests.test_steward_shadow import (
    _SHA,
    _HistoricalGitOps,
    _historical_project,
    _historical_submission,
)


def _today_base_note(packet) -> bool:
    return "леджер сдач базу этой генерации не записал" in packet.fact(
        "diff_vs_areas"
    ).detail


class _CapturingGit(_HistoricalGitOps):
    def __init__(self, paths: list[str]) -> None:
        super().__init__(paths)
        self.bases: list[str | None] = []

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        self.bases.append(base_branch)
        return await super().branch_diff_paths(branch, base_branch, repo)


async def test_today_base_substitution_reaches_replay_report(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Поколение #5 пишет base_note в detail фактов карточки.

    ReplayReport и render_report этого числа не знают: нет счётчика, нет
    строки, нет доли. CLI печатает 2×2, долю восстановленного и «эскалации
    не по существу» — и молчит, сколько сдач сравнили с сегодняшним
    резолвом имени. decide() читает value.within_declared, не detail,
    поэтому приписка не меняет клетку таблицы, и отсутствие точки сравнения
    по-прежнему читается как измерение.
    """
    project_id = await _historical_project(db, "finding-1402-1")
    monkeypatch.setattr(
        plugins, "git_ops", _HistoricalGitOps(["hub/services/steward_shadow.py"])
    )
    silent_id = await _historical_submission(
        db, project_id, human_verdict="approved"
    )
    named_id = await _historical_submission(
        db, project_id, human_verdict="approved"
    )
    await repo.record_submission(
        db, task_id=named_id, generation=1, sha=_SHA, base_branch="release/old"
    )
    await db.commit()

    entries, dropped = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries, dropped)
    assert excluded == []
    assert len(cases) == 2

    by_id = {c.entry.task_id: c.packet for c in cases}
    assert _today_base_note(by_id[silent_id])
    assert not _today_base_note(by_id[named_id])

    report = sh.replay(cases, window_days=60, excluded=excluded)
    assert report.table.both_approve == 2
    assert report.table.escalated == 0

    text = sh.render_report(report)
    assert "Таблица 2x2" in text
    assert "карточка сдачи не сохранена" in text
    assert "вершина и дифф" in text
    silent = sum(1 for c in cases if _today_base_note(c.packet))
    assert silent == 1
    assert any("сегодняшн" in line and str(silent) in line for line in text.splitlines()), (
        "подмена базы названа в пакете и не доехала до отчёта реплея:\n" + text
    )


async def test_unnamed_today_base_note_names_resolved_fallback(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустые ledger_base и ctx.base_branch: заметка не должна писать
    «не названа» — git_ops._resolve_base подставляет PAIR_BASE_BRANCH.
    """
    project_id = await _historical_project(db, "finding-1402-1-unnamed")
    await db.execute("UPDATE projects SET default_branch='' WHERE id=?", (project_id,))
    await db.commit()
    git = _CapturingGit(["hub/services/steward_shadow.py"])
    monkeypatch.setattr(plugins, "git_ops", git)
    task_id = await _historical_submission(db, project_id, human_verdict="approved")
    rows = await fetchall(
        db,
        "SELECT created_at FROM events WHERE task_id=? AND "
        "kind='review_verdict_recorded'",
        (task_id,),
    )
    cutoff = dict(rows[0])["created_at"]

    packet = await build_historical_packet(db, task_id, 1, cutoff)
    assert git.bases == [""]
    assert _resolve_base(git.bases[0]) == PAIR_BASE_BRANCH
    detail = packet.fact("diff_vs_areas").detail
    assert "леджер сдач базу этой генерации не записал" in detail
    assert "не названа" not in detail
    assert PAIR_BASE_BRANCH in detail
