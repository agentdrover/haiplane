"""Автоповтор доставки принятой задачи после base_updated (#1999).

Прод 22.10 (#1989): decide accept/deliver получил base_updated, задача completed,
гейт её больше не трогает, ручная доставка из реестра проигрывает гонку с develop.
Теперь отказ base_updated вооружает автоповтор, свип доставки повторяет ТОТ ЖЕ
``deliver_on_disposition`` после зелёного CI новой головы.
"""

from __future__ import annotations

import asyncio

import pytest

from hub import repository as repo
from hub.db import connect
from hub.services import lifecycle
from hub.services.delivery_state import scan_completed_deliveries
from tests.accepted_delivery_retry_support import (
    FAILED,
    PASSED,
    PENDING,
    TIP,
    accepted_task,
    approved_task,
    armed_rig,
)


async def _feed(rig) -> str:
    rows = await repo.get_task_updates(rig.db, rig.task_id)
    return "\n".join(dict(u)["content"] or "" for u in rows)


async def test_accepted_task_is_delivered_after_green_ci(db, monkeypatch):
    rig = await armed_rig(db, monkeypatch)
    armed = await rig.retry_row()
    assert (armed["state"], armed["attempts"]) == ("armed", 0)
    assert armed["pr_number"] == 77 and armed["submission_generation"] >= 1

    rig.ci(PENDING)
    await scan_completed_deliveries(db)
    rig.git.merge_pr.assert_not_awaited()
    waiting = await rig.retry_row()
    assert (waiting["state"], waiting["attempts"]) == ("armed", 0), (
        "CI идёт: без расхода"
    )

    rig.ci(PASSED)
    await scan_completed_deliveries(db)

    assert rig.git.merge_pr.await_count == 1
    assert rig.git.merge_pr.await_args.kwargs["expected_head_sha"] == TIP
    done = await rig.retry_row()
    assert (done["state"], done["outcome"], done["attempts"]) == (
        "closed",
        "delivered",
        1,
    )
    assert await repo.pipeline_merge_recorded(db, rig.task_id, 77)
    assert (await rig.task())["status"] == "completed"
    assert "автоповтор" in (await _feed(rig)).lower()
    registry = await repo.get_delivery_discrepancy(db, rig.task_id)
    assert registry is not None and registry["state"] == "delivered", "реестр перечитан"

    await scan_completed_deliveries(db)
    assert rig.git.merge_pr.await_count == 1, "закрытая строка не повторяется"


@pytest.mark.parametrize(
    "scenario, state, outcome, gate_calls",
    [
        ("author_push", "closed", "stale_approval", 2),
        ("generation_changed", "closed", "generation_changed", 1),
        ("approval_not_current", "closed", "approval_not_current", 1),
        ("task_reopened", "closed", "task_not_completed", 1),
        ("pr_changed", "closed", "pr_changed", 1),
        ("red_ci", "closed", "ci_failed", 1),
        ("ci_running", "armed", "", 1),
    ],
)
async def test_retry_never_bypasses_the_gate(
    db, monkeypatch, scenario, state, outcome, gate_calls
):
    rig = await armed_rig(db, monkeypatch)
    if scenario == "author_push":
        rig.head_moved_to("author0push")

        async def diff(workspace, base, sha):
            return (
                "@@\n+другая строка автора\n"
                if sha == "author0push"
                else "@@\n+та же\n"
            )

        rig.git.branch_diff.side_effect = diff
    elif scenario == "generation_changed":
        await repo.update_task(db, rig.task_id, submission_generation=9)
        await db.commit()
    elif scenario == "approval_not_current":
        await repo.update_task(db, rig.task_id, review_verdict="changes_requested")
        await db.commit()
    elif scenario == "task_reopened":
        await repo.update_task(db, rig.task_id, status="running")
        await db.commit()
    elif scenario == "pr_changed":
        await repo.update_task(db, rig.task_id, pr_number=78)
        await db.commit()
    elif scenario == "red_ci":
        rig.ci(FAILED)
    else:
        rig.ci(PENDING)

    for _ in range(3):
        await scan_completed_deliveries(db)

    rig.git.merge_pr.assert_not_awaited()
    row = await rig.retry_row()
    assert (row["state"], row["outcome"]) == (state, outcome)
    refusals = (await _feed(rig)).count("Доставка по решению человека НЕ выполнена")
    assert refusals == gate_calls, "до гейта доходит только то, что стоп не ловит сам"
    stops = await rig.events("accepted_delivery_retry_stopped")
    if state == "closed":
        assert len(stops) == 1, "стоп говорит один раз: событие и алерт"
        assert outcome in stops[0]["payload"]
        assert "автоповтор остановлен" in (await _feed(rig)).lower()
    else:
        assert not stops and row["attempts"] == 0


