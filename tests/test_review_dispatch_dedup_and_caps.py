"""Hub-dispatched cross-model reviews (#757): dedup_and_caps (split of the former single file, #1670)."""

from __future__ import annotations

import asyncio
import json
import re

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import config, services
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.integrations.registry import plugins
from hub.models import TaskSubmitReview
from hub.services.review_dispatch import (
    DEEP,
    maybe_dispatch_review,
    sweep_review_dispatches,
)
from tests.review_dispatch_support import (
    _DIFF_506,
    _HARMLESS_DIFF,
    _RATE_LIMIT,
    _TIP,
    _age_dispatches,
    _any_dispatch_row,
    _author_closed_them,
    _author_patch,
    _cap_project,
    _card,
    _confirmed,
    _dispatch_notes,
    _DispatchRecorder,
    _generation_with_findings,
    _local_principal,
    _machine_report,
    _no_local_path,
    _node,
    _pinned_setup,
    _PinnedGitOps,
    _reporting_stub,
    _rows_of,
    _Sequence,
    _small_delta_profile,
    _stub_reviewer,
    _submitted,
    _submitted_in,
    _walk_the_circle,
    _wire,
)


# --- #1399: одна сдача и один профиль — один облачный вызов -----------------
#
# Прод, 23.09 и 25.09: #1301 gen2 (deep, 347/348) и #1378 gen1 (lite, 412/413)
# получили по два оплаченных облачных ревьюера на одну сдачу, оба без
# replaces_dispatch_id. У #1301 второй вызов пришёл от ВТОРОГО
# hub_submit_for_review того же коммита из review (mcp_call_events 8434 и
# 8439): пересдача того же sha не поднимает поколение (#1265) и зовёт
# диспетчер снова, а страж новизны видит только ОТЧЁТЫ, не заказы.

_DISPATCHED_NOTE = "Кросс-модельное ревью вызвано хабом"


async def _dispatched_notes(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND content LIKE ?",
        (task_id, f"{_DISPATCHED_NOTE}%"),
    )
    return len(rows)


async def test_second_trigger_same_generation_and_profile_is_noop(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1399): уже вызванная сдача второго ревьюера того же профиля не
    покупает — ни повтором сдачи того же коммита, ни прямым вызовом."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "once-per-submission")
    rows = await _rows_of(db, task_id)
    assert [(r["profile"], r["status"]) for r in rows] == [("lite", "active")]

    # Триггер №1: тот же коммит сдан ещё раз из review — путь #1301.
    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    assert view.status.value == "review"
    # Триггер №2: диспетчер позван напрямую на ту же сдачу.
    assert await maybe_dispatch_review(db, task_id) is False

    assert len(recorder.calls) == 1, "второго агента Cursor не создавали"
    assert len(await _rows_of(db, task_id)) == 1, "второй строки заказа нет"
    assert await _dispatched_notes(db, task_id) == 1, (
        "запись «ревью вызвано» в карточке одна"
    )


class _SlowRecorder(_DispatchRecorder):
    """Создание агента занимает время — окно, в которое прилетает второй
    триггер (у #1378 вызовы разошлись на 5 секунд)."""

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(0.3)
        n = len(self.calls)
        return {"agent": {"id": f"bc-{n}"}, "run": {"id": f"run-{n}"}}, None


async def test_concurrent_triggers_create_one_dispatch(
    client: AsyncClient, db: aiosqlite.Connection, db_dsn: str, monkeypatch
):
    """AC-2 (#1399): два триггера одной сдачи параллельно, каждый на СВОЁМ
    соединении, как запросы на проде (#1065): ровно один вызов и одна строка."""
    from hub import db as hub_db
    from hub.services import lifecycle

    recorder = _SlowRecorder(None)
    _wire(monkeypatch, recorder)

    async def _no_dispatch(_db, _task_id):
        return None

    # Сдача без диспетча: оба вызова ниже — первые на эту сдачу.
    monkeypatch.setattr(lifecycle, "_dispatch_cross_model_review", _no_dispatch)
    task_id = await _submitted(client, db, "concurrent-triggers")
    assert recorder.calls == []

    first = await hub_db.connect(db_dsn)
    second = await hub_db.connect(db_dsn)
    try:
        results = await asyncio.gather(
            maybe_dispatch_review(first, task_id),
            maybe_dispatch_review(second, task_id),
        )
    finally:
        await first.close()
        await second.close()

    assert sorted(results) == [False, True], results
    assert len(recorder.calls) == 1, "create_agent вызван ровно один раз"
    assert len(await _rows_of(db, task_id)) == 1, "строка заказа одна"
    assert await _dispatched_notes(db, task_id) == 1


