"""Возврат на доработку по суждению судьи применяется, а не висит в review (#2053).

Живой случай (#2042, #2057): судья нашёл дефекты, статус не изменился, автор
ничего не получил. Сцены настоящие: суждение пишет тот же контракт, применяет
тот же проход поллера, что и approve.
"""

from __future__ import annotations

import json

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.db import fetchall
from hub.models import (
    ReviewVerdict,
    SessionRegister,
    StewardJudgementSubmit,
    TaskReviewVerdict,
)
from hub import services
from hub.services import inbox_decisions
from hub.services.steward_changes_apply import apply_judge_changes_requests
from hub.services.steward_judgement import record_steward_judgement
from tests.test_steward_applied import _client_task, _grant_act
from tests.test_steward_shadow import _project

FINDINGS = [
    {
        "finding_uid": "abc123",
        "title": "гонка при повторной записи",
        "file": "hub/x.py",
        "line": 7,
        "severity": "high",
    },
    {"title": "нет теста на край", "severity": "low"},
]


async def _judge_cr(db, task_id: int, *, generation: int = 1) -> None:
    await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=generation,
            kind="verdict",
            verdict="changes_requested",
            confidence="high",
            grounds=[{"source": "ci_pinned_sha", "detail": "ci 1 ok"}],
            findings=FINDINGS,
            model="gpt-5.3-codex",
        ),
        TokenIdentity("steward-bot", "steward", principal_id=42),
    )


async def _online(db, task_id: int) -> None:
    await services.register_session(
        db, SessionRegister(session_id="sess-2053"), agent="dev", principal_id=None
    )
    await repo.update_task(
        db, task_id, claimed_by="dev", claim_session_id="sess-2053", claimed_at="x"
    )
    await db.commit()


async def _offline(db, task_id: int) -> None:
    await _online(db, task_id)
    await db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now', '-1 day')"
    )
    await db.commit()


async def _scene(db, monkeypatch, slug: str, **fields) -> int:
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    _grant_act(monkeypatch)
    project_id = await _project(db, slug)
    task_id = await _client_task(db, project_id, **fields)
    await _judge_cr(db, task_id)
    return task_id


async def _verdict(db, task_id: int) -> dict:
    task = dict(await repo.get_task(db, task_id))
    return {
        "verdict": task["review_verdict"],
        "findings": json.loads(task.get("review_findings") or "[]"),
        "task": task,
    }


async def test_a_judge_changes_request_goes_back_to_the_author(db, monkeypatch):
    """AC: чистая сдача судьи с находками - вердикт, цикл +1, задача у автора."""
    task_id = await _scene(db, monkeypatch, "jcr-main")
    await _online(db, task_id)

    assert await apply_judge_changes_requests(db) == 1

    got = await _verdict(db, task_id)
    assert got["task"]["status"] == "running"
    assert got["task"]["review_cycle"] == 1
    assert got["verdict"] == "changes_requested"
    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "returned"
    # Находки судьи - в вердикте списком, uid назван в сообщении.
    assert len(got["findings"]) == 2
    assert "[uid abc123]" in got["findings"][0]["message"]
    assert got["findings"][0]["severity"] == "high"
    assert got["findings"][0]["file"] == "hub/x.py"
    # Повтор ничего не делает.
    assert await apply_judge_changes_requests(db) == 0
    assert (await _verdict(db, task_id))["task"]["review_cycle"] == 1


async def test_after_the_return_nothing_shows_in_the_owners_inbox(db, monkeypatch):
    task_id = await _scene(db, monkeypatch, "jcr-inbox")
    await _online(db, task_id)
    queue = (await inbox_decisions.collect(db, {}))["queue"]
    assert any(e["task_id"] == task_id for e in queue), "до применения - у владельца"

    await apply_judge_changes_requests(db)

    queue = (await inbox_decisions.collect(db, {}))["queue"]
    assert not any(e["task_id"] == task_id for e in queue)


