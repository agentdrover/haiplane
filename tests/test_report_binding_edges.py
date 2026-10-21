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
from tests.test_report_binding import _mint, _redeem, _reviewer_task
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
