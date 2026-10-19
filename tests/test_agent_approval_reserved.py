"""APPROVED пишет человек или автоматика хаба, не внешний агент (#1872).

19.10.2026 #1867 закрыта APPROVED агента при карточке «вердикт: человек».
Каждый отказ здесь проверяется по всей картине, а не по коду ответа:
вердикт, ревизия, поколение, статус, лента, события и число задач (черновики
follow-up). Отказ, который что-то записал, — то, ради чего файл написан.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import urllib.error
from argparse import Namespace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import cli, config, mcp_server
from hub import repository as repo
from hub.config import TokenIdentity
from tests.state_support import auth, drive_to_review, make_state_task
from tests.test_auto_verdict import _post_review, _submitted_task
from tests.test_verdict_replay_guard import _in_review, _snapshot

REASON = "approval_reserved_for_human"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def hdr(monkeypatch) -> dict[str, dict[str, str]]:
    headers = auth(monkeypatch)
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            **config.HUB_TOKENS,
            # role=agent, но с человеческим полномочием: граница правила (#1872).
            "gate-token": TokenIdentity(
                "gatebot",
                "agent",
                principal_id=11,
                permissions=frozenset({"tasks.human_gate"}),
            ),
        },
    )
    headers["gate"] = {"Authorization": "Bearer gate-token"}
    return headers


def _approved(**extra: Any) -> dict:
    return {"verdict": "approved", "agent": "reviewer", **extra}


async def _post(client: AsyncClient, task_id: int, headers: dict, body: dict):
    return await client.post(
        f"/api/tasks/{task_id}/review-verdict", json=body, headers=headers
    )


def _assert_refusal(resp, label: str) -> None:
    assert resp.status_code == 409, (label, resp.status_code, resp.text)
    detail = resp.json()["detail"]
    assert detail["reason"] == REASON, (label, detail)
    assert detail["hint"], label


# ---- AC-1: отказ на всех транспортах и во всех состояниях ------------------


async def _mcp_attempt(client: AsyncClient, monkeypatch, headers, task_id, **args):
    """Через опубликованный вход mcp.call_tool, а не .fn."""

    async def _api_post(path: str, body: dict | None = None, **_):
        resp = await client.post(path, json=body or {}, headers=headers)
        if resp.status_code >= 400:
            raise mcp_server.HubApiError(
                mcp_server._parse_api_error(resp, resp.status_code)
            )
        return resp.json()

    async def _api_get(path: str, **_):
        return (await client.get(path, headers=headers)).json()

    monkeypatch.setattr(mcp_server, "_api_post", _api_post)
    monkeypatch.setattr(mcp_server, "_api_get", _api_get)
    result = await mcp_server.mcp.call_tool(
        "hub_submit_review", {"task_id": task_id, "verdict": "approved", **args}
    )
    return json.dumps(result, default=str, ensure_ascii=False)


async def _cli_attempt(client: AsyncClient, monkeypatch, token: str, task_id, capsys):
    """Настоящий cli.cmd_review_verdict и настоящий _api; сеть заменена мостом к ASGI."""
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(cli, "HUB_URL", "http://hub.test")
    monkeypatch.setattr(cli, "HUB_TOKEN", token)

    def _urlopen(req, timeout=0):
        fut = asyncio.run_coroutine_threadsafe(
            client.request(
                req.get_method(),
                req.full_url.replace("http://hub.test", ""),
                content=req.data,
                headers={
                    "Authorization": req.get_header("Authorization"),
                    "Content-Type": "application/json",
                },
            ),
            loop,
        )
        resp = fut.result(timeout=30)
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url, resp.status_code, "err", {}, io.BytesIO(resp.content)
            )
        raise AssertionError("CLI approval was accepted")

    args = Namespace(
        task_id=task_id,
        verdict="approved",
        comments="",
        agent="reviewer",
        findings_json="",
        create_tasks_for_out_of_scope=False,
        expected_generation=1,
    )

    def _run() -> int:
        with patch("urllib.request.urlopen", _urlopen):
            try:
                return cli.cmd_review_verdict(args)
            except SystemExit as exc:
                return int(exc.code or 0)

    rc = await asyncio.to_thread(_run)
    return rc, capsys.readouterr().err


async def test_an_agent_approval_is_refused_everywhere(
    client: AsyncClient,
    db: aiosqlite.Connection,
    hdr: dict,
    monkeypatch,
    capsys,
):
    task_id = await _in_review(client, hdr, "Reserved")
    before = await _snapshot(db, task_id)
    assert before["verdict"] is None and before["status"] == "review"

    # REST: в теле можно назвать себя кем угодно, решает только личность.
    spoof = _approved(
        expected_generation=1, human_caller=True, agent_caller=False, role="human"
    )
    for label, body in (
        ("с поколением", _approved(expected_generation=1)),
        ("без поколения", _approved()),
        ("чужое поколение", _approved(expected_generation=99)),
        ("поле тела", spoof),
        ("имя человека в теле", _approved(agent="denis", expected_generation=1)),
    ):
        _assert_refusal(await _post(client, task_id, hdr["rev"], body), label)
        assert await _snapshot(db, task_id) == before, label

    # MCP: опубликованный вход, JSON-строка ошибки с причиной.
    blob = await _mcp_attempt(
        client, monkeypatch, hdr["rev"], task_id, expected_generation=1
    )
    assert REASON in blob, blob
    assert await _snapshot(db, task_id) == before, "MCP"

    # CLI: код выхода 1 и причина на stderr.
    rc, err = await _cli_attempt(client, monkeypatch, "rev-token", task_id, capsys)
    assert rc == 1 and REASON in err, (rc, err)
    assert await _snapshot(db, task_id) == before, "CLI"

    # web: обычный POST — 303 с текстом, HTMX — 409 с текстом.
    plain = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved"},
        headers=hdr["rev"],
        follow_redirects=False,
    )
    assert plain.status_code == 303, plain.text
    assert "review_error=" in plain.headers["location"]
    assert "APPROVED" in __import__("urllib.parse").parse.unquote(
        plain.headers["location"]
    )
    htmx = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved"},
        headers={**hdr["rev"], "HX-Request": "true"},
    )
    assert htmx.status_code == 409, htmx.text
    assert "task-action-note" in htmx.text and "APPROVED" in htmx.text
    assert await _snapshot(db, task_id) == before, "web"

    # needs_decision: тот же отказ.
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()
    before_nd = await _snapshot(db, task_id)
    _assert_refusal(
        await _post(client, task_id, hdr["rev"], _approved(expected_generation=1)),
        "needs_decision",
    )
    assert await _snapshot(db, task_id) == before_nd
    await repo.update_task(db, task_id, status="review")
    await db.commit()

    # Агентский CHANGES_REQUESTED с находкой принят.
    cr = await _post(
        client,
        task_id,
        hdr["rev"],
        {
            "verdict": "changes_requested",
            "agent": "reviewer",
            "expected_generation": 1,
            "findings": [{"id": 1, "severity": "high", "message": "гонка записи"}],
        },
    )
    assert cr.status_code == 200, cr.text
    assert cr.json()["review_verdict"] == "changes_requested"


async def test_a_standing_verdict_and_a_missing_submission_do_not_open_the_door(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict
):
    # Стоящий APPROVED человека; тот же запрос агента — не 200-повтор, а отказ.
    standing = await _in_review(client, hdr, "Standing")
    ok = await _post(client, standing, hdr["human"], _approved(expected_generation=1))
    assert ok.status_code == 200, ok.text
    before = await _snapshot(db, standing)
    assert before["verdict"] == "approved"
    _assert_refusal(
        await _post(client, standing, hdr["rev"], _approved(expected_generation=1)),
        "повтор стоящего",
    )
    assert await _snapshot(db, standing) == before

    # Без сдачи: правило срабатывает раньше проверки «есть сдача».
    created = await client.post(
        "/api/tasks", json={"title": "Never submitted"}, headers=hdr["human"]
    )
    bare = created.json()["id"]
    before_bare = await _snapshot(db, bare)
    _assert_refusal(await _post(client, bare, hdr["rev"], _approved()), "без сдачи")
    assert await _snapshot(db, bare) == before_bare


async def test_a_state_task_approval_by_an_agent_is_refused_the_same_way(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict
):
    task_id = await make_state_task(db, title="state", under_feature=True)
    await drive_to_review(client, db, task_id, headers=hdr["impl"])
    from tests.state_support import fingerprint

    before = await fingerprint(db, task_id)
    resp = await _post(client, task_id, hdr["rev"], _approved(expected_generation=1))
    _assert_refusal(resp, "state")
    assert await fingerprint(db, task_id) == before


# ---- AC-2: полные цепочки --------------------------------------------------


async def test_delegated_and_human_approvals_work_as_before(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    from hub.services import auto_verdict

    # --- задачи создаются до включения токенов (подставка зовёт API без токена)
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    human_rest = await _submitted_task(client, db, "spike-h-rest", {"verdict": "human"})
    human_web = await _submitted_task(client, db, "spike-h-web", {"verdict": "human"})
    report_only = await _submitted_task(
        client, db, "spike-report", {"verdict": "human"}
    )
    auto = await _submitted_task(client, db, "spike-auto", {"verdict": "auto"})
    locked = await _submitted_task(client, db, "default", {"verdict": "steward"})

    # агентский отчёт при verdict=human не одобряет
    await _post_review(client, report_only)
    body = (await client.get(f"/api/tasks/{report_only}")).json()
    assert body["review_verdict"] is None and body["status"] == "review"

    # автопилот verdict=auto (не default) одобряет через maybe_auto_verdict
    await _post_review(client, auto)
    body = (await client.get(f"/api/tasks/{auto}")).json()
    assert body["review_verdict"] == "approved", "основания выполнены"

    # на default автопилот отказывает как раньше
    await _post_review(client, locked)
    assert await auto_verdict.maybe_auto_verdict(db, locked) is False
    body = (await client.get(f"/api/tasks/{locked}")).json()
    assert body["review_verdict"] is None

    # --- человек: REST и web
    headers = auth(monkeypatch)
    ok = await _post(client, human_rest, headers["human"], _approved(agent="denis"))
    assert ok.status_code == 200 and ok.json()["review_verdict"] == "approved"
    web = await client.post(
        f"/tasks/{human_web}/web-review-verdict",
        data={"verdict": "approved"},
        headers=headers["human"],
        follow_redirects=False,
    )
    assert web.status_code == 303 and "review_error" not in web.headers["location"]
    row = dict(await repo.get_task(db, human_web))
    assert row["review_verdict"] == "approved"

    # --- #743 как раньше: человек проходит замок, агент с human_gate его видит
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            **config.HUB_TOKENS,
            "gate-token": TokenIdentity(
                "gatebot",
                "agent",
                principal_id=11,
                permissions=frozenset({"tasks.human_gate"}),
            ),
        },
    )
    gate = {"Authorization": "Bearer gate-token"}
    lock = await _post(client, locked, gate, _approved())
    assert lock.status_code == 409, lock.text
    assert lock.json()["detail"]["error"] == "default_verdict_reserved_for_steward"
    own = await _post(client, human_rest, headers["rev"], _approved())
    # человек уже одобрил это поколение: агент остановлен правилом, а не повтором
    assert own.json()["detail"]["reason"] == REASON


async def test_the_stewards_chain_applies_after_its_own_checks(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    from hub.services import steward_apply, steward_applied
    from hub.services.steward_advisor import apply_advisor_outcomes
    from tests.test_steward_applied import (
        _client_task,
        _concur,
        _decide,
        _grant_act,
        _judge,
    )
    from tests.test_steward_shadow import _project

    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    _grant_act(monkeypatch)
    project_id = await _project(db, "steward-chain-1872")

    # Свои проверки стюарда не сошлись: вердикта нет (правило #1872 тут ни при чём).
    unconverged = await _client_task(db, project_id)
    await _judge(db, unconverged, verdict="approve")
    await _concur(db, monkeypatch, unconverged)
    await apply_advisor_outcomes(db)
    assert not dict(await repo.get_task(db, unconverged)).get("review_verdict")

    async def _no_refusals(_db, _task_id, _generation=None):
        return []

    async def _converged(_db, _task_id, _generation):
        return _decide()

    monkeypatch.setattr(steward_apply, "apply_refusals", _no_refusals)
    monkeypatch.setattr(steward_applied, "self_approval_for", _converged)
    task_id = await _client_task(db, project_id)
    await _judge(db, task_id, verdict="approve")
    await _concur(db, monkeypatch, task_id)
    assert await apply_advisor_outcomes(db) >= 1
    row = dict(await repo.get_task(db, task_id))
    # Внутренний писатель agent_caller не передаёт: правило #1872 ему не мешает.
    assert row["review_verdict"] == "approved", row["review_verdict"]


async def test_solo_mode_lets_only_the_server_computed_self_approval_through(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    task_id = await _submitted_task(client, db, "spike-solo", {"verdict": "human"})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            # assigned_agent подставки — «dev-agent»: это исполнитель сдачи.
            "impl-token": TokenIdentity("dev-agent", "agent", principal_id=7),
            "rev-token": TokenIdentity("reviewer", "agent", principal_id=9),
        },
    )
    impl = {"Authorization": "Bearer impl-token"}
    rev = {"Authorization": "Bearer rev-token"}

    # allow включён, но независимый агент — отказ как всем: флаг не исключение.
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "allow")
    before = await _snapshot(db, task_id)
    _assert_refusal(
        await _post(client, task_id, rev, _approved(expected_generation=1)),
        "независимый при allow",
    )
    assert await _snapshot(db, task_id) == before

    # forbid: исполнитель остановлен независимостью (403), правило его не касается.
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    forbid = await _post(client, task_id, impl, _approved(expected_generation=1))
    assert forbid.status_code == 403, forbid.text
    assert await _snapshot(db, task_id) == before

    # allow: сервер сам вычислил self_approved=True — исполнитель одобряет.
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "allow")
    mine = await _post(client, task_id, impl, _approved(expected_generation=1))
    assert mine.status_code == 200, mine.text
    assert mine.json()["review_verdict"] == "approved"
    assert (await _snapshot(db, task_id))["self_approved"], "метка self-approved"


async def test_an_agent_with_the_human_gate_counts_as_a_human(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict
):
    task_id = await _in_review(client, hdr, "Gate boundary")
    resp = await _post(client, task_id, hdr["gate"], _approved(expected_generation=1))
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_verdict"] == "approved"


# ---- AC-3: инструкции ревьюеру-агенту --------------------------------------


def test_reviewer_instructions_do_not_ask_an_agent_to_approve() -> None:
    import inspect

    from hub.integrations.dispatch import DispatchIntegration

    plugin = DispatchIntegration()
    text = plugin.build_review_message(
        task_id=42,
        title="t",
        description="d",
        review_cycle=0,
        max_cycles=3,
        generation=3,
    )
    assert not re.search(r"review-verdict\s+42\s+approved", text), text
    assert "approved --agent" not in text
    assert "oc-hub update 42 --kind review" in text, "положительный итог — отчёт"
    assert REASON in text

    reviewer = (ROOT / "agents" / "code-reviewer.md").read_text(encoding="utf-8")
    assert REASON in reviewer
    assert "`approved`, or" not in reviewer
    assert not re.search(
        r"verdict[^.\n]*\bapproved\b[^.\n]*hub_submit_review", reviewer
    )

    brief = inspect.getsource(mcp_server.hub_get_review_brief)
    assert "approved|changes_requested" not in brief
    doc = (ROOT / "docs" / "software-development-workflow.md").read_text("utf-8")
    assert "`approval_reserved_for_human`" in doc
    # Схема основного потока: от агента-ревьюера нет перехода approved -> merge.
    assert 'P -- "approved"' not in doc
    assert "(не от агента)" in doc
    rules = (ROOT / "docs" / "cursor-agent-rules.md").read_text("utf-8")
    assert "approval_reserved_for_human" in rules