async def test_an_offline_executor_gets_no_running_task_but_an_open_one(
    db, monkeypatch
):
    task_id = await _scene(db, monkeypatch, "jcr-offline")
    await _offline(db, task_id)

    await apply_judge_changes_requests(db)

    task = (await _verdict(db, task_id))["task"]
    assert task["status"] == "open"
    assert not task["claimed_by"] and not task["claim_session_id"]
    assert task["review_cycle"] == 1
    events = await fetchall(
        db,
        "SELECT payload FROM events WHERE task_id=? AND kind='executor_claim_released'",
        (task_id,),
    )
    assert len(events) == 1
    assert json.loads(dict(events[0])["payload"])["reason"] == (
        "changes_requested_executor_offline"
    )


async def test_a_human_changes_request_checks_the_executor_too(db, monkeypatch):
    from hub.services.lifecycle import record_review_verdict

    project_id = await _project(db, "jcr-human")
    gone = await _client_task(db, project_id)
    await _offline(db, gone)
    here = await _client_task(db, project_id)
    await services.register_session(
        db, SessionRegister(session_id="sess-here"), agent="dev2", principal_id=None
    )
    await repo.update_task(db, here, claimed_by="dev2", claim_session_id="sess-here")
    await db.commit()

    body = TaskReviewVerdict(
        agent="rev", verdict=ReviewVerdict.changes_requested, comments="чинить"
    )
    await record_review_verdict(db, gone, body)
    await record_review_verdict(db, here, body)

    assert dict(await repo.get_task(db, gone))["status"] == "open"
    assert dict(await repo.get_task(db, here))["status"] == "running"
    assert dict(await repo.get_task(db, here))["claimed_by"] == "dev2"


async def test_a_judgement_of_a_stale_generation_changes_nothing(db, monkeypatch):
    task_id = await _scene(db, monkeypatch, "jcr-stale")
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()

    assert await apply_judge_changes_requests(db) == 0

    task = (await _verdict(db, task_id))["task"]
    assert task["status"] == "review" and not task["review_verdict"]


async def test_the_contour_off_or_not_delegated_changes_nothing(db, monkeypatch):
    task_id = await _scene(db, monkeypatch, "jcr-locks")
    from hub.services import steward_shadow

    async def shadow(_db):
        return "shadow"

    monkeypatch.setattr(steward_shadow, "effective_mode", shadow)
    assert await apply_judge_changes_requests(db) == 0
    assert dict(await repo.get_task(db, task_id))["status"] == "review"

    _grant_act(monkeypatch)
    await db.execute("UPDATE projects SET gate_policy='{}'")
    await db.commit()
    assert await apply_judge_changes_requests(db) == 0
    assert dict(await repo.get_task(db, task_id))["status"] == "review"


async def test_a_standing_verdict_is_not_overwritten(db, monkeypatch):
    task_id = await _scene(db, monkeypatch, "jcr-standing")
    await repo.update_task(
        db, task_id, review_verdict="approved", review_verdict_generation=1
    )
    await db.commit()

    assert await apply_judge_changes_requests(db) == 0
    assert dict(await repo.get_task(db, task_id))["status"] == "review"


async def test_a_spent_cycle_budget_goes_to_needs_decision(db, monkeypatch):
    task_id = await _scene(db, monkeypatch, "jcr-limit", review_cycle=3)

    assert await apply_judge_changes_requests(db) == 1

    task = (await _verdict(db, task_id))["task"]
    assert task["status"] == "needs_decision"
    assert task["review_cycle"] == 3
    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "escalated"
    events = await fetchall(
        db,
        "SELECT payload FROM events WHERE task_id=? AND kind LIKE '%needs_decision%'",
        (task_id,),
    )
    assert any("review_cycle_limit" in dict(e)["payload"] for e in events)
    queue = (await inbox_decisions.collect(db, {}))["queue"]
    assert not any(e["task_id"] == task_id and e["action"] == "verdict" for e in queue)
    assert await apply_judge_changes_requests(db) == 0


async def test_a_claim_without_an_address_stays_with_its_holder(db, monkeypatch):
    """Присутствие знает только реестр: захват без адреса - не «ушёл»."""
    task_id = await _scene(db, monkeypatch, "jcr-noaddr")
    await repo.update_task(db, task_id, claimed_by="dev", claim_session_id="")
    await db.commit()

    await apply_judge_changes_requests(db)

    task = (await _verdict(db, task_id))["task"]
    assert task["status"] == "running" and task["claimed_by"] == "dev"