async def test_escalation_and_retry_after_failure_still_dispatch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1399): защита от дубля не глушит задуманные вторые вызовы —
    добор lite→deep, переспрос после отказа 429 и новый вызов после отказа."""
    # (а) Эскалация: неполный lite-отчёт покупает deep на той же сдаче.
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    _no_local_path(monkeypatch)
    escalated = await _submitted(client, db, "guard-escalation")
    await _machine_report(client, escalated, incomplete=True)
    assert len(recorder.calls) == 2, "добор deep поставлен"
    assert [r["profile"] for r in await _rows_of(db, escalated)] == ["lite", "deep"]

    # (б) Отказ 429 и переспрос свипом: новая строка с replaces_dispatch_id.
    provider = _Sequence(
        [
            (None, _RATE_LIMIT),
            ({"agent": {"id": "bc-second"}, "run": {"id": "run-second"}}, None),
        ]
    )
    _wire(monkeypatch, provider)
    retried = await _submitted(client, db, "guard-retry", policy={"review": "dispatch"})
    assert len(provider.calls) == 1
    await _age_dispatches(db)
    await sweep_review_dispatches(db)
    assert len(provider.calls) == 2, "переспрос после отказа состоялся"
    rows = await _rows_of(db, retried)
    assert rows[0]["status"] == "failed"
    assert rows[-1]["agent_id"] == "bc-second"
    assert rows[-1]["replaces_dispatch_id"] == rows[0]["id"]

    # (в) Отказ на вызове не держит сдачу: следующий триггер той же сдачи
    # зовёт ревьюера, а не упирается в собственную несостоявшуюся попытку.
    again = _Sequence(
        [
            (None, _RATE_LIMIT),
            ({"agent": {"id": "bc-after"}, "run": {"id": "run-after"}}, None),
        ]
    )
    _wire(monkeypatch, again)
    refused = await _submitted(
        client, db, "guard-after-refusal", policy={"review": "dispatch"}
    )
    assert len(again.calls) == 1
    assert await maybe_dispatch_review(db, refused) is True
    assert len(again.calls) == 2, "отказ не заблокировал следующий вызов"
    assert (await _rows_of(db, refused))[-1]["agent_id"] == "bc-after"


async def test_order_claims_migration_survives_existing_duplicates(
    db: aiosqlite.Connection,
):
    """#1399, риск data_migration: миграция ложится на базу, где дубли уже
    есть (как 412/413 на проде), не трогает их, и бронь на такой сдаче
    отказывает, а на другом профиле — выдаётся."""
    from hub.db import _migrate

    await db.execute("DROP TABLE review_order_claims")
    await db.execute(
        "DELETE FROM _migrations WHERE name = 'create_review_order_claims'"
    )
    task_id = await _node(db, title="dup", task_type="epic", parent_id=None)
    for agent in ("bc-a", "bc-b"):
        await repo.create_review_dispatch(
            db,
            task_id=task_id,
            submission_generation=1,
            agent_id=agent,
            run_id="",
            model="grok-4.6",
            profile="lite",
        )
    await db.commit()

    await _migrate(db)

    assert len(await _rows_of(db, task_id)) == 2, "дубли пережили миграцию"
    assert await repo.claim_review_order(db, task_id, 1, "lite") is False
    assert await repo.claim_review_order(db, task_id, 1, "deep") is True
    assert await repo.claim_review_order(db, task_id, 1, "deep") is False, (
        "вторая бронь того же профиля не выдаётся"
    )


async def test_second_trigger_leaves_the_running_reviewers_code_alive(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1399, находка a1d2d6301a527145: повторный триггер НЕ гасит код доступа
    уже запущенного ревьюера.

    issue_code удаляет невыкупленные коды того же принципала на эту задачу и
    поколение. Если второй триггер доходит до выпуска кода раньше, чем бронь
    ему откажет, у оплаченного агента в промпте мёртвый код: redeem — 401,
    отчёта нет. Принципал здесь настоящий — без него код не выпускается и
    дефект не виден.
    """
    from hub.services import chat_pair

    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "code-survives-retrigger")
    assert len(recorder.calls) == 1
    found = re.search(r'"code":"([^"]+)"', recorder.calls[0]["prompt_text"])
    assert found, "в промпте первого ревьюера нет кода доступа"

    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    assert await maybe_dispatch_review(db, task_id) is False
    assert len(recorder.calls) == 1

    assert await chat_pair.redeem_code(db, found.group(1)) is not None, (
        "код запущенного ревьюера погашен повторным триггером — отчёт не дойдёт"
    )


