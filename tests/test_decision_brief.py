"""Справка для решения без LLM (#1810): что произошло, риски, рекомендация, проверки.

AC-1: «Принять» только когда ВСЕ доказательства чисты.
AC-2: словарь стюарда переводится в словарь формы; старое поколение не слушаем.
AC-3: у каждого состояния своё действие; у state-задачи нет проверок кода.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub import repository_ci_integrity as integrity_store
from hub.services import decision_brief
from tests.state_support import drive_to_review, make_state_task
from tests.test_auto_verdict import _post_review, _submitted_task

ACCEPT = "Принять"


async def _brief(db: aiosqlite.Connection, task_id: int, ctx: dict | None = None):
    task = dict(await repo.get_task(db, task_id) or {})
    return await decision_brief.build(db, task, ctx)


async def _review_task(client, db, monkeypatch, slug: str, **review) -> int:
    # Автопилот выключен, чтобы он не вынес вердикт раньше показа справки.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    task_id = await _submitted_task(client, db, slug, {"verdict": "human"})
    await _post_review(client, task_id, **review)
    return task_id


async def _clean_scan(db, task_id: int, *, reviewer: bool = True) -> None:
    """Целостность CI чиста; у отчёта есть личность ревьюера (если ``reviewer``)."""
    task = dict(await repo.get_task(db, task_id) or {})
    await integrity_store.save_ci_integrity_scan(
        db, task_id, task["submission_sha"], "clean", "[]", ""
    )
    if reviewer:
        await db.execute(
            "UPDATE machine_reviews SET principal_id=9 WHERE task_id=?", (task_id,)
        )
    # Работу ревьюера подтверждает провайдер: без этого «Принять» невозможно.
    await db.execute(
        "UPDATE machine_reviews SET provider_tokens=250000 WHERE task_id=?",
        (task_id,),
    )
    await db.commit()


def _shape(brief: dict) -> None:
    assert set(brief) >= {"happened", "need", "risks", "recommend", "checks"}
    assert len(brief["risks"]) <= 3
    assert set(brief["recommend"]) >= {"action", "verdict", "why"}


async def test_accept_only_when_all_evidence_clean(
    client, db: aiosqlite.Connection, monkeypatch
):
    clean = await _review_task(client, db, monkeypatch, "brief-clean")
    await _clean_scan(db, clean)
    brief = await _brief(db, clean)
    _shape(brief)
    assert brief["recommend"]["action"] == ACCEPT
    assert brief["recommend"]["verdict"] == "approved"

    doubts: dict[str, int] = {}
    doubts["unresolved"] = await _review_task(
        client,
        db,
        monkeypatch,
        "brief-unresolved",
        unresolved=[{"title": "не удалось проверить", "why": "агент упал"}],
    )
    doubts["incomplete"] = await _review_task(
        client, db, monkeypatch, "brief-incomplete", incomplete=True
    )
    doubts["self_report"] = await _review_task(client, db, monkeypatch, "brief-self")
    await db.execute(
        "UPDATE machine_reviews SET self_reviewed=1 WHERE task_id=?",
        (doubts["self_report"],),
    )
    doubts["no_provenance"] = await _review_task(client, db, monkeypatch, "brief-prov")
    await db.execute(
        "UPDATE ci_run_reports SET trust='unverified', trust_reason='no_provenance' "
        "WHERE task_id=?",
        (doubts["no_provenance"],),
    )
    doubts["integrity"] = await _review_task(client, db, monkeypatch, "brief-integ")
    task = dict(await repo.get_task(db, doubts["integrity"]) or {})
    await integrity_store.save_ci_integrity_scan(
        db,
        doubts["integrity"],
        task["submission_sha"],
        "signs",
        json.dumps([{"path": ".github/workflows/ci.yml", "rule": "workflow"}]),
        "",
    )
    await db.commit()
    for name, task_id in doubts.items():
        if name != "integrity":
            await _clean_scan(db, task_id)
        got = await _brief(db, task_id)
        assert got["recommend"]["action"] != ACCEPT, name
        assert got["recommend"]["verdict"] != "approved", name
        assert got["recommend"]["why"], f"{name}: причина не названа"
        assert got["risks"], f"{name}: риск не назван"

    confirmed = await _review_task(
        client,
        db,
        monkeypatch,
        "brief-confirmed",
        findings_confirmed=[
            {
                "title": "сломан возврат",
                "category": "correctness",
                "severity": "high",
                "file": "hub/x.py",
                "line": 1,
                "locator": "lines",
                "start_line": 1,
                "detail": "возвращает None",
            }
        ],
    )
    await _clean_scan(db, confirmed)
    got = await _brief(db, confirmed)
    assert got["recommend"]["action"] == "На доработку"
    assert got["recommend"]["verdict"] == "changes_requested"


async def _judgement(db, task_id: int, generation: int, verdict: str, **extra) -> None:
    await repo.insert_steward_judgement(
        db,
        task_id=task_id,
        generation=generation,
        kind="verdict",
        submitted_verdict=verdict,
        verdict=verdict,
        escalate_reason=extra.get("escalate_reason", ""),
        grounds=json.dumps(
            [{"source": "ci_report", "detail": "TOKEN=hunter2 из лога"}]
        ),
    )
    await db.commit()


async def test_steward_vocabulary_mapping(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id = await _review_task(client, db, monkeypatch, "brief-steward")
    await _clean_scan(db, task_id)
    generation = int(
        dict(await repo.get_task(db, task_id) or {})["submission_generation"]
    )
    # Суждение СТАРОГО поколения не слушаем: без него — правила, и они чисты.
    await _judgement(db, task_id, generation - 1, "changes_requested")
    stale = await _brief(db, task_id)
    assert stale["recommend"]["action"] == ACCEPT

    await _judgement(db, task_id, generation, "changes_requested")
    mapped = await _brief(db, task_id)
    assert mapped["recommend"]["action"] == "На доработку"
    assert mapped["recommend"]["verdict"] == "changes_requested"
    assert "ci_report" in " ".join(mapped["recommend"]["why"])
    assert "hunter2" not in decision_brief.recommend_line(mapped), "detail не выводим"

    second = await _review_task(client, db, monkeypatch, "brief-steward-esc")
    await _clean_scan(db, second)
    gen2 = int(dict(await repo.get_task(db, second) or {})["submission_generation"])
    await _judgement(db, second, gen2, "escalate", escalate_reason="нужен человек")
    esc = await _brief(db, second)
    assert esc["recommend"]["action"] == "Решить вам"
    assert esc["recommend"]["verdict"] == "", "действия по умолчанию нет"
    assert "причина в суждении" in " ".join(esc["recommend"]["why"])
    assert "нужен человек" not in decision_brief.recommend_line(esc)

    third = await _review_task(client, db, monkeypatch, "brief-steward-ok")
    await _clean_scan(db, third)
    gen3 = int(dict(await repo.get_task(db, third) or {})["submission_generation"])
    await _judgement(db, third, gen3, "approve")
    ok = await _brief(db, third)
    assert ok["recommend"]["action"] == ACCEPT
    assert ok["recommend"]["verdict"] == "approved"
    # Вердикт за человеком по маршруту — справка так и говорит.
    assert "вам" in " ".join(ok["recommend"]["why"]).lower() or ok["need"]


async def test_steward_approve_is_downgraded_by_doubt(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id = await _review_task(
        client,
        db,
        monkeypatch,
        "brief-steward-doubt",
        unresolved=[{"title": "не проверено", "why": "агент упал"}],
    )
    await _clean_scan(db, task_id)
    gen = int(dict(await repo.get_task(db, task_id) or {})["submission_generation"])
    await _judgement(db, task_id, gen, "approve")
    got = await _brief(db, task_id)
    assert got["recommend"]["action"] != ACCEPT


async def _task_in(db, status: str, **fields) -> int:
    task_id = await repo.create_task(
        db,
        title=f"brief {status}",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="bot",
        rationale="",
        status=status,
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    if fields:
        await repo.update_task(db, task_id, **fields)
    await db.commit()
    return task_id


async def test_brief_per_state(client, db: aiosqlite.Connection, monkeypatch):
    ready = await _task_in(db, "draft", dor_passed=1, readiness_score=90)
    got = await _brief(db, ready)
    _shape(got)
    assert got["recommend"]["action"] == "Одобрить"

    not_ready = await _task_in(db, "draft", dor_passed=0, readiness_score=20)
    got = await _brief(db, not_ready)
    assert got["recommend"]["action"] == "Доработать постановку"
    assert got["recommend"]["why"]

    stalled = await _task_in(db, "needs_decision")
    await repo.add_task_update(db, stalled, "hub", "alert", "гейт остановился")
    got = await _brief(db, stalled)
    assert got["recommend"]["action"] == "Решить вам"
    assert got["recommend"]["why"]

    repair = await _task_in(db, "running", review_verdict="changes_requested")
    got = await _brief(db, repair, {"repair_offered": True})
    assert got["recommend"]["action"] == "Повторный прогон"
    plain = await _task_in(db, "running")
    got = await _brief(db, plain, {"repair_offered": False})
    assert got["recommend"]["action"] == "Идёт работа"

    in_prod = await _task_in(db, "completed")
    got = await _brief(db, in_prod, {"delivery": {"state": "in_prod"}})
    assert got["recommend"]["action"] == "Доставлено"
    for state in ("not_in_prod", "unknown"):
        merged = await _task_in(db, "completed")
        got = await _brief(db, merged, {"delivery": {"state": state}})
        assert got["recommend"]["action"] == "Влито, выкат не подтверждён", state

    state_id = await make_state_task(db)
    await drive_to_review(client, db, state_id)
    got = await _brief(db, state_id)
    _shape(got)
    names = " ".join(c["name"] for c in got["checks"]).lower()
    for absent in ("ci", "машинн", "достав"):
        assert absent not in names, f"у state-задачи нет проверки «{absent}»"
    risks = " ".join(r["text"] for r in got["risks"])
    assert "CI не доказан" not in risks
    assert got["recommend"]["action"] in (ACCEPT, "Проверить")
    assert "rollback" in " ".join(c["name"] + c["detail"] for c in got["checks"])

    partial = await make_state_task(db)
    await drive_to_review(client, db, partial)
    await db.execute(
        "DELETE FROM task_evidence WHERE task_id=? AND ac_id='AC-2'", (partial,)
    )
    await db.commit()
    got = await _brief(db, partial)
    assert got["recommend"]["action"] != ACCEPT
    assert got["risks"]


async def test_unreadable_evidence_is_unknown_not_clean(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Нет отчёта CI — «неизвестно», и «Принять» невозможно."""
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    task_id = await _submitted_task(
        client, db, "brief-no-ci", {"verdict": "human"}, with_ci=False
    )
    await _post_review(client, task_id)
    await _clean_scan(db, task_id)
    got = await _brief(db, task_id)
    assert got["recommend"]["action"] != ACCEPT
    assert any("CI" in r["text"] for r in got["risks"])


