"""Hub-dispatched cross-model reviews (#757): orders_and_lots (split of the former single file, #1670)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.services.review_dispatch import (
    DEEP,
    LITE,
    pick_review_model,
    pick_review_profile,
    sweep_review_dispatches,
)
from tests.review_dispatch_support import (
    _DIFF_506,
    _HARMLESS_DIFF,
    _LIMIT_REFUSAL,
    _LOCAL_FIRST,
    _QWEN,
    _a_report_from,
    _age_dispatches,
    _alerts_of,
    _all_dispatches,
    _any_dispatch_row,
    _cap_project,
    _card,
    _card_text,
    _dispatch_notes,
    _DispatchRecorder,
    _docs_diff,
    _Listing,
    _local_dispatches,
    _local_principal,
    _no_local_path,
    _pinned_setup,
    _qwen_reviewer,
    _reporting_stub,
    _rows_of,
    _scratch,
    _Sequence,
    _small_delta_profile,
    _submitted,
    _submitted_in,
    _sudo_sandbox,
    _wire,
)


# --- #1408: 404 на создание не доказывает, что агента нет ---------------------
#
# 25.09.2026, #1399 сдача 2: create ответил HTTP 404 agent_not_found, заказ 429
# записан failed, а агент bc-1ddea9c5 создан (08:18:06, 3,47 млн токенов по
# счёту Cursor) и через десять минут сдал отчёт 582. Переспрос #1242 уже звал
# второго ревьюера и отменился лишь потому, что отчёт лёг на 7 секунд раньше.
_CREATED_BUT_404 = cursor_cloud.Refusal(
    status=404, code="agent_not_found", detail='{"error":{"code":"agent_not_found"}}'
)


def _no_real_pause(monkeypatch) -> list[float]:
    """Паузы сверки не спят, а записываются: потолок проверяется суммой."""
    from hub.services import review_dispatch as rd

    pauses: list[float] = []

    async def _pause(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(rd, "_verification_pause", _pause)
    return pauses


def _still_running(monkeypatch) -> None:
    async def _run(agent_id, run_id):
        return {"status": "RUNNING"}

    monkeypatch.setattr(cursor_cloud, "get_run", _run)


async def _claims_of(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM review_order_claims WHERE task_id=?", (task_id,)
    )
    return int(dict(rows[0])["n"])


async def test_create_404_with_live_agent_adopts_the_order(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1408): 404 на создание, а агент с меткой заказа у провайдера есть.

    Заказ усыновляет найденного агента (active, agent_id и run_id записаны),
    алерта «ревью НЕ вызвано» нет, второго POST нет, и переспрос на следующих
    проходах свипа не планируется — упавшей строки у сдачи нет.
    """
    provider = _DispatchRecorder(None, refusal=_CREATED_BUT_404)
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    _no_real_pause(monkeypatch)
    _still_running(monkeypatch)

    async def _listing(limit=50, cursor=""):
        return {
            "items": [
                {
                    "id": "bc-1ddea9c5",
                    "name": provider.calls[0]["name"],
                    "latestRunId": "run-582",
                }
            ],
            "nextCursor": "",
        }

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    task_id = await _submitted(
        client, db, "create-404-adopt", policy={"review": "dispatch"}
    )

    assert len(provider.calls) == 1, "второй POST — второй оплаченный агент"
    rows = await _rows_of(db, task_id)
    assert [(r["agent_id"], r["run_id"], r["status"]) for r in rows] == [
        ("bc-1ddea9c5", "run-582", "active")
    ], rows
    alerts = await _alerts_of(db, task_id)
    assert not [a for a in alerts if "НЕ вызвано" in a], alerts
    notes = await _card(db, task_id)
    assert any("HTTP 404" in n and "подобран по метке" in n for n in notes), notes

    for _ in range(3):
        await _age_dispatches(db)
        await sweep_review_dispatches(db)
    assert len(provider.calls) == 1, "переспрос по усыновлённому заказу не нужен"
    assert not [a for a in await _alerts_of(db, task_id) if "Переспрос ревью" in a]