async def test_retry_limits_survive_restart(db, db_dsn, monkeypatch):
    spare = [await approved_task(db) for _ in range(2)]  # до первого стенда
    rig = await armed_rig(db, monkeypatch)
    first_armed_at = (await rig.retry_row())["armed_at"]

    for n in range(1, 7):  # шесть попыток; голова каждый раз уходит вперёд
        sha = f"base0moved0{n}"
        rig.head_moved_to(sha, behind_base=True)
        await scan_completed_deliveries(db)
        row = await rig.retry_row()
        assert (row["state"], row["attempts"]) == ("armed", n), (
            "base_updated повторился"
        )
        assert rig.git.update_pr_branch.await_count == 1 + n
    ok, reason, _ = await rig.deliver(
        "registry_deliver"
    )  # повторный base_updated вручную
    assert not ok
    assert (await rig.retry_row())["armed_at"] == first_armed_at, "срок не сброшен"
    assert (await rig.retry_row())["attempts"] == 6, "ручной отказ не вооружает заново"

    before = rig.git.update_pr_branch.await_count
    rig.head_moved_to("base0moved0x", behind_base=True)
    await scan_completed_deliveries(db)
    exhausted = await rig.retry_row()
    assert (exhausted["state"], exhausted["outcome"]) == (
        "closed",
        "attempts_exhausted",
    )
    assert rig.git.update_pr_branch.await_count == before, "седьмой попытки нет"
    assert len(await rig.events("accepted_delivery_retry_stopped")) == 1

    # 24 часа от первого вооружения
    other = await accepted_task(db, monkeypatch, task_id=spare[0])
    ok, reason, _ = await other.deliver()
    assert reason.startswith("base_updated")
    other.head_moved_to(TIP)
    other.ci(PASSED)
    await db.execute(
        "UPDATE accepted_delivery_retries SET armed_at=datetime('now', '-25 hours') "
        "WHERE task_id=?",
        (other.task_id,),
    )
    await db.commit()
    await scan_completed_deliveries(db)
    expired = await other.retry_row()
    assert (expired["state"], expired["outcome"]) == ("closed", "window_expired")
    other.git.merge_pr.assert_not_awaited()

    # рестарт: бронь поставлена и сеть ответила, процесс умер до записи исхода
    third = await accepted_task(db, monkeypatch, task_id=spare[1])
    await third.deliver()
    third.head_moved_to(TIP)
    third.ci(PASSED)
    await db.execute(
        "UPDATE accepted_delivery_retries SET state='reserved', attempts=2, "
        "reserved_at=datetime('now') WHERE task_id=?",
        (third.task_id,),
    )
    await db.commit()
    fresh = await connect(db_dsn)
    try:
        await scan_completed_deliveries(fresh)
        third.git.merge_pr.assert_not_awaited()  # бронь свежая: соседний проход жив
        assert (await third.retry_row())["state"] == "reserved"
        await fresh.execute(
            "UPDATE accepted_delivery_retries SET reserved_at=datetime('now', '-2 hours') "
            "WHERE task_id=?",
            (third.task_id,),
        )
        await fresh.commit()
        await scan_completed_deliveries(fresh)
    finally:
        await fresh.close()
    done = await third.retry_row()
    assert (done["state"], done["outcome"], done["attempts"]) == (
        "closed",
        "delivered",
        3,
    )
    assert third.git.merge_pr.await_count == 1


async def test_concurrent_delivery_merges_once(db, db_dsn, monkeypatch):
    rig = await armed_rig(db, monkeypatch)
    await repo.record_delivery_discrepancy(
        db,
        task_id=rig.task_id,
        state="pr_open",
        reason="PR открыт",
        pr_number=77,
        delivery_path="",
        disposition="deliver",
    )

    async def slow_merge(*args, **kwargs):
        await asyncio.sleep(0.2)
        return True

    rig.git.merge_pr.side_effect = slow_merge
    other, third = await connect(db_dsn), await connect(db_dsn)  # по соединению на ход
    try:
        results = await asyncio.gather(
            scan_completed_deliveries(db),
            lifecycle.deliver_from_registry(other, rig.task_id, by="owner"),
            scan_completed_deliveries(third),
            return_exceptions=True,
        )
    finally:
        await other.close()
        await third.close()

    assert rig.git.merge_pr.await_count == 1, results
    rows = await rig.retry_rows()
    assert len(rows) == 1 and rows[0]["attempts"] <= 1, "один расход, одна строка"
    assert rows[0]["state"] == "closed" and rows[0]["outcome"] == "delivered"
    assert await repo.pipeline_merge_recorded(db, rig.task_id, 77)


async def test_manual_refusal_names_the_auto_retry(client, db, monkeypatch):
    rig = await accepted_task(db, monkeypatch)
    await repo.record_delivery_discrepancy(
        db,
        task_id=rig.task_id,
        state="pr_open",
        reason="PR открыт",
        pr_number=77,
        delivery_path="",
        disposition="",
    )
    resp = await client.post(f"/api/delivery/discrepancies/{rig.task_id}/deliver")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"].startswith("base_updated")
    assert detail["actor_hint"] == "none", "человеку делать нечего"
    message = detail["message"].lower()
    assert "автоповтор" in message and "6" in message and "24" in message
    assert "красный ci" in message or "красном ci" in message
    row = await rig.retry_row()
    assert row["state"] == "armed", "ручной отказ вооружил задачу"

    rig.head_moved_to("base0moved0web", behind_base=True)
    page = await client.post(
        f"/tasks/{rig.task_id}/web-deliver-undelivered",
        headers={"HX-Request": "true"},
    )
    assert page.status_code == 200, page.text
    assert "автоповтор" in page.text.lower()
