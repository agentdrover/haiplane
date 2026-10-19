"""Единый предикат «сдачу можно принять» (#1810, круг 5).

«Принять» — доказанные условия, а не «сомнений не набралось»: каждое условие
вне белого списка отчётов тоже ломает совет, и выдать «Принять» мимо
``prove_accept`` нельзя.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services import decision_brief
from hub.services.accept_proof import AcceptProof
from tests.test_decision_brief_whitelist import ACCEPT, _action, _task


async def test_a_proof_cannot_be_forged_and_accept_needs_one():
    with pytest.raises(TypeError):
        AcceptProof("a" * 40)
    with pytest.raises(TypeError):
        decision_brief._accept(None, ["почему"])  # type: ignore[arg-type]


@pytest.mark.parametrize("current", [True, False])
async def test_a_standing_changes_requested_blocks_accept(
    client, db: aiosqlite.Connection, monkeypatch, current: bool
):
    task_id, _ = await _task(client, db, monkeypatch, f"ap-verdict-{current}", 1)
    task = dict(await repo.get_task(db, task_id) or {})
    generation = int(task["submission_generation"])
    finding = {"id": 1, "severity": "high", "message": "сломано", "scope": "in_scope"}
    await db.execute(
        "UPDATE tasks SET review_verdict='changes_requested', "
        "review_verdict_generation=?, review_findings=? WHERE id=?",
        (generation if current else generation - 1, json.dumps([finding]), task_id),
    )
    await db.commit()
    got = await _action(db, task_id)
    # Вердикт прошлой сдачи код с тех пор пересдан — не действует.
    assert (got != ACCEPT) is current


@pytest.mark.parametrize("tip", ["b" * 40, ""])
async def test_branch_must_stand_on_the_submitted_sha(
    client, db: aiosqlite.Connection, monkeypatch, tip: str
):
    task_id, _ = await _task(client, db, monkeypatch, f"ap-sha-{bool(tip)}", 1)
    assert await _action(db, task_id) == ACCEPT

    async def moved(*_a, **_k):
        return tip

    monkeypatch.setattr(plugins.git_ops, "head_sha", moved)
    assert await _action(db, task_id) != ACCEPT, "ушла/неизвестна вершина ветки"


@pytest.mark.parametrize(
    "section", ["unresolved", "lost_dimensions", "findings_rejected"]
)
@pytest.mark.parametrize("broken", ["not json", "{}"])
async def test_a_broken_report_section_is_unreadable_not_empty(
    client, db: aiosqlite.Connection, monkeypatch, section: str, broken: str
):
    task_id, ids = await _task(
        client, db, monkeypatch, f"ap-{section}-{len(broken)}", 1
    )
    await db.execute(
        f"UPDATE machine_reviews SET {section}=? WHERE id=?",  # nosec B608
        (broken, ids[0]),
    )
    await db.commit()
    assert await _action(db, task_id) != ACCEPT


async def _carry(db, task_id: int, source_id: int, **over) -> int:
    """Перенос #1361: копия в текущем поколении без токенов, с пометкой источника."""
    task = dict(await repo.get_task(db, task_id) or {})
    generation = int(task["submission_generation"])
    await db.execute(
        "UPDATE machine_reviews SET submission_generation=? WHERE id=?",
        (generation - 1, source_id),
    )
    source = dict(await repo.get_machine_review(db, source_id) or {})
    carried = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill=source["harness_skill"],
        harness_version=source["harness_version"],
        agent_count=source["agent_count"],
        orchestrator=source["orchestrator"],
        model=source["model"],
        raw_count=int(source["raw_count"]),
        findings_confirmed="[]",
        findings_rejected=source["findings_rejected"],
        submitted_by=source["submitted_by"],
        incomplete=False,
        unresolved="[]",
        lost_dimensions="[]",
        profile=source["profile"],
        principal_id=source["principal_id"],
        carried_from_review_id=over.get("root", source_id),
        ci_integrity=("", ""),
    )
    await db.commit()
    return carried


async def test_a_carried_report_is_judged_through_its_source(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, ids = await _task(client, db, monkeypatch, "ap-carry", 1)
    await _carry(db, task_id, ids[0])
    # Копия без токенов, исходный отчёт с provider_tokens>0: перенос годен.
    assert await _action(db, task_id) == ACCEPT


async def test_a_carried_report_with_a_weak_source_does_not_accept(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, ids = await _task(client, db, monkeypatch, "ap-carry-weak", 1)
    await db.execute(
        "UPDATE machine_reviews SET provider_tokens=NULL WHERE id=?", (ids[0],)
    )
    await _carry(db, task_id, ids[0])
    assert await _action(db, task_id) != ACCEPT


async def test_a_carried_report_without_its_source_is_unreadable(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, ids = await _task(client, db, monkeypatch, "ap-carry-lost", 1)
    await _carry(db, task_id, ids[0], root=987654)
    assert await _action(db, task_id) != ACCEPT


def test_accept_has_exactly_one_emitter():
    """«Принять» собирается в одном месте: любая другая выдача мимо доказательства."""
    import inspect
    import re

    source = re.sub(r"\s+", " ", inspect.getsource(decision_brief))
    assert source.count("_recommend( ACCEPT") + source.count("_recommend(ACCEPT") == 1
