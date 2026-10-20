"""Сценарий AC-4 задачи #1611: дверь поллера стюарда и отпечаток готовности.

Вынесен из tests/test_steward_dispatch.py: тот файл под потолком размера.
"""

from __future__ import annotations

import json

import aiosqlite

from hub import config
from hub import repository as repo
from hub.services.refinement import get_readiness
from hub.services.steward_dispatch import _order_one_dor_run


async def _auto_ready_draft(db: aiosqlite.Connection, ready_draft, project_id: int):
    """Драфт с dor_passed=1, отпечатком, R0 и областями вне контура автопилота."""
    task_id = await ready_draft(db, project_id)
    await repo.update_task(
        db, task_id, risk_class="R0", affected_areas=json.dumps(["docs/notes.md"])
    )
    await db.commit()
    await get_readiness(db, task_id)  # ленивая починка обновляет отпечаток
    return task_id


async def check_poller_door(db, monkeypatch, project_with_policy, ready_draft) -> None:
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    project_id = await project_with_policy(db, "dor-door", {"dor": "steward"})
    stale = await _auto_ready_draft(db, ready_draft, project_id)
    fresh = await _auto_ready_draft(db, ready_draft, project_id)
    # Постановка stale изменена в обход пересчёта; политика сменилась на auto
    # уже после выборки поллера (выборка видела dor=steward).
    await repo.update_task(db, stale, business_value="")
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE id=?",
        (json.dumps({"dor": "auto"}), project_id),
    )
    await db.commit()
    assert dict(await repo.get_task(db, stale))["dor_passed"] == 1

    await _order_one_dor_run(db, stale)
    assert dict(await repo.get_task(db, stale))["status"] == "draft", (
        "по устаревшему dor_passed поллер задачу не открывает"
    )

    await _order_one_dor_run(db, fresh)
    assert dict(await repo.get_task(db, fresh))["status"] == "open", (
        "при совпавшем отпечатке автоодобрение работает как прежде"
    )


async def seal_evidence(db: aiosqlite.Connection, *task_ids: int) -> None:
    """Дописать задачам отпечаток нынешних входов: «dor_passed посчитан по ним»."""
    from hub.services.dor_snapshot import load_dor_snapshot

    for task_id in task_ids:
        evidence = (await load_dor_snapshot(db, task_id)).fingerprint()
        await repo.update_task(db, task_id, readiness_evidence=evidence)
    await db.commit()
