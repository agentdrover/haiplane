"""Привязка отчёта ревью к заказу: код ревьюера -> сессия -> отчёт (#1946).

Цепочка ``chat_pair_codes.dispatch_id`` -> ``chat_pair_sessions.dispatch_id`` ->
``TokenIdentity.chat_pair_dispatch_id`` -> ``machine_reviews.dispatch_id``. Принципал
остаётся проверкой авторства: чужой заказ, поколение или принципал не привязываются.
"""

from __future__ import annotations

import json

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.models import MachineReviewSubmit
from hub.services import chat_pair, report_binding
from hub.services import review_dispatch as dispatcher
from tests.test_steward_dispatch import _due_task, _events
from tests.test_steward_waits_for_ladder import PRINCIPAL, _dispatch_row, _task_dict

OTHER = PRINCIPAL + 1


# ---------------------------------------------------------------------------
# Привязка: код ревьюера -> сессия -> отчёт
# ---------------------------------------------------------------------------


async def _reviewer_task(db: aiosqlite.Connection, slug: str) -> int:
    _, task_id = await _due_task(db, slug)
    for pid, name in ((PRINCIPAL, "rev"), (OTHER, "rev2")):
        await db.execute(
            "INSERT OR IGNORE INTO principals (id, kind, username, status) "
            "VALUES (?, 'agent', ?, 'active')",
            (pid, name),
        )
    await db.commit()
    return task_id


async def _redeem(db: aiosqlite.Connection, code: str):
    session = await chat_pair.redeem_code(db, code)
    assert session is not None
    identity = await chat_pair.resolve_session(db, session["token"])
    assert identity is not None
    return identity


async def test_the_code_of_an_order_carries_its_dispatch_into_the_session(
    db: aiosqlite.Connection,
):
    task_id = await _reviewer_task(db, "code-binding")
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    code, _ = await chat_pair.issue_code(
        db, PRINCIPAL, kind="reviewer", bound_task_id=task_id, bound_generation=1
    )
    await report_binding.bind_code_to_dispatch(
        db,
        code,
        dispatch_id=deep,
        task_id=task_id,
        generation=1,
        principal_id=PRINCIPAL,
    )
    await db.commit()
    identity = await _redeem(db, code)
    assert identity.chat_pair_kind == "reviewer"
    assert identity.chat_pair_dispatch_id == deep


async def test_a_session_redeemed_before_the_binding_is_caught_up(
    db: aiosqlite.Connection,
):
    """Агент успел выкупить код, пока хаб вставлял строку заказа: сессию догоняют."""
    task_id = await _reviewer_task(db, "code-race")
    lite = await _dispatch_row(db, task_id, status="done", profile="lite")
    first_code, _ = await chat_pair.issue_code(
        db, PRINCIPAL, kind="reviewer", bound_task_id=task_id, bound_generation=1
    )
    await report_binding.bind_code_to_dispatch(
        db,
        first_code,
        dispatch_id=lite,
        task_id=task_id,
        generation=1,
        principal_id=PRINCIPAL,
    )
    first = await _redeem(db, first_code)
    code, _ = await chat_pair.issue_code(
        db, PRINCIPAL, kind="reviewer", bound_task_id=task_id, bound_generation=1
    )
    second = await _redeem(db, code)  # до привязки
    assert second.chat_pair_dispatch_id is None
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    await report_binding.bind_code_to_dispatch(
        db,
        code,
        dispatch_id=deep,
        task_id=task_id,
        generation=1,
        principal_id=PRINCIPAL,
    )
    await db.commit()
    rows = await fetchall(
        db, "SELECT kind, dispatch_id FROM chat_pair_sessions ORDER BY id"
    )
    assert [dict(r)["dispatch_id"] for r in rows] == [lite, deep]
    assert first.chat_pair_dispatch_id == lite