@pytest.mark.parametrize("count", [5])
async def test_risks_are_capped_and_sorted(
    client, db: aiosqlite.Connection, monkeypatch, count: int
):
    task_id = await _review_task(
        client,
        db,
        monkeypatch,
        "brief-risks",
        unresolved=[{"title": "a", "why": "b"}],
        findings_confirmed=[
            {
                "title": "t",
                "category": "correctness",
                "severity": "high",
                "file": "hub/x.py",
                "line": 1,
                "locator": "lines",
                "start_line": 1,
                "detail": "d",
            }
        ],
    )
    await repo.update_task(
        db,
        task_id,
        risks=json.dumps(
            [
                {"kind": "technical", "severity": "low", "description": f"r{i}"}
                for i in range(count)
            ]
        ),
    )
    await db.commit()
    got = await _brief(db, task_id)
    assert len(got["risks"]) == 3
    weights = [r["severity"] for r in got["risks"]]
    order = {"high": 3, "medium": 2, "low": 1}
    assert [order[w] for w in weights] == sorted(
        (order[w] for w in weights), reverse=True
    )


async def _second_report(client, task_id: int, **review) -> None:
    await _post_review(client, task_id, **review)


async def test_every_report_of_the_generation_counts(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Находка 1-2 ревью: смотрим ВСЕ отчёты поколения, а не последний."""
    finding = {
        "title": "сломан возврат",
        "category": "correctness",
        "severity": "high",
        "file": "hub/x.py",
        "line": 1,
        "locator": "lines",
        "start_line": 1,
        "detail": "возвращает None",
    }
    late_incomplete = await _review_task(client, db, monkeypatch, "brief-late-inc")
    await _second_report(client, late_incomplete, incomplete=True)
    await _clean_scan(db, late_incomplete)
    got = await _brief(db, late_incomplete)
    assert got["recommend"]["action"] != ACCEPT, "неполный отчёт позже полного"

    conflict = await _review_task(
        client, db, monkeypatch, "brief-conflict", findings_confirmed=[finding]
    )
    await _second_report(client, conflict)
    await _clean_scan(db, conflict)
    got = await _brief(db, conflict)
    assert got["recommend"]["action"] != ACCEPT, "дефект у другого ревьюера"
    assert any("подтвердил" in r["text"].lower() for r in got["risks"])


async def test_reviewer_identity_is_required_for_accept(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id = await _review_task(client, db, monkeypatch, "brief-no-principal")
    await _clean_scan(db, task_id, reviewer=False)
    got = await _brief(db, task_id)
    assert got["recommend"]["action"] != ACCEPT
    assert any("Независимость" in r["text"] for r in got["risks"])


async def test_unresolved_outcome_is_found_by_its_own_uid(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Находка 4: исход пишется по unresolved_uids; not_judged не закрывает."""
    from hub.services.finding_identity import unresolved_uids

    item = {"title": "не удалось проверить", "why": "агент упал"}
    task_id = await _review_task(
        client, db, monkeypatch, "brief-outcome", unresolved=[item]
    )
    await _clean_scan(db, task_id)
    task = dict(await repo.get_task(db, task_id) or {})
    review = dict(await repo.get_latest_machine_review(db, task_id) or {})
    (uid,) = unresolved_uids([item])

    async def record(outcome: str) -> None:
        await repo.upsert_finding_outcome(
            db,
            review_id=review["id"],
            task_id=task_id,
            submission_generation=task["submission_generation"],
            finding_uid=uid,
            finding_index=-1,
            finding_title=item["title"],
            outcome=outcome,
            note="",
            linked_task_id=None,
            reported_by="dev",
            finding_kind="unresolved",
        )
        await db.commit()

    assert (await _brief(db, task_id))["recommend"]["action"] != ACCEPT
    await record("not_judged")
    assert (await _brief(db, task_id))["recommend"]["action"] != ACCEPT
    await record("fixed")
    got = await _brief(db, task_id)
    assert got["recommend"]["action"] == ACCEPT, got["recommend"]


async def test_report_check_follows_the_doubts_not_the_wording(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Находка 6: «Машинное ревью» не зелёная при неотвеченных unresolved."""
    task_id = await _review_task(
        client,
        db,
        monkeypatch,
        "brief-check",
        unresolved=[{"title": "не удалось", "why": "x"}],
    )
    await _clean_scan(db, task_id)
    got = await _brief(db, task_id)
    review_check = next(c for c in got["checks"] if c["name"] == "Машинное ревью")
    assert review_check["state"] == "doubt"


async def test_unresolved_in_a_sibling_report_blocks_accept(
    client, db: aiosqlite.Connection, monkeypatch
):
    """P1 gen2: неотвеченный unresolved в НЕ последнем отчёте поколения."""
    task_id = await _review_task(
        client,
        db,
        monkeypatch,
        "brief-sibling-unres",
        unresolved=[{"title": "не удалось проверить", "why": "агент упал"}],
    )
    await _second_report(client, task_id)  # чистый отчёт B позже
    await _clean_scan(db, task_id)
    got = await _brief(db, task_id)
    assert got["recommend"]["action"] != ACCEPT
    review_check = next(c for c in got["checks"] if c["name"] == "Машинное ревью")
    assert review_check["state"] == "doubt"


async def test_a_report_without_data_never_earns_accept(
    client, db: aiosqlite.Connection, monkeypatch
):
    """P1 gen2: «Принять» — только когда КАЖДЫЙ отчёт доказывает, что проверка была."""
    alone = await _review_task(
        client,
        db,
        monkeypatch,
        "brief-empty-alone",
        raw_count=0,
        findings_rejected=[],
        tokens_spent=240_000,
    )
    await _clean_scan(db, alone)
    got = await _brief(db, alone)
    assert got["recommend"]["action"] != ACCEPT, "отчёт с raw_count=0 — нет данных"

    after_full = await _review_task(client, db, monkeypatch, "brief-empty-after")
    await _second_report(client, after_full, raw_count=0, findings_rejected=[])
    await _clean_scan(db, after_full)
    got = await _brief(db, after_full)
    assert got["recommend"]["action"] != ACCEPT, "полный отчёт, затем пустой"
    review_check = next(c for c in got["checks"] if c["name"] == "Машинное ревью")
    assert review_check["state"] == "doubt"
