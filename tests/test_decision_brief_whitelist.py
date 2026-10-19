"""«Принять» по белому списку условий на КАЖДЫЙ отчёт поколения (#1810, круг 4).

Прежний подход искал плохие признаки по одному, и каждый круг ревью находил
следующий путь. Здесь проверяется обратное: каждое условие белого списка,
нарушенное по отдельности и у любого из отчётов, ломает «Принять».
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub import repository_ci_integrity as integrity_store
from hub.services import decision_brief
from tests.test_auto_verdict import _post_review, _submitted_task

ACCEPT = "Принять"

_CONFIRMED = (
    '[{"title":"сломан возврат","severity":"high","category":"correctness",'
    '"file":"hub/x.py","line":1,"locator":"lines","start_line":1,"detail":"d"}]'
)

#: условие белого списка -> как его нарушить в строке отчёта
CONDITIONS = {
    "outcome_confirmed": f"findings_confirmed='{_CONFIRMED}'",
    "outcome_no_data": "raw_count=0",
    "incomplete_true": "incomplete=1",
    "incomplete_unstated": "incomplete=NULL",
    "lost_dimensions": "lost_dimensions='[\"security: files not read\"]'",
    "unresolved_unanswered": 'unresolved=\'[{"title":"не проверено","why":"x"}]\'',
    "provider_tokens_missing": "provider_tokens=NULL",
    "provider_tokens_zero": "provider_tokens=0",
    "text_origin": "orchestrator='cursor-cloud-result:claimed'",
    "self_review": "self_reviewed=1",
    "no_reviewer_identity": "principal_id=NULL",
}


async def _task(
    client, db, monkeypatch, slug: str, reports: int
) -> tuple[int, list[int]]:
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    task_id = await _submitted_task(client, db, slug, {"verdict": "human"})
    for _ in range(reports):
        await _post_review(client, task_id)
    task = dict(await repo.get_task(db, task_id) or {})
    await integrity_store.save_ci_integrity_scan(
        db, task_id, task["submission_sha"], "clean", "[]", ""
    )
    await db.execute(
        "UPDATE machine_reviews SET provider_tokens=250000, principal_id=9 "
        "WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT id FROM machine_reviews WHERE task_id=? ORDER BY id", (task_id,)
    )
    return task_id, [r[0] for r in rows]


async def _action(db: aiosqlite.Connection, task_id: int) -> str:
    task = dict(await repo.get_task(db, task_id) or {})
    return (await decision_brief.build(db, task))["recommend"]["action"]


async def test_control_every_report_fit_earns_accept(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, ids = await _task(client, db, monkeypatch, "wl-control", 2)
    assert len(ids) == 2
    assert await _action(db, task_id) == ACCEPT


async def test_no_report_at_all_never_earns_accept(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, ids = await _task(client, db, monkeypatch, "wl-none", 0)
    assert ids == []
    assert await _action(db, task_id) != ACCEPT


@pytest.mark.parametrize("target", ["only", "first", "second"])
@pytest.mark.parametrize("condition", sorted(CONDITIONS))
async def test_each_condition_alone_breaks_accept(
    client, db: aiosqlite.Connection, monkeypatch, condition: str, target: str
):
    task_id, ids = await _task(
        client,
        db,
        monkeypatch,
        f"wl-{condition}-{target}".replace("_", "-"),
        1 if target == "only" else 2,
    )
    victim = ids[-1] if target in ("only", "second") else ids[0]
    await db.execute(
        f"UPDATE machine_reviews SET {CONDITIONS[condition]} WHERE id=?",  # nosec B608
        (victim,),
    )
    await db.commit()
    assert await _action(db, task_id) != ACCEPT, (condition, target)


async def test_a_review_still_in_flight_blocks_accept(
    client, db: aiosqlite.Connection, monkeypatch
):
    from types import SimpleNamespace

    from hub.services import review_evidence

    task_id, _ = await _task(client, db, monkeypatch, "wl-inflight", 1)
    assert await _action(db, task_id) == ACCEPT

    async def flying(*_a, **_k):
        return SimpleNamespace(headline="ревью в полёте: тест")

    monkeypatch.setattr(review_evidence, "inflight_view", flying)
    assert await _action(db, task_id) != ACCEPT


async def test_an_unreadable_report_is_unknown_not_clean(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, ids = await _task(client, db, monkeypatch, "wl-unreadable", 1)
    await db.execute(
        "UPDATE machine_reviews SET findings_confirmed='{\"x\": 1}', raw_count='много' "
        "WHERE id=?",
        (ids[0],),
    )
    await db.commit()
    assert await _action(db, task_id) != ACCEPT