async def test_blind_create_keeps_the_order_claimed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1399, находка 815b0841472792be: слепой исход (ответ не дошёл и
    спросить провайдера не вышло) бронь НЕ снимает — агент, возможно, уже
    оплачен, и следующий триггер купил бы второго."""
    recorder = _DispatchRecorder(
        None, refusal=cursor_cloud.Refusal(status=0, detail="таймаут ответа")
    )
    _wire(monkeypatch, recorder)
    _no_local_path(monkeypatch)

    async def _cannot_ask(name, pages=3):
        return cursor_cloud.Reconciliation("", "", False)

    monkeypatch.setattr(cursor_cloud, "find_agent_by_name", _cannot_ask)
    task_id = await _submitted(
        client, db, "blind-keeps-claim", policy={"review": "dispatch"}
    )
    assert len(recorder.calls) == 1

    assert await maybe_dispatch_review(db, task_id) is False
    assert len(recorder.calls) == 1, (
        "после слепого исхода второй вызов провайдера покупал бы второго агента"
    )


async def test_refused_call_holds_the_claim_until_the_stub_is_written(
    client: AsyncClient, db: aiosqlite.Connection, db_dsn: str, monkeypatch
):
    """#1399, находка 6d0bd1c790fb24eb: отказ провайдера не отпускает бронь
    ДО записи заглушки второй двери. Триггер, пришедший в это окно (здесь —
    ровно на входе в _owe_the_refused_call), ревьюера не покупает."""
    from hub import db as hub_db
    from hub.services import lifecycle
    from hub.services import review_dispatch as rd

    provider = _Sequence(
        [
            (None, _RATE_LIMIT),
            ({"agent": {"id": "bc-window"}, "run": {"id": "run-window"}}, None),
        ]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)

    async def _no_dispatch(_db, _task_id):
        return None

    monkeypatch.setattr(lifecycle, "_dispatch_cross_model_review", _no_dispatch)
    task_id = await _submitted(
        client, db, "refusal-window", policy={"review": "dispatch"}
    )

    original = rd._owe_the_refused_call
    in_window: list[bool] = []

    async def _trigger_in_the_window(*args, **kwargs):
        other = await hub_db.connect(db_dsn)
        try:
            in_window.append(await maybe_dispatch_review(other, task_id))
        finally:
            await other.close()
        return await original(*args, **kwargs)

    monkeypatch.setattr(rd, "_owe_the_refused_call", _trigger_in_the_window)
    assert await maybe_dispatch_review(db, task_id) is False

    assert in_window == [False], "триггер в окне отказа купил ревьюера"
    assert len(provider.calls) == 1


async def test_deep_over_daily_cap_downgrades_to_lite_with_reason(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1414): два неотказавших deep сегодня при потолке 2 — третья
    сдача, которой правило дало бы deep, получает lite и названную причину."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-cap"}, "run": {"id": "r-cap"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "")
    pid = await _cap_project(db, "deep-cap", {"deep_daily_cap": 2})

    first = await _submitted_in(db, pid)
    second = await _submitted_in(db, pid)
    third = await _submitted_in(db, pid)

    assert [(await _any_dispatch_row(db, t))["profile"] for t in (first, second)] == [
        "deep",
        "deep",
    ]
    assert (await _any_dispatch_row(db, third))["profile"] == "lite"
    assert "ЛЁГКОЕ ревью" in recorder.calls[-1]["prompt_text"]
    notes = await _dispatch_notes(db, third)
    assert len(notes) == 1
    assert "профиль lite" in notes[0]
    assert "deep по правилу (процессная поверхность" in notes[0], notes[0]
    assert "суточный потолок 2 исчерпан — lite" in notes[0], notes[0]
    events = await db.execute_fetchall(
        "SELECT payload FROM events WHERE kind='review_dispatched' AND task_id=?",
        (third,),
    )
    reasons = json.loads(events[-1][0])["profile_reasons"]
    assert any("суточный потолок 2 исчерпан" in r for r in reasons), reasons

    # Другой проект потолком этого не связан.
    other = await _cap_project(db, "deep-cap-other", {"deep_daily_cap": 2})
    assert (await _any_dispatch_row(db, await _submitted_in(db, other)))[
        "profile"
    ] == "deep"

    # Отказ провайдера и вчерашний заказ — не оплаченный сегодня deep.
    await db.execute(
        "UPDATE review_dispatches SET status='failed' WHERE task_id=?", (first,)
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at=datetime('now', '-1 day') "
        "WHERE task_id=?",
        (second,),
    )
    await db.execute("DELETE FROM review_order_claims")
    await db.commit()
    fourth = await _submitted_in(db, pid)
    fifth = await _submitted_in(db, pid)
    sixth = await _submitted_in(db, pid)
    assert [
        (await _any_dispatch_row(db, t))["profile"] for t in (fourth, fifth, sixth)
    ] == ["deep", "deep", "lite"], "отказ и вчерашний deep в счёт не идут"

    # Сводка ревью (#1406) называет, сколько сдач потолок увёл в lite.
    from hub.services.review_economy import review_economy

    economy = await review_economy(db, since_days=7, escaped={})
    assert economy["deep_cap"]["downgraded_submissions"] == 2, economy["deep_cap"]


async def test_security_and_human_request_bypass_deep_cap(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1414): риск security и ручной запрос человека потолком не
    режутся, даже когда потолок исчерпан."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-by"}, "run": {"id": "r-by"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "")
    pid = await _cap_project(db, "deep-cap-bypass", {"deep_daily_cap": 0})

    ordinary = await _submitted_in(db, pid)
    assert (await _any_dispatch_row(db, ordinary))["profile"] == "lite", (
        "потолок 0 исчерпан с первой сдачи"
    )
    security = await _submitted_in(
        db,
        pid,
        risks=[{"kind": "security", "severity": "low", "description": "d"}],
    )
    human = await _submitted_in(db, pid, diff=_HARMLESS_DIFF, override="require")

    assert (await _any_dispatch_row(db, security))["profile"] == "deep"
    assert (await _any_dispatch_row(db, human))["profile"] == "deep"
    for task_id in (security, human):
        assert all("потолок" not in n for n in await _dispatch_notes(db, task_id))


async def test_deep_cap_holds_under_concurrent_orders(
    client: AsyncClient, db: aiosqlite.Connection, db_dsn: str, monkeypatch
):
    """AC-3 (#1414): потолок 1 и два параллельных заказа deep по разным
    задачам проекта, каждый на своём соединении — ровно один deep."""
    from hub import db as hub_db
    from hub.services import lifecycle

    recorder = _SlowRecorder(None)
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "")

    async def _no_dispatch(_db, _task_id):
        return None

    monkeypatch.setattr(lifecycle, "_dispatch_cross_model_review", _no_dispatch)
    pid = await _cap_project(db, "deep-cap-race", {"deep_daily_cap": 1})
    one = await _submitted_in(db, pid)
    two = await _submitted_in(db, pid)
    assert recorder.calls == []

    first = await hub_db.connect(db_dsn)
    second = await hub_db.connect(db_dsn)
    try:
        results = await asyncio.gather(
            maybe_dispatch_review(first, one),
            maybe_dispatch_review(second, two),
        )
    finally:
        await first.close()
        await second.close()

    assert results == [True, True], results
    profiles = sorted(
        [(await _any_dispatch_row(db, one))["profile"]]
        + [(await _any_dispatch_row(db, two))["profile"]]
    )
    assert profiles == ["deep", "lite"], profiles


async def test_no_deep_cap_means_unchanged_behaviour(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-4 (#1414): без ключа и с пустым REVIEW_DEEP_DAILY_CAP всё deep, как
    до задачи; отрицательный потолок API отклоняет 422."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-nc"}, "run": {"id": "r-nc"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "")
    pid = await _cap_project(db, "deep-no-cap", {})

    tasks = [await _submitted_in(db, pid) for _ in range(3)]
    assert [(await _any_dispatch_row(db, t))["profile"] for t in tasks] == ["deep"] * 3
    for task_id in tasks:
        notes = await _dispatch_notes(db, task_id)
        assert len(notes) == 1
        assert "потолок" not in notes[0]
    assert not (
        await db.execute_fetchall(
            "SELECT 1 FROM review_order_claims WHERE profile LIKE 'deep_cap%'"
        )
    ), "без потолка мест не бронируют"

    # Глобальное умолчание работает, когда ключа нет.
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "3")
    capped = await _submitted_in(db, pid)
    assert (await _any_dispatch_row(db, capped))["profile"] == "lite"

    for bad in (-1, "2", True, 1.5):
        resp = await client.patch(
            f"/api/projects/{pid}", json={"gate_policy": {"deep_daily_cap": bad}}
        )
        assert resp.status_code == 422, (bad, resp.text)
    ok = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"deep_daily_cap": 0}}
    )
    assert ok.status_code == 200, ok.text


async def test_deep_cap_binds_only_the_cloud_channel(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1414, решение владельца 25.09: потолок — только облачный канал.

    Локальный ревьюер квоту Cursor не тратит: при исчерпанном потолке он
    получает deep, места не бронирует и в счёт не идёт. Облачная сдача с тем
    же профилем в том же проекте — lite.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-ch"}, "run": {"id": "r-ch"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "")
    pid = await _cap_project(
        db, "deep-cap-channel", {"review": "dispatch", "deep_daily_cap": 1}
    )
    await repo.update_project(db, pid, forge="gitverse")
    await db.commit()

    local = [await _submitted_in(db, pid) for _ in range(2)]
    await wait_for_local_runs()
    await db.commit()
    assert recorder.calls == [], "gitverse облаком не ревьюится"
    for task_id in local:
        row = await _any_dispatch_row(db, task_id)
        assert (row["channel"], row["profile"]) == ("local", "deep"), row
        assert all("потолок" not in n for n in await _dispatch_notes(db, task_id))
    assert not (
        await db.execute_fetchall(
            "SELECT 1 FROM review_order_claims WHERE profile = ?",
            (repo.DEEP_SEAT_CLAIM,),
        )
    ), "локальный заказ места под потолком не бронирует"

    await repo.update_project(db, pid, forge="github")
    await db.commit()
    cloud = [await _submitted_in(db, pid) for _ in range(2)]
    assert [
        ((r := await _any_dispatch_row(db, t))["channel"], r["profile"]) for t in cloud
    ] == [("cloud", "deep"), ("cloud", "lite")], "локальные deep в счёт не вошли"


async def test_ladder_top_up_respects_deep_cap(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1414, находка high: добор лестницы (#879) — принудительный облачный
    deep, и потолок его тоже держит. Потолок исчерпан — добор не куплен,
    причина названа, лестница не повторяется."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-tu"}, "run": {"id": "r-tu"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "")
    pid = await _cap_project(db, "deep-cap-topup", {"deep_daily_cap": 1})

    taken = await _submitted_in(db, pid)
    assert (await _any_dispatch_row(db, taken))["profile"] == "deep"
    cheap = await _submitted_in(db, pid, diff=_HARMLESS_DIFF)
    assert [r["profile"] for r in await _rows_of(db, cheap)] == ["lite"]
    calls = len(recorder.calls)

    await _machine_report(client, cheap, incomplete=True)

    assert len(recorder.calls) == calls, "добор deep сверх потолка не куплен"
    assert [r["profile"] for r in await _rows_of(db, cheap)] == ["lite"]
    alerts = [
        r[0]
        for r in await db.execute_fetchall(
            "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
            (cheap,),
        )
    ]
    assert any(
        "суточный потолок deep исчерпан — добор не куплен" in a for a in alerts
    ), alerts
    assert not await db.execute_fetchall(
        "SELECT 1 FROM review_order_claims WHERE task_id=? AND profile=?",
        (cheap, "deep"),
    ), "бронь заказа добора снята"


async def test_global_deep_cap_default_applies_without_project_key(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1414: REVIEW_DEEP_DAILY_CAP='3' и проект без ключа — три deep куплены,
    четвёртый понижен до lite с причиной."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-gl"}, "run": {"id": "r-gl"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "3")
    pid = await _cap_project(db, "deep-cap-global", {})

    tasks = [await _submitted_in(db, pid) for _ in range(4)]
    assert [(await _any_dispatch_row(db, t))["profile"] for t in tasks] == [
        "deep",
        "deep",
        "deep",
        "lite",
    ]
    notes = await _dispatch_notes(db, tasks[-1])
    assert "суточный потолок 3 исчерпан — lite" in notes[0], notes


async def test_same_submission_takes_one_deep_seat(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1414: переспрос той же сдачи (replaces_dispatch_id, #1242) второго
    места не занимает — ни себе в отказ, ни соседям в счёт."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-rs"}, "run": {"id": "r-rs"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "")
    pid = await _cap_project(db, "deep-cap-seat", {"deep_daily_cap": 2})

    first = await _submitted_in(db, pid)
    original = await _any_dispatch_row(db, first)
    assert original["profile"] == "deep"
    await db.execute(
        "UPDATE review_dispatches SET status='failed' WHERE id=?", (original["id"],)
    )
    await db.commit()

    assert await maybe_dispatch_review(db, first, replaces_dispatch_id=original["id"])
    retry = await _any_dispatch_row(db, first)
    assert retry["replaces_dispatch_id"] == original["id"]
    assert retry["profile"] == "deep", "переспрос своей сдачи места не теряет"

    second = await _submitted_in(db, pid)
    assert (await _any_dispatch_row(db, second))["profile"] == "deep", (
        "сдача с переспросом заняла одно место из двух, не два"
    )

    # Потолок теперь полон (first, second) — а переспрос first всё равно deep:
    # его место уже его.
    await db.execute(
        "UPDATE review_dispatches SET status='failed' WHERE id=?", (retry["id"],)
    )
    await db.commit()
    assert await maybe_dispatch_review(db, first, replaces_dispatch_id=retry["id"])
    again = await _any_dispatch_row(db, first)
    assert again["replaces_dispatch_id"] == retry["id"]
    assert again["profile"] == "deep", "полный потолок свою сдачу не режет"

    third = await _submitted_in(db, pid)
    assert (await _any_dispatch_row(db, third))["profile"] == "lite"


async def test_small_resubmission_delta_gets_lite_and_names_the_cancelled_reason(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1416): пересдача gen2 с доказанной дельтой к gen1, авторская
    часть 30 строк с subprocess.run (и 200 строк uv.lock рядом, вне счёта),
    порог 80 — lite, и в причинах названы и правило, и отменённый повод."""
    monkeypatch.setattr(config, "REVIEW_SMALL_DELTA_LINES", "80")

    profile, reasons = await _small_delta_profile(
        client, db, monkeypatch, "spike-small-delta", 30
    )

    assert profile == "lite", reasons
    assert any("маленькая пересдача: 30 ≤ 80" in r for r in reasons), reasons
    cancelled = [r for r in reasons if "отменён повод deep" in r]
    assert cancelled and all(
        "процессная поверхность — запуск подпроцессов" in r for r in cancelled
    ), reasons

    # Сводка ревью (#1406): сколько пересдач правило увело в lite.
    from hub.services.review_economy import review_economy

    economy = await review_economy(db, since_days=7, escaped={})
    assert economy["small_delta"]["downgraded_submissions"] == 1, economy
    assert economy["small_delta"]["resubmissions"] == 1, economy


async def test_large_or_unprovable_delta_keeps_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1416): 200 строк авторской дельты — deep как раньше; 30 строк,
    но прежний коммит не предок (предмет — весь дифф) — тоже deep."""
    monkeypatch.setattr(config, "REVIEW_SMALL_DELTA_LINES", "80")

    large, large_reasons = await _small_delta_profile(
        client, db, monkeypatch, "spike-large-delta", 200
    )
    assert large == "deep", large_reasons
    assert not any("маленькая пересдача" in r for r in large_reasons)

    whole, whole_reasons = await _small_delta_profile(
        client, db, monkeypatch, "spike-unprovable-delta", 30, ancestor=False
    )
    assert whole == "deep", whole_reasons
    assert not any("маленькая пересдача" in r for r in whole_reasons)


async def test_small_delta_rule_respects_security_human_request_and_off_switch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1416): дельта 10 строк — но риск security, ручной запрос
    человека или выключенный порог: профиль как до задачи (deep)."""
    monkeypatch.setattr(config, "REVIEW_SMALL_DELTA_LINES", "80")
    security = [{"kind": "security", "severity": "low", "description": "d"}]
    cases = {
        "security": await _small_delta_profile(
            client, db, monkeypatch, "spike-small-sec", 10, risks=security
        ),
        "human": await _small_delta_profile(
            client, db, monkeypatch, "spike-small-human", 10, override="require"
        ),
        "project-off": await _small_delta_profile(
            client,
            db,
            monkeypatch,
            "spike-small-project-off",
            10,
            policy={"verdict": "auto", "small_delta_lines": 0},
        ),
    }
    monkeypatch.setattr(config, "REVIEW_SMALL_DELTA_LINES", "0")
    cases["off"] = await _small_delta_profile(
        client, db, monkeypatch, "spike-small-off", 10
    )

    for name, (profile, reasons) in cases.items():
        assert profile == "deep", (name, reasons)
        assert not any("маленькая пересдача" in r for r in reasons), (name, reasons)


