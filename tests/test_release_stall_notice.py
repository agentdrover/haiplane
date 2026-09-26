"""A stalled auto-release reaches the activity feed (#962).

On 26.08.2026 GitHub refused the release merge of develop into main three
poll cycles in a row — the histories had diverged after a squash release —
and the only trace was a deduplicated warning in the server log. The policy
stood still until a human read the logs and resolved it by hand. These tests
pin the behaviour that makes the next stall visible: one activity-feed entry
per persistent refusal, none for a flicker, and a sweep that survives the
feed write failing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hub import poller
from hub import repository as repo

# #1420: a probe that could not look (the forge answered nothing usable) is
# the one class that still waits RELEASE_STALL_CYCLES before it counts — one
# failed probe is a network hiccup, three in a row is a release nobody can see.
# A definite red (ci_fail, a conflict, a refused merge) no longer waits: see the
# #1420 tests at the end of this file.
REFUSAL = "релизный PR #40 не смержен: ci_unavailable (gh: 502)"


@pytest.fixture(autouse=True)
def _fresh_sweep_state():
    poller._release_notices.clear()
    poller._release_stalls.clear()
    yield
    poller._release_notices.clear()
    poller._release_stalls.clear()


async def _project(db, slug: str = "default"):
    row = await repo.get_project_by_slug(db, slug)
    if row is None:
        await repo.create_project(
            db, slug=slug, name=slug, workspace_path=f"/tmp/{slug}"
        )
        row = await repo.get_project_by_slug(db, slug)
    await db.commit()
    return row


async def _stall_entries(db) -> list[dict]:
    cur = await db.execute(
        "SELECT kind, summary, detail FROM activity_log"
        " WHERE summary LIKE '%релиз стоит%' ORDER BY id"
    )
    return [dict(r) for r in await cur.fetchall()]


async def _sweep(db, merged: bool, reason: str, times: int = 1) -> None:
    """Drive the release sweep, each cycle answering (merged, reason)."""
    with patch(
        "hub.services.release.merge_ready_release",
        AsyncMock(return_value=(merged, reason)),
    ):
        for _ in range(times):
            await poller._sweep_release_policy(db)


async def test_persistent_refusal_logged_once(db):
    # AC-1: three cycles with the same refusal put exactly one entry in the
    # activity feed; further cycles with the same reason add nothing — a line
    # per cycle is how a real signal gets muted (#534).
    await _project(db)

    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES)
    entries = await _stall_entries(db)
    assert len(entries) == 1
    assert entries[0]["kind"] == "release_blocked"
    assert entries[0]["summary"] == f"default: авария — релиз стоит: {REFUSAL}"

    await _sweep(db, False, REFUSAL, times=5)
    assert len(await _stall_entries(db)) == 1


async def test_signal_resets_on_merge_or_reason_change(db):
    # AC-2: a successful merge — or a different reason — resets the signal,
    # so the NEXT persistent stall produces a new entry instead of hiding
    # behind the old one.
    await _project(db)

    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES)
    await _sweep(db, True, "релиз PR #41 смержен в main")
    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES)
    assert len(await _stall_entries(db)) == 2

    other = "релизный PR #42 не смержен: ci_fail (mypy)"
    await _sweep(db, False, other)
    entries = await _stall_entries(db)
    assert len(entries) == 3
    assert other in entries[-1]["summary"]


async def test_transient_refusal_not_logged(db):
    # AC-3: one or two refused cycles are a flicker (a network hiccup, a race
    # with CI), not a stall — the feed stays silent when the refusal clears
    # below the threshold, whether by a merge or by the reason vanishing.
    await _project(db)

    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES - 1)
    await _sweep(db, True, "релиз PR #41 смержен в main")
    assert await _stall_entries(db) == []

    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES - 1)
    await _sweep(db, False, "")
    await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES - 1)
    assert await _stall_entries(db) == []


async def test_activity_failure_does_not_break_sweep(db):
    # AC-4: the feed write failing is absorbed — every project is still
    # swept, and the entry lands on a later cycle instead of being lost.
    await _project(db, "default")
    await _project(db, "second")

    with patch(
        "hub.poller.log_activity", AsyncMock(side_effect=RuntimeError("db locked"))
    ):
        await _sweep(db, False, REFUSAL, times=poller.RELEASE_STALL_CYCLES)
    assert await _stall_entries(db) == []
    assert set(poller._release_stalls) == {"default", "second"}

    await _sweep(db, False, REFUSAL)
    entries = await _stall_entries(db)
    assert {e["summary"].split(":")[0] for e in entries} == {"default", "second"}


# --- AC-4 (#970): в ленту едет диагноз, а не «GitHub отказал» ---------------


async def test_stall_notice_names_the_conflict(db):
    """Настоящий merge_ready_release, а не пересказ мока.

    #962 построил дорогу от стойла до ленты и повёз по ней ту же непрозрачную
    фразу, поэтому человек всё равно шёл смотреть в GitHub. Тест держит весь
    путь целиком: конфликтный релизный PR → причина → запись в ленте, по
    которой понятен следующий шаг.
    """
    from unittest.mock import AsyncMock as _AsyncMock

    from hub import repository as hub_repo
    from hub.integrations.protocols import MergeabilityOutcome
    from tests.test_release_policy import _git as _git_plugin

    g = _git_plugin(existing_pr=83)
    g.check_pr_mergeable = _AsyncMock(
        return_value=(MergeabilityOutcome.conflicting, "конфликт в hub/db.py")
    )
    row = await _project(db)
    await hub_repo.update_project(
        db, dict(row)["id"], gate_policy='{"release": "auto"}'
    )
    await db.commit()

    for _ in range(poller.RELEASE_STALL_CYCLES):
        await poller._sweep_release_policy(db)

    entries = await _stall_entries(db)
    assert entries, "стойло обязано дойти до ленты"
    text = " ".join((e["summary"] or "") + (e["detail"] or "") for e in entries)
    assert "hub/db.py" in text, f"в ленте нет диагноза, только шум: {entries}"
    assert "отказал" not in text, (
        f"«GitHub отказал» — это не то, что человек может починить: {entries}"
    )
    g.merge_pr.assert_not_awaited()


# --- #1420: идущий CI — рутина, красный — авария, и авария доходит ----------
#
# 25.09 CI develop был красным 1 ч 43 мин, а запись о нём (#9084) стояла в ленте
# той же фразой «релиз стоит», что и пять обычных долгих прогонов того же дня.
# Эти тесты держат различие и доставку: рутина молчит, авария пишется с первого
# цикла отдельным kind и видна стюарду (hub_prod_state) и в дайджесте, снятие
# записывается с длительностью.

RUNNING = "релизный PR #484 не смержен: ci_pending (checks_running)"
RED = "релизный PR #484 не смержен: ci_fail (checks_failed)"


async def _feed(db, kind: str) -> list[dict]:
    cur = await db.execute(
        "SELECT kind, summary, detail FROM activity_log WHERE kind = ? ORDER BY id",
        (kind,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def test_running_ci_is_routine_not_a_stall(db):
    # AC-1: CI, который просто идёт, 3–5 циклов подряд — не стойло и не авария.
    from hub.services.prod_state import prod_state

    await _project(db)

    await _sweep(db, False, RUNNING, times=poller.RELEASE_STALL_CYCLES + 2)

    assert await _stall_entries(db) == [], "идущий CI записан как «релиз стоит»"
    assert await _feed(db, "release_blocked") == []
    assert (await prod_state(db))["release_blocks"] == []


async def test_running_ci_past_time_threshold_is_noted_once_not_as_alert(db):
    # Рутина, которая тянется дольше порога ВРЕМЕНИ, попадает в ленту одной
    # записью — но не аварией и не фразой «релиз стоит».
    from hub.services.prod_state import prod_state

    await _project(db)
    clock = [1000.0]
    with patch.object(poller, "_release_clock", lambda: clock[0]):
        await _sweep(db, False, RUNNING, times=2)
        clock[0] += poller.RELEASE_ROUTINE_STALL_MINUTES * 60 + 1
        await _sweep(db, False, RUNNING, times=3)

    notes = await _feed(db, "release")
    assert len(notes) == 1, notes
    assert "релиз стоит" not in notes[0]["summary"]
    assert "ждёт" in notes[0]["summary"]
    assert await _feed(db, "release_blocked") == []
    assert (await prod_state(db))["release_blocks"] == []


async def test_red_release_ci_is_an_alert_on_first_cycle_and_reaches_steward(db):
    # AC-2: красный CI релиза — авария с первого цикла, отдельным kind, с
    # причиной; строка «релиз заблокирован» у стюарда и в дайджесте; повторные
    # циклы с той же причиной новых записей не дают (#534).
    import json
    from datetime import UTC, datetime, timedelta

    from hub.services.digest import generate_due_digests
    from hub.services.prod_state import format_prod_state, prod_state

    row = await _project(db)
    await repo.update_project(db, dict(row)["id"], gate_policy='{"release": "auto"}')
    await db.commit()

    await _sweep(db, False, RED)

    alerts = await _feed(db, "release_blocked")
    assert len(alerts) == 1, "авария обязана лечь с первого цикла"
    assert "checks_failed" in alerts[0]["summary"]

    snapshot = await prod_state(db)
    assert len(snapshot["release_blocks"]) == 1
    block = snapshot["release_blocks"][0]
    assert block["project"] == "default"
    assert "checks_failed" in block["reason"]
    assert block["since"]
    text = format_prod_state(snapshot)
    assert "Релиз заблокирован с" in text
    assert "checks_failed" in text.split("Релиз заблокирован с", 1)[1]

    await _sweep(db, False, RED, times=poller.RELEASE_STALL_CYCLES + 2)
    assert len(await _feed(db, "release_blocked")) == 1, "авария — одна на событие"
    assert len((await prod_state(db))["release_blocks"]) == 1

    assert await generate_due_digests(db, now=datetime.now(UTC) + timedelta(days=1))
    digest = dict((await repo.list_digests(db))[0])
    blocks = json.loads(digest["payload"])["release_blocks"]
    assert [b["kind"] for b in blocks] == ["release_blocked"]
    assert "checks_failed" in blocks[0]["reason"]


async def test_release_alert_clears_with_duration(db):
    # AC-3: авария записана, релиз затем смержен — запись о снятии с
    # длительностью, строка у стюарда исчезает; повторный цикл тишина.
    import re

    from hub.services.prod_state import format_prod_state, prod_state

    await _project(db)
    await _sweep(db, False, RED)
    assert (await prod_state(db))["release_blocks"]

    await _sweep(db, True, "релиз PR #484 смержен в main")

    cleared = await _feed(db, "release_unblocked")
    assert len(cleared) == 1, cleared
    assert re.search(r"блокировка снята, длилась \d+ мин", cleared[0]["summary"])
    snapshot = await prod_state(db)
    assert snapshot["release_blocks"] == []
    assert "Релиз заблокирован" not in format_prod_state(snapshot)

    await _sweep(db, True, "релиз PR #485 смержен в main")
    assert len(await _feed(db, "release_unblocked")) == 1


async def test_unknown_reason_is_an_alert(db):
    # Constraint: причина, которой классификатор не знает, — авария, не рутина.
    await _project(db)
    await _sweep(db, False, "релизный PR #7 не смержен: что-то новое (x)")
    assert len(await _feed(db, "release_blocked")) == 1


async def test_digest_page_shows_the_release_block(client, db):
    # Блок дайджеста виден на странице /digests, а не только в payload.
    from datetime import UTC, datetime, timedelta

    from hub.services.digest import generate_due_digests

    row = await _project(db)
    await repo.update_project(db, dict(row)["id"], gate_policy='{"release": "auto"}')
    await db.commit()
    await _sweep(db, False, RED)
    await _sweep(db, True, "релиз PR #484 смержен в main")
    await generate_due_digests(db, now=datetime.now(UTC) + timedelta(days=1))

    page = await client.get("/digests")
    assert page.status_code == 200
    assert "Релиз был заблокирован" in page.text
    assert "checks_failed" in page.text
    assert "длилось 0 мин" in page.text


# --- #1420, круг 2: находки lite-ревью #632 --------------------------------


async def test_dashboard_prod_card_shows_open_release_block(client, db):
    # e7b51055423a1451: карточка «Что в проде» на дашборде читает тот же
    # снимок — открытая авария видна в ней первой строкой, снятая исчезает.
    await _project(db)
    await _sweep(db, False, RED)

    page = (await client.get("/")).text
    card = page[page.index('id="prod-state"') :]
    assert "Релиз заблокирован" in card
    assert "checks_failed" in card
    assert card.index("Релиз заблокирован") < card.index("Успешных выкатов")

    await _sweep(db, True, "релиз PR #484 смержен в main")
    page = (await client.get("/")).text
    assert "Релиз заблокирован" not in page[page.index('id="prod-state"') :]


def _text(result) -> str:
    from mcp.types import CallToolResult, TextContent

    if isinstance(result, CallToolResult):
        return "\n".join(b.text for b in result.content if isinstance(b, TextContent))
    return str(result)


async def test_my_context_carries_the_open_release_block(client, db):
    # ca52883c1a7ac3a7: общий hub_my_context несёт строку «Релиз заблокирован
    # с <время>: <причина>», пока авария открыта; источник — дешёвый
    # /api/release-blocks по событиям, а не снимок прода.
    from hub import mcp_server

    await _project(db)
    await _sweep(db, False, RED)

    async def _via_rest(path: str, *args, **kwargs):
        if path == "/api/release-blocks":
            resp = await client.get(path)
            assert resp.status_code == 200
            return resp.json()
        if path == "/api/diagnostics/identity":
            return {"username": "steward", "role": "agent", "principal_id": 1}
        return {"tasks": [], "next_cursor": None}

    with patch.object(mcp_server, "_api_get", side_effect=_via_rest):
        text = _text(await mcp_server.hub_my_context())
    assert "Релиз заблокирован с" in text
    assert "checks_failed" in text.split("Релиз заблокирован с", 1)[1]

    await _sweep(db, True, "релиз PR #484 смержен в main")
    with patch.object(mcp_server, "_api_get", side_effect=_via_rest):
        text = _text(await mcp_server.hub_my_context())
    assert "Релиз заблокирован" not in text


async def test_release_blocks_query_is_indexed_by_kind(db):
    # ca52883c1a7ac3a7: запрос открытых аварий идёт по индексу, а не сканом
    # всей ленты событий.
    from hub.services import release_alert as ra

    cur = await db.execute(
        "EXPLAIN QUERY PLAN " + ra.OPEN_BLOCKS_SQL, ra.open_blocks_args()
    )
    plan = " ".join(str(tuple(r)) for r in await cur.fetchall())
    assert "idx_events_kind_project" in plan, plan


async def test_red_ci_alert_names_the_run_and_failed_checks(db, monkeypatch):
    # 7c1d20f701b32445: авария по красному CI несёт прогон и упавшие проверки,
    # когда форж их отдал, — в payload события и в строке для стюарда.
    from unittest.mock import AsyncMock as _AsyncMock

    from hub.integrations.registry import plugins
    from hub.services.prod_state import format_prod_state, prod_state

    url = "https://github.com/agentdrover/haiplane/actions/runs/36200000001"
    logs = _AsyncMock(
        return_value={"failed_checks": ["Ruff and pytest"], "run_url": url}
    )
    monkeypatch.setattr(plugins.git_ops, "get_ci_failure_logs", logs, raising=False)
    await _project(db)
    await _sweep(db, False, RED)

    assert logs.await_args.args[0] == 484
    block = (await prod_state(db))["release_blocks"][0]
    assert block["ci"]["run_url"] == url
    assert block["ci"]["failed_checks"] == ["Ruff and pytest"]
    text = format_prod_state(await prod_state(db))
    assert url in text and "Ruff and pytest" in text
    alert = (await _feed(db, "release_blocked"))[0]
    assert url in alert["detail"]


async def test_red_ci_alert_says_when_forge_gave_nothing(db, monkeypatch):
    # 7c1d20f701b32445: форж ничего не отдал — строка так и говорит.
    from unittest.mock import AsyncMock as _AsyncMock

    from hub.integrations.registry import plugins
    from hub.services.prod_state import format_prod_state, prod_state

    monkeypatch.setattr(
        plugins.git_ops,
        "get_ci_failure_logs",
        _AsyncMock(return_value={"failed_checks": [], "run_url": ""}),
        raising=False,
    )
    await _project(db)
    await _sweep(db, False, RED)

    text = format_prod_state(await prod_state(db))
    assert "прогон CI форж не назвал" in text
    assert "упавшие проверки форж не назвал" in text


async def test_unreadable_since_is_named_not_zero(db):
    # baac9fd4eb694be8: нечитаемый штамп since — «время не прочитано», а не
    # «0 мин»; при снятии — «длительность не прочитана».
    import json

    from hub.services.prod_state import format_prod_state, prod_state

    row = await _project(db)
    await repo.insert_event(
        db,
        kind="release_blocked",
        project_id=dict(row)["id"],
        actor="hub",
        payload={"project": "default", "reason": RED, "since": "вчера вечером"},
    )
    await db.execute(
        "UPDATE events SET created_at='не штамп' WHERE kind='release_blocked'"
    )
    await db.commit()

    text = format_prod_state(await prod_state(db))
    assert "время не прочитано" in text
    assert "0 мин" not in text

    await _sweep(db, True, "релиз PR #484 смержен в main")
    cleared = await _feed(db, "release_unblocked")
    assert "длительность не прочитана" in cleared[0]["summary"]
    assert "0 мин" not in cleared[0]["summary"]
    cur = await db.execute("SELECT payload FROM events WHERE kind='release_unblocked'")
    assert json.loads((await cur.fetchone())[0])["minutes"] is None
