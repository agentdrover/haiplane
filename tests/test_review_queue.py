"""Очередь ревью одним вызовом (#1334).

НАБЛЮДЕНО 22–23.09.2026. Стюард собирал сводку очереди брифом на задачу:
20+ вызовов ``/api/tasks/{id}/review-brief`` по 8–60 секунд, и вынимал из
каждого пять полей. Бриф дорог, потому что считает всё — дифф, call sites,
предпас. Два прохода упали по таймауту.

Очередь читает только хранимые факты. Главный риск — она заведёт своё
правило «отчёт текущий» или «вердикт текущий» и разойдётся с брифом; поэтому
AC-1 сверяет её с брифом поле в поле, а AC-2 роняет любой вызов диффа, сети
или самого брифа изнутри сборщика.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.services import review_queue
from hub.services.review_brief import build_review_brief


class _Git(NoopGitOps):
    """Вершина ветки, которую хаб наблюдает, задаётся тестом."""

    def __init__(self, tip: str = "aaa111"):
        self.tip = tip

    async def fetch_base(self, repo: str, base: str):
        return (True, "")

    async def head_sha(self, repo: str, base: str) -> str:
        return self.tip

    async def branch_diff(self, repo: str, base: str, branch: str):
        return "+++ b/x.py\n+line\n"


@pytest.fixture(autouse=True)
def _forget_observed_tips():
    from hub.services import lifecycle

    lifecycle.forget_observed_tips()
    yield
    lifecycle.forget_observed_tips()


@pytest.fixture
def git(monkeypatch):
    from hub.services import orchestration

    monkeypatch.setattr(
        orchestration,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )
    g = _Git()
    monkeypatch.setattr(plugins, "git_ops", g)
    return g


async def _submitted(client: AsyncClient, git: _Git, title: str, tip: str) -> int:
    git.tip = tip
    resp = await client.post("/api/tasks", json={"title": title})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: do it"},
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text
    return task_id


_FINDING = {"title": "Off by one", "severity": "high", "category": "logic"}


async def _report(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    confirmed: list[dict] | None = None,
    unresolved: list[dict] | None = None,
    incomplete: bool | None = False,
) -> None:
    row = dict(await repo.get_task(db, task_id))
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=int(row["submission_generation"] or 0),
        model="gpt-5.3-codex",
        agent_count=3,
        tokens_spent=1000,
        raw_count=len(confirmed or []),
        findings_confirmed=json.dumps(confirmed or []),
        unresolved=json.dumps(unresolved or []),
        incomplete=incomplete,
        submitted_by="reviewer",
    )
    await db.commit()


async def _in_flight(db: aiosqlite.Connection, task_id: int) -> None:
    row = dict(await repo.get_task(db, task_id))
    await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=int(row["submission_generation"] or 0),
        agent_id="bc-1",
        run_id="run-1",
        model="grok-4.6",
        profile="standard",
    )
    await db.commit()


async def _merge_failed(db: aiosqlite.Connection, task_id: int) -> None:
    """Отказ доставки так, как его пишет гейт (``_deliver_pair_task``)."""
    detail = "merge_failed: GitHub refused the merge"
    row = dict(await repo.get_task(db, task_id))
    # Гейт доставляет только одобренное: вердикт текущей сдачи уже записан.
    await db.execute(
        "UPDATE tasks SET review_verdict='approved', review_verdict_generation=? "
        "WHERE id=?",
        (int(row["submission_generation"] or 0), task_id),
    )
    await repo.update_task(db, task_id, status="needs_decision")
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Ревью одобрено, но PR #7 не доставлен — {detail}. Задача не может "
        "считаться выполненной, пока работа не в базовой ветке. Решение за "
        "человеком (hub_decide_task).",
    )
    await repo.insert_event(
        db,
        kind="needs_decision",
        task_id=task_id,
        actor="hub",
        payload={"reason": "merge_gate", "detail": detail, "via": "poller"},
    )
    await db.commit()


async def _five_states(client: AsyncClient, db, git: _Git) -> dict[str, int]:
    ids = {
        "clean": await _submitted(client, git, "Clean report", "c1"),
        "findings": await _submitted(client, git, "Report with findings", "f1"),
        "in_flight": await _submitted(client, git, "Review in flight", "i1"),
        "none": await _submitted(client, git, "No report", "n1"),
        "incomplete": await _submitted(client, git, "Incomplete report", "p1"),
        "merge_failed": await _submitted(client, git, "Merge failed", "m1"),
    }
    await _report(db, ids["clean"])
    await _report(
        db,
        ids["findings"],
        confirmed=[_FINDING],
        unresolved=[{"title": "Unjudged", "why": "verifier died"}],
    )
    await _in_flight(db, ids["in_flight"])
    await _report(db, ids["incomplete"], incomplete=True)
    await _report(db, ids["merge_failed"])
    await _merge_failed(db, ids["merge_failed"])
    return ids


def _brief_fields(brief) -> dict[str, Any]:
    """Те же пять полей, что стюард 23.09 вынимал jq-ом из брифа."""
    mr = brief.machine_review
    current = mr is not None and mr.is_current
    flight = brief.review_in_flight
    latest = brief.latest_review
    gen_review = brief.current_generation_review
    return {
        "submission_generation": brief.submission_generation,
        "submission_sha": brief.submission_sha,
        "sha_check": brief.sha_check,
        "report_state": brief.review_report.state,
        "report_outcome": mr.outcome if mr is not None else "",
        "findings_confirmed": len(mr.findings_confirmed) if current else None,
        "findings_unresolved": len(mr.unresolved) if current else None,
        "in_flight_model": flight.model if flight else None,
        "in_flight_grace_until": flight.grace_until if flight else None,
        "verdict": latest.verdict.value if latest else None,
        "verdict_generation": latest.submission_generation if latest else None,
        "verdict_is_current": latest.is_current if latest else False,
        "generation_has_review": gen_review.has_review,
        "generation_review_reason": gen_review.reason,
    }


def _row_fields(row) -> dict[str, Any]:
    return {
        "submission_generation": row.submission_generation,
        "submission_sha": row.submission_sha,
        "sha_check": row.sha_check,
        "report_state": row.report_state,
        "report_outcome": row.report_outcome,
        "findings_confirmed": row.findings_confirmed,
        "findings_unresolved": row.findings_unresolved,
        "in_flight_model": row.review_in_flight.model if row.review_in_flight else None,
        "in_flight_grace_until": (
            row.review_in_flight.grace_until if row.review_in_flight else None
        ),
        "verdict": row.verdict,
        "verdict_generation": row.verdict_generation,
        "verdict_is_current": row.verdict_is_current,
        "generation_has_review": row.generation_has_review,
        "generation_review_reason": row.generation_review_reason,
    }


# ---- AC-1: поле в поле с брифом ----


async def test_the_queue_matches_the_brief_field_by_field(
    client: AsyncClient, db: aiosqlite.Connection, git: _Git
) -> None:
    ids = await _five_states(client, db, git)
    # Ветка одной задачи ушла после сдачи: бриф это видит, очередь обязана
    # повторить ЕГО ответ, а не свой.
    briefs = {}
    for name, task_id in ids.items():
        git.tip = (
            "moved999"
            if name == "none"
            else {
                "clean": "c1",
                "findings": "f1",
                "in_flight": "i1",
                "incomplete": "p1",
                "merge_failed": "m1",
            }[name]
        )
        briefs[name] = await build_review_brief(db, task_id)

    queue = await review_queue.review_queue(db)
    rows = {row.task_id: row for row in queue.rows}

    assert set(rows) == set(ids.values()), (
        "строка на каждую задачу review/needs_decision"
    )
    for name, task_id in ids.items():
        assert _row_fields(rows[task_id]) == _brief_fields(briefs[name]), name

    assert rows[ids["none"]].sha_check == "diverged"
    assert rows[ids["clean"]].sha_check == "match"
    assert rows[ids["clean"]].report_status == "current"
    assert rows[ids["findings"]].report_status == "current"
    assert rows[ids["findings"]].findings_confirmed == 1
    assert rows[ids["findings"]].findings_unresolved == 1
    assert rows[ids["in_flight"]].report_status == "in_flight"
    assert rows[ids["none"]].report_status == "none"
    assert rows[ids["incomplete"]].report_status == "incomplete"

    stalled = rows[ids["merge_failed"]]
    assert stalled.status == "needs_decision"
    assert stalled.verdict == "approved" and stalled.verdict_is_current
    assert "merge_failed" in stalled.stall_reason, (
        "причина стойла лежала последней строкой ленты — очередь обязана её назвать"
    )
    assert rows[ids["clean"]].stall_reason == ""
    assert all(r.waiting_minutes is not None for r in queue.rows)


async def test_an_unobserved_tip_is_unknown_not_match(
    client: AsyncClient, db: aiosqlite.Connection, git: _Git
) -> None:
    task_id = await _submitted(client, git, "Never observed", "u1")
    from hub.services import lifecycle

    # Рестарт хаба: наблюдений нет. Сдача закрепила u1, ветка стоит на u1 —
    # но очередь в сеть не ходит и потому этого не знает.
    lifecycle.forget_observed_tips()

    row = (await review_queue.review_queue(db)).rows[0]

    assert row.task_id == task_id
    assert row.sha_check == "unknown", "неизвестное не выдаётся за match"
    assert row.sha_check_reason


# ---- AC-2: без диффа, без сети, без брифа ----


class _Forbidden(NoopGitOps):
    """Любой вызов git/forge из сборщика — записывается и роняет вызов."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattribute__(self, name: str):
        if name.startswith("_") or name == "calls":
            return object.__getattribute__(self, name)
        calls = object.__getattribute__(self, "calls")

        async def refuse(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"очередь позвала git_ops.{name}")

        return refuse


