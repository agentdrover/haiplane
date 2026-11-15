"""Scenario bodies of the dispatch tests of #2336 (checks_done, no tokens_spent, run text).

The tests themselves are named in the acceptance criteria and live in
``tests/test_review_dispatch.py``; that file is at its size ledger, so the
bodies sit here and the tests there only call them.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.review_checks import RUN_TEXT_LIMIT
from hub.services.review_dispatch import DEEP, LITE, sweep_review_dispatches
from tests.review_dispatch_support import (
    _any_dispatch_row,
    _DispatchRecorder,
    _submitted,
    _wire,
)

CHECKS = [
    {
        "what": "изменённые тесты проходят",
        "how": "uv run pytest -q tests/test_x.py",
        "result": "12 passed, rc 0",
    },
    {
        "what": "места вызова новой функции",
        "how": "чтение кода: grep по hub/",
        "result": "три места, все передают аргумент",
    },
]

_USAGE_ALERT = "расходится с данными провайдера"
_BILL = 5_086_096


def report(**extra) -> dict:
    """Отчёт облачного ревьюера «проверено, кандидатов нет», без tokens_spent."""
    return {
        "harness_skill": "lite-diff-review",
        "raw_count": 0,
        "incomplete": False,
        "findings_confirmed": [],
        "findings_rejected": [],
        "unresolved": [],
        "lost_dimensions": [],
        "checks_done": CHECKS,
        "agent": "cursor-cloud-reviewer",
        "model": "grok-4.6",
        **extra,
    }


def asks_for_checks_not_tokens(prompts: list[tuple[str, str]]) -> None:
    """AC-4: каждый промпт требует checks_done и не требует «реальных tokens_spent»."""
    for where, prompt in prompts:
        assert "checks_done" in prompt, where
        for key in ('"what"', '"how"', '"result"'):
            assert key in prompt, (where, key)
        assert "findings_rejected" in prompt and "не дефект" in prompt, where
        assert "tokens_spent" not in prompt, f"токены у ревьюера не просят: {where}"
        assert "реальными raw_count" not in prompt, where


async def prompts_of_both_profiles(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, build_prompt
) -> list[tuple[str, str]]:
    """Промпты, которые хаб собирает: настоящие заказы lite и deep, MCP и HTTP."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-chk"}, "run": {"id": "r-chk"}})
    _wire(monkeypatch, recorder)
    await _submitted(client, db, "spike-chk-lite")
    await _submitted(
        client,
        db,
        "spike-chk-deep",
        areas=["hub/db.py"],
        diff="+++ b/hub/x.py\n+x = 1\n",
    )
    found = [
        (f"dispatched #{n}", call["prompt_text"])
        for n, call in enumerate(recorder.calls)
    ]
    for profile in (LITE, DEEP):
        for http in (False, True):
            found.append(
                (
                    f"{profile} http={http}",
                    build_prompt(profile, http, generation=3, pinned_sha="c" * 40),
                )
            )
    return found


async def _usage_alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    return [u["content"] for u in updates if _USAGE_ALERT in u["content"]]


async def undeclared_tokens_raise_no_usage_alert(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """AC-5: нет tokens_spent - нет алерта #757; заявлен и расходится - алерт прежний."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-u"}, "run": {"id": "r-u"}})
    _wire(monkeypatch, recorder)

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": _BILL}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)

    quiet = await _submitted(client, db, "spike-undeclared")
    resp = await client.post(f"/api/tasks/{quiet}/machine-review", json=report())
    assert resp.status_code == 200, resp.text
    loud = await _submitted(client, db, "spike-declared")
    resp = await client.post(
        f"/api/tasks/{loud}/machine-review", json=report(tokens_spent=120_000)
    )
    assert resp.status_code == 200, resp.text

    await sweep_review_dispatches(db)

    assert await _usage_alerts(db, quiet) == []
    stamped = dict(await repo.get_latest_machine_review(db, quiet))
    assert stamped["tokens_spent"] is None and stamped["provider_tokens"] == _BILL
    assert (await _any_dispatch_row(db, quiet))["status"] == "done"
    flags = await _usage_alerts(db, loud)
    assert len(flags) == 1, flags
    assert f"tokens_spent=120000, Cursor usage={_BILL}" in flags[0]


async def cloud_run_text_is_kept_scrubbed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """AC-6: текст прогона - без секретов, с потолком, в брифе с пометкой."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-t"}, "run": {"id": "r-t"}})
    _wire(monkeypatch, recorder)  # ключ облака, заданный здесь, - известный секрет
    task_id = await _submitted(client, db, "spike-run-text")
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=report())
    assert resp.status_code == 200, resp.text
    text = (
        "НАЧАЛО: прогнал тесты и прочитал дифф, ключ test-key в среде не нужен.\n"
        + "промежуточная строка прогона\n" * 2 * RUN_TEXT_LIMIT
        + "ХВОСТ"
    )

    async def _finished(agent_id, run_id):
        return {"id": run_id, "status": "FINISHED", "result": text}

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 900_000}}

    monkeypatch.setattr(cursor_cloud, "get_run", _finished)
    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)

    await sweep_review_dispatches(db)

    kept = dict(await repo.get_latest_machine_review(db, task_id))["run_text"]
    assert kept.startswith("НАЧАЛО: прогнал тесты")
    assert "test-key" not in kept and "[redacted]" in kept
    assert 0 < len(kept) <= RUN_TEXT_LIMIT, len(kept)
    assert "ХВОСТ" not in kept, "потолок длины режет текст, а не пропускает целиком"
    shown = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    brief = shown["machine_review"]
    assert brief["run_text"] == kept
    assert "слово ревьюера" in brief["run_text_note"]
