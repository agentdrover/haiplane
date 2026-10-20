"""Сдача на ревью с expected_sha: хаб сверяет голову ветки сам (#1937).

Клиент (launcher k8) заявляет sha, который проверил критик. Заявление не
закрепляется: хаб наблюдает голову (resolve_branch_tip) и при расхождении
отказывает 409 до любой записи, кроме одного alert.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from hub import cli, mcp_server, models
from hub import repository as repo
from hub.services import ci_trust, lifecycle, orchestration, review_dispatch

X = "a" * 40
Y = "b" * 40


def _install_git(monkeypatch, tip: str, *, fetch_ok: bool = True):
    from hub.integrations.noop import NoopGitOps
    from hub.integrations.registry import plugins

    class _Git(NoopGitOps):
        def __init__(self) -> None:
            self.tip = tip
            self.fetch_ok = fetch_ok

        async def fetch_base(self, repo: str, base: str):
            return (True, "") if self.fetch_ok else (False, "network down")

        async def head_sha(self, repo: str, base: str) -> str:
            return self.tip

    git = _Git()
    monkeypatch.setattr(plugins, "git_ops", git)
    monkeypatch.setattr(
        orchestration,
        "project_git_context",
        AsyncMock(
            return_value={"repo": os.path.dirname(__file__), "base_branch": "develop"}
        ),
    )
    return git


async def _running_task(db: aiosqlite.Connection, title: str):
    task = await lifecycle.create_task(
        db, models.TaskCreate(title=title, source="agent", agent="bot")
    )
    await lifecycle.approve_task(db, task.id, models.TaskApprove(force=True))
    await lifecycle.pair_start_task(
        db,
        task.id,
        models.TaskPairStart(agent="dev", branch_slug="mine", plan="Plan: сдать"),
    )
    return task


class _Spies:
    """Всё, что отказ обязан не вызвать."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[str] = []
        for owner, name in (
            (ci_trust, "refresh_quietly"),
            (ci_trust, "invalidate"),
            (orchestration, "ensure_delivery_pr"),
            (review_dispatch, "maybe_dispatch_review"),
        ):
            real = getattr(owner, name)

            async def spy(*args, _n=name, _real=real, **kwargs):
                self.calls.append(_n)
                return await _real(*args, **kwargs)

            monkeypatch.setattr(owner, name, spy)


async def _snapshot(db: aiosqlite.Connection, task_id: int) -> dict:
    await db.commit()
    tables = [
        r[0]
        for r in await (
            await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ).fetchall()
    ]
    counts = {}
    for name in tables:
        cur = await db.execute(f'SELECT COUNT(*) FROM "{name}"')
        counts[name] = (await cur.fetchone())[0]
    row = dict(await repo.get_task(db, task_id))
    row.pop("updated_at", None)
    return {"counts": counts, "task": row}


def _only_one_alert_added(before: dict, after: dict) -> list[str]:
    grew = {
        t: after["counts"][t] - before["counts"][t]
        for t in after["counts"]
        if after["counts"][t] != before["counts"][t]
    }
    return [f"{t}:{n}" for t, n in sorted(grew.items())]


async def test_matching_expected_sha_is_pinned(db: aiosqlite.Connection, monkeypatch):
    """AC-1: expected_sha = наблюдённая голова (в т.ч. в верхнем регистре) -> сдача."""
    _install_git(monkeypatch, X)
    task = await _running_task(db, "Совпало")
    body = models.TaskSubmitReview(agent="dev", expected_sha=X.upper())
    assert body.expected_sha == X, "регистр нормализуется до сравнения"
    done = await lifecycle.submit_for_review(db, task.id, body)
    assert done.submission_generation == 1
    assert done.submission_sha == X, "закрепляется наблюдённый sha, не заявление"
    assert done.status == "review"


