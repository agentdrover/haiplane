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

    doc = (ROOT / "docs" / "software-development-workflow.md").read_text("utf-8")
    assert "`approval_reserved_for_human`" in doc
    # Схема основного потока: от агента-ревьюера нет перехода approved -> merge.
    assert 'P -- "approved"' not in doc
    assert "(не от агента)" in doc
    rules = (ROOT / "docs" / "cursor-agent-rules.md").read_text("utf-8")
    assert "approval_reserved_for_human" in rules


# ---- рантайм-тексты для агента: проверка смысла по источникам --------------
#
# Словарь запретных конструкций — ЗДЕСЬ и только здесь. Ловится смысл:
# (а) текст поручает агенту записать APPROVED (канал записи и слово исхода в
#     одном предложении без признака запрета/передачи человеку), либо содержит
#     готовую конструкцию «слать approved»;
# (б) шаг одобрения назван действием агента (actor=agent).

#: Каналы, которыми агент что-то записывает в вердикт/ревью.
AGENT_CHANNELS = re.compile(
    r"hub_submit_review|review-verdict|--kind review|review_verdict", re.I
)
#: Слово исхода «одобрено».
OUTCOME = re.compile(r"\bapproved\b", re.I)
#: Точные конструкции «одобрение — не агента». Общих слов-гасителей (not, never,
#: written by, «APPROVED:») здесь нет: они гасили бы и нарушение вроде
#: «A reviewer, not the author, must run hub_submit_review with verdict approved».
REFUSES_OR_HANDS_OFF = re.compile(
    r"APPROVED comes from a human|APPROVED is a human's|"
    r"approved'? is refused|approved is refused|refuses an agent's approved|"
    r"never writes `?approved|APPROVED агент не записывает|"
    r"approval_reserved_for_human|"
    r"takes changes_requested only|changes_requested only|"
    r"a human approves|APPROVED\s*—\s*человек|"
    r"APPROVED: human or hub policy",
)
#: Готовые конструкции «агент, шли approved».
SEND_APPROVED = re.compile(
    r"(?:submit|send|file)[^.\n]{0,20}verdict=approved|approved\s*\|\s*changes|'approved' or|"
    r"--message[^\n]*APPROVED|review-verdict\s+\S+\s+approved|можно мержить|"
    r"Вердикт APPROVED\s*—|Obtain an APPROVED|"
    # отчёт ревью сдаётся hub_submit_machine_review; hub_submit_review — только
    # changes_requested
    r"(?:report|отчёт)[^.\n;]{0,25}(?:via|through|через)\s+hub_submit_review",
    re.I,
)


def advises_agent_to_approve(text: str) -> list[str]:
    """Предложения текста, которые поручают агенту слать APPROVED."""
    bad = [m.group(0) for m in SEND_APPROVED.finditer(text)]
    for sentence in re.split(r"(?<=[.!?])\s|\n", text):
        if (
            AGENT_CHANNELS.search(sentence)
            and OUTCOME.search(sentence)
            and not REFUSES_OR_HANDS_OFF.search(sentence)
        ):
            bad.append(sentence.strip())
    return bad


def approval_step_is_agents(schema: dict) -> list[str]:
    """(б): шаг одобрения, названный действием агента, в схеме процесса."""
    bad = []
    if schema["gates"]["review"].get("actor") == "agent":
        bad.append("gates.review.actor=agent")
    for t in schema["transitions"]:
        if (
            t.get("tool") == "hub_submit_review"
            and t.get("actor") == "agent"
            and t.get("to") == "completed"
        ):
            bad.append(f"transition {t['from']}->completed actor=agent")
    return bad


class Env:
    def __init__(self, client, db, hdr, monkeypatch, task_id):
        self.client, self.db, self.hdr = client, db, hdr
        self.monkeypatch, self.task_id = monkeypatch, task_id


async def _brief(env: Env, **args) -> str:
    from tests.test_mcp_server import _call_tool_text

    async def _api_get(path: str, **_):
        return (await env.client.get(path, headers=env.hdr["rev"])).json()

    env.monkeypatch.setattr(mcp_server, "_api_get", _api_get)
    result = await mcp_server.mcp.call_tool(
        "hub_get_review_brief", {"task_id": env.task_id, **args}
    )
    return _call_tool_text(result)


