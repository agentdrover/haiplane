"""Предохранитель стюарда: человек помечает возврат «не ошибка пары» (#2139)."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
import pytest

from hub import repository as repo
from hub.db import _migrate, fetchall

NOTE = "правка формулировок документации, дефекта в коде нет"


async def _paired_review(
    client, db: aiosqlite.Connection, hdr: dict, title: str
) -> int:
    """Задача в review, поколение 1, пара судья+советник (approve+concur) на нём."""
    from tests.test_steward_shadow import _project
    from tests.test_verdict_replay_guard import _in_review

    task_id = await _in_review(client, hdr, title)
    project_id = await _project(db, f"pm-{task_id}")
    await repo.update_task(db, task_id, project_id=project_id)
    judge_id = await repo.insert_steward_judgement(
        db,
        task_id=task_id,
        generation=1,
        kind="verdict",
        submitted_verdict="approve",
        verdict="approve",
        confidence="high",
        escalate_reason="",
        grounds="[]",
        findings="[]",
        closures="[]",
        model="gpt-5.3-codex",
        submitted_by="steward-bot",
        principal_id=42,
        packet_hash="pkt",
        contour=2,
    )
    await repo.insert_steward_judgement(
        db,
        task_id=task_id,
        generation=1,
        kind="advisor",
        submitted_verdict="concur",
        verdict="concur",
        confidence="high",
        grounds="[]",
        findings="[]",
        closures="[]",
        model="claude-sonnet-5",
        submitted_by="steward-bot",
        principal_id=42,
        judged_id=judge_id,
        packet_hash="pkt",
        contour=2,
    )
    await db.commit()
    return task_id


def _cr_body(**extra: Any) -> dict[str, Any]:
    return {
        "verdict": "changes_requested",
        "agent": "denis",
        "comments": "переформулируйте раздел",
        "expected_generation": 1,
        **extra,
    }


async def _verdict_payloads(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind='review_verdict_recorded' "
        "AND task_id=? ORDER BY id",
        (task_id,),
    )
    return [json.loads(dict(r)["payload"]) for r in rows]


@pytest.fixture
def hdr(monkeypatch) -> dict[str, dict[str, str]]:
    from tests.state_support import auth

    return auth(monkeypatch)


async def _polled(db: aiosqlite.Connection, task_id: int) -> list[tuple[str, str]]:
    """Находки страховочного опроса по задаче как (source, ref) - без записи."""
    from hub.services import steward_exit

    out: list[tuple[str, str]] = []
    for pair in await steward_exit.concur_pairs(db):
        if pair.task_id == task_id:
            out += [
                (f.source, f.ref)
                for f in await steward_exit._poll_pair(db, pair)
                if f.source == "human_changes_requested"
            ]
    return out


async def test_human_return_marked_not_pair_miss_keeps_act(
    db: aiosqlite.Connection, client, hdr
):
    """AC-1: pair_miss=false с заметкой - записи нет, act не отказан, счётчик +1."""
    from hub.services import steward_exit
    from tests.test_steward_shadow import _clean_act_sample, _rows

    task_id = await _paired_review(client, db, hdr, "marked")
    project_id = int(dict(await repo.get_task(db, task_id))["project_id"])
    await _clean_act_sample(db, project_id)
    assert await steward_exit.act_refusals_v2(db) == []

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json=_cr_body(pair_miss=False, pair_miss_note=NOTE),
        headers=hdr["human"],
    )
    assert resp.status_code == 200, resp.text

    payload = (await _verdict_payloads(db, task_id))[-1]
    assert payload["pair_miss"] is False and payload["pair_miss_note"] == NOTE
    assert await _rows(db) == []
    assert await steward_exit.detect_false_approvals(db) == []
    assert await _polled(db, task_id) == []
    assert "false_approve" not in [r[0] for r in await steward_exit.act_refusals_v2(db)]
    report = await steward_exit.contour_report(db)
    assert report["human_returns_not_pair_miss_14d"] == 1
    assert report["false_approve"] == 0
    assert report["false_approve_events_14d"] == {}


async def _return_event(db: aiosqlite.Connection, task_id: int, **extra: Any) -> int:
    event = await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="denis",
        payload={"verdict": "changes_requested", "submission_generation": 1, **extra},
    )
    await db.commit()
    return int(event or 0)


async def test_human_return_without_valid_mark_is_pair_miss(db: aiosqlite.Connection):
    """AC-2: нет поля, null, строка, число, пустое - запись; триггер и опрос согласны."""
    from hub.services import steward_exit
    from tests.test_steward_shadow import _project, _rows, _v2_row

    project_id = await _project(db, "pm-invalid")
    cases: list[dict[str, Any]] = [
        {},  # старое событие до миграции
        {"pair_miss": None},
        {"pair_miss": "false"},
        {"pair_miss": 0},
        {"pair_miss": "no"},
        {"pair_miss": True},
        {"pair_miss": [False]},
    ]
    for extra in cases:
        task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
        event = await _return_event(db, task_id, **extra)
        rows = [r for r in await _rows(db) if r["task_id"] == task_id]
        assert [(r["source"], r["ref"]) for r in rows] == [
            ("human_changes_requested", str(event))
        ], extra
        assert await _polled(db, task_id) == [
            ("human_changes_requested", str(event))
        ], extra
        assert any(
            f.task_id == task_id for f in await steward_exit.sticky_false_approvals(db)
        )
    assert "false_approve" in [r[0] for r in await steward_exit.act_refusals_v2(db)]

    # и обратное: JSON false даёт пустой результат в обоих местах
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _return_event(db, task_id, pair_miss=False, pair_miss_note=NOTE)
    assert [r for r in await _rows(db) if r["task_id"] == task_id] == []
    assert await _polled(db, task_id) == []


async def test_only_human_with_note_can_mark_not_pair_miss(
    db: aiosqlite.Connection, client, hdr
):
    """AC-3: агент - 403, человек без заметки - 422; ничего не записано; MCP без поля."""
    from hub import mcp_server

    task_id = await _paired_review(client, db, hdr, "refused")
    before = await _verdict_payloads(db, task_id)

    async def post(who: str, **extra: Any):
        return await client.post(
            f"/api/tasks/{task_id}/review-verdict",
            json=_cr_body(**extra),
            headers=hdr[who],
        )

    agent = await post("rev", pair_miss=False, pair_miss_note=NOTE)
    assert agent.status_code == 403, agent.text
    assert agent.json()["detail"]["reason"] == "pair_miss_human_only"
    for extra in (
        {"pair_miss": False},
        {"pair_miss": False, "pair_miss_note": ""},
        {"pair_miss": False, "pair_miss_note": "   "},
        {"pair_miss": True, "pair_miss_note": NOTE},
        {"pair_miss": "false", "pair_miss_note": NOTE},
        {"pair_miss": 0, "pair_miss_note": NOTE},
        {"pair_miss": "0", "pair_miss_note": NOTE},
        {"pair_miss": "no", "pair_miss_note": NOTE},
        {"pair_miss": [False], "pair_miss_note": NOTE},
        {"pair_miss_note": NOTE},
    ):
        resp = await post("human", **extra)
        assert resp.status_code == 422, (extra, resp.text)
        assert resp.json()["detail"]["reason"] == "pair_miss_invalid", extra
    approved = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "approved",
            "agent": "denis",
            "expected_generation": 1,
            "pair_miss": False,
            "pair_miss_note": NOTE,
        },
        headers=hdr["human"],
    )
    assert approved.status_code == 422, approved.text
    assert await _verdict_payloads(db, task_id) == before
    assert (await fetchall(db, "SELECT 1 FROM steward_false_approvals")) == []

    for tool in await mcp_server.mcp.list_tools():
        assert "pair_miss" not in json.dumps(tool.inputSchema), tool.name


async def test_migration_keeps_other_sources_and_existing_rows(
    db: aiosqlite.Connection,
):
    """AC-4: триггер пересоздан; старые записи целы; reopened и prod_defect как раньше."""
    from tests.test_steward_shadow import _complete, _project, _rows, _v2_row

    project_id = await _project(db, "pm-mig")
    old = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    old_event = await _return_event(db, old)  # без пометки: запись
    await db.execute(
        "UPDATE steward_false_approvals SET cleared_at=datetime('now'), "
        "cleared_by='denis' WHERE task_id=?",
        (old,),
    )
    await db.commit()
    snapshot = [dict(r) for r in await _rows(db)]

    trig = await fetchall(
        db,
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='trg_false_approve_human_return'",
    )
    assert len(trig) == 1 and "pair_miss" in dict(trig[0])["sql"]
    for name in ("trg_false_approve_reopened", "trg_false_approve_prod_defect_insert"):
        assert await fetchall(
            db, "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        )
    # повторное применение шагов ничего не меняет
    await _migrate(db)
    assert [dict(r) for r in await _rows(db)] == snapshot
    assert snapshot[0]["ref"] == str(old_event)

    reopened = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _complete(db, reopened, "2026-09-01 10:00:00")
    await repo.update_task(db, reopened, status="running")
    caused = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await db.execute(
        "INSERT INTO tasks (title, status, found_in, caused_by_task_id) "
        "VALUES ('прод', 'open', 'prod', ?)",
        (caused,),
    )
    await db.commit()
    sources = {(r["task_id"], r["source"]) for r in await _rows(db)}
    assert (reopened, "reopened") in sources and (caused, "prod_defect") in sources


def test_cli_sends_the_not_pair_miss_mark(monkeypatch, capsys):
    """CLI: --not-pair-miss NOTE кладёт pair_miss=false и заметку в тело (#2139)."""
    from argparse import Namespace

    from hub import cli_review_verdict as rv

    sent: dict[str, Any] = {}

    def fake_api(method, path, body=None, *a, **k):
        sent.update(body or {})
        return {"status": "running"}

    monkeypatch.setattr(rv, "_api", fake_api)
    monkeypatch.setattr(rv, "_read_task", lambda _id: None)
    args = Namespace(
        task_id=1,
        verdict="changes_requested",
        comments="c",
        agent="",
        findings_json="",
        expected_generation=1,
        not_pair_miss=NOTE,
        json=True,
    )
    assert rv.cmd_review_verdict(args) == 0
    assert sent["pair_miss"] is False and sent["pair_miss_note"] == NOTE
    args.not_pair_miss = None
    sent.clear()
    assert rv.cmd_review_verdict(args) == 0
    assert "pair_miss" not in sent


async def test_mark_on_a_standing_verdict_is_not_a_silent_repeat(
    db: aiosqlite.Connection, client, hdr
):
    """Codex P-1: повтор стоящего возврата с пометкой не отвечает успехом молча."""
    from hub.services import steward_exit

    task_id = await _paired_review(client, db, hdr, "standing")
    first = await client.post(
        f"/api/tasks/{task_id}/review-verdict", json=_cr_body(), headers=hdr["human"]
    )
    assert first.status_code == 200, first.text
    before = await _verdict_payloads(db, task_id)
    again = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json=_cr_body(pair_miss=False, pair_miss_note=NOTE),
        headers=hdr["human"],
    )
    after = await _verdict_payloads(db, task_id)
    # либо пометка легла новым событием, либо отказ; "успех без записи" недопустим
    assert again.status_code != 200 or len(after) == len(before) + 1, again.text
    assert await steward_exit.human_returns_not_pair_miss(db) == len(
        [p for p in after if p.get("pair_miss") is False]
    )


async def test_counter_does_not_trust_the_display_name(
    db: aiosqlite.Connection, client, hdr
):
    """Codex P-2: человек с именем agent=policy в счётчик входит."""
    from hub.services import steward_exit

    task_id = await _paired_review(client, db, hdr, "named")
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json=_cr_body(agent="policy", pair_miss=False, pair_miss_note=NOTE),
        headers=hdr["human"],
    )
    assert resp.status_code == 200, resp.text
    assert await steward_exit.human_returns_not_pair_miss(db) == 1