async def test_intake_binds_only_an_order_the_reporter_may_answer(
    db: aiosqlite.Connection,
):
    """Принципал - проверка авторства: чужой заказ, поколение и принципал не привязываются."""
    task_id = await _reviewer_task(db, "intake-binding")
    mine = await _dispatch_row(db, task_id, status="active", profile="lite")
    stranger = await _dispatch_row(
        db, task_id, status="active", profile="deep", principal=OTHER
    )
    other_task = await _reviewer_task(db, "intake-other-task")
    foreign = await _dispatch_row(db, other_task, status="active", profile="lite")
    await db.execute(
        "UPDATE review_dispatches SET submission_generation=2 WHERE id=?", (stranger,)
    )
    cases = [
        (mine, PRINCIPAL, mine),
        (stranger, PRINCIPAL, None),  # другое поколение и чужой принципал
        (foreign, PRINCIPAL, None),  # заказ другой задачи
        (99999, PRINCIPAL, None),  # заказа нет
        (mine, OTHER, None),  # заказ закреплён за другим принципалом
        (None, PRINCIPAL, None),  # сессия заказа не назвала
    ]
    for claimed, principal, expected in cases:
        got = await report_binding.accepted_dispatch(db, task_id, 1, principal, claimed)
        assert (got["id"] if got else None) == expected, (claimed, principal)


async def test_record_machine_review_stamps_the_bound_dispatch(
    db: aiosqlite.Connection,
):
    from hub.services.machine_review_intake import record_machine_review

    task_id = await _reviewer_task(db, "intake-stamp")
    lite = await _dispatch_row(db, task_id, status="active", profile="lite")
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    body = MachineReviewSubmit(
        raw_count=0, incomplete=False, findings_confirmed=[], findings_rejected=[]
    )
    await record_machine_review(
        db, task_id, body, principal_id=PRINCIPAL, username="rev", dispatch_id=lite
    )
    await record_machine_review(
        db, task_id, body, principal_id=PRINCIPAL, username="rev", dispatch_id=None
    )
    rows = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert [r["dispatch_id"] for r in rows] == [lite, None]
    assert rows[0]["profile"] == "lite", (
        "профиль берётся у заказа отчёта, не у последнего"
    )
    assert rows[1]["profile"] == "deep", "без привязки - прежнее правило"
    # Отчёт без привязки не отменяет привязанный: lite его, deep ждёт свой.
    deep_row = dict(
        (await fetchall(db, "SELECT * FROM review_dispatches WHERE id=?", (deep,)))[0]
    )
    own = await dispatcher.report_of_dispatch(db, task_id, 1, deep_row)
    assert own is not None and own["id"] == rows[1]["id"]


async def test_the_migration_creates_the_table_and_the_columns(
    db: aiosqlite.Connection,
):
    attempts = await fetchall(db, "PRAGMA table_info(ladder_attempts)")
    assert {r["name"] for r in attempts} >= {
        "id",
        "task_id",
        "generation",
        "kind",
        "after_review_id",
        "dispatch_id",
        "outcome",
        "reason",
        "created_at",
        "closed_at",
    }
    for table in ("machine_reviews", "chat_pair_codes", "chat_pair_sessions"):
        cols = await fetchall(db, f"PRAGMA table_info({table})")  # nosec B608
        assert "dispatch_id" in {r["name"] for r in cols}, table
    # События - история: запись попытки не заменяет их.
    placed = json.dumps({"generation": 1})
    assert placed and not await _events(db, dispatcher.LADDER_TOP_UP_EVENT)


