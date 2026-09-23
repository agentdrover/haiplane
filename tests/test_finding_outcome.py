"""Находки отчёта не теряются за сдачей без ревью (#1331).

23.09 на #1231 и #1234 сдачи 2 и 3 остались без отчёта (лимит провайдера), и
на сдаче 4 finding_outcomes по находкам отчёта сдачи 1 получили 422: набор
открытых находок собирался только из поколения, которое сдача заменяет. У
этого поколения отчёта не было — и находки, за которыми работу вернули,
становились недостижимыми: ни исхода, ни драфта по отложенному дефекту.

Правило теперь: открытые находки берутся из ПОСЛЕДНЕГО поколения с отчётом,
не старше заменяемого. Более свежий отчёт старые находки не воскрешает.
"""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.services import finding_outcome


def _finding(title: str) -> dict:
    return {
        "title": title,
        "severity": "high",
        "category": "correctness",
        "locator": "file",
        "file": "hub/db.py",
    }


async def _started(client: AsyncClient, title: str) -> int:
    resp = await client.post("/api/tasks", json={"title": title})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: работать"},
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
        unresolved=json.dumps([], ensure_ascii=False),
        incomplete=False,
    )
    await db.commit()
    return int(dict(await repo.get_latest_machine_review(db, task_id))["id"])


async def _sent_back(client: AsyncClient, task_id: int) -> None:
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )
    assert resp.status_code == 200, resp.text


async def _generation(db: aiosqlite.Connection, task_id: int) -> int:
    return int(dict(await repo.get_task(db, task_id))["submission_generation"])


async def _report_then_a_gap(
    client: AsyncClient, db: aiosqlite.Connection, title: str, finding: str
) -> tuple[int, int, str]:
    """Сдача 1 с отчётом и находкой, сдача 2 без отчёта, работа снова в running.

    Сдача 2 идёт в режиме warn: её долг — ровно та находка, о которой тест, и
    отказ здесь не дал бы дойти до сдачи 3. Возвращает ``(task_id, review_id,
    finding_uid)`` находки отчёта сдачи 1.
    """
    task_id = await _started(client, title)
    assert (await client.post(f"/api/tasks/{task_id}/submit-review", json={})).status_code == 200
    review_id = await _report(db, task_id, 1, [_finding(finding)])
    await _sent_back(client, task_id)
    assert (await client.post(f"/api/tasks/{task_id}/submit-review", json={})).status_code == 200
    # Сдача 2 ревью не получила: отчёта о поколении 2 нет вовсе — ровно форма
    # прода (#1231: отчёты 353 о сдаче 1 и 447 о сдаче 4, между ними ничего).
    assert await _generation(db, task_id) == 2
    assert await repo.machine_reviews_of_generation(db, task_id, 2) == []
    await _sent_back(client, task_id)
    uid = (await finding_outcome.open_findings(db, task_id, 1))[0]["finding_uid"]
    return task_id, review_id, uid


async def test_a_report_behind_an_unreviewed_submission_can_still_be_answered(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1: исход по находке сдачи 1 принят на сдаче 3 и записан к её отчёту."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    task_id, review_id, uid = await _report_then_a_gap(
        client, db, "Отчёт за пропуском", "утечка курсора"
    )

    # Долг на сдаче видит ту же находку: предупреждение называет её поимённо,
    # а не молчит, будто отвечать не о чем.
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    refused = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert refused.status_code == 422, refused.text
    assert "утечка курсора" in refused.text

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"finding_outcomes": [{"finding_uid": uid, "outcome": "fixed"}]},
    )
    assert resp.status_code == 200, resp.text
    assert await _generation(db, task_id) == 3

    stored = [dict(r) for r in await repo.list_finding_outcomes(db, review_id)]
    assert [(r["finding_uid"], r["outcome"]) for r in stored] == [(uid, "fixed")]
    # Исход отвечает отчёту сдачи 1 — и поколение у него то же, что у отчёта:
    # круг ревью сверяет закрытое с находками по поколению отчёта.
    assert stored[0]["submission_generation"] == 1
    assert await finding_outcome.open_findings(db, task_id, 2) == []


async def test_a_deferred_finding_behind_an_unreviewed_submission_leaves_a_draft(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2: отложенный дефект за пропуском рождает драфт, как обычно."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    task_id, review_id, uid = await _report_then_a_gap(
        client, db, "Отложенное за пропуском", "гонка при записи"
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {
                    "finding_uid": uid,
                    "outcome": "deferred",
                    "note": "чинится отдельной задачей",
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text

    drafts = await repo.list_tasks_by_status(db, "draft", limit=50)
    spawned = [dict(r) for r in drafts if dict(r).get("caused_by_task_id") == task_id]
    assert len(spawned) == 1, "отложенный дефект остаётся работой"
    assert spawned[0]["title"] == "гонка при записи"
    assert uid in spawned[0]["description"]
    stored = [dict(r) for r in await repo.list_finding_outcomes(db, review_id)]
    assert [r["outcome"] for r in stored] == ["deferred"]


async def test_a_newer_report_does_not_revive_an_older_finding(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3: был отчёт о сдаче 2 — спрашивается он, находка сдачи 1 не открыта.

    Отказ на находку, которой нет в рассматриваемом отчёте, сохраняется, и
    правило «брать все отчёты задачи» его бы сломало.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    task_id = await _started(client, "Свежий отчёт главнее")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    old_review = await _report(db, task_id, 1, [_finding("старая находка")])
    await _sent_back(client, task_id)
    old_uid = (await finding_outcome.open_findings(db, task_id, 1))[0]["finding_uid"]
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await _report(db, task_id, 2, [_finding("новая находка")])
    await _sent_back(client, task_id)

    open_now = await finding_outcome.open_findings(db, task_id, 2)
    assert [i["title"] for i in open_now] == ["новая находка"]

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"finding_outcomes": [{"finding_uid": old_uid, "outcome": "fixed"}]},
    )
    assert resp.status_code == 422, resp.text
    assert "not an open confirmed finding" in resp.text
    assert await repo.list_finding_outcomes(db, old_review) == []


async def test_a_fabricated_uid_behind_a_gap_is_still_refused(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Ограничение: выдуманный uid не закрывается и за пропуском."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    task_id, _review_id, _uid = await _report_then_a_gap(
        client, db, "Выдумка за пропуском", "настоящая"
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"finding_outcomes": [{"finding_uid": "0" * 16, "outcome": "fixed"}]},
    )
    assert resp.status_code == 422
    assert "not an open confirmed finding" in resp.text


async def test_no_report_at_all_owes_nothing(
    client: AsyncClient, db: aiosqlite.Connection
):
    """Ни одного отчёта у задачи — открытых находок нет, а не исключение."""
    task_id = await _started(client, "Без отчётов")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert await finding_outcome.open_findings(db, task_id, 1) == []
    assert await finding_outcome.open_findings(db, task_id, 0) == []