async def test_create_404_without_agent_fails_after_verification(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1408): 404, и агента с меткой нет до самого потолка сверки.

    До потолка второй агент не создаётся и бронь #1399 держится; после —
    заказ failed с причиной, бронь снята, переспрос #1242 работает как раньше.
    """
    from hub.services.review_dispatch import (
        UNKNOWN_CREATE_CEILING_SECONDS,
    )

    provider = _Sequence(
        [
            (None, _CREATED_BUT_404),
            ({"agent": {"id": "bc-retry"}, "run": {"id": "run-retry"}}, None),
        ]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    pauses = _no_real_pause(monkeypatch)
    _still_running(monkeypatch)
    task_holder: dict[str, int] = {}
    seen_during: list[tuple[int, int]] = []

    async def _listing(limit=50, cursor=""):
        # Агента нашей метки нет, но метка нашей формы в ответе есть:
        # пустота подтверждена этим же ответом (find_agent_by_name).
        task_id = task_holder.get("id")
        claims = await _claims_of(db, task_id) if task_id else -1
        seen_during.append((len(provider.calls), claims))
        return {
            "items": [{"id": "bc-other", "name": "haiplane:review:t1:g1:a1"}],
            "nextCursor": "",
        }

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    real_claim = repo.claim_review_order

    async def _claim(db_, task_id, generation, profile):
        task_holder["id"] = task_id
        return await real_claim(db_, task_id, generation, profile)

    monkeypatch.setattr(repo, "claim_review_order", _claim)
    task_id = await _submitted(
        client, db, "create-404-absent", policy={"review": "dispatch"}
    )

    assert len(seen_during) > 1, "сверка повторяется, а не спрашивает один раз"
    assert all(calls == 1 for calls, _ in seen_during), (
        "до потолка сверки второй агент не создаётся"
    )
    assert all(claims == 1 for _, claims in seen_during), (
        "бронь #1399 держится, пока исход не установлен"
    )
    assert sum(pauses) == UNKNOWN_CREATE_CEILING_SECONDS, pauses
    rows = await _rows_of(db, task_id)
    assert [(r["agent_id"], r["status"]) for r in rows] == [("", "failed")], rows
    assert await _claims_of(db, task_id) == 0, "после потолка бронь снята"
    refused = [a for a in await _alerts_of(db, task_id) if "НЕ вызвано" in a]
    assert len(refused) == 1, refused
    assert "HTTP 404" in refused[0] and "сверк" in refused[0], refused[0]

    await _age_dispatches(db)
    await sweep_review_dispatches(db)
    assert len(provider.calls) == 2, "переспрос работает как раньше"
    rows = await _rows_of(db, task_id)
    assert rows[-1]["agent_id"] == "bc-retry" and rows[-1]["status"] == "active"


async def _failed_order_then_report(
    client, db, monkeypatch, slug: str, listing_items: list[dict]
) -> tuple[int, int, str]:
    """Сдача, чей заказ записан failed после 404, и отчёт ревьюера по ней."""
    provider = _Sequence([(None, _CREATED_BUT_404)])
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    _no_real_pause(monkeypatch)
    reviewer_pid, _, _ = await _pinned_setup(db, monkeypatch)
    phase = {"late": False}

    async def _listing(limit=50, cursor=""):
        items = [{"id": "bc-other", "name": "haiplane:review:t1:g1:a1"}]
        if phase["late"]:
            items = items + listing_items
        marker = provider.calls[0]["name"]
        return {
            "items": [
                {**i, "name": marker} if i.get("name") == "<marker>" else i
                for i in items
            ],
            "nextCursor": "",
        }

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    task_id = await _submitted(client, db, slug, policy={"review": "dispatch"})
    rows = await _rows_of(db, task_id)
    assert [(r["agent_id"], r["status"]) for r in rows] == [("", "failed")], rows
    profile = rows[0]["profile"]
    assert profile, "заглушка отказа помнит профиль, с которым шёл заказ"
    phase["late"] = True
    review_id = await _a_report_from(db, task_id, reviewer_pid, "cursor-cloud-reviewer")
    return task_id, review_id, profile


async def _review_row(db: aiosqlite.Connection, review_id: int) -> dict:
    rows = await db.execute_fetchall(
        "SELECT * FROM machine_reviews WHERE id=?", (review_id,)
    )
    return dict(rows[0])


async def test_report_after_failed_order_is_linked_and_billed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1408): отчёт по сдаче, чей заказ записан failed, — ЕГО отчёт.

    Прод 25.09: отчёт 582 лёг с пустым profile и provider_tokens NULL, а
    оплаченный прогон 429 учитывался как отказ. Отчёт привязывается к заказу:
    профиль заказа на отчёте, агент найден по метке, счёт снят с провайдера
    и на заказе, и на отчёте; повторного заказа нет.
    """
    task_id, review_id, profile = await _failed_order_then_report(
        client,
        db,
        monkeypatch,
        "late-report-billed",
        [{"id": "bc-1ddea9c5", "name": "<marker>", "latestRunId": "run-582"}],
    )
    assert (await _review_row(db, review_id))["profile"] == profile

    async def _usage(agent_id, run_id=None):
        # #1413: счёт снимается с агента целиком, а не с прогона run-582.
        assert (agent_id, run_id) == ("bc-1ddea9c5", None), (agent_id, run_id)
        return {"totalUsage": {"totalTokens": 3_467_426}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    await sweep_review_dispatches(db)

    rows = await _rows_of(db, task_id)
    assert len(rows) == 1, "отчёт привязан к заказу, а не породил второй"
    assert (rows[0]["agent_id"], rows[0]["run_id"], rows[0]["status"]) == (
        "bc-1ddea9c5",
        "run-582",
        "done",
    ), rows[0]
    assert rows[0]["provider_tokens"] == 3_467_426
    assert (await _review_row(db, review_id))["provider_tokens"] == 3_467_426
    await _age_dispatches(db)
    await sweep_review_dispatches(db)
    assert not [a for a in await _alerts_of(db, task_id) if "Переспрос ревью" in a]


async def test_report_after_failed_order_names_an_unavailable_bill(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1408), вторая ветвь: агента по метке не нашлось — счёт недоступен.

    Отчёт всё равно привязан (профиль заказа, заказ закрыт done), а
    provider_tokens остаётся NULL не молча: карточка называет заказ и причину.
    """
    task_id, review_id, profile = await _failed_order_then_report(
        client, db, monkeypatch, "late-report-unbilled", []
    )
    await sweep_review_dispatches(db)

    rows = await _rows_of(db, task_id)
    assert [(r["agent_id"], r["status"]) for r in rows] == [("", "done")], rows
    assert rows[0]["provider_tokens"] is None
    review = await _review_row(db, review_id)
    assert review["profile"] == profile and review["provider_tokens"] is None
    unbilled = [a for a in await _alerts_of(db, task_id) if "Счёт прогона" in a]
    assert len(unbilled) == 1, unbilled
    assert f"#{rows[0]['id']}" in unbilled[0] and "недоступен" in unbilled[0]
    await sweep_review_dispatches(db)
    assert len([a for a in await _alerts_of(db, task_id) if "Счёт прогона" in a]) == 1


async def test_create_404_blind_verification_goes_to_a_human(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1408, ограничение: сверка не смогла спросить до потолка — не отказ.

    Исход создания неизвестен: failed-строки нет (переспрос купил бы второго
    поверх, возможно, живого агента), второго POST нет, бронь #1399 остаётся
    на свой срок, а карточка называет это человеку.
    """
    provider = _DispatchRecorder(None, refusal=_CREATED_BUT_404)
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    _no_real_pause(monkeypatch)

    async def _cannot_ask(name, pages=3):
        return cursor_cloud.Reconciliation("", "", False)

    monkeypatch.setattr(cursor_cloud, "find_agent_by_name", _cannot_ask)
    task_id = await _submitted(
        client, db, "create-404-blind", policy={"review": "dispatch"}
    )

    assert len(provider.calls) == 1
    assert await _rows_of(db, task_id) == []
    assert await _claims_of(db, task_id) == 1, "бронь держится до своего срока"
    refused = [a for a in await _alerts_of(db, task_id) if "НЕ вызвано" in a]
    assert len(refused) == 1 and "исход создания неизвестен" in refused[0], refused
    assert "человек" in refused[0], refused[0]


async def test_reopened_order_without_its_report_returns_to_failed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1408: открытый заказ-отказ, отчёт к которому не сопоставился.

    Без агента опрашивать прогон нечем, а без отчёта нового о заказе ничего
    не известно: строка возвращается в failed, и переспрос #1242 видит её
    снова, а не висит «активной» без агента навсегда.
    """
    provider = _Sequence([(None, _CREATED_BUT_404)])
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    _no_real_pause(monkeypatch)
    reviewer_pid, _, _ = await _pinned_setup(db, monkeypatch)
    listing = _Listing([[{"id": "bc-other", "name": "haiplane:review:t1:g1:a1"}]])
    monkeypatch.setattr(cursor_cloud, "list_agents", listing)
    task_id = await _submitted(
        client, db, "reopened-no-report", policy={"review": "dispatch"}
    )
    assert await repo.reopen_refused_order_for_report(db, task_id, 1, reviewer_pid)
    await db.commit()
    checks = listing.calls

    await sweep_review_dispatches(db)

    rows = await _rows_of(db, task_id)
    assert [(r["agent_id"], r["status"]) for r in rows] == [("", "failed")], rows
    assert listing.calls == checks, "без отчёта по метке не сверяют"


async def _hang(*args, **kwargs):
    await asyncio.sleep(3600)


async def test_create_404_verification_ceiling_is_wall_clock(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка 90c389b87f1a980c: висящий список не растягивает потолок."""
    from hub.services import review_dispatch as rd

    _wire(monkeypatch, _DispatchRecorder(None, refusal=_CREATED_BUT_404))
    _no_local_path(monkeypatch)
    monkeypatch.setattr(rd, "UNKNOWN_CREATE_CEILING_SECONDS", 0.3)
    monkeypatch.setattr(cursor_cloud, "list_agents", _hang)
    started = asyncio.get_running_loop().time()
    task_id = await asyncio.wait_for(
        _submitted(client, db, "create-404-hang", policy={"review": "dispatch"}), 5
    )
    assert asyncio.get_running_loop().time() - started < 2.0
    assert await _rows_of(db, task_id) == [] and await _claims_of(db, task_id) == 1
    alerts = await _alerts_of(db, task_id)
    assert any("исход создания неизвестен" in a for a in alerts), alerts


async def test_create_404_cancelled_verification_leaves_a_trace(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка fb268a615c11e601: отмена сдачи посреди сверки видна в карточке."""
    _wire(monkeypatch, _DispatchRecorder(None, refusal=_CREATED_BUT_404))
    _no_local_path(monkeypatch)
    asked = asyncio.Event()

    async def _listing(limit=50, cursor=""):
        asked.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    submit = asyncio.create_task(
        _submitted(client, db, "create-404-cancel", policy={"review": "dispatch"})
    )
    await asyncio.wait_for(asked.wait(), 5)
    submit.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submit
    task_id = int((await db.execute_fetchall("SELECT MAX(id) FROM tasks"))[0][0])
    alerts = await _alerts_of(db, task_id)
    assert any("исход создания неизвестен" in a for a in alerts), alerts


async def test_report_after_failed_order_waits_out_a_listing_failure(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка 1e7563683681fb25: сбой списка не закрывает счёт с первого раза."""
    task_id, review_id, _ = await _failed_order_then_report(
        client, db, monkeypatch, "late-report-list-down", []
    )

    async def _down(limit=50, cursor=""):
        return None

    monkeypatch.setattr(cursor_cloud, "list_agents", _down)
    await sweep_review_dispatches(db)
    assert [r["status"] for r in await _rows_of(db, task_id)] == ["active"]
    assert not [a for a in await _alerts_of(db, task_id) if "Счёт прогона" in a]

    await db.execute(
        "UPDATE machine_reviews SET created_at=datetime('now', '-2 hours') WHERE id=?",
        (review_id,),
    )
    await sweep_review_dispatches(db)
    assert [r["status"] for r in await _rows_of(db, task_id)] == ["done"]
    unbilled = [a for a in await _alerts_of(db, task_id) if "Счёт прогона" in a]
    assert len(unbilled) == 1 and "не вышло" in unbilled[0], unbilled


async def test_create_404_last_check_keeps_its_budget_on_live_pauses(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка 2130251c27040fb8: паузы не отнимают у последней проверки время.

    Живые паузы в масштабе: шесть пауз по 0,0625 с ровно съедают потолок
    0,375 с — как 6 × 5 с = 30 с на проде. Двоичные дроби намеренно: в float
    0,3 // 0,05 = 5, и проверок стало бы шесть, а не семь, как на проде.
    Список пуст и отвечает быстро — это подтверждённая пустота, а не слепота.
    """
    from hub.services import review_dispatch as rd

    _wire(monkeypatch, _DispatchRecorder(None, refusal=_CREATED_BUT_404))
    _no_local_path(monkeypatch)
    monkeypatch.setattr(rd, "UNKNOWN_CREATE_CEILING_SECONDS", 0.375)
    monkeypatch.setattr(rd, "UNKNOWN_CREATE_PAUSE_SECONDS", 0.0625)
    calls: list[int] = []

    async def _listing(limit=50, cursor=""):
        # Быстрый, но настоящий сетевой ответ: он уступает цикл, и таймаут 0
        # успевает его отменить — так ведёт себя httpx, а не синхронная заглушка.
        calls.append(1)
        await asyncio.sleep(0.001)
        return {"items": [{"id": "bc-other", "name": "haiplane:review:t1:g1:a1"}]}

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    started = asyncio.get_running_loop().time()
    task_id = await _submitted(
        client, db, "create-404-live-pauses", policy={"review": "dispatch"}
    )
    assert asyncio.get_running_loop().time() - started < 2.0
    assert len(calls) > 1
    rows = await _rows_of(db, task_id)
    assert [(r["agent_id"], r["status"]) for r in rows] == [("", "failed")], rows
    assert await _claims_of(db, task_id) == 0
    refused = [a for a in await _alerts_of(db, task_id) if "НЕ вызвано" in a]
    assert len(refused) == 1 and "сверка по метке заказа агента не нашла" in refused[0]


# --- #1403: часть lite-сдач получает deep по жребию ---------------------------


def _lot_task(task_id: int, **over) -> dict:
    """Задача, которой правило риска даёт lite: класс R1, рисков нет."""
    return {"id": task_id, "risk_class": "R1", "risks": "[]", **over}


async def _lot_profile(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    *,
    cloud: bool = False,
    diff: str | None = None,
    **over,
) -> tuple[str, list[str], str]:
    from hub.services import review_dispatch as rd

    return await rd._chosen_profile(
        db,
        _lot_task(task_id, **over),
        generation,
        _HARMLESS_DIFF if diff is None else diff,
        rd.DeltaSubject(paths=[], base_paths=[], author_diff="", note=""),
        cloud,
    )


async def test_a_share_of_lite_submissions_gets_deep_by_lot(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1403): доля 0.2, 1000 разных пар, которым правило дало lite, —
    deep по жребию получают 15-25%, у каждой причина с долей и ``random``."""
    monkeypatch.setattr(config, "REVIEW_RANDOM_DEEP_SHARE", "0.2", raising=False)

    drawn = []
    for i in range(1, 1001):
        drawn.append(await _lot_profile(db, i, 1 + i % 3))

    lot = [d for d in drawn if d[0] == "deep"]
    assert 150 <= len(lot) <= 250, len(lot)
    assert all(d[2] == "random" for d in lot)
    assert all(d[0] == "lite" and d[2] == "rule" for d in drawn if d not in lot)
    assert all(any("deep по жребию (доля 20%)" in r for r in d[1]) for d in lot)

    # Строка заказа: доля 1 — каждая сдача с lite правила получает deep, и
    # колонка пишется рядом с профилем.
    monkeypatch.setattr(config, "REVIEW_RANDOM_DEEP_SHARE", "1", raising=False)
    recorder = _DispatchRecorder({"agent": {"id": "bc-lot"}, "run": {"id": "r-lot"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "lot-row")
    row = await _any_dispatch_row(db, task_id)
    assert (row["profile"], row["profile_assignment"]) == ("deep", "random")
    assert "ЛЁГКОЕ ревью" not in recorder.calls[0]["prompt_text"]
    events = await db.execute_fetchall(
        "SELECT payload FROM events WHERE kind='review_dispatched' AND task_id=?",
        (task_id,),
    )
    reasons = json.loads(events[-1][0])["profile_reasons"]
    assert any("deep по жребию (доля 100%)" in r for r in reasons), reasons


async def test_the_lot_is_repeatable_for_the_same_submission(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1403): одна пара (задача, поколение) — один профиль каждый раз."""
    monkeypatch.setattr(config, "REVIEW_RANDOM_DEEP_SHARE", "0.5", raising=False)
    for i in range(1, 60):
        first = await _lot_profile(db, i, 2)
        again = await _lot_profile(db, i, 2)
        assert first == again, i
    # Жребий зависит от поколения: пересдача бросает заново, а не копирует.
    profiles = {(await _lot_profile(db, 7, g))[0] for g in range(1, 40)}
    assert profiles == {"lite", "deep"}


async def test_rule_deep_and_human_request_never_enter_the_lot(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1403): deep по правилу и ручной запрос — deep и ``rule`` при
    любой доле, в том числе при нулевом жребии для всех остальных."""
    for share in ("0", "0.2", "1"):
        monkeypatch.setattr(config, "REVIEW_RANDOM_DEEP_SHARE", share, raising=False)
        for i in range(1, 40):
            by_rule = await _lot_profile(db, i, 1, risk_class="R4")
            human = await _lot_profile(db, i, 1, machine_review_override="require")
            assert by_rule[0] == "deep" and by_rule[2] == "rule", (share, i)
            assert human[0] == "deep" and human[2] == "rule", (share, i)
            assert not any("жребий" in r for r in by_rule[1] + human[1])


async def test_zero_share_turns_the_lot_off(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-4 (#1403): доля 0 (и нечитаемая) — профили те же, что у
    pick_review_profile, ни одной строки ``random``."""
    for share in ("0", "", "много", "nan"):
        monkeypatch.setattr(config, "REVIEW_RANDOM_DEEP_SHARE", share, raising=False)
        for i in range(1, 120):
            profile, reasons, assignment = await _lot_profile(db, i, 1)
            assert (profile, reasons) == pick_review_profile(
                _lot_task(i), _HARMLESS_DIFF
            ), (share, i)
            assert assignment == "rule"


async def test_the_lot_stops_at_the_circle_and_names_it(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1403: остановка deep кругом (#1432) жребий режет — lite и причина."""
    from hub.services import review_dispatch as rd

    monkeypatch.setattr(config, "REVIEW_RANDOM_DEEP_SHARE", "1", raising=False)

    async def _stood(db, task):
        return (3, 2)

    monkeypatch.setattr(rd, "circle_deep_stop", _stood)
    profile, reasons, assignment = await _lot_profile(db, 5, 1)

    assert (profile, assignment) == ("lite", "rule")
    assert reasons[0] == (f"жребий выпал deep, но {rd.circle_stop_reason(3)} — lite"), (
        reasons
    )


async def test_the_lot_counts_against_the_daily_cap_and_names_it(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1403: deep по жребию занимает место под суточным потолком (#1414);
    когда места нет — lite, ``rule`` и причина «жребий выпал deep, но …»."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-lc"}, "run": {"id": "r-lc"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "")
    monkeypatch.setattr(config, "REVIEW_RANDOM_DEEP_SHARE", "1", raising=False)
    pid = await _cap_project(db, "lot-cap", {"deep_daily_cap": 1})

    first = await _submitted_in(db, pid, diff=_HARMLESS_DIFF)
    second = await _submitted_in(db, pid, diff=_HARMLESS_DIFF)

    one = await _any_dispatch_row(db, first)
    assert (one["profile"], one["profile_assignment"]) == ("deep", "random")
    two = await _any_dispatch_row(db, second)
    assert (two["profile"], two["profile_assignment"]) == ("lite", "rule")
    events = await db.execute_fetchall(
        "SELECT payload FROM events WHERE kind='review_dispatched' AND task_id=?",
        (second,),
    )
    reasons = json.loads(events[-1][0])["profile_reasons"]
    assert reasons[0] == ("жребий выпал deep, но суточный потолок 1 исчерпан — lite"), (
        reasons
    )


async def test_small_delta_and_docs_lite_do_not_enter_the_lot(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1403: lite малой дельты (#1416) и lite документации — не от правила
    риска, в жребии не участвуют даже при доле 1."""
    monkeypatch.setattr(config, "REVIEW_SMALL_DELTA_LINES", "80")
    monkeypatch.setattr(config, "REVIEW_RANDOM_DEEP_SHARE", "1", raising=False)

    profile, reasons = await _small_delta_profile(
        client, db, monkeypatch, "lot-small-delta", 30
    )
    assert profile == "lite", reasons
    assert any("маленькая пересдача" in r for r in reasons), reasons
    row = await _any_dispatch_row(
        db, int((await db.execute_fetchall("SELECT MAX(id) FROM tasks"))[0][0])
    )
    assert row["profile_assignment"] == "rule"
    assert not any("жребий" in r for r in reasons)

    docs = await _lot_profile(db, 9, 1, diff=_docs_diff("docs/a.md"))
    assert docs[0] == "lite" and docs[2] == "rule"
    assert not any("жребий" in r for r in docs[1])


def test_the_lot_is_off_by_default():
    """#1403, решение владельца 01.10: умолчание доли — 0, жребий выкатывается
    выключенным и включается env-переменной. Читается свежим процессом без
    переменной: conftest глушит долю для всех остальных тестов."""
    env = {k: v for k, v in os.environ.items() if k != "REVIEW_RANDOM_DEEP_SHARE"}
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hub import config; from hub.services import review_dispatch as rd; "
            "print(repr(config.REVIEW_RANDOM_DEEP_SHARE), rd.random_deep_share())",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert float(out.stdout.split()[-1]) == 0.0, out.stdout


# --- Контур надзора покупает deep (#1559) ------------------------------------

_LADDER_DIFF = (
    "+++ b/hub/services/auto_verdict.py\n+def autopilot_stance() -> None:\n+    pass\n"
)
_R2 = {"risk_class": "R2", "risks": "[]"}


def test_a_change_to_the_oversight_ladder_buys_deep():
    """AC-1 (#1559): R2, процессных поверхностей нет, дифф правит auto_verdict.py
    (живой случай #1440) — deep и причина называет путь контура."""
    profile, reasons = pick_review_profile(_R2, _LADDER_DIFF)

    assert profile == DEEP, reasons
    assert reasons == ["решатель одобрения — hub/services/auto_verdict.py"], reasons
    # Сгенерированный файл на пути решателя решателем не считается.
    generated = "+++ b/hub/auth.py.snap\n+x = 1\n" + _HARMLESS_DIFF
    assert pick_review_profile(_R2, generated)[0] == LITE


def test_ladder_deep_reads_the_diff_not_the_declared_areas():
    """AC-2 (#1559): путь контура в affected_areas, но дифф его не тронул —
    lite с прежней причиной; тронул — deep, что бы ни было заявлено."""
    declared = {**_R2, "affected_areas": json.dumps(["hub/services/auto_verdict.py"])}

    profile, reasons = pick_review_profile(declared, _HARMLESS_DIFF)
    assert (profile, reasons) == (
        LITE,
        ["класс риска R2, процессных поверхностей нет"],
    )
    undeclared = {**_R2, "affected_areas": json.dumps(["app/notes.py"])}
    assert pick_review_profile(undeclared, _LADDER_DIFF)[0] == DEEP
    # Документация вне docs/agent-context остаётся lite.
    assert pick_review_profile(_R2, _docs_diff("docs/notes.md"))[0] == LITE


async def test_ladder_deep_respects_small_delta_and_daily_cap(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1559): малая дельта (#1416) и стоп круга (#1432) понижают deep
    контура с названным поводом, жребий (#1403) его не видит, а суточный
    потолок (#1414) второй такой сдаче места не даёт."""
    from hub.services import review_dispatch as rd

    profile, reasons = pick_review_profile(_R2, _LADDER_DIFF, small_delta=(10, 80))
    assert profile == LITE
    assert rd.SMALL_DELTA_REASON_MARK in reasons[0], reasons
    assert reasons[1:] == [
        "отменён повод deep: решатель одобрения — hub/services/auto_verdict.py"
    ]
    over = pick_review_profile(_R2, _LADDER_DIFF, small_delta=(200, 80))
    assert over[0] == DEEP
    stopped = pick_review_profile(_R2, _LADDER_DIFF, circle_stop=3)
    assert stopped[0] == LITE and stopped[1][0] == rd.circle_stop_reason(3)
    # Жребий: deep контура не lite правила риска.
    assert not rd._lite_by_risk_rule(*pick_review_profile(_R2, _LADDER_DIFF))

    recorder = _DispatchRecorder({"agent": {"id": "bc-lad"}, "run": {"id": "r-lad"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "")
    pid = await _cap_project(db, "ladder-cap", {"deep_daily_cap": 1})
    first = await _submitted_in(db, pid, diff=_LADDER_DIFF)
    second = await _submitted_in(db, pid, diff=_LADDER_DIFF)

    assert (await _any_dispatch_row(db, first))["profile"] == "deep"
    assert (await _any_dispatch_row(db, second))["profile"] == "lite"
    notes = await _dispatch_notes(db, second)
    assert "решатель одобрения — hub/services/auto_verdict.py" in notes[0], notes
    assert "суточный потолок 1 исчерпан — lite" in notes[0], notes


def test_ladder_deep_sees_deleted_and_renamed_files():
    """#1559: удаление и переименование пути контура — тоже правка контура."""
    deleted = (
        "diff --git a/hub/auth.py b/hub/auth.py\n"
        "--- a/hub/auth.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
    )
    renamed = (
        "diff --git a/hub/old.py b/hub/services/steward_shadow.py\n"
        "similarity index 100%\nrename from hub/old.py\nrename to hub/services/steward_shadow.py\n"
    )
    assert pick_review_profile(_R2, deleted)[1] == ["решатель одобрения — hub/auth.py"]
    assert pick_review_profile(_R2, renamed)[1] == [
        "решатель одобрения — hub/services/steward_shadow.py"
    ]


def test_a_generated_file_on_a_ladder_path_does_not_buy_deep():
    """#1559, находка 8c9f7d15794f50a4: реальный формат git diff. Заголовки
    «diff --git»/«---» сгенерированного файла остаются после split_generated;
    путь контура в них deep не покупает, а обычный файл контура рядом — да."""
    generated = (
        "diff --git a/hub/auth.py.snap b/hub/auth.py.snap\n"
        "index 1111111..2222222 100644\n"
        "--- a/hub/auth.py.snap\n"
        "+++ b/hub/auth.py.snap\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    plain = (
        "diff --git a/app/notes.py b/app/notes.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/app/notes.py\n+++ b/app/notes.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
    )
    ladder = (
        "diff --git a/hub/auth.py b/hub/auth.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/hub/auth.py\n+++ b/hub/auth.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
    )
    assert pick_review_profile(_R2, generated + plain)[0] == LITE
    profile, reasons = pick_review_profile(_R2, generated + ladder)
    assert (profile, reasons) == (DEEP, ["решатель одобрения — hub/auth.py"])


def test_only_approval_deciders_buy_deep_not_the_rest_of_the_ladder():
    """#1559, решение владельца 03.10 (вариант Б): правка одного lifecycle.py,
    config.py, project_policy.py, docs/agent-context, docs/repository-rules.md
    или .github/ при R2 остаётся lite с прежней причиной."""
    for path in (
        "hub/services/lifecycle.py",
        "hub/config.py",
        "hub/services/project_policy.py",
        "docs/agent-context/system-map.md",
        "docs/repository-rules.md",
        ".github/workflows/ci.yml",
    ):
        diff = f"+++ b/{path}\n+x = 1\n"
        assert pick_review_profile(_R2, diff)[0] == LITE, path
    code = "+++ b/hub/services/lifecycle.py\n+x = 1\n"
    both = code + "+++ b/hub/services/steward_apply.py\n+x = 1\n"
    assert pick_review_profile(_R2, both) == (
        DEEP,
        ["решатель одобрения — hub/services/steward_apply.py"],
    )


def test_approval_deciders_are_a_subset_of_the_ladder():
    """#1559: каждая запись APPROVAL_DECIDERS — буквально элемент
    LADDER_SURFACES; убрали элемент из контура — падает с его именем."""
    from hub.services.auto_approve import (
        APPROVAL_DECIDERS,
        LADDER_SURFACES,
        decider_hits,
        ladder_hits,
        surface_hits,
    )

    for entry in APPROVAL_DECIDERS:
        assert entry in LADDER_SURFACES, f"{entry} не входит в LADDER_SURFACES"
    assert len(set(APPROVAL_DECIDERS)) == len(APPROVAL_DECIDERS)
    # Правило совпадения одно: точный путь или префикс, по любому списку.
    paths = ["hub/services/lifecycle.py", "hub/auth.py", "docs/agent-context/a.md"]
    assert ladder_hits(paths) == surface_hits(paths, LADDER_SURFACES) == sorted(paths)
    assert decider_hits(paths) == surface_hits(paths, APPROVAL_DECIDERS)
    assert decider_hits(paths) == ["hub/auth.py"]
    assert surface_hits(["a/b/c.py"], ("a/b/",)) == ["a/b/c.py"]


_SILENT_STUB = "import sys\nsys.stdin.read()\n"


async def test_deep_goes_local_first_when_the_project_asks(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-1 (#1561): deep_reviewer=local — deep идёт в локальный путь, облако не
    зовётся; на той же политике lite остаётся в облаке, как сейчас."""
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-lite"}, "run": {"id": "r-l"}})
    _wire(monkeypatch, recorder)
    reviewer_pid = await _local_principal(db, monkeypatch)
    _qwen_reviewer(monkeypatch, tmp_path)

    deep = await _submitted(client, db, "lf-deep", policy=_LOCAL_FIRST, diff=_DIFF_506)
    await wait_for_local_runs()
    await db.commit()

    assert recorder.calls == [], "облако на deep не зовётся и не оплачивается"
    rows = await _all_dispatches(db, deep)
    assert [(r["channel"], r["profile"], r["status"]) for r in rows] == [
        ("local", "deep", "done")
    ], rows
    assert rows[0]["reviewer_principal_id"] == reviewer_pid
    reports = await repo.machine_reviews_of_generation(db, deep, 1)
    assert len(reports) == 1 and dict(reports[0])["principal_id"] == reviewer_pid

    lite = await _submitted(
        client,
        db,
        "lf-lite",
        policy=_LOCAL_FIRST,
        diff=_docs_diff("docs/notes.md"),
    )
    await wait_for_local_runs()
    assert len(recorder.calls) == 1, "lite остаётся в облаке"
    lite_rows = await _all_dispatches(db, lite)
    assert [(r["channel"], r["profile"]) for r in lite_rows] == [("cloud", "lite")]


async def test_a_failed_local_deep_falls_back_to_the_cloud_and_says_why(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-2 (#1561): путь не готов или прогон кончился без отчёта — облако,
    причина названа в карточке, двух оплаченных deep одновременно нет."""
    from hub.services.review_dispatch import wait_for_local_runs

    # (a) локальный путь не настроен: отказ синхронный, облако — сразу.
    recorder = _DispatchRecorder({"agent": {"id": "bc-fb"}, "run": {"id": "r-fb"}})
    _wire(monkeypatch, recorder)
    _no_local_path(monkeypatch)
    sync = await _submitted(
        client, db, "lf-notready", policy=_LOCAL_FIRST, diff=_DIFF_506
    )
    await wait_for_local_runs()
    assert len(recorder.calls) == 1
    rows = await _all_dispatches(db, sync)
    assert [(r["channel"], r["profile"], r["status"]) for r in rows] == [
        ("cloud", "deep", "active")
    ], rows
    card = await _card_text(db, sync)
    assert "LOCAL_REVIEW_CMD" in card, "причина отказа локального названа именем"
    assert "deep_reviewer" in card, "и сказано, что облако заказано вместо локального"

    # (b) прогон ушёл и кончился без отчёта: облако ПОСЛЕ него, не вместе.
    recorder2 = _DispatchRecorder({"agent": {"id": "bc-fb2"}, "run": {"id": "r-f2"}})
    _wire(monkeypatch, recorder2)
    await _local_principal(db, monkeypatch)
    _qwen_reviewer(monkeypatch, tmp_path, _SILENT_STUB)
    async_ = await _submitted(
        client, db, "lf-noreport", policy=_LOCAL_FIRST, diff=_DIFF_506
    )
    assert recorder2.calls == [], "пока локальный идёт, облако не заказывается"
    await wait_for_local_runs()
    await db.commit()
    assert len(recorder2.calls) == 1, "прогон кончился без отчёта — облако"
    rows = await _all_dispatches(db, async_)
    assert [(r["channel"], r["profile"], r["status"]) for r in rows] == [
        ("local", "deep", "failed"),
        ("cloud", "deep", "active"),
    ], rows
    card = await _card_text(db, async_)
    assert "без отчёта" in card and "облако" in card.lower(), card
    paid = [r for r in rows if r["status"] == "active"]
    assert len(paid) == 1, "на сдачу не бывает двух живых оплаченных deep"


async def test_a_local_run_records_the_local_model_not_the_cloud_choice(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-3 (#1561): CLI настроен на qwen3.8-max, предпочтение облака — grok-4.6:
    строка заказа, событие, карточка, отчёт и правило семейств говорят qwen."""
    from hub.services import auto_verdict
    from hub.services.model_family import family
    from hub.services.review_dispatch import wait_for_local_runs

    assert pick_review_model("claude-fable-5") == "grok-4.6"
    _wire(monkeypatch, _DispatchRecorder(None))
    await _local_principal(db, monkeypatch)
    _qwen_reviewer(monkeypatch, tmp_path)

    task_id = await _submitted(
        client, db, "lf-model", policy=_LOCAL_FIRST, diff=_DIFF_506
    )
    await wait_for_local_runs()
    await db.commit()

    row = (await _all_dispatches(db, task_id))[0]
    assert row["model"] == _QWEN
    assert family(row["model"]) == "alibaba"
    events = await db.execute_fetchall(
        "SELECT payload FROM events WHERE kind='review_dispatched' AND task_id=?",
        (task_id,),
    )
    assert [json.loads(e[0])["model"] for e in events] == [_QWEN]
    card = await _card_text(db, task_id)
    assert _QWEN in card
    assert "grok-4.6" not in card, "модель облачного выбора нигде не записана"
    report = dict((await repo.machine_reviews_of_generation(db, task_id, 1))[0])
    assert report["model"] == _QWEN, (
        "отчёт, восстановленный из вывода, называет модель локального CLI, "
        "а не то, что прогон сказал о себе"
    )
    reviewer, _ = await auto_verdict._reviewer_model(db, task_id, 1, report)
    assert reviewer == _QWEN

    # Тот же честный учёт на второй двери: облако отказало, локальный ответил.
    recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
    _wire(monkeypatch, recorder)
    door = await _submitted(
        client,
        db,
        "lf-door",
        policy={"review": "dispatch", "local_review_fallback": "on"},
        diff=_DIFF_506,
    )
    await wait_for_local_runs()
    local_rows = await _local_dispatches(db, door)
    assert [r["model"] for r in local_rows] == [_QWEN]
    door_card = await _card_text(db, door)
    assert f"модель {_QWEN}" in door_card, door_card
    assert "grok-4.6" not in door_card, door_card


async def test_without_the_key_deep_stays_in_the_cloud(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-4 (#1561): ключа нет — облако первым, локальный только второй дверью;
    сводка показывает deep_reviewer=cloud [default]."""
    from hub.services import effective_policy
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-def"}, "run": {"id": "r-d"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _qwen_reviewer(monkeypatch, tmp_path)

    task_id = await _submitted(
        client, db, "lf-default", policy={"review": "dispatch"}, diff=_DIFF_506
    )
    await wait_for_local_runs()
    assert len(recorder.calls) == 1, "облако первым, как сейчас"
    rows = await _all_dispatches(db, task_id)
    assert [(r["channel"], r["model"]) for r in rows] == [("cloud", "grok-4.6")]

    project = await repo.resolve_project_for_task(db, task_id)
    summary = await effective_policy.effective_policy(db, project)
    row = next(k for k in summary["keys"] if k["key"] == "deep_reviewer")
    assert (row["value"], row["source"]) == ("cloud", "default")
    assert any(
        "deep_reviewer" in line and "cloud" in line and "[default]" in line
        for line in effective_policy.format_effective_policy(summary)
    )


async def _local_first_declined(
    client, db, monkeypatch, tmp_path, slug: str, *, principal: bool = True, **setup
) -> tuple[_DispatchRecorder, int]:
    """Сдача deep по политике; ``setup`` ломает одно условие локального пути."""
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-dec"}, "run": {"id": "r-dec"}})
    _wire(monkeypatch, recorder)
    if principal:
        await _local_principal(db, monkeypatch)
    _qwen_reviewer(monkeypatch, tmp_path)
    for name, value in setup.items():
        monkeypatch.setattr(config, name, value)
    task_id = await _submitted(client, db, slug, policy=_LOCAL_FIRST, diff=_DIFF_506)
    await wait_for_local_runs()
    await db.commit()
    return recorder, task_id


async def test_local_first_names_every_refusal_and_falls_to_the_cloud(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1561: модель не названа / семейство то же — облако, причина в карточке."""
    recorder, no_model = await _local_first_declined(
        client, db, monkeypatch, tmp_path, "lf-nomodel"
    )
    assert recorder.calls == []  # предпосылка: с моделью локальный идёт

    import shlex

    monkeypatch.setattr(
        config, "LOCAL_REVIEW_CMD", shlex.join(["-c", _reporting_stub()])
    )
    recorder = _DispatchRecorder({"agent": {"id": "bc-nm"}, "run": {"id": "r-nm"}})
    _wire(monkeypatch, recorder)
    unnamed = await _submitted(
        client, db, "lf-nomodel2", policy=_LOCAL_FIRST, diff=_DIFF_506
    )
    assert len(recorder.calls) == 1
    assert "модель локального CLI не названа" in await _card_text(db, unnamed)
    assert [r["channel"] for r in await _all_dispatches(db, unnamed)] == ["cloud"]

    monkeypatch.setattr(config, "LOCAL_REVIEW_MODEL", "claude-local-9")
    recorder = _DispatchRecorder({"agent": {"id": "bc-fam"}, "run": {"id": "r-fam"}})
    _wire(monkeypatch, recorder)
    same = await _submitted(
        client, db, "lf-family", policy=_LOCAL_FIRST, diff=_DIFF_506
    )
    assert len(recorder.calls) == 1
    assert "правило разных семейств" in await _card_text(db, same)
    assert [r["channel"] for r in await _all_dispatches(db, same)] == ["cloud"]


async def test_the_deep_ceiling_is_named_and_does_not_cut_a_real_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1561: 3,2 млн потраченного (средний deep grok) локальный deep не режут;
    сверх LOCAL_REVIEW_DEEP_TOKEN_CEILING — облако с названным потолком."""
    from hub.services import review_dispatch as rd

    async def _spent(_db, _task_id):
        return 3_200_000

    monkeypatch.setattr(rd, "_tokens_already_spent", _spent)
    assert config.LOCAL_REVIEW_TOKEN_CEILING < 3_200_000, "предпосылка #1152"
    recorder, ran = await _local_first_declined(
        client, db, monkeypatch, tmp_path, "lf-ceil-ok"
    )
    assert recorder.calls == []
    assert [r["channel"] for r in await _all_dispatches(db, ran)] == ["local"]

    async def _spent_much(_db, _task_id):
        return 11_000_000

    monkeypatch.setattr(rd, "_tokens_already_spent", _spent_much)
    recorder, cut = await _local_first_declined(
        client, db, monkeypatch, tmp_path, "lf-ceil-cut", principal=False
    )
    assert len(recorder.calls) == 1
    assert "LOCAL_REVIEW_DEEP_TOKEN_CEILING" in await _card_text(db, cut)


async def test_local_first_does_not_spend_the_daily_deep_cap_but_its_fallback_does(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1561, решение владельца 03.10 (вариант Б): локальный deep суточный
    потолок #1414 не расходует; облачный fallback после отказа Qwen — как
    обычный облачный deep: при исчерпанном потолке получает lite с причиной.
    Повторный триггер на живой заказ второго не даёт."""
    from hub.services.review_dispatch import maybe_dispatch_review, wait_for_local_runs

    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "")
    policy = {**_LOCAL_FIRST, "deep_daily_cap": 0}
    recorder = _DispatchRecorder({"agent": {"id": "bc-cap1"}, "run": {"id": "r-c1"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _qwen_reviewer(monkeypatch, tmp_path)
    ran = await _submitted(client, db, "lf-cap", policy=policy, diff=_DIFF_506)
    await wait_for_local_runs()
    rows = await _all_dispatches(db, ran)
    assert [(r["channel"], r["profile"]) for r in rows] == [("local", "deep")], rows
    assert recorder.calls == []

    twice = await _submitted(
        client, db, "lf-twice", policy=_LOCAL_FIRST, diff=_DIFF_506
    )
    assert await maybe_dispatch_review(db, twice) is False, "заказ уже стоит"
    await wait_for_local_runs()
    assert len(await _all_dispatches(db, twice)) == 1

    _qwen_reviewer(monkeypatch, tmp_path, _SILENT_STUB)
    failed = await _submitted(client, db, "lf-cap-fb", policy=policy, diff=_DIFF_506)
    await wait_for_local_runs()
    await db.commit()
    rows = await _all_dispatches(db, failed)
    assert [(r["channel"], r["profile"], r["status"]) for r in rows] == [
        ("local", "deep", "failed"),
        ("cloud", "lite", "active"),
    ], rows
    assert "суточный потолок 0 исчерпан" in await _card_text(db, failed)


async def test_a_local_first_deep_cut_by_a_hub_stop_gets_the_cloud_after_start(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1561, находка 6032bf684d44dd9f: остановка хаба посреди local-first deep
    не оставляет сдачу без ревью — после старта свип заказывает облако один раз."""
    import shlex
    import sys

    from hub.services.review_dispatch import (
        cancel_local_runs,
        sweep_review_dispatches,
    )

    recorder = _DispatchRecorder({"agent": {"id": "bc-stop"}, "run": {"id": "r-sp"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    slow = "import sys, time; sys.stdin.read(); time.sleep(30)"
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, sys.executable)
    )
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_CMD", shlex.join(["-c", slow, "--model", _QWEN])
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEW_MODEL", "")
    task_id = await _submitted(
        client, db, "lf-stop", policy=_LOCAL_FIRST, diff=_DIFF_506
    )
    await asyncio.sleep(0.5)
    await cancel_local_runs()
    await db.commit()
    assert recorder.calls == [], "на остановке облако не заказывается"
    assert [r["status"] for r in await _all_dispatches(db, task_id)] == ["failed"]

    await sweep_review_dispatches(db)
    await sweep_review_dispatches(db)
    await db.commit()
    assert len(recorder.calls) == 1, "после старта облако заказано ровно один раз"
    rows = await _all_dispatches(db, task_id)
    assert [(r["channel"], r["status"]) for r in rows] == [
        ("local", "failed"),
        ("cloud", "active"),
    ], rows
    assert (await _card_text(db, task_id)).count("прерван остановкой хаба") == 1
