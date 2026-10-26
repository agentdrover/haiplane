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


async def _online(db, task_id: int, session: str = "sess-2053") -> None:
    await services.register_session(
        db, SessionRegister(session_id=session), agent="dev", principal_id=None
    )
    await repo.update_task(
        db, task_id, claimed_by="dev", claim_session_id=session, claimed_at="x"
    )
    await db.commit()


async def _offline(db, task_id: int, session: str = "sess-2053") -> None:
    await _online(db, task_id, session)
    await db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now', '-1 day') "
        "WHERE session_id = ?",
        (session,),
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


async def test_the_budget_escalation_of_an_old_generation_does_not_touch_the_new_one(
    db, monkeypatch
):
    """Codex P1: суждение gen 1, исчерпанный бюджет, а сдача уже gen 2."""
    import pytest
    from fastapi import HTTPException

    from hub.services.steward_applied import _hand_to_the_human

    task_id = await _scene(db, monkeypatch, "jcr-fence", review_cycle=3)
    # Решение принято на сдачу 1, пересдача случилась до записи эскалации.
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()

    with pytest.raises(HTTPException):
        await _hand_to_the_human(db, task_id, 3, 1)

    assert dict(await repo.get_task(db, task_id))["status"] == "review"


async def test_a_lost_mark_or_a_standing_verdict_stops_the_budget_escalation(
    db, monkeypatch
):
    import pytest
    from fastapi import HTTPException

    from hub.services.steward_applied import apply_judgement

    task_id = await _scene(db, monkeypatch, "jcr-fence2", review_cycle=3)
    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    with pytest.raises(HTTPException):  # метки применения нет
        await apply_judgement(db, task_id, 1, claim=(row["id"], "no-such-stamp"))
    assert dict(await repo.get_task(db, task_id))["status"] == "review"


async def test_revoked_delegation_between_the_check_and_the_write_stops_the_return(
    db, monkeypatch
):
    """Codex P1: проект забрал вердикт после ранней проверки - возврата нет."""
    from hub.services import steward_advisor

    task_id = await _scene(db, monkeypatch, "jcr-revoke")
    await _online(db, task_id)

    real = steward_advisor._stamp_now

    async def stamp_then_revoke(conn):
        await conn.execute("UPDATE projects SET gate_policy='{}'")
        await conn.commit()
        return await real(conn)

    monkeypatch.setattr(steward_advisor, "_stamp_now", stamp_then_revoke)

    await apply_judge_changes_requests(db)

    task = (await _verdict(db, task_id))["task"]
    assert task["status"] == "review" and not task["review_verdict"]
    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "failed"
    assert (
        "policy" in row["advisor_outcome_detail"]
        or "verdict" in (row["advisor_outcome_detail"])
    )


async def test_act_withdrawn_between_the_check_and_the_write_stops_the_return(
    db, monkeypatch
):
    from hub.services import steward_advisor, steward_shadow

    task_id = await _scene(db, monkeypatch, "jcr-act-off")
    await _online(db, task_id)
    real = steward_advisor._stamp_now

    async def stamp_then_shadow(conn):
        async def not_granted(_db):
            return False

        monkeypatch.setattr(steward_shadow, "act_granted_now", not_granted)
        return await real(conn)

    monkeypatch.setattr(steward_advisor, "_stamp_now", stamp_then_shadow)

    await apply_judge_changes_requests(db)

    assert dict(await repo.get_task(db, task_id))["status"] == "review"


async def test_judge_uids_are_advertised_only_when_the_judge_brought_one(db):
    from hub.services.steward_applied import judge_findings

    got = judge_findings({"findings": json.dumps(FINDINGS)})
    assert "[uid abc123]" in got[0].message
    assert "uid" not in got[1].message


async def _silent_for(db, minutes: int, session: str = "sess-2053") -> None:
    await db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now', ?) "
        "WHERE session_id = ?",
        (f"-{minutes} minutes", session),
    )
    await db.commit()


async def _human_cr(db, task_id: int) -> None:
    from hub.services.lifecycle import record_review_verdict

    await record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(
            agent="rev", verdict=ReviewVerdict.changes_requested, comments="чинить"
        ),
    )


async def _alerts(db, task_id: int) -> list[str]:
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if "исполнитель молчит" in dict(u)["content"]
    ]


async def _unchanged(db, task_id: int) -> None:
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running"
    assert task["claimed_by"] == "dev" and task["claim_session_id"] == "sess-2053"


