"""Пересдача без исходов находок называет UID открытых находок (#1909).

19.10 на #1854 автор пересдал после CHANGES_REQUESTED без finding_outcomes: UID
лежали в брифе ревьюера, которым автор не пользуется, а ответ хаба о них
молчал. Теперь ответ сдачи при открытых находках прошлого отчёта без исхода
несёт список: uid, вид, заголовок, допустимые исходы своего словаря и готовый
пример finding_outcomes. Сдача принимается (warn), блокировки нет.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.mcp_server import hub_submit_for_review
from hub.models import CONFIRMED_OUTCOMES, UNRESOLVED_OUTCOMES, FindingOutcomeItem
from hub.services import finding_outcome
from tests.test_finding_outcomes_on_done import quiet_git_ops  # noqa: F401


def _finding(title: str) -> dict:
    return {
        "title": title,
        "severity": "high",
        "category": "correctness",
        "locator": "file",
        "file": "hub/db.py",
    }


def _unresolved(title: str) -> dict:
    return {"title": title, "why": "адъюдикаторы разошлись"}


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
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    confirmed: list[dict],
    unresolved: list[dict] | None = None,
    *,
    confirmed_raw: str | None = None,
) -> int:
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill="lite-diff-review",
        raw_count=len(confirmed),
        findings_confirmed=(
            confirmed_raw
            if confirmed_raw is not None
            else json.dumps(confirmed, ensure_ascii=False)
        ),
        unresolved=json.dumps(unresolved or [], ensure_ascii=False),
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


async def _first_generation_sent_back(
    client: AsyncClient,
    db: aiosqlite.Connection,
    title: str,
    confirmed: list[dict],
    unresolved: list[dict] | None = None,
    *,
    confirmed_raw: str | None = None,
) -> int:
    task_id = await _started(client, title)
    first = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert first.status_code == 200, first.text
    await _report(db, task_id, 1, confirmed, unresolved, confirmed_raw=confirmed_raw)
    await _sent_back(client, task_id)
    return task_id


async def test_resubmit_without_outcomes_names_open_uids(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1: 2 confirmed + 1 unresolved, пересдача без исходов — три UID и пример."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    task_id = await _first_generation_sent_back(
        client,
        db,
        "Подсказка исходов",
        [_finding("утечка курсора"), _finding("гонка записи")],
        [_unresolved("спорный порядок")],
    )
    owed = await finding_outcome.open_findings(db, task_id, 1)
    uids = {f["finding_uid"]: f["finding_kind"] for f in owed}
    assert len(uids) == 3

    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text  # сдача принята: только предупреждение
    body = resp.json()
    hint = body["outcome_hint"]
    assert hint["count"] == 3 and hint["omitted"] == 0
    assert {i["finding_uid"]: i["kind"] for i in hint["items"]} == uids
    titles = {i["title"] for i in hint["items"]}
    assert titles == {"утечка курсора", "гонка записи", "спорный порядок"}
    for item in hint["items"]:
        allowed = (
            UNRESOLVED_OUTCOMES if item["kind"] == "unresolved" else CONFIRMED_OUTCOMES
        )
        assert set(item["allowed_outcomes"]) == {o.value for o in allowed}
    assert {e["finding_uid"] for e in hint["example"]} == set(uids)
    for uid in uids:
        assert uid in hint["text"]

    # Тот же список — в записи ленты.
    updates = await repo.get_task_updates(db, task_id)
    alerts = [dict(u)["content"] for u in updates if dict(u)["kind"] == "alert"]
    assert any(
        all(uid in text for uid in uids) and hint["text"] in text for text in alerts
    )

    # MCP-ответ несёт те же UID и пример, без нового инструмента.
    with (
        patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get,
        patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post,
    ):
        get.return_value = {"id": task_id, "status": "running"}
        post.return_value = body
        out = json.loads(await hub_submit_for_review(task_id))
    for uid in uids:
        assert uid in out["message"]
    assert "finding_outcomes" in out["message"]

    # Пример принимается следующей сдачей без правок.
    example = [FindingOutcomeItem(**e).model_dump(mode="json") for e in hint["example"]]
    next_resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={"finding_outcomes": example}
    )
    assert next_resp.status_code == 200, next_resp.text
    assert next_resp.json()["outcome_hint"] is None
    assert await finding_outcome.open_findings(db, task_id, 2) == []


async def test_hint_absent_when_not_needed_and_bounded(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2: первая сдача и полные исходы — без подсказки; 30 находок — 20 и остаток."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")

    # Первая сдача: отчётов нет.
    first_task = await _started(client, "Первая сдача")
    first = await client.post(f"/api/tasks/{first_task}/submit-review", json={})
    assert first.status_code == 200 and first.json()["outcome_hint"] is None

    # Все исходы названы.
    task_id = await _first_generation_sent_back(
        client, db, "Все исходы", [_finding("раз"), _finding("два")]
    )
    uids = [
        f["finding_uid"] for f in await finding_outcome.open_findings(db, task_id, 1)
    ]
    full = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [{"finding_uid": u, "outcome": "fixed"} for u in uids]
        },
    )
    assert full.status_code == 200 and full.json()["outcome_hint"] is None

    # Исходы частично: в подсказке только неотвеченная, пример только по ней.
    partial_task = await _first_generation_sent_back(
        client, db, "Частично", [_finding("а"), _finding("б")]
    )
    a, b = [
        f["finding_uid"]
        for f in await finding_outcome.open_findings(db, partial_task, 1)
    ]
    part = await client.post(
        f"/api/tasks/{partial_task}/submit-review",
        json={"finding_outcomes": [{"finding_uid": a, "outcome": "fixed"}]},
    )
    assert part.status_code == 200, part.text
    hint = part.json()["outcome_hint"]
    assert [i["finding_uid"] for i in hint["items"]] == [b]
    assert [e["finding_uid"] for e in hint["example"]] == [b]

    # 30 открытых: первые 20 и счётчик остатка.
    many_task = await _first_generation_sent_back(
        client, db, "Много находок", [_finding(f"находка {n}") for n in range(30)]
    )
    many = await client.post(f"/api/tasks/{many_task}/submit-review", json={})
    assert many.status_code == 200, many.text
    big = many.json()["outcome_hint"]
    assert big["count"] == 30
    assert len(big["items"]) == 20 and big["omitted"] == 10
    assert len(big["example"]) == 20
    assert "и ещё 10" in big["text"]


async def test_hint_is_silent_when_the_gate_is_off_and_survives_unreadable_sections(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Режим off — без подсказки; нечитаемая секция отчёта не роняет сдачу."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "off")
    task_id = await _first_generation_sent_back(
        client, db, "Гейт выключен", [_finding("находка")]
    )
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200 and resp.json()["outcome_hint"] is None

    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    broken = await _first_generation_sent_back(
        client,
        db,
        "Битый отчёт",
        [],
        [_unresolved("читаемая")],
        confirmed_raw="{не json",
    )
    ok = await client.post(f"/api/tasks/{broken}/submit-review", json={})
    assert ok.status_code == 200, ok.text
    hint = ok.json()["outcome_hint"]
    assert [i["kind"] for i in hint["items"]] == ["unresolved"]


async def test_hint_follows_findings_behind_an_unreviewed_generation(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1331: поколение без отчёта — подсказка называет находки последнего отчёта."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    task_id = await _first_generation_sent_back(
        client, db, "За пропуском", [_finding("перенесённая")]
    )
    assert (
        await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    ).status_code == 200  # поколение 2 без отчёта
    await _sent_back(client, task_id)
    third = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert third.status_code == 200, third.text
    hint = third.json()["outcome_hint"]
    assert [i["title"] for i in hint["items"]] == ["перенесённая"]


async def test_hint_is_one_line_per_defect_when_two_reports_carry_it(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """lite и deep на одной сдаче с одним дефектом: в подсказке он один раз."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    task_id = await _first_generation_sent_back(
        client, db, "Два отчёта", [_finding("тот же дефект")]
    )
    await _report(db, task_id, 1, [_finding("тот же дефект")])
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text
    hint = resp.json()["outcome_hint"]
    assert hint["count"] == 1 and len(hint["items"]) == 1


