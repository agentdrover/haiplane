"""Края жизненного цикла привязки «отчёт -> заказ» (#1946, круг 2 после CR gen 1).

Один корень трёх находок: привязка теряется или обходит прежнее восстановление
там, где заказ или отчёт появляются в необычном порядке.

1. Ранний отчёт (до вставки строки заказа) остаётся непривязанным и достаётся
   следующему заказу: поздняя привязка обязана догонять и принятые отчёты кода.
2. Заказ, найденный по привязке, проходит то же восстановление отказанного
   заказа (#1408), что и найденный по последнему заказу поколения.
3. Заглушка, к которой привязан код, не удаляется после открытия второй двери.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.db import fetchall
from hub.models import MachineReviewSubmit
from hub.services import chat_pair, report_binding
from hub.services import review_dispatch as dispatcher
from hub.services.machine_review_intake import record_machine_review
from tests.test_report_binding import OTHER, _mint, _redeem, _reviewer_task
from tests.test_steward_waits_for_ladder import PRINCIPAL, _dispatch_row


def _body(incomplete: bool = True) -> MachineReviewSubmit:
    return MachineReviewSubmit(
        raw_count=0, incomplete=incomplete, findings_confirmed=[], findings_rejected=[]
    )


async def test_a_late_binding_catches_up_the_reports_already_taken(
    db: aiosqlite.Connection,
):
    """Находка 1: R1 пришёл до привязки, R2 после; следующий заказ R1 не получает."""
    task_id = await _reviewer_task(db, "early-report")
    code = await _mint(db, task_id)
    identity = await _redeem(db, code)  # выкуплен до строки заказа
    code_id = identity.chat_pair_code_id
    assert code_id is not None
    await record_machine_review(
        db,
        task_id,
        _body(),
        principal_id=PRINCIPAL,
        username="rev",
        dispatch_id=identity.chat_pair_dispatch_id,
        code_id=code_id,
    )
    lite = await _dispatch_row(db, task_id, status="active", profile="lite")
    await report_binding.bind_code_to_dispatch(
        db,
        code,
        dispatch_id=lite,
        task_id=task_id,
        generation=1,
        principal_id=PRINCIPAL,
    )
    await db.commit()
    await record_machine_review(
        db,
        task_id,
        _body(),
        principal_id=PRINCIPAL,
        username="rev",
        dispatch_id=lite,
        code_id=code_id,
    )
    reports = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert [r["dispatch_id"] for r in reports] == [lite, lite], "ранний отчёт догнан"
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    deep_row = dict(
        (await fetchall(db, "SELECT * FROM review_dispatches WHERE id=?", (deep,)))[0]
    )
    assert await dispatcher.report_of_dispatch(db, task_id, 1, deep_row) is None


async def test_a_bound_refused_order_is_reopened_by_its_report(
    db: aiosqlite.Connection,
):
    """Находка 2: заглушка отказа (failed, agent_id='') ожила по отчёту своего кода."""
    task_id = await _reviewer_task(db, "reopen-bound")
    cur = await db.execute(
        "INSERT INTO review_dispatches (task_id, submission_generation, agent_id, "
        "run_id, model, profile, status, channel, reviewer_principal_id) "
        "VALUES (?, 1, '', '', 'grok-4.6', 'lite', 'failed', 'cloud', ?)",
        (task_id, PRINCIPAL),
    )
    stub = int(cur.lastrowid or 0)
    await db.commit()
    await record_machine_review(
        db,
        task_id,
        _body(False),
        principal_id=PRINCIPAL,
        username="rev",
        dispatch_id=stub,
    )
    rows = await fetchall(
        db, "SELECT status FROM review_dispatches WHERE id=?", (stub,)
    )
    assert dict(rows[0])["status"] == "active"
    (report,) = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert report["dispatch_id"] == stub


async def test_the_stub_of_a_refused_call_survives_the_real_second_door(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 3, БЕЗ подмены второй двери: код не указывает на удалённую строку."""
    from tests.review_dispatch_support import (
        _LIMIT_REFUSAL,
        _DispatchRecorder,
        _local_principal,
        _pinned_setup,
        _reporting_stub,
        _stub_reviewer,
        _submitted,
        _wire,
    )

    recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
    _wire(monkeypatch, recorder)
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    task_id = await _submitted(
        client,
        db,
        "stub-survives",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await dispatcher.wait_for_local_runs()
    await db.commit()

    codes = await fetchall(
        db,
        "SELECT c.dispatch_id, d.id AS found FROM chat_pair_codes c "
        "LEFT JOIN review_dispatches d ON d.id = c.dispatch_id "
        "WHERE c.kind='reviewer' AND c.principal_id = ?",
        (cloud_pid,),
    )
    assert codes, "код облачного ревьюера выписан"
    for row in codes:
        assert row["dispatch_id"] is not None and row["found"] == row["dispatch_id"], (
            "код указывает на живую строку заказа, а не на удалённую"
        )
    stub = await report_binding.accepted_dispatch(
        db, task_id, 1, cloud_pid, int(codes[0]["dispatch_id"])
    )
    assert stub is not None and stub["channel"] == "cloud"
    assert chat_pair  # модуль нужен тесту: код выкупает облако позже


# ---------------------------------------------------------------------------
# Круг Codex 2 (#1946): устаревший None при приёме, профиль и восстановление
# при поздней привязке, оставленная заглушка в порядке ступеней
# ---------------------------------------------------------------------------


async def _bound_later(db: aiosqlite.Connection, slug: str):
    """Код выкуплен до заказов: личность несёт dispatch_id=None."""
    task_id = await _reviewer_task(db, slug)
    code = await _mint(db, task_id)
    identity = await _redeem(db, code)
    assert identity.chat_pair_dispatch_id is None
    return task_id, code, identity.chat_pair_code_id


async def _bind(db, code, order, task_id):
    await report_binding.bind_code_to_dispatch(
        db,
        code,
        dispatch_id=order,
        task_id=task_id,
        generation=1,
        principal_id=PRINCIPAL,
    )
    await db.commit()


async def test_a_report_stamped_after_the_binding_landed_is_not_left_unbound(
    db: aiosqlite.Connection,
):
    """Codex 1: запрос прочитал dispatch_id=None, привязка легла, потом отчёт сохранён."""
    task_id, code, code_id = await _bound_later(db, "stale-none")
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    await _bind(db, code, deep, task_id)
    await record_machine_review(
        db, task_id, _body(), principal_id=PRINCIPAL, username="rev", code_id=code_id
    )
    (report,) = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert report["dispatch_id"] == deep and report["profile"] == "deep"


async def test_a_late_binding_corrects_the_profile_of_a_caught_up_report(
    db: aiosqlite.Connection,
):
    """Codex 4: ранний отчёт получил профиль последнего заказа - привязка его исправляет."""
    task_id, code, code_id = await _bound_later(db, "late-profile")
    await _dispatch_row(db, task_id, status="active", profile="lite")
    await record_machine_review(
        db, task_id, _body(), principal_id=PRINCIPAL, username="rev", code_id=code_id
    )
    (early,) = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert early["profile"] == "lite"
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    await _bind(db, code, deep, task_id)
    (fixed,) = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert fixed["dispatch_id"] == deep and fixed["profile"] == "deep"


async def test_a_late_binding_reopens_a_refused_stub_whose_report_came_first(
    db: aiosqlite.Connection,
):
    """Codex 2: отчёт пришёл до заглушки отказа; привязка кода к ней оживляет заказ."""
    task_id, code, code_id = await _bound_later(db, "early-then-stub")
    await record_machine_review(
        db,
        task_id,
        _body(False),
        principal_id=PRINCIPAL,
        username="rev",
        code_id=code_id,
    )
    cur = await db.execute(
        "INSERT INTO review_dispatches (task_id, submission_generation, agent_id, "
        "run_id, model, profile, status, channel, reviewer_principal_id) "
        "VALUES (?, 1, '', '', 'grok-4.6', 'lite', 'failed', 'cloud', ?)",
        (task_id, PRINCIPAL),
    )
    stub = int(cur.lastrowid or 0)
    await _bind(db, code, stub, task_id)
    rows = await fetchall(
        db, "SELECT status FROM review_dispatches WHERE id=?", (stub,)
    )
    assert dict(rows[0])["status"] == "active"


async def test_a_kept_stub_with_a_replacement_does_not_take_a_rung(
    db: aiosqlite.Connection,
):
    """Codex 3: заглушка с локальной заменой не занимает ступень облачных заказов."""
    from tests.test_ladder_attempts import _report

    task_id = await _reviewer_task(db, "kept-stub-rung")
    cur = await db.execute(
        "INSERT INTO review_dispatches (task_id, submission_generation, agent_id, "
        "run_id, model, profile, status, channel, reviewer_principal_id) "
        "VALUES (?, 1, '', '', 'm', 'lite', 'failed', 'cloud', ?)",
        (task_id, PRINCIPAL),
    )
    stub = int(cur.lastrowid or 0)
    await db.execute(
        "INSERT INTO review_dispatches (task_id, submission_generation, agent_id, "
        "run_id, model, profile, status, channel, reviewer_principal_id, "
        "replaces_dispatch_id) VALUES (?, 1, 'local:1', 'r', 'm', 'lite', 'done', "
        "'local', ?, ?)",
        (task_id, OTHER, stub),
    )
    deep = await _dispatch_row(db, task_id, status="active", profile="deep")
    unbound = await _report(db, task_id, incomplete=False, profile="deep")
    deep_row = dict(
        (await fetchall(db, "SELECT * FROM review_dispatches WHERE id=?", (deep,)))[0]
    )
    own = await dispatcher.report_of_dispatch(db, task_id, 1, deep_row)
    assert own is not None and own["id"] == unbound


async def test_a_stale_identity_report_resolved_to_a_refused_stub_restores_it(
    db: aiosqlite.Connection,
):
    """CR gen 2: личность с dispatch_id=None, затем заглушка S, замена L, отчёт S."""
    task_id, code, code_id = await _bound_later(db, "stale-then-stub")
    cur = await db.execute(
        "INSERT INTO review_dispatches (task_id, submission_generation, agent_id, "
        "run_id, model, profile, status, channel, reviewer_principal_id) "
        "VALUES (?, 1, '', '', 'grok-4.6', 'lite', 'failed', 'cloud', ?)",
        (task_id, PRINCIPAL),
    )
    stub = int(cur.lastrowid or 0)
    await _bind(db, code, stub, task_id)
    await db.execute(
        "INSERT INTO review_dispatches (task_id, submission_generation, agent_id, "
        "run_id, model, profile, status, channel, reviewer_principal_id, "
        "replaces_dispatch_id) VALUES (?, 1, 'local:1', 'r', 'm', 'lite', 'active', "
        "'local', ?, ?)",
        (task_id, OTHER, stub),
    )
    await db.commit()
    await record_machine_review(
        db,
        task_id,
        _body(False),
        principal_id=PRINCIPAL,
        username="rev",
        code_id=code_id,
    )
    (report,) = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert report["dispatch_id"] == stub
    active = {dict(r)["id"] for r in await repo.list_active_review_dispatches(db)}
    assert stub in active, "заглушка S восстановлена и видна свипу"


async def test_only_the_stub_of_the_reports_code_is_restored(
    db: aiosqlite.Connection,
):
    """Codex: две заглушки, код привязан к A, личность с None - оживает только A."""
    task_id, code, code_id = await _bound_later(db, "two-stubs")
    ids = []
    for _ in range(2):
        cur = await db.execute(
            "INSERT INTO review_dispatches (task_id, submission_generation, "
            "agent_id, run_id, model, profile, status, channel, "
            "reviewer_principal_id) VALUES (?, 1, '', '', 'm', 'lite', 'failed', "
            "'cloud', ?)",
            (task_id, PRINCIPAL),
        )
        ids.append(int(cur.lastrowid or 0))
    first, second = ids
    await _bind(db, code, first, task_id)
    await record_machine_review(
        db,
        task_id,
        _body(False),
        principal_id=PRINCIPAL,
        username="rev",
        code_id=code_id,
    )
    rows = await fetchall(db, "SELECT id, status FROM review_dispatches ORDER BY id")
    assert {dict(r)["id"]: dict(r)["status"] for r in rows} == {
        first: "active",
        second: "failed",
    }


async def test_a_finally_closed_order_is_not_reopened_by_the_next_report(
    db: aiosqlite.Connection, monkeypatch
):
    """CR gen 3: S восстановлена, свип закрыл done без агента - новый отчёт её не оживляет."""
    from types import SimpleNamespace

    from hub.integrations import cursor_cloud

    async def _no_agent(_marker):
        return SimpleNamespace(agent_id="", run_id="", asked=True)

    async def _usage(_db, _dispatch):
        return None

    monkeypatch.setattr(cursor_cloud, "find_agent_by_name", _no_agent)
    monkeypatch.setattr(dispatcher, "_stamp_dispatch_usage", _usage)
    task_id = await _reviewer_task(db, "final-done")
    cur = await db.execute(
        "INSERT INTO review_dispatches (task_id, submission_generation, agent_id, "
        "run_id, model, profile, status, channel, reviewer_principal_id) "
        "VALUES (?, 1, '', '', 'm', 'lite', 'failed', 'cloud', ?)",
        (task_id, PRINCIPAL),
    )
    stub = int(cur.lastrowid or 0)
    await db.commit()

    async def _report_to_stub():
        await record_machine_review(
            db,
            task_id,
            _body(False),
            principal_id=PRINCIPAL,
            username="rev",
            dispatch_id=stub,
        )

    async def _status() -> str:
        rows = await fetchall(
            db, "SELECT status FROM review_dispatches WHERE id=?", (stub,)
        )
        return str(dict(rows[0])["status"])

    async def _alerts() -> int:
        updates = await repo.get_task_updates(db, task_id)
        return sum("Счёт прогона по заказу" in dict(u)["content"] for u in updates)

    await _report_to_stub()
    assert await _status() == "active", "первый отчёт оживляет отказанную заглушку"
    await db.execute(
        "UPDATE review_dispatches SET created_at=datetime('now','-2 days')"
    )
    await db.commit()
    await dispatcher.sweep_review_dispatches(db)
    assert await _status() == "done" and await _alerts() == 1
    await _report_to_stub()
    assert await _status() == "done", "окончательно разобранный заказ не оживает"
    await dispatcher.sweep_review_dispatches(db)
    assert await _alerts() == 1, "второго итогового алерта нет"