async def _src_brief_default(env):
    return [await _brief(env)]


async def _src_brief_full(env):
    return [await _brief(env, section="full")]


async def _src_mcp_catalog(env):
    tools = await mcp_server.mcp.list_tools()
    return [(t.description or "") + json.dumps(t.inputSchema) for t in tools]


async def _src_openapi(env):
    return [(await env.client.get("/openapi.json")).text]


async def _src_envelope(env):
    from hub import mcp_envelope

    reasons = [None, "self_review_forbidden", "human_decision_required"]
    out = [
        mcp_envelope.compute_next_action(st, aw, reason=r)
        for st in ("review", "running", "needs_decision", "pending_report")
        for aw in ("review", "none", "human_decision", "ci")
        for r in reasons
    ]
    for decider in ("human", "policy", "steward", "none"):
        route = {"decider": decider, "final": decider, "reason": "x"}
        out.append(mcp_envelope.next_action_of(route) or "")
    return out


async def _src_actionable_errors(env):
    from hub import actionable_errors as ae

    details = [
        ae.verdict_repeats_previous_detail("yesterday"),
        ae.verdict_contradicts_its_text_detail(
            "changes_requested", "approved", for_agent=True
        ),
        ae.verdict_contradicts_its_text_detail("changes_requested", "approved"),
        ae.changes_requested_requires_content_detail(),
    ]
    # Положительный исход агента — отчёт; человеку верно «исправьте поле verdict».
    assert details[1]["suggested_tool"] == "hub_submit_machine_review", details[1]
    assert details[2]["suggested_tool"] == "hub_submit_review", details[2]
    assert details[1]["retry_by_same_caller"] is True
    return [json.dumps(d, ensure_ascii=False) for d in details]


async def _src_workflow_reference(env):
    from hub import workflow_reference as wr

    return [
        json.dumps(wr.workflow_reference_dict(), indent=1),
        "\n".join(wr.lifecycle_map_lines()),
        wr.mcp_workflow_instruction_section(),
        wr.build_mcp_instructions(),
    ]


async def _src_done_message(env):
    return [
        mcp_server._format_hub_report_done_message(env.task_id, 1, st)
        for st in ("review", "pending_report", "needs_decision", "ci_check")
    ]


async def _src_dispatch(env):
    from hub.integrations.dispatch import DispatchIntegration

    d = DispatchIntegration()
    return [
        d.build_review_message(
            task_id=7, title="t", description="d", review_cycle=0, max_cycles=3
        ),
        d.build_arbiter_message(
            task_id=7,
            title="t",
            description="d",
            review_history=[],
            review_cycle=3,
            max_cycles=3,
        ),
        d.build_fix_message(7, "t", "d", "fix", 1, 3),
    ]


async def _src_feed_done_report(env):
    """Запись хаба о маршруте done-отчёта в review (pending_report и running)."""
    out = []
    # Путь pending_report: done-отчёт без одобрения становится сдачей на ревью.
    await repo.update_task(env.db, env.task_id, status="pending_report")
    await env.db.commit()
    done = await env.client.post(
        f"/api/tasks/{env.task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "готово"},
        headers=env.hdr["impl"],
    )
    out.append(done.text)
    feed = (
        await env.client.get(
            f"/api/tasks/{env.task_id}/updates", headers=env.hdr["human"]
        )
    ).json()
    out += [u["content"] for u in feed]
    assert any("routed to review" in t for t in out), out
    return out