async def test_done_report_without_outcomes_carries_the_hint_in_warnings(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """hub_report_done: тот же список приходит заметкой ответа на отчёт."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    task_id = await _first_generation_sent_back(
        client, db, "Отчёт о готовности", [_finding("неотвеченная")]
    )
    uid = (await finding_outcome.open_findings(db, task_id, 1))[0]["finding_uid"]
    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "готово"},
    )
    assert resp.status_code in (200, 201), resp.text
    warnings = resp.json().get("warnings") or []
    assert any(uid in w and "finding_outcomes" in w for w in warnings)


async def test_done_report_hint_is_silent_when_the_gate_is_off(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Режим off молчит и на пути отчёта о готовности."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "off")
    task_id = await _first_generation_sent_back(
        client, db, "Отчёт при off", [_finding("неотвеченная")]
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "готово"},
    )
    assert resp.status_code in (200, 201), resp.text
    assert not any("finding_outcomes" in w for w in (resp.json().get("warnings") or []))


@pytest.mark.usefixtures("quiet_git_ops")
async def test_pending_report_completion_route_carries_the_hint(
    db: aiosqlite.Connection, client: AsyncClient, monkeypatch
):
    """done при auto_review=false завершает без бампа поколения и всё равно называет долг."""
    from tests.test_finding_outcomes_on_done import (
        _pending_report_after_a_dispatch_run,
        _sent_back_with_a_finding,
    )

    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    task_id, _review_id, uid = await _sent_back_with_a_finding(client, db, "Без бампа")
    await repo.update_task(db, task_id, auto_review=0)
    await _pending_report_after_a_dispatch_run(db, task_id)

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "готово"},
    )
    assert resp.status_code == 200, resp.text
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"
    assert any(uid in w for w in resp.json().get("warnings") or [])


async def test_same_commit_retry_carries_the_hint_and_stays_idempotent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Повтор того же sha из review: находки, появившиеся без ответа, названы."""
    from tests.test_submit_gates import _install_fixed_tip_git

    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    _install_fixed_tip_git(monkeypatch, "tip-1")
    task_id = await _started(client, "Повтор без ответа")
    first = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert first.status_code == 200 and first.json()["outcome_hint"] is None
    await _report(db, task_id, 1, [_finding("осталась без ответа")])
    retry = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert retry.status_code == 200, retry.text
    assert retry.json()["submission_generation"] == 1, "новое поколение не открыто"
    hint = retry.json()["outcome_hint"]
    assert [i["title"] for i in hint["items"]] == ["осталась без ответа"]

    uid = hint["items"][0]["finding_uid"]
    body = {"finding_outcomes": [{"finding_uid": uid, "outcome": "fixed"}]}
    answered = await client.post(f"/api/tasks/{task_id}/submit-review", json=body)
    assert answered.status_code == 200 and answered.json()["outcome_hint"] is None
    again = await client.post(f"/api/tasks/{task_id}/submit-review", json=body)
    assert again.status_code == 200 and again.json()["outcome_hint"] is None


async def test_feed_note_lists_each_open_finding_once_and_within_the_cap(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """15 подтверждённых + 15 неразрешённых: в записи ленты 20 UID, а не 25-30."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    task_id = await _first_generation_sent_back(
        client,
        db,
        "Лимит в ленте",
        [_finding(f"подтверждённая {n}") for n in range(15)],
        [_unresolved(f"спорная {n}") for n in range(15)],
    )
    owed = {
        f["finding_uid"] for f in await finding_outcome.open_findings(db, task_id, 1)
    }
    assert len(owed) == 30
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert" and "Исходы находок" in dict(u)["content"]
    ]
    assert len(alerts) == 1
    named = {uid for uid in owed if uid in alerts[0]}
    assert len(named) == 20
    assert "и ещё 10" in alerts[0]