def test_small_delta_lines_policy_key_is_validated():
    """#1416: ключ проекта small_delta_lines — целое ≥ 0, иначе отказ."""
    from hub.models import validated_gate_policy

    assert validated_gate_policy({"small_delta_lines": 40}) == {"small_delta_lines": 40}
    for bad in (-1, True, "40", 1.5):
        with pytest.raises(ValueError, match="small_delta_lines"):
            validated_gate_policy({"small_delta_lines": bad})


def test_author_delta_lines_counts_changed_lines_only():
    """#1416: счёт — добавленные и удалённые строки, без заголовков и без
    сгенерированных файлов; комбинированный дифф слияния тоже читается."""
    from hub.services.review_dispatch import author_delta_lines

    plain = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n-old\n---- deleted dashes\n+new\n"
        "\\ No newline at end of file\n"
    )
    assert author_delta_lines(plain) == 3
    combined = (
        "diff --cc a.py\nindex 1,2..3\n--- a/a.py\n+++ b/a.py\n"
        "@@@ -1,1 -1,1 +1,2 @@@\n++resolved\n+ ours\n  context\n"
    )
    assert author_delta_lines(combined) == 2
    assert author_delta_lines(_author_patch(30, generated=200)) == 30


# --- #1405: ревью покупается только на зелёном CI закреплённого коммита -------