async def _src_refusal_422(env):
    resp = await _post(
        env.client,
        env.task_id,
        env.hdr["rev"],
        {
            "verdict": "changes_requested",
            "agent": "reviewer",
            "expected_generation": 1,
            "findings": [
                {"id": 1, "severity": "low", "message": "x", "scope": "out_of_scope"}
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    return [resp.text]


async def _src_skills(env):
    texts = [p.read_text("utf-8") for p in sorted((ROOT / "skills").glob("*/SKILL.md"))]
    texts += [p.read_text("utf-8") for p in sorted((ROOT / "agents").glob("*.md"))]
    return texts


async def _src_seeded_skills(env):
    from hub import db as hub_db

    return [
        hub_db.MULTI_AGENT_REVIEW_SKILL,
        hub_db.MACHINE_REVIEW_CYCLE_SKILL,
        hub_db.EXECUTOR_PAIR_DISCIPLINE_SKILL,
    ]


async def _src_hub_string_literals(env):
    """Все строковые литералы hub/ с каналом записи вердикта (кроме docstring).

    Закрывает то, до чего не дотянуться вызовом: записи ленты orchestration и
    poller, подсказки в ветках, которые в тесте не случаются.
    """
    import ast

    out: list[str] = []
    for path in sorted((ROOT / "hub").rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        docs = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(
                n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            and n.body
            and isinstance(n.body[0], ast.Expr)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docs
                and AGENT_CHANNELS.search(node.value)
                and not re.match(r"\s*(SELECT|UPDATE|INSERT|DELETE|WITH)\b", node.value)
            ):
                out.append(node.value)
    assert out
    return out


SOURCES = {
    "hub_string_literals": _src_hub_string_literals,
    "brief_default": _src_brief_default,
    "brief_full": _src_brief_full,
    "mcp_catalog": _src_mcp_catalog,
    "openapi": _src_openapi,
    "envelope": _src_envelope,
    "actionable_errors": _src_actionable_errors,
    "workflow_reference": _src_workflow_reference,
    "mcp_done_message": _src_done_message,
    "dispatch_prompts": _src_dispatch,
    "feed_done_report": _src_feed_done_report,
    "refusal_422": _src_refusal_422,
    "skills_files_and_roles": _src_skills,
    "seeded_skills": _src_seeded_skills,
}


@pytest.mark.parametrize("source", sorted(SOURCES))
async def test_runtime_texts_an_agent_reads_never_advise_approved(
    source: str,
    client: AsyncClient,
    db: aiosqlite.Connection,
    hdr: dict,
    monkeypatch,
):
    task_id = await _in_review(client, hdr, f"Runtime {source}")
    env = Env(client, db, hdr, monkeypatch, task_id)
    texts = await SOURCES[source](env)
    assert texts and all(isinstance(t, str) for t in texts), source
    for text in texts:
        assert not advises_agent_to_approve(text), (
            source,
            advises_agent_to_approve(text),
        )


def test_the_approval_step_is_never_the_agents_in_the_process_schema() -> None:
    from hub import workflow_reference as wr

    assert approval_step_is_agents(wr.workflow_reference_dict()) == []


#: Модули hub/, где упомянут hub_submit_review; для каждого — источник выше или причина.
COVERED_MODULES = {
    "hub/actionable_errors.py": "actionable_errors, refusal_422",
    "hub/app.py": "openapi",
    "hub/mcp_envelope.py": "envelope",
    "hub/mcp_server.py": "mcp_catalog, mcp_done_message, brief_*",
    "hub/models.py": "openapi (описание поля)",
    "hub/workflow_reference.py": "workflow_reference",
    "hub/services/lifecycle.py": "refusal_422, комментарии",
    "hub/services/orchestration.py": "feed_done_report",
    "hub/services/task_updates.py": "feed_done_report",
    "hub/services/review_brief_projection.py": "brief_default",
    "hub/services/machine_review_intake.py": "комментарий кода, агенту не показывается",
    "hub/poller.py": "ленты poller: 'Awaiting review verdict' без слова исхода; "
    "needs_decision адресован человеку",
}


def test_a_new_module_naming_the_review_tool_must_be_covered() -> None:
    found = {
        str(p.relative_to(ROOT))
        for p in (ROOT / "hub").rglob("*.py")
        if "hub_submit_review" in p.read_text("utf-8")
    }
    assert found <= set(COVERED_MODULES), sorted(found - set(COVERED_MODULES))


#: Фразы, которые детектор ОБЯЗАН флагнуть (в т.ч. с отвлекающими not/never/written by).
MUST_FLAG = [
    "A reviewer, not the author, must run hub_submit_review with verdict approved.",
    "Never forget: send hub_submit_review with verdict=approved when done.",
    "The verdict written by the reviewer via hub_submit_review is approved.",
    "Run oc-hub review-verdict 7 approved --agent x",
    "Submit the verdict with hub_submit_review (verdict=approved|changes_requested).",
    "Reviewer runs hub_get_review_brief and hub_submit_review; after APPROVED, report done.",
    "Вердикт запишет человек или независимый ревьюер (hub_submit_review); после APPROVED повторите отчёт.",
    "Obtain an APPROVED verdict via hub_submit_review, then report done again.",
    "The reviewer files a report via hub_submit_review.",
    'oc-hub update 7 --kind review --message "ok\\nAPPROVED"',
    "Вердикт APPROVED — если задача выполнена, можно мержить.",
]

#: Фразы из нынешних текстов хаба: не флагуются.
MUST_PASS = [
    "APPROVED comes from a human or the hub policy, then report done again.",
    "Agent: hub_submit_review takes changes_requested only (one scope=in_scope finding); "
    "a positive outcome is a review report. APPROVED: human or hub policy.",
    "An agent never writes `approved` (#1872): the hub refuses it with 409 approval_reserved_for_human.",
    "APPROVED — человек, с expected_generation (кнопка на карточке или hp-hub review-verdict --expected-generation N).",
    "An agent's approved is refused (409 approval_reserved_for_human) on hub_submit_review.",
    "Команд вердикта не запускай: APPROVED агент не записывает, решает человек.",
    "Reviewer lane: a report (hub_submit_machine_review) or changes_requested via hub_submit_review.",
]


@pytest.mark.parametrize("phrase", MUST_FLAG)
def test_the_detector_flags_forbidden_phrases(phrase: str) -> None:
    assert advises_agent_to_approve(phrase), phrase


@pytest.mark.parametrize("phrase", MUST_PASS)
def test_the_detector_passes_current_hand_off_phrases(phrase: str) -> None:
    assert advises_agent_to_approve(phrase) == [], phrase


def _both_ways(human: bool):
    return "human" if human else "agent"


async def test_the_contradiction_hint_depends_on_who_calls(
    client: AsyncClient, db: aiosqlite.Connection, hdr: dict
):
    task_id = await _in_review(client, hdr, "Contradiction")
    body = {
        "verdict": "changes_requested",
        "agent": "reviewer",
        "comments": "APPROVED, but fix the lock first",
        "expected_generation": 1,
    }
    before = await _snapshot(db, task_id)

    agent = await _post(client, task_id, hdr["rev"], body)
    assert agent.status_code == 422, agent.text
    d = agent.json()["detail"]
    assert d["reason"] == "verdict_contradicts_its_text"
    assert d["suggested_tool"] == "hub_submit_machine_review", d
    assert "not an agent's approved" in d["hint"], d

    human = await _post(client, task_id, hdr["human"], body)
    assert human.status_code == 422, human.text
    d = human.json()["detail"]
    assert d["suggested_tool"] == "hub_submit_review", d
    assert "change the verdict field" in d["hint"], d
    assert await _snapshot(db, task_id) == before, "оба отказа ничего не пишут"

    # Совет человеку верен: с approved в поле тот же текст принят, повтор — 200.
    fixed = {**body, "verdict": "approved", "agent": "denis"}
    ok = await _post(client, task_id, hdr["human"], fixed)
    assert ok.status_code == 200, ok.text
    again = await _post(client, task_id, hdr["human"], fixed)
    assert again.status_code == 200, again.text
    assert (await _snapshot(db, task_id))["revision"] == 1


def test_the_arbiter_prompt_has_no_verdict_command_and_names_what_follows() -> None:
    from hub.integrations.dispatch import DispatchIntegration

    text = DispatchIntegration().build_arbiter_message(
        task_id=7,
        title="t",
        description="d",
        review_history=[],
        review_cycle=3,
        max_cycles=3,
    )
    # На поколении арбитража вердикт уже стоит (409 verdict_already_stands);
    # после отчёта задача в needs_decision, решает человек.
    assert not re.search(r"review-verdict|--kind review|CHANGES_REQUESTED", text), text
    assert "--kind arbitration" in text
    assert "needs_decision" in text and "решает человек" in text
    assert "задача пойдёт на доработку" not in text