async def test_a_silent_claimer_is_named_once_per_generation_and_keeps_the_task(
    db, monkeypatch
):
    from hub.services import rework_silence

    task_id = await _scene(db, monkeypatch, "jcr-silent")
    await _offline(db, task_id)

    await apply_judge_changes_requests(db)

    await _unchanged(db, task_id)
    alerts = await _alerts(db, task_id)
    assert len(alerts) == 1 and "60 мин" in alerts[0]
    # Повтор на том же поколении молчит; следующая сдача - новая запись.
    assert not await rework_silence.note_if_silent(db, task_id)
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()
    assert await rework_silence.note_if_silent(db, task_id)
    assert len(await _alerts(db, task_id)) == 2
    await _unchanged(db, task_id)


async def test_a_human_changes_request_names_a_silent_claimer_too(db, monkeypatch):
    project_id = await _project(db, "jcr-human-silent")
    task_id = await _client_task(db, project_id)
    await _offline(db, task_id)

    await _human_cr(db, task_id)

    await _unchanged(db, task_id)
    assert len(await _alerts(db, task_id)) == 1


async def test_an_active_claimer_gets_no_alert(db, monkeypatch):
    """Свежая запись исполнителя, живой слот, молчание короче окна - подсказки нет."""
    project_id = await _project(db, "jcr-active")
    quiet_updates = await _client_task(db, project_id)
    await _offline(db, quiet_updates, "s-quiet")
    await repo.add_task_update(db, quiet_updates, "dev", "status", "правлю")

    short = await _client_task(db, project_id)
    await _online(db, short, "s-short")
    await _silent_for(db, 11, "s-short")

    slot = await _client_task(db, project_id)
    await _offline(db, slot, "s-slot")
    await db.execute(
        "INSERT INTO executor_slots (task_id, channel, slot, captured_at) "
        "VALUES (?, 'slot', 'slot-1', datetime('now'))",
        (slot,),
    )
    await db.commit()

    for task_id in (quiet_updates, short, slot):
        await _human_cr(db, task_id)
        task = dict(await repo.get_task(db, task_id))
        assert task["status"] == "running" and task["claimed_by"] == "dev"
        assert await _alerts(db, task_id) == []


async def test_the_project_window_sets_the_alert_threshold(db, monkeypatch):
    task_id = await _scene(db, monkeypatch, "jcr-window")
    await db.execute(
        "UPDATE projects SET gate_policy=?",
        (json.dumps({"verdict": "steward", "slot_dead_minutes": 5}),),
    )
    await _online(db, task_id)
    await _silent_for(db, 11)

    await apply_judge_changes_requests(db)

    await _unchanged(db, task_id)
    assert "5 мин" in (await _alerts(db, task_id))[0]


async def test_a_failing_silence_check_never_blocks_the_verdict(db, monkeypatch):
    from hub.services import rework_silence

    async def boom(*_a, **_k):
        raise RuntimeError("signal broke")

    monkeypatch.setattr(rework_silence, "silence_minutes", boom)
    project_id = await _project(db, "jcr-boom")
    task_id = await _client_task(db, project_id)
    await _offline(db, task_id)

    await _human_cr(db, task_id)

    assert dict(await repo.get_task(db, task_id))["review_verdict"] == (
        "changes_requested"
    )
    assert await _alerts(db, task_id) == []


async def test_a_shadow_only_project_is_skipped_without_a_trace(db, monkeypatch):
    """Codex 4 P2: тень - не делегирование; суждение не занимается и не падает."""
    task_id = await _scene(db, monkeypatch, "jcr-shadow-only")
    await db.execute(
        "UPDATE projects SET gate_policy=?",
        (json.dumps({"steward_shadow": True}),),
    )
    await db.commit()

    assert await apply_judge_changes_requests(db) == 0

    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["advisor_outcome"] == ""
    assert dict(await repo.get_task(db, task_id))["status"] == "review"
    assert await _alerts(db, task_id) == []
    # Делегирование позже применяет то же суждение.
    await db.execute(
        "UPDATE projects SET gate_policy=?", (json.dumps({"verdict": "steward"}),)
    )
    await db.commit()
    assert await apply_judge_changes_requests(db) == 1


def test_more_findings_than_a_verdict_holds_are_folded_not_lost():
    from hub.models import TaskReviewVerdict
    from hub.services.steward_applied import judge_findings

    for total in (50, 51, 100):
        raw = [
            {"finding_uid": f"u{n:03d}", "title": f"дефект {n}", "severity": "low"}
            for n in range(1, total + 1)
        ]
        raw[-1]["severity"] = "high"
        got = judge_findings({"findings": json.dumps(raw)})
        assert len(got) == 50
        # Вердикт принимает их без ValidationError.
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested, comments="x", findings=got
        )
        if total == 50:
            assert "Ещё" not in got[-1].message
            continue
        folded = total - 49
        assert got[-1].message.startswith(f"Ещё {folded} находок")
        assert "[uid u050]" in got[-1].message
        assert got[-1].severity.value == "high"


