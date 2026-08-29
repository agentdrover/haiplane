"""Находка ревью заканчивается исходом, а не тишиной (#911).

Измерено до этого гейта: 47 подтверждённых находок за семь дней и ноль
суждений. Находка, на которую никто не ответил, не становится неверной — она
становится невидимой, и названный ею дефект уезжает в прод. Заодно исчезают
оба числа, по которым видно, окупается ли машинное ревью: без знаменателя не
считаются ни precision, ни цена исправленной находки.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.models import FindingOutcomeItem
from hub.services import finding_outcome


def _finding(title: str, **over) -> dict:
    base = {
        "title": title,
        "severity": "high",
        "category": "correctness",
        "locator": "file",
        "file": "hub/db.py",
    }
    base.update(over)
    return base


async def _submitted_task(client: AsyncClient, title: str) -> int:
    resp = await client.post("/api/tasks", json={"title": title})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    return task_id


async def _report(
    db: aiosqlite.Connection, task_id: int, generation: int, findings: list[dict]
) -> int:
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill="lite-diff-review",
        raw_count=len(findings),
        findings_confirmed=json.dumps(findings, ensure_ascii=False),
        incomplete=False,
    )
    await db.commit()
    row = await repo.get_latest_machine_review(db, task_id)
    return int(dict(row)["id"])


async def test_submit_requires_disposition_for_confirmed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1: сдача с незакрытыми находками отклонена, и находки НАЗВАНЫ.

    Отказ, говорящий «у вас девять незакрытых находок», отправляет автора
    искать, какие именно девять. Гейт их уже знает.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Findings owe an answer")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await _report(db, task_id, 1, [_finding("утечка курсора"), _finding("гонка")])
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )

    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 422, resp.text
    assert "утечка курсора" in resp.text and "гонка" in resp.text


async def test_no_confirmed_findings_no_requirement(client: AsyncClient, monkeypatch):
    """AC-3: где ответа не должны — гейт молчит.

    На первой сдаче отчётов нет вовсе. Правило, срабатывающее там, где отвечать
    не на что, учит обходить себя.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Nothing to answer")

    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text


async def test_wont_fix_spawns_defect_draft(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2: то, что не чинят, остаётся работой, а не исчезает вместе с решением."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Wont fix leaves a trace")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    review_id = await _report(db, task_id, 1, [_finding("тяжёлый запрос")])
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )
    uid = (await finding_outcome.open_findings(db, task_id, 1))[0]["finding_uid"]

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {
                    "finding_uid": uid,
                    "outcome": "wont_fix",
                    "note": "цена оптимизации выше цены задержки",
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text

    drafts = await repo.list_tasks_by_status(db, "draft", limit=20)
    spawned = [dict(r) for r in drafts if dict(r).get("caused_by_task_id") == task_id]
    assert len(spawned) == 1, "решение не чинить оставляет дефект-драфт"
    assert spawned[0]["found_in"] == "review"
    assert spawned[0]["work_type"] == "bug"
    assert "цена оптимизации выше цены задержки" in spawned[0]["description"]

    stored = [dict(r) for r in await repo.list_finding_outcomes(db, review_id)]
    assert [r["outcome"] for r in stored] == ["wont_fix"]


async def test_the_authors_account_is_not_a_human_judgement(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Исход автора НЕ попадает в finding_dispositions и не трогает precision.

    _disposition_metrics читает finding_dispositions без фильтра по тому, кто
    решал. Запись туда самоотчёта автора — самый дешёвый способ уничтожить
    метрику: каждое собственное «исправлено» считалось бы подтверждением
    человеком того, что находка настоящая, и precision начала бы мерить мнение
    автора о своей же работе.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Account is not judgement")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    review_id = await _report(db, task_id, 1, [_finding("лишний индекс")])
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )
    uid = (await finding_outcome.open_findings(db, task_id, 1))[0]["finding_uid"]

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"finding_outcomes": [{"finding_uid": uid, "outcome": "fixed"}]},
    )
    # Сначала убедиться, что исход ВООБЩЕ записан: без этого «в диспозициях
    # пусто» верно и тогда, когда не записано ничего никуда, и тест зелен при
    # снятом гейте.
    assert resp.status_code == 200, resp.text
    stored = [dict(r) for r in await repo.list_finding_outcomes(db, review_id)]
    assert [r["outcome"] for r in stored] == ["fixed"]

    assert await repo.list_finding_dispositions(db, review_id) == [], (
        "самоотчёт автора не является суждением человека (#876)"
    )
    metrics = await repo.fetchall(db, "SELECT COUNT(*) AS n FROM finding_dispositions")
    assert int(dict(metrics[0])["n"]) == 0