async def test_the_route_stamps_the_dispatch_of_the_reviewer_session(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Сквозной путь: код заказа -> сессия -> POST machine-review -> dispatch_id."""
    from hub.auth import login_limiter
    from hub.services import admin as admin_svc

    chat_pair.chat_pair_limiter._buckets.clear()
    login_limiter._buckets.clear()
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-token": config.TokenIdentity("bot", "agent")
        },  # pragma: allowlist secret
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    reviewer = await admin_svc.create_principal(
        db, kind="human", username="alice", role_slug="operator"
    )
    pid = int(reviewer["id"])
    _, task_id = await _due_task(db, "route-binding")
    lite = await _dispatch_row(
        db, task_id, status="done", profile="lite", principal=pid
    )
    deep = await _dispatch_row(
        db, task_id, status="active", profile="deep", principal=pid
    )
    code, _ = await chat_pair.issue_code(
        db, pid, kind="reviewer", bound_task_id=task_id, bound_generation=1
    )
    await report_binding.bind_code_to_dispatch(
        db, code, dispatch_id=lite, task_id=task_id, generation=1, principal_id=pid
    )
    await db.commit()
    redeemed = await client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert redeemed.status_code == 200, redeemed.text
    session = {"Authorization": f"Bearer {redeemed.json()['token']}"}

    for _ in range(2):  # два отчёта одной сессии - оба заказу lite
        filed = await client.post(
            f"/api/tasks/{task_id}/machine-review",
            json={"raw_count": 0, "incomplete": True},
            headers=session,
        )
        assert filed.status_code == 200, filed.text

    rows = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert [r["dispatch_id"] for r in rows] == [lite, lite]
    deep_row = dict(
        (await fetchall(db, "SELECT * FROM review_dispatches WHERE id=?", (deep,)))[0]
    )
    assert await dispatcher.report_of_dispatch(db, task_id, 1, deep_row) is None


async def test_a_dispatched_order_binds_its_code_and_its_report(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Через настоящий заказ: код ревьюера привязан к строке заказа, отчёт - к заказу."""
    from tests.review_dispatch_support import _DispatchRecorder, _submitted, _wire
    from tests.test_review_dispatch_report_contract import (
        _CODE_IN_PROMPT,
        _auth_on,
        _pinned_setup,
    )

    recorder = _DispatchRecorder({"agent": {"id": "bc-bind"}, "run": {"id": "run-b"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    _auth_on(monkeypatch)
    chat_pair.chat_pair_limiter._buckets.clear()
    task_id = await _submitted(
        client, db, "ladder-bind-order", policy={"review": "dispatch"}
    )
    code = _CODE_IN_PROMPT.search(recorder.calls[-1]["prompt_text"]).group(0)
    (order,) = await repo.list_active_review_dispatches(db)
    order_id = int(dict(order)["id"])
    rows = await fetchall(
        db, "SELECT dispatch_id FROM chat_pair_codes WHERE kind='reviewer'"
    )
    assert [dict(r)["dispatch_id"] for r in rows] == [order_id]

    redeemed = await client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={"x-forwarded-for": "203.0.113.21"},
    )
    assert redeemed.status_code == 200, redeemed.text
    filed = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={"raw_count": 0, "incomplete": False},
        headers={"Authorization": f"Bearer {redeemed.json()['token']}"},
    )
    assert filed.status_code == 200, filed.text
    (report,) = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert report["dispatch_id"] == order_id


async def test_the_local_channel_binds_the_code_of_its_order(
    db: aiosqlite.Connection,
):
    """Локальный заказ: код вставки и свежий код слота привязаны к строке заказа."""
    from types import SimpleNamespace

    from hub.services.review_dispatch import _delivery_block, _prompt_at_slot

    task_id = await _reviewer_task(db, "local-binding")
    task = await _task_dict(db, task_id)
    code, _ = await chat_pair.issue_code(
        db, PRINCIPAL, kind="reviewer", bound_task_id=task_id, bound_generation=1
    )
    order = SimpleNamespace(
        model="local-model",
        profile="lite",
        profile_assignment="rule",
        only_tests=None,
        reasons=[],
        rules_note="",
        diff_note="",
        snapshot=None,
        access_code=code,
    )
    inserted = await dispatcher._insert_local_dispatch(
        db, task, order, "тест", PRINCIPAL, 1, None, False
    )
    dispatch_id = inserted[0]
    assert dispatch_id is not None
    rows = await fetchall(db, "SELECT dispatch_id FROM chat_pair_codes")
    assert [dict(r)["dispatch_id"] for r in rows] == [dispatch_id]

    # Слот взят: код выписывается заново - и снова под тот же заказ.
    base = dispatcher.instance_base_url().rstrip("/")
    prompt = "шапка\n" + _delivery_block(task_id, code, base) + "хвост\n"
    fresh_prompt = await _prompt_at_slot(
        "",
        db,
        prompt=prompt,
        code=code,
        task_id=task_id,
        generation=1,
        principal_id=PRINCIPAL,
        dispatch_id=dispatch_id,
    )
    assert fresh_prompt != prompt, "блок доставки переписан свежим кодом"
    rows = await fetchall(db, "SELECT dispatch_id FROM chat_pair_codes")
    assert [dict(r)["dispatch_id"] for r in rows] == [dispatch_id]