async def test_a_moved_or_unverified_head_refuses_the_submission(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-2: другая голова / голову не увидели -> 409 и ни одной записи, кроме alert."""
    git = _install_git(monkeypatch, X)
    spies = _Spies(monkeypatch)
    body = models.TaskSubmitReview(
        agent="dev",
        expected_sha=Y,
        accept_areas=True,
        prevention=models.DefectPrevention(
            kind="accepted_risk", reason="r", revisit="v"
        ),
    )

    async def refused(task_id: int, reason: str):
        before = await _snapshot(db, task_id)
        with pytest.raises(HTTPException) as err:
            await lifecycle.submit_for_review(db, task_id, body)
        assert err.value.status_code == 409
        assert err.value.detail["reason"] == reason
        after = await _snapshot(db, task_id)
        assert after["task"] == before["task"], "карточка не изменилась"
        assert _only_one_alert_added(before, after) == ["task_updates:1"]
        updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
        assert updates[-1]["kind"] == "alert"
        return err.value.detail, updates[-1]["content"]

    # другая голова из running
    task = await _running_task(db, "Голова ушла")
    git.tip = X
    detail, alert = await refused(task.id, "head_moved")
    assert detail["expected_sha"] == Y and detail["observed_sha"] == X
    assert X in alert and Y in alert

    # голову не удалось наблюдать
    git.fetch_ok = False
    task = await _running_task(db, "Сеть")
    detail, _ = await refused(task.id, "head_unverified")
    assert detail["reason"] == "head_unverified" and detail.get("cause")
    git.fetch_ok = True

    # наблюдение не 40 hex
    git.tip = "short-tip"
    task = await _running_task(db, "Мусор")
    await refused(task.id, "head_unverified")

    # из review: тот же sha X сохранён, ожидание Y
    git.tip = X
    task = await _running_task(db, "Из review")
    first = await lifecycle.submit_for_review(
        db, task.id, models.TaskSubmitReview(agent="dev", expected_sha=X)
    )
    assert first.submission_generation == 1
    spies.calls.clear()
    await refused(task.id, "head_moved")
    assert spies.calls == [], f"отказ дошёл до {spies.calls}"
    row = dict(await repo.get_task(db, task.id))
    assert (row["status"], row["submission_generation"], row["submission_sha"]) == (
        "review",
        1,
        X,
    )


async def test_without_expected_sha_nothing_changes(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-3: без поля (или пустое/null) — как раньше; same-sha no-op и headless целы."""
    git = _install_git(monkeypatch, X)
    for index, value in enumerate((None, "")):
        task = await _running_task(db, f"Без поля {index}")
        done = await lifecycle.submit_for_review(
            db, task.id, models.TaskSubmitReview(agent="dev", expected_sha=value)
        )
        assert (done.submission_generation, done.submission_sha) == (1, X)

    # пустое наблюдение без expected_sha по-прежнему не отказ
    git.fetch_ok = False
    task = await _running_task(db, "Нет наблюдения")
    done = await lifecycle.submit_for_review(
        db, task.id, models.TaskSubmitReview(agent="dev")
    )
    assert done.submission_generation == 1 and done.submission_sha == ""
    git.fetch_ok = True

    # same-sha повтор из review с expected_sha = X — прежний no-op
    task = await _running_task(db, "Повтор")
    await lifecycle.submit_for_review(db, task.id, models.TaskSubmitReview(agent="dev"))
    again = await lifecycle.submit_for_review(
        db, task.id, models.TaskSubmitReview(agent="dev", expected_sha=X)
    )
    assert again.submission_generation == 1 and again.submission_sha == X

    # headless-цепочка: шаг общий, тело пустое, отказывать нечем
    row = dict(await repo.get_task(db, task.id))
    state = lifecycle.SubmitContext(db=db, task_id=task.id, task=row)
    git.tip = Y
    await lifecycle._step_pin_submission_sha(state)
    assert state.submission_sha == Y
    headless = {s.name: s for s in lifecycle.HEADLESS_STEPS}
    assert headless["pin_submission_sha"].refuses is False
    pair = {s.name: s for s in lifecycle.SUBMIT_STEPS}
    assert pair["pin_submission_sha"].refuses is True, "pair-цепочка может отказать"


async def test_cli_and_mcp_pass_expected_sha(client: AsyncClient, db, monkeypatch):
    """AC-4: формат — 422/локальная ошибка/ошибка инструмента без сети."""
    # REST: неверный формат -> 422 (значение в ответ не возвращается)
    resp = await client.post("/api/tasks", json={"title": "REST формат"})
    task_id = resp.json()["id"]
    for bad in ("abc123", "g" * 40, X + "0", X + "\n", " " + X, "é" * 40):
        resp = await client.post(
            f"/api/tasks/{task_id}/submit-review", json={"expected_sha": bad}
        )
        assert resp.status_code == 422, bad
        assert "expected_sha" in resp.text

    # state-задача: поле только для commit-задач
    state_task = await lifecycle.create_task(
        db,
        models.TaskCreate(
            title="Состояние", source="agent", agent="bot", result_kind="state"
        ),
    )
    with pytest.raises(HTTPException) as err:
        await lifecycle.submit_for_review(
            db,
            state_task.id,
            models.TaskSubmitReview(agent="dev", expected_sha=X),
        )
    assert err.value.status_code == 422

    # CLI: неверный формат — rc != 0 и ни одного сетевого вызова
    api = MagicMock(return_value={"id": 7, "status": "review"})
    argv = ["oc-hub", "submit-review", "7", "--expected-sha", "abc123"]
    with patch.object(sys, "argv", argv), patch.object(cli, "_api", api):
        assert cli.main() != 0
    assert api.call_count == 0
    # верный формат доходит до тела
    argv = ["oc-hub", "submit-review", "7", "--expected-sha", X.upper()]
    with patch.object(sys, "argv", argv), patch.object(cli, "_api", api):
        assert cli.main() == 0
    assert api.call_args.args[2]["expected_sha"] == X.upper()
    # без флага тела нет поля
    argv = ["oc-hub", "submit-review", "7"]
    with patch.object(sys, "argv", argv), patch.object(cli, "_api", api):
        assert cli.main() == 0
    assert "expected_sha" not in api.call_args.args[2]

    # MCP: неверный формат — ошибка инструмента, ни одного сетевого вызова
    get = AsyncMock(return_value={"status": "running", "submission_generation": 0})
    post = AsyncMock(return_value={"status": "review", "submission_generation": 1})
    with (
        patch.object(mcp_server, "_api_get", get),
        patch.object(mcp_server, "_api_post", post),
    ):
        out = json.loads(
            await mcp_server.hub_submit_for_review(7, expected_sha="abc123")
        )
        assert out["reason"] == "expected_sha_invalid"
        assert get.call_count == 0 and post.call_count == 0
        await mcp_server.hub_submit_for_review(7, expected_sha=X)
    assert post.call_args.args[1]["expected_sha"] == X

    tools = {t.name: t for t in await mcp_server.mcp.list_tools()}
    assert "expected_sha" in tools["hub_submit_for_review"].inputSchema["properties"]