_OTHER_SHA = "d" * 40
# Проект, CI которого хаб видит: отчёт о каком-то коммите у него уже есть.
_PROJECT_HAS_CI = [{"head_sha": _OTHER_SHA, "validation_status": "pass"}]


async def _events(db: aiosqlite.Connection, kind: str) -> list[dict]:
    return [
        json.loads(dict(r)["payload"])
        for r in await repo.list_events(db, since=0, kinds=[kind], limit=50)
    ]


async def test_red_ci_on_the_pinned_sha_buys_no_review_run(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1: CI отчитался раньше сдачи (обычный порядок) и назвал проверку fail.
    recorder = _DispatchRecorder({"agent": {"id": "bc-red"}, "run": {"id": "r"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(
        client,
        db,
        "red-ci",
        ci_reports=[
            {
                "validation_status": "fail",
                "checks": {"lint": "pass", "mutations": "fail", "types": "skipped"},
            }
        ],
    )
    assert recorder.calls == [], "красный CI — провайдер не вызван"

    # Лестница, вторая ось каскада и переспрос идут через тот же ранний отказ.
    assert not await maybe_dispatch_review(db, task_id, force_profile=DEEP)
    assert not await maybe_dispatch_review(
        db, task_id, force_profile=DEEP, force_model="gpt-5.3-codex"
    )
    assert not await maybe_dispatch_review(db, task_id, replaces_dispatch_id=1)
    assert recorder.calls == []
    rows = await db.execute_fetchall(
        "SELECT 1 FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert rows == []

    red = [c for c in await _card(db, task_id) if "ревью не куплено" in c.lower()]
    assert len(red) == 1, "одно событие на сдачу, не на каждый триггер"
    assert "CI красный" in red[0]
    assert "mutations" in red[0] and "validation" in red[0]
    assert "lint" not in red[0] and "types" not in red[0]
    [event] = await _events(db, "review_withheld_red_ci")
    assert event["failed"] == ["mutations", "validation"]
    assert event["sha"] == _TIP


async def test_projects_without_ci_and_human_requests_are_ordered_as_before(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4: проект, у которого хаб CI не видит, — заказ сразу.
    recorder = _DispatchRecorder({"agent": {"id": "bc-asis"}, "run": {"id": "r"}})
    _wire(monkeypatch, recorder)
    plain = await _submitted(client, db, "no-ci-project")
    assert len(recorder.calls) == 1
    assert not [c for c in await _card(db, plain) if "ждёт CI" in c]

    # Ручной запрос человека: ни отсутствие отчёта, ни красный CI его не держат.
    await _submitted(
        client, db, "asked-no-report", ci_reports=_PROJECT_HAS_CI, override="require"
    )
    assert len(recorder.calls) == 2
    await _submitted(
        client,
        db,
        "asked-red",
        ci_reports=[{"validation_status": "fail", "checks": {"tests": "fail"}}],
        override="require",
    )
    assert len(recorder.calls) == 3


async def test_a_submission_without_ci_report_is_ordered_now_and_says_so(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2: проект с CI, отчёта о закреплённом sha к сдаче нет — заказ сразу,
    # как до задачи, и это названо в ленте одной строкой.
    recorder = _DispatchRecorder({"agent": {"id": "bc-now"}, "run": {"id": "r"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "no-report", ci_reports=_PROJECT_HAS_CI)
    assert len(recorder.calls) == 1, "отчёта нет — заказ сразу, не ожидание"

    # Лестница по той же сдаче снова проходит условие — строка не повторяется.
    await maybe_dispatch_review(db, task_id, force_profile=DEEP)
    lines = [
        c for c in await _card(db, task_id) if "без отчёта CI о закреплённом sha" in c
    ]
    assert len(lines) == 1
    assert _TIP[:12] in lines[0]
    [event] = await _events(db, "review_ordered_without_ci")
    assert event["generation"] == 1 and event["sha"] == _TIP

    # Зелёный отчёт на закреплённом sha (находка 30d8d47c25e52e26): один
    # заказ, строка заказа, и ни события о красном, ни строки «без отчёта».
    before = len(recorder.calls)
    green = await _submitted(
        client, db, "green-pinned", ci_reports=[{"validation_status": "pass"}]
    )
    assert len(recorder.calls) == before + 1
    rows = await db.execute_fetchall(
        "SELECT 1 FROM review_dispatches WHERE task_id=?", (green,)
    )
    assert len(rows) == 1
    silent = await db.execute_fetchall(
        "SELECT 1 FROM events WHERE task_id=? AND kind IN "
        "('review_withheld_red_ci', 'review_ordered_without_ci')",
        (green,),
    )
    assert silent == []


async def test_a_late_ci_report_orders_nothing(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3: отложенного пути нет — ни приём отчёта CI, ни поллер ревью не
    # заказывают; событие о красном остаётся одним.
    from tests.ci_trust_support import accept_backed

    recorder = _DispatchRecorder({"agent": {"id": "bc-late"}, "run": {"id": "r"}})
    _wire(monkeypatch, recorder)

    async def _no_run(agent_id, run_id=None):
        return None

    monkeypatch.setattr(cursor_cloud, "get_run", _no_run)
    red = await _submitted(
        client,
        db,
        "late-green",
        ci_reports=[{"validation_status": "fail", "checks": {"tests": "fail"}}],
    )
    ordered = await _submitted(client, db, "late-any", ci_reports=_PROJECT_HAS_CI)
    assert len(recorder.calls) == 1, "заказ только у сдачи без отчёта"

    for task_id, status in ((red, "pass"), (ordered, "pass"), (ordered, "fail")):
        await accept_backed(
            db,
            task_id,
            head_sha=_TIP,
            ac_results={},
            validation_status=status,
            checks={"tests": status},
        )
        for _ in range(3):
            await sweep_review_dispatches(db)
    assert len(recorder.calls) == 1, "поздний отчёт CI заказа не ставит"
    # Находки 64ac296015b7d20d и c609380e10b71078: заказ без CI, поздний
    # красный отчёт — добор deep не покупается, и отказ назван добору одним
    # событием, а не вторым «ревью не куплено».
    before = len(recorder.calls)
    for _ in range(2):
        assert not await maybe_dispatch_review(db, ordered, force_profile=DEEP)
    assert len(recorder.calls) == before, "добор на красном не покупается"
    [topup] = await _events(db, "review_topup_withheld_red_ci")
    assert topup["failed"] == ["tests", "validation"]
    assert [c for c in await _card(db, ordered) if "Добор ревью не куплен" in c]
    red_events = [e for e in await _events(db, "review_withheld_red_ci")]
    assert len(red_events) == 1, "событие о красном — одно на сдачу"


# --- #1432: круг ревью останавливает покупку deep ----------------------------
#
# #1405 прошла пять deep подряд (отчёты #635, #637, #640, #641, #642), каждый с
# новыми находками, а круг показывал laps=1: у #640 исходы не названы, и
# неназванный исход обрывал цепь. Порог круга только писал алерт — следующая
# пересдача снова покупала deep.

_CIRCLE_LAYERS = [
    (
        [
            _confirmed("гонка на записи", "concurrency", 10),
            _confirmed("код возврата", "error-handling", 12),
            _confirmed("утечка дескриптора", "resource-leak", 14),
        ],
        [],
    ),
    (
        [
            _confirmed("мутация выжила", "test-adequacy", 20),
            _confirmed("граница окна", "boundary", 22),
        ],
        [],
    ),
    ([_confirmed("дедуп по тексту", "idempotency", 30)], []),
]
_CIRCLE_STOP_REASON = "deep приостановлен до решения человека"


async def _circle_resubmission(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    slug: str,
    unreadable: bool = False,
    **submitted: object,
) -> tuple[int, _DispatchRecorder]:
    """Сдача 4 после двух заходов круга (1→2, 2→3), дифф — процессная
    поверхность: правило без круга дало бы deep. Возвращает задачу и
    подставку провайдера, через которую прошли все заказы."""
    recorder = _DispatchRecorder({"agent": {"id": f"bc-{slug}"}, "run": {"id": slug}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, slug, diff=_DIFF_506, **submitted)  # type: ignore[arg-type]
    await _walk_the_circle(db, task_id, _CIRCLE_LAYERS)
    sha = "d" * 40
    await db.execute(
        "UPDATE tasks SET submission_generation = 4, submission_sha = ? WHERE id = ?",
        (sha, task_id),
    )
    await repo.record_submission(
        db, task_id=task_id, generation=4, sha=sha, base_branch="develop"
    )
    await db.commit()
    if unreadable:
        plugins.git_ops = _UnreadableGitOps(_TIP, ["docs/notes.md"])
    await maybe_dispatch_review(db, task_id)
    return task_id, recorder


class _UnreadableGitOps(_PinnedGitOps):
    async def branch_diff(self, repo, base, branch, context=0):
        raise RuntimeError("git недоступен")


async def _generation_reasons(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> list[str]:
    rows = await db.execute_fetchall(
        "SELECT payload FROM events WHERE kind='review_dispatched' AND task_id=?",
        (task_id,),
    )
    payloads = [json.loads(r[0]) for r in rows]
    mine = [p for p in payloads if p.get("generation") == generation]
    assert mine, payloads
    return list(mine[-1]["profile_reasons"])


async def _stop_alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [c for c in await _card(db, task_id) if "Круг ревью остановил deep" in c]


async def _stop_events(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    return [
        json.loads(dict(r)["payload"])
        for r in await repo.list_events(
            db, since=0, kinds=["review_circle_deep_stopped"], limit=50
        )
        if dict(r)["task_id"] == task_id
    ]


async def test_a_circle_at_the_stop_threshold_buys_lite_not_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1432): два захода, порог остановки 2 — пересдача, которой
    правило дало бы deep, уходит провайдеру lite; причина и отменённый повод
    названы; в ленте один алерт с тремя исходами и способом снять."""
    monkeypatch.setattr(config, "REVIEW_CIRCLE_DEEP_STOP", "2")
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id, recorder = await _circle_resubmission(
        client, db, monkeypatch, "circle-stop"
    )

    from hub.services.review_dispatch import announce_circle_deep_stop, review_circle

    assert (await review_circle(db, task_id)).count == 2, "предпосылка: два захода"
    rows = await _rows_of(db, task_id)
    assert rows[0]["profile"] == "deep", "предпосылка: правило даёт этому диффу deep"
    assert [(r["submission_generation"], r["profile"]) for r in rows[1:]] == [
        (4, "lite")
    ], rows
    assert len(recorder.calls) == 2, "провайдеру ушёл ровно один заказ сдачи 4"

    reasons = await _generation_reasons(db, task_id, 4)
    assert f"круг: 2 захода, {_CIRCLE_STOP_REASON}" in reasons, reasons
    cancelled = [r for r in reasons if r.startswith("отменён повод deep:")]
    assert cancelled and all("процессная поверхность" in r for r in cancelled), reasons

    [said] = await _stop_alerts(db, task_id)
    for outcome in ("принять как есть", "отпустить", "продолжать"):
        assert outcome in said, said
    assert "machine_review_override=require" in said, "назван способ снять"
    assert "Ревью не выключено" in said
    assert [e["generation"] for e in await _stop_events(db, task_id)] == [4]

    # Дедуп — по событию, а не по тексту карточки: запись стёрта, событие
    # осталось — второй алерт на то же поколение не пишется.
    await db.execute(
        "DELETE FROM task_updates WHERE task_id=? AND content LIKE ?",
        (task_id, "%Круг ревью остановил deep%"),
    )
    await db.commit()
    await announce_circle_deep_stop(db, task_id, 4, 2, 2)
    assert await _stop_alerts(db, task_id) == []
    assert len(await _stop_events(db, task_id)) == 1

    # Находка 3c1089ccae171db0: непрочитанный дифф — тоже deep по правилу, и
    # остановка действует и на него.
    blind, _ = await _circle_resubmission(
        client, db, monkeypatch, "circle-stop-blind", unreadable=True
    )
    assert [r["profile"] for r in await _rows_of(db, blind)] == ["deep", "lite"]
    assert "отменён повод deep: дифф сдачи прочитать не удалось" in (
        await _generation_reasons(db, blind, 4)
    )
    assert len(await _stop_alerts(db, blind)) == 1


async def test_unnamed_outcomes_do_not_reset_the_circle(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1432): исходы поколения не названы (как у отчёта #640 задачи
    #1405) — цепь не рвётся, заход засчитан. Явный отказ по ЧАСТИ находок
    цепь тоже не рвёт; явный отказ по ВСЕМ — рвёт."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-unn"}, "run": {"id": "r-unn"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)
    from hub.services.review_dispatch import review_circle

    task_id = await _submitted(client, db, "circle-unnamed")
    layer1, layer2, layer3 = (c for c, _ in _CIRCLE_LAYERS)
    layer4 = [_confirmed("новый слой", "concurrency", 40)]
    first = await _generation_with_findings(db, task_id, 1, confirmed=layer1)
    await _author_closed_them(db, task_id, first, 1, confirmed=layer1, unresolved=[])
    second = await _generation_with_findings(db, task_id, 2, confirmed=layer2)
    # Поколение 2: исходы не названы вовсе (режим warn на сдаче).
    third = await _generation_with_findings(db, task_id, 3, confirmed=layer3)

    circle = await review_circle(db, task_id)
    assert circle.count == 2, (
        "неназванный исход — «неизвестно», а не «не закрыто»: цепь 1→2→3 цела",
        circle.breakdown(),
    )
    assert circle.laps[-1].closed == 0, "закрытым правкой не названо ничего"
    # Находка c3ee4a6426933f4d: алерт не утверждает правок, которых не было.
    from hub.services.review_dispatch import announce_circle_deep_stop

    assert await announce_circle_deep_stop(db, task_id, 3, 2, 2)
    [said] = await _stop_alerts(db, task_id)
    assert "заход 1 (сдача 2): закрыто 3, пришло новых 2" in said, said
    assert "заход 2 (сдача 3): закрытий правкой не названо, пришло новых 1" in said
    assert "закрывались" not in said

    # Часть находок поколения 2 объявлена ложной, часть — нет: не отказ по всем.
    await _author_closed_them(
        db,
        task_id,
        second,
        2,
        confirmed=layer2[:1],
        unresolved=[],
        outcome_confirmed="false_positive",
    )
    assert (await review_circle(db, task_id)).count == 2, "отказ по части цепь не рвёт"
    # Находка a5c6984b7a76cdfc: остальное отложено (deferred) — дефект признан
    # настоящим, это не отказ; вместе с false_positive цепь по-прежнему цела.
    await _author_closed_them(
        db,
        task_id,
        second,
        2,
        confirmed=layer2[1:],
        unresolved=[],
        outcome_confirmed="deferred",
    )
    assert (await review_circle(db, task_id)).count == 2, "deferred цепь не рвёт"

    # Все находки поколения 3 — явный отказ: цепь обрывается на 3→4.
    await _author_closed_them(
        db,
        task_id,
        third,
        3,
        confirmed=layer3,
        unresolved=[],
        outcome_confirmed="wont_fix",
    )
    await _generation_with_findings(db, task_id, 4, confirmed=layer4)
    assert (await review_circle(db, task_id)).count == 0, (
        "все исходы поколения — явный отказ: это не заход"
    )


async def test_the_ladder_does_not_buy_deep_past_a_stopped_circle(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1432): остановка стоит — добор лестницы (#879) после неполного
    lite и переспрос другой моделью (#1243) deep не заказывают, причина
    названа, алерт об остановке по-прежнему один."""
    monkeypatch.setattr(config, "REVIEW_CIRCLE_DEEP_STOP", "2")
    task_id, recorder = await _circle_resubmission(
        client, db, monkeypatch, "circle-ladder"
    )
    assert [r["profile"] for r in await _rows_of(db, task_id)] == ["deep", "lite"]
    calls = len(recorder.calls)

    await _machine_report(client, task_id, incomplete=True)

    assert len(recorder.calls) == calls, "добор deep при остановке не куплен"
    assert [r["profile"] for r in await _rows_of(db, task_id)] == ["deep", "lite"]
    card = await _card(db, task_id)
    assert any("добор не куплен" in c and _CIRCLE_STOP_REASON in c for c in card), card

    assert not await maybe_dispatch_review(
        db, task_id, force_profile=DEEP, force_model="gpt-5.3-codex"
    )
    assert len(recorder.calls) == calls, "вторая ось остановку не обходит"
    assert any(
        "переспрос другой моделью не куплен" in c and _CIRCLE_STOP_REASON in c
        for c in await _card(db, task_id)
    )
    assert len(await _stop_alerts(db, task_id)) == 1
    assert len(await _stop_events(db, task_id)) == 1


async def test_human_request_security_and_zero_threshold_keep_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-4 (#1432): ручной запрос человека, заявленный security, порог 0
    (конфиг или ключ проекта) и нечитаемый порог — deep как до задачи."""
    monkeypatch.setattr(config, "REVIEW_CIRCLE_DEEP_STOP", "2")
    security = [{"kind": "security", "severity": "low", "description": "d"}]
    cases = {
        "human": await _circle_resubmission(
            client, db, monkeypatch, "circle-human", override="require"
        ),
        "security": await _circle_resubmission(
            client, db, monkeypatch, "circle-security", risks=security
        ),
        "project-zero": await _circle_resubmission(
            client,
            db,
            monkeypatch,
            "circle-project-zero",
            policy={"verdict": "auto", "circle_deep_stop": 0},
        ),
        "project-unreadable": await _circle_resubmission(
            client,
            db,
            monkeypatch,
            "circle-project-bad",
            policy={"verdict": "auto", "circle_deep_stop": "2"},
        ),
    }
    monkeypatch.setattr(config, "REVIEW_CIRCLE_DEEP_STOP", "0")
    cases["zero"] = await _circle_resubmission(client, db, monkeypatch, "circle-zero")
    monkeypatch.setattr(config, "REVIEW_CIRCLE_DEEP_STOP", "два")
    cases["unreadable"] = await _circle_resubmission(
        client, db, monkeypatch, "circle-unreadable"
    )

    for name, (task_id, _) in cases.items():
        rows = await _rows_of(db, task_id)
        assert [(r["submission_generation"], r["profile"]) for r in rows] == [
            (1, "deep"),
            (4, "deep"),
        ], (name, rows)
        reasons = await _generation_reasons(db, task_id, 4)
        assert not any(_CIRCLE_STOP_REASON in r for r in reasons), (name, reasons)
        assert await _stop_alerts(db, task_id) == [], name

    # Добор и вторая ось: ручной запрос и security остановкой не режутся.
    from hub.services.review_dispatch import _forced_deep_past_circle

    monkeypatch.setattr(config, "REVIEW_CIRCLE_DEEP_STOP", "2")
    for name in ("human", "security"):
        task = dict(await repo.get_task(db, cases[name][0]))
        assert await _forced_deep_past_circle(db, task, DEEP, "") is False, name
        assert not [c for c in await _card(db, task["id"]) if "не заказан" in c]

    from hub.models import validated_gate_policy

    assert validated_gate_policy({"circle_deep_stop": 3}) == {"circle_deep_stop": 3}
    for bad in (-1, True, "2"):
        with pytest.raises(ValueError, match="circle_deep_stop"):
            validated_gate_policy({"circle_deep_stop": bad})