async def test_a_changes_request_with_100_findings_is_applied(db, monkeypatch):
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    _grant_act(monkeypatch)
    project_id = await _project(db, "jcr-100")
    task_id = await _client_task(db, project_id)
    await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=1,
            kind="verdict",
            verdict="changes_requested",
            confidence="high",
            grounds=[{"source": "ci_pinned_sha", "detail": "ci 1 ok"}],
            findings=[{"title": f"дефект {n}"} for n in range(100)],
            model="gpt-5.3-codex",
        ),
        TokenIdentity("steward-bot", "steward", principal_id=42),
    )

    assert await apply_judge_changes_requests(db) == 1

    got = await _verdict(db, task_id)
    assert got["task"]["status"] == "running" and len(got["findings"]) == 50


async def _stick(db, task_id: int) -> None:
    await db.execute(
        "UPDATE steward_judgements SET advisor_outcome='applying', "
        "advisor_claimed_at=datetime('now', '-61 minutes') "
        "WHERE task_id=? AND kind='verdict'",
        (task_id,),
    )
    await db.commit()


async def test_an_interrupted_changes_request_claim_is_reapplied_once(db, monkeypatch):
    """#2053: обрыв применения CR не уходит как approve - сдача возвращается автору."""
    from hub.services import steward_advisor

    task_id = await _scene(db, monkeypatch, "jcr-interrupt")
    await _online(db, task_id)
    await _stick(db, task_id)
    assert await apply_judge_changes_requests(db) == 0, "метка занята"

    await steward_advisor._escalate_interrupted(db)
    assert await apply_judge_changes_requests(db) == 1

    got = await _verdict(db, task_id)
    assert got["task"]["status"] == "running" and got["task"]["review_cycle"] == 1
    assert len(got["findings"]) == 2
    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "returned"
    assert await apply_judge_changes_requests(db) == 0


async def test_a_twice_interrupted_changes_request_goes_to_the_human(db, monkeypatch):
    from hub.services import steward_advisor

    task_id = await _scene(db, monkeypatch, "jcr-interrupt2")
    await _online(db, task_id)
    await _stick(db, task_id)
    await steward_advisor._escalate_interrupted(db)
    await _stick(db, task_id)  # повтор тоже оборвался

    await steward_advisor._escalate_interrupted(db)

    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "escalated"
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if "Применение советника прервано" in dict(u)["content"]
    ]
    assert len(alerts) == 1 and "возврат на доработку" in alerts[0]
    assert "approve судьи" not in alerts[0]


def test_folded_findings_keep_file_line_and_mark_truncation():
    from hub.services.steward_applied import judge_findings

    raw = [
        {"title": f"дефект {n} " + "я" * 60, "file": f"hub/f{n}.py", "line": n}
        for n in range(1, 101)
    ]
    got = judge_findings({"findings": json.dumps(raw)})
    msg = got[-1].message
    assert len(msg) <= 2000
    assert "hub/f50.py:50 " in msg
    shown = sum(1 for n in range(50, 101) if f"hub/f{n}.py:{n} " in msg)
    hidden = 51 - shown
    assert hidden > 0 and msg.endswith(f"… ещё {hidden} не показано")

    small = judge_findings({"findings": json.dumps(raw[:51])})
    assert "не показано" not in small[-1].message
    assert "hub/f51.py:51 " in small[-1].message


async def _alerts_of(db, task_id: int) -> list[str]:
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if "Применение советника прервано" in dict(u)["content"]
    ]


async def test_a_claim_left_after_the_verdict_was_written_closes_as_returned(
    db, monkeypatch
):
    """(a) Вердикт уже записан, метка осталась: returned, без тревоги и без «причины»."""
    from hub.services import steward_advisor

    task_id = await _scene(db, monkeypatch, "jcr-written")
    await _online(db, task_id)
    assert await apply_judge_changes_requests(db) == 1
    await _stick(db, task_id)  # исход так и не записался

    await steward_advisor._escalate_interrupted(db)

    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "returned"
    assert await _alerts_of(db, task_id) == []
    assert (await _verdict(db, task_id))["task"]["review_cycle"] == 1


async def test_a_retry_that_wrote_the_verdict_and_died_is_not_called_unapplied(
    db, monkeypatch
):
    """(b) Первый обрыв до записи, повтор записал вердикт и умер: не «не применено»."""
    from hub.services import steward_advisor

    task_id = await _scene(db, monkeypatch, "jcr-written2")
    await _online(db, task_id)
    await _stick(db, task_id)
    await steward_advisor._escalate_interrupted(db)
    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert not row["advisor_outcome_detail"], "счётчик не светится как причина отказа"
    assert await apply_judge_changes_requests(db) == 1
    await _stick(db, task_id)

    await steward_advisor._escalate_interrupted(db)

    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "returned"
    assert await _alerts_of(db, task_id) == []