async def test_the_queue_does_not_recompute_diffs_or_call_the_forge(
    client: AsyncClient,
    db: aiosqlite.Connection,
    git: _Git,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for n in range(20):
        task_id = await _submitted(client, git, f"Queued {n}", f"s{n}")
        if n % 3 == 0:
            await _report(db, task_id, confirmed=[_FINDING] if n % 2 else [])

    forbidden = _Forbidden()
    monkeypatch.setattr(plugins, "git_ops", forbidden)
    called: list[str] = []

    def trap(name: str):
        async def refuse(*args, **kwargs):
            called.append(name)
            raise AssertionError(f"очередь позвала {name}")

        return refuse

    from hub import services
    from hub.services import lifecycle, orchestration, review_brief, review_evidence

    monkeypatch.setattr(review_brief, "build_review_brief", trap("build_review_brief"))
    monkeypatch.setattr(review_evidence, "review_report", trap("review_report"))
    monkeypatch.setattr(lifecycle, "resolve_branch_tip", trap("resolve_branch_tip"))
    monkeypatch.setattr(services, "resolve_branch_tip", trap("resolve_branch_tip"))
    monkeypatch.setattr(
        orchestration, "project_git_context", trap("project_git_context")
    )
    monkeypatch.setattr(services, "project_git_context", trap("project_git_context"))

    queue = await review_queue.review_queue(db)

    assert len(queue.rows) == 20
    assert forbidden.calls == [], forbidden.calls
    assert called == [], called
    # Наблюдения, сделанные при сдаче, очередь прочитала — не спросив сеть.
    assert {r.sha_check for r in queue.rows} == {"match"}


# ---- AC-3: три читателя, один сборщик, порядок готовности ----


async def test_every_surface_reads_one_queue(
    client: AsyncClient,
    db: aiosqlite.Connection,
    git: _Git,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting = await _submitted(client, git, "Waits for a report", "w1")
    with_findings = await _submitted(client, git, "Has findings", "h1")
    ready = await _submitted(client, git, "Ready to approve", "r1")
    await _report(db, with_findings, confirmed=[_FINDING])
    await _report(db, ready)

    resp = await client.get("/api/review-queue")
    assert resp.status_code == 200, resp.text
    api = resp.json()
    order = [r["task_id"] for r in api["rows"]]
    assert order == [ready, with_findings, waiting], (
        "сначала готовые к одобрению, потом с находками, потом ждущие отчёта"
    )
    assert [r["readiness"] for r in api["rows"]] == [
        "ready",
        "findings",
        "awaiting_report",
    ]

    from hub import cli

    out = StringIO()
    with (
        patch.object(cli, "_api", MagicMock(return_value=api)) as cli_api,
        patch("sys.stdout", new=out),
    ):
        args = cli.build_parser().parse_args(["review-queue"])
        assert args.func(args) == 0
    assert cli_api.call_args.args[:2] == ("GET", "/api/review-queue")
    text = out.getvalue()
    positions = [text.index(f"#{task_id} ") for task_id in order]
    assert positions == sorted(positions), text

    page = await client.get("/review-queue")
    assert page.status_code == 200, page.text
    html = page.text
    positions = [html.index(f"#{task_id} ") for task_id in order]
    assert positions == sorted(positions)

    # Один сборщик: подменённый ответ видят обе серверные поверхности.
    from hub.models import ReviewQueueView

    sentinel = ReviewQueueView(rows=[], note="подменено сборщиком")
    monkeypatch.setattr(review_queue, "review_queue", AsyncMock(return_value=sentinel))
    assert (await client.get("/api/review-queue")).json()["note"] == sentinel.note
    assert sentinel.note in (await client.get("/review-queue")).text
