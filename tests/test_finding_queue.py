"""Находки переживают статус задачи, а очередь — единственный путь к ним (#1038).

Разбор находки никогда не был дорогим: форма с радиокнопками живёт в карточке
задачи с #876. Дорогим было ДОЙТИ до неё. Отчёт пишется, пока задача в review;
к моменту, когда её находки стоит судить, задача уже completed, а все разделы
инбокса собираются из ``list_tasks_by_status`` — то есть находки исчезают ровно
тогда, когда задача завершается. За семь дней так накопилось 47 подтверждённых
находок без единого ответа.
"""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub.repository import count_unjudged_findings, list_unjudged_findings
from hub.services.orchestration import practice_metrics


def _finding(title: str, **over) -> dict:
    base = {
        "title": title,
        "severity": "high",
        "category": "correctness",
        "locator": "file",
        "file": "hub/db.py",
        "detail": "",
    }
    base.update(over)
    return base


async def _task(db: aiosqlite.Connection, task_id: int, status: str, generation: int):
    await db.execute(
        "INSERT INTO tasks (id, title, status, submission_generation) "
        "VALUES (?, ?, ?, ?)",
        (task_id, f"задача {task_id}", status, generation),
    )


async def _report(
    db: aiosqlite.Connection,
    review_id: int,
    task_id: int,
    generation: int,
    findings: list[dict],
):
    await db.execute(
        "INSERT INTO machine_reviews (id, task_id, submission_generation, "
        "findings_confirmed) VALUES (?, ?, ?, ?)",
        (review_id, task_id, generation, json.dumps(findings, ensure_ascii=False)),
    )


def _titles(rows) -> list[str]:
    return [json.loads(str(r["finding"]))["title"] for r in rows]


async def test_a_completed_task_is_still_in_the_queue(db: aiosqlite.Connection):
    """AC-1: именно этот случай сейчас теряется.

    Задача завершена, значит не попадает ни в один раздел инбокса — все они
    строятся по статусу. Находки при этом никуда не делись и ответа не имеют.
    """
    await _task(db, 7, "completed", 1)
    await _report(db, 10, 7, 1, [_finding("живая-A"), _finding("живая-B")])
    await db.commit()

    rows = await list_unjudged_findings(db)
    assert _titles(rows) == ["живая-A", "живая-B"]
    assert rows[0]["task_status"] == "completed", (
        "очередь не должна зависеть от статуса задачи — иначе чинится не тот дефект"
    )


async def test_a_stale_report_is_not_queued(db: aiosqlite.Connection):
    """AC-3: находки пересданной генерации описывают код, которого уже нет.

    Просить человека судить их — просить о дефекте в файле, каким тот был.
    """
    await _task(db, 8, "completed", 2)
    await _report(db, 20, 8, 1, [_finding("устаревшая-1"), _finding("устаревшая-2")])
    await _report(db, 21, 8, 2, [_finding("живая")])
    await db.commit()

    assert _titles(await list_unjudged_findings(db)) == ["живая"]
    assert (await count_unjudged_findings(db))["findings"] == 1


async def test_judging_one_finding_leaves_its_siblings_queued(
    db: aiosqlite.Connection,
):
    """AC-4: разметка идёт поштучно, а не отчётами целиком."""
    await _task(db, 9, "completed", 1)
    await _report(db, 30, 9, 1, [_finding("A"), _finding("B"), _finding("C")])
    await db.execute(
        "INSERT INTO finding_dispositions (review_id, task_id, submission_generation, "
        "finding_index, finding_uid, disposition, decided_by) "
        "VALUES (30, 9, 1, 1, 'uid-b', 'fixed', 'denis')"
    )
    await db.commit()

    assert _titles(await list_unjudged_findings(db)) == ["A", "C"]


async def test_queue_length_equals_the_metric(db: aiosqlite.Connection):
    """AC-2: одно множество — один ответ (#518).

    Считается ОДНИМ тестом на одних данных. Два независимых теста подтвердили
    бы, что каждое число самосогласовано, и промолчали бы о том, что они
    расходятся между собой — а именно это и происходило: метрика вычитала одну
    сумму из другой и потому считала находки устаревших отчётов.
    """
    await _task(db, 11, "completed", 2)
    await _report(db, 40, 11, 1, [_finding("устаревшая")])
    await _report(db, 41, 11, 2, [_finding("живая-1"), _finding("живая-2")])
    await _task(db, 12, "review", 1)
    await _report(db, 42, 12, 1, [_finding("живая-3")])
    await db.commit()

    queue = await list_unjudged_findings(db)
    metrics = await practice_metrics(db, since_days=90)
    assert (
        len(queue) == metrics["machine_reviews"]["dispositions"]["confirmed_unjudged"]
    )
    assert len(queue) == 3, "устаревший отчёт не считается ни там, ни там"


async def test_the_queue_records_through_the_same_path(client: AsyncClient):
    """AC-6: запись из очереди идёт тем же путём, что из карточки.

    Второй способ судить — второе место, где правило «решает человек» может
    разойтись с первым.
    """
    resp = await client.post("/api/tasks", json={"title": "Queue task"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "model": "grok-4.6",
            "raw_count": 1,
            "findings_confirmed": [
                {
                    "locator": "file",
                    "file": "hub/db.py",
                    "title": "утечка",
                    "severity": "high",
                }
            ],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "reviewer",
        },
    )

    page = await client.get("/findings")
    assert page.status_code == 200
    assert "утечка" in page.text

    posted = await client.post(
        f"/tasks/{task_id}/web-finding-dispositions",
        data={"disposition-0": "fixed", "return_to": "queue"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert posted.headers["location"] == "/findings", (
        "разметка из очереди возвращает в очередь, иначе цена навигации остаётся"
    )
    assert (await client.get("/findings")).text.count("утечка") == 0