async def test_an_outcome_about_another_generation_is_refused(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Ответ про находку, которой эта сдача не несёт, — не частичный успех."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Wrong address")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await _report(db, task_id, 1, [_finding("настоящая")])
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"finding_outcomes": [{"finding_uid": "0" * 16, "outcome": "fixed"}]},
    )
    assert resp.status_code == 422
    assert "not an open confirmed finding" in resp.text


def test_an_unfixed_finding_owes_a_reason():
    """«Исправлено» видно в диффе; остальное видно только в том, что сказано."""
    with pytest.raises(ValueError):
        FindingOutcomeItem(finding_uid="abc", outcome="false_positive")
    assert FindingOutcomeItem(
        finding_uid="abc", outcome="false_positive", note="в коде этого нет"
    ).note


async def test_a_rejected_submission_leaves_no_outcome_behind(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Отказ не должен оставлять следов: иначе исправленная сдача ломается.

    Запись идёт на общий коннект процесса, и отказ его не откатывает. Если
    исходы писались до остальных гейтов, отклонённая попытка закрывала часть
    находок — и ПОЛНЫЙ повторный ответ автора падал с «находка уже не открыта»,
    то есть правильный ответ отвергался из-за собственной отброшенной попытки.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Refusal leaves nothing")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    review_id = await _report(db, task_id, 1, [_finding("A"), _finding("B")])
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )
    open_items = await finding_outcome.open_findings(db, task_id, 1)
    uid_a = next(i["finding_uid"] for i in open_items if i["title"] == "A")
    uid_b = next(i["finding_uid"] for i in open_items if i["title"] == "B")

    # Неполный ответ — отказ.
    refused = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {"finding_uid": uid_a, "outcome": "wont_fix", "note": "живём"}
            ]
        },
    )
    assert refused.status_code == 422
    assert await repo.list_finding_outcomes(db, review_id) == [], (
        "отклонённая сдача ничего не записала"
    )
    drafts = await repo.list_tasks_by_status(db, "draft", limit=20)
    assert not [r for r in drafts if dict(r).get("caused_by_task_id") == task_id], (
        "и дефект-драфт за отклонённую сдачу не завела"
    )

    # Тот же ответ, дополненный до полного, обязан пройти.
    ok = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {"finding_uid": uid_a, "outcome": "wont_fix", "note": "живём"},
                {"finding_uid": uid_b, "outcome": "fixed"},
            ]
        },
    )
    assert ok.status_code == 200, ok.text
    assert len(await repo.list_finding_outcomes(db, review_id)) == 2


async def test_one_answer_closes_the_finding_in_both_ladder_reports(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Лестница #879 описывает один дефект дважды — ответ на него один.

    finding_uid выводится из содержания, поэтому одна и та же находка в
    lite- и deep-отчёте несёт один uid. Ключ по одному uid схлопывал две
    строки в одну: ответ ложился на последний отчёт, а находка первого
    оставалась неотвеченной навсегда — ровно то исчезновение, которое мерили.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Ladder answers once")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    same = _finding("одна и та же утечка")
    lite = await _report(db, task_id, 1, [same])
    deep = await _report(db, task_id, 1, [same])
    assert lite != deep
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )
    open_items = await finding_outcome.open_findings(db, task_id, 1)
    assert len(open_items) == 2, "оба отчёта несут её и оба ждут ответа"
    uid = open_items[0]["finding_uid"]
    assert open_items[1]["finding_uid"] == uid

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"finding_outcomes": [{"finding_uid": uid, "outcome": "fixed"}]},
    )
    assert resp.status_code == 200, resp.text
    assert len(await repo.list_finding_outcomes(db, lite)) == 1
    assert len(await repo.list_finding_outcomes(db, deep)) == 1


async def test_a_named_task_replaces_the_draft(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Названная работа не дублируется: одна находка — одно место учёта."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Named task wins")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await _report(db, task_id, 1, [_finding("уже разложено")])
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )
    uid = (await finding_outcome.open_findings(db, task_id, 1))[0]["finding_uid"]

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {
                    "finding_uid": uid,
                    "outcome": "deferred",
                    "note": "вынесено отдельно",
                    "linked_task_id": task_id,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    drafts = await repo.list_tasks_by_status(db, "draft", limit=20)
    assert not [r for r in drafts if dict(r).get("caused_by_task_id") == task_id], (
        "работа уже названа автором — второе место учёта не заводится"
    )