def test_folded_findings_that_fit_exactly_are_all_shown():
    from hub.services.steward_applied import _fit_folded

    head = "h" * 50
    room = 2000 - len(head)
    first = "a" * ((room - 3) // 2)
    second = "b" * (room - 3 - len(first))
    got = _fit_folded(head, [first, second])
    assert got == first + " | " + second and len(head + got) == 2000


def test_a_long_folded_finding_does_not_hide_the_short_ones_after_it():
    from hub.services.steward_applied import _fit_folded

    head = "h" * 50
    parts = ["hub/a.py:1 коротко", "hub/b.py:2 " + "я" * 3000, "hub/c.py:3 хвост"]
    got = _fit_folded(head, parts)
    assert len(head + got) <= 2000
    assert "hub/a.py:1" in got and "hub/b.py:2" in got and "hub/c.py:3 хвост" in got
    assert "не показано" not in got or got.endswith("не показано")


def test_the_folded_text_never_exceeds_the_limit_whatever_the_lengths():
    import random

    from hub.services.steward_applied import _fit_folded

    head = "h" * 50
    reproduced = ["a" * 1770] + ["b" * 2000] * 10 + ["c" * 157]
    got = _fit_folded(head, reproduced)
    assert len(head + got) <= 2000 and "не показано" in got
    rng = random.Random(2053)
    for _ in range(500):
        parts = [
            "x" * rng.choice([5, 80, 400, 1700, 2000, 2400])
            for _ in range(rng.randint(2, 60))
        ]
        out = _fit_folded(head, parts)
        assert len(head + out) <= 2000, [len(p) for p in parts]
        omitted = (
            out.rsplit("ещё ", 1)[-1].split(" ")[0] if "не показано" in out else "0"
        )
        assert int(omitted) <= len(parts)


async def test_a_human_changes_request_over_a_stale_claim_is_not_ours(db, monkeypatch):
    """Человек записал CR, пока метка висела: закрываем без returned, тревоги и повтора."""
    from hub.services import steward_advisor

    task_id = await _scene(db, monkeypatch, "jcr-human")
    await _online(db, task_id)
    await _stick(db, task_id)
    await repo.update_task(
        db,
        task_id,
        status="running",
        review_verdict="changes_requested",
        review_verdict_generation=1,
    )
    await db.commit()

    await steward_advisor._escalate_interrupted(db)

    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "skipped"
    assert await _alerts_of(db, task_id) == []


def test_pieces_that_fit_without_a_suffix_are_all_kept():
    from hub.services.steward_applied import _fit_folded

    head = "h" * 50
    parts = ["a" * 162] + ["b" * 160] * 7 + ["c" * 159] * 4
    # Берём бюджет впритык: ровно без пометки.
    total = len(" | ".join(parts))
    got = _fit_folded("h" * (2000 - total), parts)
    assert got == " | ".join(parts) and "ещё" not in got
    out = _fit_folded(head, parts)
    # 1951 знак при бюджете 1950: режется одна находка, но все 12 на месте.
    assert len(head + out) <= 2000 and "ещё" not in out
    assert len(out.split(" | ")) == 12


async def test_a_claim_whose_task_was_resubmitted_before_recovery_closes(
    db, monkeypatch
):
    """Автор пересдал (gen 2) до разбора: по записанному факту returned, без ложного повтора."""
    from hub.services import steward_advisor

    task_id = await _scene(db, monkeypatch, "jcr-resubmit")
    await _online(db, task_id)
    assert await apply_judge_changes_requests(db) == 1
    await _stick(db, task_id)
    await repo.update_task(db, task_id, status="review", submission_generation=2)
    await db.commit()

    await steward_advisor._escalate_interrupted(db)

    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "returned"
    assert await _alerts_of(db, task_id) == []
    retries = await fetchall(db, "SELECT 1 FROM events WHERE kind='steward_cr_retry'")
    assert not retries


async def test_a_claim_of_a_stale_generation_without_a_verdict_closes_superseded(
    db, monkeypatch
):
    from hub.services import steward_advisor

    task_id = await _scene(db, monkeypatch, "jcr-superseded")
    await _online(db, task_id)
    await _stick(db, task_id)
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()

    await steward_advisor._escalate_interrupted(db)

    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "skipped"
    assert await _alerts_of(db, task_id) == []
    assert not await fetchall(db, "SELECT 1 FROM events WHERE kind='steward_cr_retry'")
