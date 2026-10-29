"""Recovery of a stale manual claim requires a human and a pinned snapshot."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from hub import config, repository as repo
from hub.config import TokenIdentity


async def _held(client, db):
    tid = (await client.post("/api/tasks", json={"title": "manual recovery"})).json()[
        "id"
    ]
    await repo.update_task(
        db,
        tid,
        status="running",
        claimed_by="agent-a",
        claim_session_id="session-a",
        claimed_at="2026-10-28T17:54:00+00:00",
        branch=f"task-{tid}/existing",
        pr_number=31,
        submission_generation=3,
        submission_sha="a" * 40,
        review_verdict="changes_requested",
        review_verdict_generation=3,
    )
    await db.commit()
    return tid


def _body(**overrides):
    data = {
        "expected_status": "running",
        "expected_holder": "agent-a",
        "expected_session": "session-a",
        "expected_claimed_at": "2026-10-28T17:54:00+00:00",
        "expected_last_update_id": 0,
        "confirmed_process_stopped": True,
        "verification": "На хосте mac процесс остановлен",
    }
    data.update(overrides)
    return {
        "reason": "Передать задачу другому исполнителю",
        "manual_claim_recovery": data,
    }


async def test_owner_recovers_stale_claim_and_preserves_history(client, db):
    tid = await _held(client, db)
    before = dict(await repo.get_task(db, tid))
    response = await client.post(f"/api/tasks/{tid}/return-to-work", json=_body())
    assert response.status_code == 200, response.text
    after = dict(await repo.get_task(db, tid))
    assert after["status"] == "open" and after["claimed_by"] is None
    assert after["claim_session_id"] is None
    for key in (
        "branch",
        "pr_number",
        "submission_sha",
        "submission_generation",
        "review_verdict",
    ):
        assert after[key] == before[key], key
    updates = [dict(row) for row in await repo.get_task_updates(db, tid)]
    assert any("На хосте mac процесс остановлен" in row["content"] for row in updates)
    events = [dict(row) for row in await repo.list_events(db, since=0)]
    record = next(
        e
        for e in events
        if e["kind"] == "task_returned_to_work" and e["task_id"] == tid
    )
    payload = json.loads(record["payload"])
    assert (
        payload["previous_holder"] == "agent-a"
        and payload["previous_session"] == "session-a"
    )
    assert payload["manual_verification"] == "На хосте mac процесс остановлен"


@pytest.mark.parametrize("field", ["job_id", "review_job_id"])
async def test_live_or_unknown_work_refuses_recovery(client, db, field):
    tid = await _held(client, db)
    await repo.update_task(db, tid, **{field: "active-job"})
    await db.commit()
    before = dict(await repo.get_task(db, tid))
    response = await client.post(f"/api/tasks/{tid}/return-to-work", json=_body())
    assert response.status_code == 409, response.text
    assert dict(await repo.get_task(db, tid)) == before
    await repo.update_task(db, tid, **{field: None})
    await db.commit()
    for verification in ("", "   "):
        response = await client.post(
            f"/api/tasks/{tid}/return-to-work", json=_body(verification=verification)
        )
        assert response.status_code in (409, 422), response.text
        assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_holder", "old"),
        ("expected_session", "old"),
        ("expected_status", "review"),
        ("expected_claimed_at", "2020-01-01"),
    ],
)
async def test_stale_snapshot_does_not_release_new_holder(client, db, field, value):
    tid = await _held(client, db)
    before = dict(await repo.get_task(db, tid))
    response = await client.post(
        f"/api/tasks/{tid}/return-to-work", json=_body(**{field: value})
    )
    assert response.status_code in (409, 422), response.text
    assert dict(await repo.get_task(db, tid)) == before


async def test_concurrent_reclaim_is_rechecked_under_write_lock(
    client, db, db_dsn, monkeypatch
):
    from fastapi import HTTPException
    from hub import db as db_module, services
    from hub.models import TaskReturnToWork
    from hub.services import return_running

    tid = await _held(client, db)
    before_lock, committed = asyncio.Event(), asyncio.Event()
    real = return_running.write_transaction

    @asynccontextmanager
    async def delayed(conn):
        before_lock.set()
        await committed.wait()
        async with real(conn):
            yield

    monkeypatch.setattr(return_running, "write_transaction", delayed)

    async def new_claim():
        await before_lock.wait()
        other = await db_module.connect(db_dsn)
        try:
            await repo.add_task_update(
                other, tid, "agent-a", "status", "Claimed by agent-a session=session-a"
            )
            await other.commit()
        finally:
            await other.close()
        committed.set()

    writer = asyncio.create_task(new_claim())
    conn = await db_module.connect(db_dsn)
    try:
        with pytest.raises(HTTPException) as caught:
            await services.return_to_work(
                conn, tid, TaskReturnToWork(**_body()), actor="denis"
            )
    finally:
        await conn.close()
    await writer
    assert caught.value.status_code == 409
    after = dict(await repo.get_task(db, tid))
    assert (
        after["status"] == "running"
        and after["claimed_at"] == "2026-10-28T17:54:00+00:00"
    )


async def test_agent_and_identity_spoof_are_denied(client, db, monkeypatch):
    tid = await _held(client, db)
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {"agent-token": TokenIdentity("agent-a", "agent", principal_id=7)},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    response = await client.post(
        f"/api/tasks/{tid}/return-to-work",
        json=_body(),
        headers={"Authorization": "Bearer agent-token"},
    )
    assert response.status_code == 403, response.text
    assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"


async def test_existing_release_and_return_flows_unchanged(client, db):
    tid = await _held(client, db)
    blocked = await client.post(
        f"/api/tasks/{tid}/return-to-work", json={"reason": "обычный возврат"}
    )
    assert blocked.status_code == 409, blocked.text
    idle = (await client.post("/api/tasks", json={"title": "idle"})).json()["id"]
    await repo.update_task(db, idle, status="running")
    await db.commit()
    returned = await client.post(
        f"/api/tasks/{idle}/return-to-work", json={"reason": "обычный возврат"}
    )
    assert returned.status_code == 200 and returned.json()["status"] == "open"


@pytest.mark.parametrize(
    "name", ["open_slot", "executor_run", "implementer_session", "declared_wait"]
)
async def test_registered_active_work_blocks_manual_recovery(client, db, name):
    from tests.test_api_decide import _SIGNS

    tid = await _held(client, db)
    await _SIGNS[name](db, tid)
    before = dict(await repo.get_task(db, tid))
    response = await client.post(f"/api/tasks/{tid}/return-to-work", json=_body())
    assert response.status_code == 409, response.text
    assert dict(await repo.get_task(db, tid)) == before


async def test_web_form_recovery_uses_pinned_values(client, db):
    tid = await _held(client, db)
    page = await client.get(f"/tasks/{tid}")
    assert 'name="manual_recover"' in page.text
    response = await client.post(
        f"/tasks/{tid}/web-return-to-work",
        data={
            "reason": "перевзять",
            "manual_recover": "true",
            "expected_holder": "agent-a",
            "expected_session": "session-a",
            "expected_claimed_at": "2026-10-28T17:54:00+00:00",
            "expected_last_update_id": "0",
            "confirmed_process_stopped": "true",
            "verification": "Проверен процесс на хосте mac",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert dict(await repo.get_task(db, tid))["status"] == "open"


def test_cli_manual_recovery_passes_complete_snapshot(monkeypatch):
    from hub import cli

    seen = []
    monkeypatch.setattr(
        cli, "_api", lambda *args: seen.append(args) or {"status": "open"}
    )
    args = cli.build_parser().parse_args(
        [
            "return-to-work",
            "123",
            "--reason",
            "перевзять",
            "--manual-recover",
            "--expected-holder",
            "agent-a",
            "--expected-session",
            "session-a",
            "--expected-claimed-at",
            "2026-10-28T17:54:00+00:00",
            "--expected-last-update-id",
            "0",
            "--confirmed-process-stopped",
            "--verification",
            "проверен mac",
        ]
    )
    assert args.func(args) == 0
    payload = seen[0][2]["manual_claim_recovery"]
    assert payload == {
        "expected_status": "running",
        "expected_holder": "agent-a",
        "expected_session": "session-a",
        "expected_claimed_at": "2026-10-28T17:54:00+00:00",
        "expected_last_update_id": 0,
        "confirmed_process_stopped": True,
        "verification": "проверен mac",
    }


async def test_api_snapshot_before_reclaim_cannot_release_new_claim(
    client, db, monkeypatch
):
    tid = await _held(client, db)
    real = repo.get_task_updates
    inserted = False

    async def race(conn, task_id):
        nonlocal inserted
        rows = await real(conn, task_id)
        if task_id == tid and not inserted:
            inserted = True
            await repo.add_task_update(
                conn, tid, "agent-a", "status", "Claimed by agent-a session=session-a"
            )
            await conn.commit()
        return rows

    monkeypatch.setattr(repo, "get_task_updates", race)
    snapshot = (await client.get(f"/api/tasks/{tid}")).json()
    update_id = max((u["id"] for u in (snapshot["updates"] or [])), default=0)
    response = await client.post(
        f"/api/tasks/{tid}/return-to-work",
        json=_body(expected_last_update_id=update_id),
    )
    assert response.status_code == 409
    assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"


async def test_web_snapshot_before_reclaim_cannot_release_new_claim(
    client, db, monkeypatch
):
    import re
    from urllib.parse import unquote

    tid = await _held(client, db)
    real = repo.get_task_updates
    inserted = False

    async def race(conn, task_id):
        nonlocal inserted
        rows = await real(conn, task_id)
        if task_id == tid and not inserted:
            inserted = True
            await repo.add_task_update(
                conn, tid, "agent-a", "status", "Claimed by agent-a session=session-a"
            )
            await conn.commit()
        return rows

    monkeypatch.setattr(repo, "get_task_updates", race)
    page = await client.get(f"/tasks/{tid}")
    match = re.search(r'name="expected_last_update_id" value="(\d+)"', page.text)
    assert match, page.text
    response = await client.post(
        f"/tasks/{tid}/web-return-to-work",
        data={
            "reason": "перевзять",
            "manual_recover": "true",
            "expected_holder": "agent-a",
            "expected_session": "session-a",
            "expected_claimed_at": "2026-10-28T17:54:00+00:00",
            "expected_last_update_id": match.group(1),
            "confirmed_process_stopped": "true",
            "verification": "Проверен процесс на хосте mac",
        },
    )
    assert response.status_code == 303 and "Снимок захвата изменился" in unquote(
        response.headers["location"]
    )
    assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"


async def test_refuses_without_confirmation_that_process_stopped(client, db):
    tid = await _held(client, db)
    response = await client.post(
        f"/api/tasks/{tid}/return-to-work", json=_body(confirmed_process_stopped=False)
    )
    assert response.status_code == 422
    assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"


async def test_web_refuses_missing_history_snapshot(client, db):
    tid = await _held(client, db)
    response = await client.post(
        f"/tasks/{tid}/web-return-to-work",
        data={
            "reason": "перевзять",
            "manual_recover": "true",
            "expected_holder": "agent-a",
            "expected_session": "session-a",
            "expected_claimed_at": "2026-10-28T17:54:00+00:00",
            "confirmed_process_stopped": "true",
            "verification": "Проверен процесс на хосте mac",
        },
    )
    assert response.status_code == 422
    assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"


@pytest.mark.parametrize(
    "override,field,kind",
    [
        ({"verification": "   "}, "verification", "blank"),
        (
            {"confirmed_process_stopped": "false"},
            "confirmed_process_stopped",
            "literal_error",
        ),
    ],
)
async def test_web_explains_manual_recovery_refusal(
    client, db, caplog, override, field, kind
):
    tid = await _held(client, db)
    form = {
        "reason": "перевзять",
        "manual_recover": "true",
        "expected_holder": "agent-a",
        "expected_session": "session-a",
        "expected_claimed_at": "2026-10-28T17:54:00+00:00",
        "expected_last_update_id": "0",
        "confirmed_process_stopped": "true",
        "verification": "Проверен хост",
    }
    form.update(override)
    response = await client.post(f"/tasks/{tid}/web-return-to-work", data=form)
    assert response.status_code == 422
    assert field in response.text and "manual_claim_recovery" not in response.text
    assert ("Опишите" if field == "verification" else "Подтвердите") in response.text
    assert f"field={field} kind={kind}" in caplog.text
    assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"


@pytest.mark.parametrize(
    "field,value,detail",
    [
        ("executor_run_id", 7, "executor_run_id"),
        ("claim_nonce", "sensitive-nonce", "claim_nonce"),
    ],
)
async def test_recovery_names_nonmanual_claim_reason(client, db, field, value, detail):
    tid = await _held(client, db)
    await repo.update_task(db, tid, **{field: value})
    await db.commit()
    response = await client.post(f"/api/tasks/{tid}/return-to-work", json=_body())
    assert response.status_code == 409
    assert detail in response.text and "sensitive-nonce" not in response.text
    assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"


@pytest.mark.parametrize(
    "field,value,label",
    [
        ("expected_last_update_id", 123, "история задачи"),
        ("expected_holder", "old", "держатель"),
        ("expected_session", "old", "сессия"),
        ("expected_claimed_at", "old", "время захвата"),
    ],
)
async def test_recovery_names_changed_snapshot_fact(client, db, field, value, label):
    tid = await _held(client, db)
    response = await client.post(
        f"/api/tasks/{tid}/return-to-work", json=_body(**{field: value})
    )
    assert response.status_code == 409 and label in response.text
    assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"


async def test_sheet_explains_missing_process_confirmation(client, db, caplog):
    from tests.card_sheet_support import SHEET_HEADERS, sheet_form

    tid = await _held(client, db)
    page = (await client.get(f"/tasks/{tid}")).text
    form, data = sheet_form(page, "sh-return")
    data.update(
        {"reason": "перевзять", "manual_recover": "1", "verification": "Проверен хост"}
    )
    data.pop("confirmed_process_stopped", None)
    response = await client.post(form.attrs["action"], data=data, headers=SHEET_HEADERS)
    assert response.status_code == 422
    assert (
        "confirmed_process_stopped" in response.text
        and "manual_claim_recovery" not in response.text
    )
    assert "Подтвердите" in response.text
    assert "field=confirmed_process_stopped kind=missing" in caplog.text
    assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"


async def test_web_explains_overlong_verification(client, db):
    tid = await _held(client, db)
    form = {
        "reason": "перевзять",
        "manual_recover": "true",
        "expected_holder": "agent-a",
        "expected_session": "session-a",
        "expected_claimed_at": "2026-10-28T17:54:00+00:00",
        "expected_last_update_id": "0",
        "confirmed_process_stopped": "true",
        "verification": "а" * 5001,
    }
    response = await client.post(f"/tasks/{tid}/web-return-to-work", data=form)
    assert (
        response.status_code == 422
        and "5000" in response.text
        and "verification" in response.text
    )
    assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"


async def test_web_explains_overlong_reason(client, db):
    tid = await _held(client, db)
    response = await client.post(
        f"/tasks/{tid}/web-return-to-work", data={"reason": "а" * 5001}
    )
    assert response.status_code == 422 and "Сократите причину возврата" in response.text
    assert (
        "5000" in response.text
        and dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"
    )


async def test_web_explains_invalid_history_version(client, db):
    tid = await _held(client, db)
    form = {
        "reason": "перевзять",
        "manual_recover": "true",
        "expected_holder": "agent-a",
        "expected_session": "session-a",
        "expected_claimed_at": "2026-10-28T17:54:00+00:00",
        "expected_last_update_id": "-1",
        "confirmed_process_stopped": "true",
        "verification": "Проверен хост",
    }
    response = await client.post(f"/tasks/{tid}/web-return-to-work", data=form)
    assert (
        response.status_code == 422
        and "версия истории отсутствует или некорректна" in response.text
    )
    assert dict(await repo.get_task(db, tid))["claimed_by"] == "agent-a"
