"""Эталоны ответов четырёх функций чтения и показа (#1691).

Рефакторинг api_task_context, hub_prepare_developer_task, _web_task_detail_page
и hub_project_status не меняет ответов. Здесь снимаются сами ответы на
фиксированных входах: REST — статус, заголовки, тело; HTML карточки — статус,
cookie, разметка; MCP — текст и structuredContent. Эталон лежит в
``tests/golden/read_views_1691.json`` и снят на коде ДО правки.

Фиксируется всё, что порождает тест: токен CSRF, время, git и доставка.
Единственная нормализация — метки времени (ISO) и ``prepared_at``: часы базы и
MCP идут по настоящему времени, а сами ответы от него зависят только значением.

Обновить эталон намеренно: ``HUB_UPDATE_READ_VIEW_BASELINE=1 uv run pytest
tests/test_api_context.py::test_context_and_views_match_the_pre_refactor_baseline``.
Менять его ради зелёного теста нельзя — тест и есть защита от изменения ответов.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

from hub import repository as repo
from hub.auth import CSRF_COOKIE_NAME

_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?"
)
_ID_FIXED_CSRF = "csrf-fixed-1691"


def norm(text: str) -> str:
    return _TS.sub("<TS>", text)


def _rest(resp) -> dict[str, Any]:
    headers = {
        k: v for k, v in sorted(resp.headers.items()) if k not in ("date", "server")
    }
    body = resp.text
    return {"status": resp.status_code, "headers": headers, "body": norm(body)}


def _html(resp) -> dict[str, Any]:
    cookie = resp.headers.get("set-cookie", "")
    return {
        "status": resp.status_code,
        "set_cookie": norm(cookie),
        "headers": {
            k: v for k, v in sorted(resp.headers.items()) if k not in ("date", "server")
        },
        "body": norm(resp.text),
    }


async def _create(client: AsyncClient, **body: Any) -> int:
    resp = await client.post("/api/tasks", json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


_FULL_REFINE = {
    "work_type": "feature",
    "user_story": "как оператор, хочу X, чтобы Y",
    "problem_statement": "проблема постановки",
    "business_value": "ценность",
    "outcome_metric": "метрика",
    "outcome_indicator": "ведущий показатель",
    "outcome_deadline": "2026-12-01",
    "outcome_revisit_condition": "если метрика не сдвинулась",
    "redesign_decision": "adapt",
    "redesign_rationale": "перенос этапов",
    "agent_fit": "sdd_native",
    "scope_in": ["блок один", "блок два"],
    "scope_out": ["чужое"],
    "validation_commands": ["uv run pytest -q", "uv run ruff check hub"],
    "size": "M",
    "wip_tag": "feature_work",
    "affected_areas": ["hub/app.py"],
    "risks": [
        {
            "kind": "security",
            "severity": "high",
            "description": "обход",
            "mitigation": "проверка",
        },
        {
            "kind": "breaking_change",
            "severity": "medium",
            "description": "контракт",
            "mitigation": "эталоны",
        },
    ],
    "acceptance_criteria": [
        {
            "id": "AC-1",
            "given": "g",
            "when": "w",
            "then": "t",
            "verifiable_by": "test",
            "test_ref": "tests/test_x.py::test_a",
        },
        {
            "id": "AC-2",
            "given": "g2",
            "when": "w2",
            "then": "t2",
            "verifiable_by": "manual",
        },
    ],
}


async def _submitted(client: AsyncClient, title: str, parent_id: int | None) -> int:
    task_id = await _create(client, title=title, parent_id=parent_id, task_type="task")
    r = await client.post(f"/api/tasks/{task_id}/refine", json=_FULL_REFINE)
    assert r.status_code == 200, r.text
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    r = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert r.json()["status"] == "review", r.text
    return task_id


async def snapshot_rest_context(client: AsyncClient, db) -> dict[str, Any]:
    from hub import services
    from hub.models import (
        FindingScope,
        ReviewFinding,
        ReviewSeverity,
        ReviewVerdict,
        TaskReviewVerdict,
    )

    out: dict[str, Any] = {}
    epic = await _create(
        client,
        title="Эпик-цель",
        task_type="epic",
        problem_statement="проблема эпика",
    )
    await client.post(
        f"/api/tasks/{epic}/refine",
        json={
            "problem_statement": "проблема эпика",
            "business_value": "ценность эпика",
        },
    )
    feature = await _create(client, title="Фича", task_type="feature", parent_id=epic)
    for i in range(7):
        await _create(client, title=f"Сосед {i}", task_type="task", parent_id=feature)
    task = await _submitted(client, "Целевая", feature)
    for i in range(10):
        await _create(client, title=f"Дитя {i}", task_type="subtask", parent_id=task)
    linked = await _create(client, title="Вынесенное")
    await services.record_review_verdict(
        db,
        task,
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested,
            agent="reviewer",
            comments="см. находки",
            findings=[
                ReviewFinding(id=1, severity=ReviewSeverity.high, message="Гонка"),
                ReviewFinding(
                    id=2,
                    severity=ReviewSeverity.low,
                    message="Вне объёма",
                    scope=FindingScope.out_of_scope,
                    linked_task_id=linked,
                ),
                ReviewFinding(
                    id=3,
                    severity=ReviewSeverity.low,
                    message="Вне объёма без ссылки",
                    scope=FindingScope.out_of_scope,
                ),
            ],
        ),
    )
    await db.commit()
    for name, query in (
        ("full", ""),
        ("summary", "?mode=summary"),
        ("full_truncated", "?max_chars=900"),
        ("summary_truncated", "?mode=summary&max_chars=700"),
    ):
        out[f"rest_filled_{name}"] = _rest(
            await client.get(f"/api/tasks/{task}/context{query}")
        )
    out["rest_epic_full"] = _rest(await client.get(f"/api/tasks/{epic}/context"))

    bare = await _create(client, title="Голая")
    out["rest_bare_full"] = _rest(await client.get(f"/api/tasks/{bare}/context"))
    out["rest_bare_summary"] = _rest(
        await client.get(f"/api/tasks/{bare}/context?mode=summary")
    )
    out["rest_missing"] = _rest(await client.get("/api/tasks/999999/context"))

    # summary под большой постановкой
    big = await _create(client, title="Большая")
    await client.post(
        f"/api/tasks/{big}/refine",
        json={
            "problem_statement": "п" * 2000,
            "business_value": "б" * 500,
            "user_story": "у" * 1000,
        },
    )
    out["rest_big_summary"] = _rest(
        await client.get(f"/api/tasks/{big}/context?mode=summary&max_chars=1500")
    )
    huge = await _create(client, title="Огромная")
    refined = await client.post(
        f"/api/tasks/{huge}/refine",
        json={
            "problem_statement": "п" * 2000,
            "business_value": "б" * 500,
            "user_story": "у" * 1000,
            "scope_in": ["с" * 300] * 8,
            "scope_out": ["в" * 300] * 8,
        },
    )
    assert refined.status_code == 200, refined.text
    out["rest_huge_summary_default_cap"] = _rest(
        await client.get(f"/api/tasks/{huge}/context?mode=summary")
    )
    return out


@contextmanager
def _frozen_card(*, delivery: str = "real", files: str = "read", offers: bool = False):
    """Карточка без случайности: токен CSRF фиксирован, git и доставка заданы."""
    patches = [patch("hub.web.generate_csrf_token", return_value=_ID_FIXED_CSRF)]
    if delivery == "raises":
        patches.append(
            patch(
                "hub.services.delivery_state.delivery_state",
                new=AsyncMock(side_effect=RuntimeError("delivery down")),
            )
        )
    if files == "read":
        listing = {
            "state": "read",
            "reason": "",
            "files": [
                {"path": "hub/app.py", "added": 3, "removed": 1},
                {"path": "hub/a.py", "added": 1, "removed": 0},
            ],
        }
    else:
        listing = {"state": "unreadable", "reason": "git не отвечает", "files": []}
    patches.append(
        patch(
            "hub.services.task_diff.submission_files",
            new=AsyncMock(return_value=listing),
        )
    )
    if offers:
        for name in ("repair_offered", "merge_offered"):
            patches.append(
                patch(
                    f"hub.services.executor_launch.{name}",
                    new=AsyncMock(return_value=True),
                )
            )
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


async def snapshot_web_cards(client: AsyncClient, db) -> dict[str, Any]:
    out: dict[str, Any] = {}
    task = await _submitted(client, "Карточка", None)
    await _machine_reports(client, db, task)

    with _frozen_card():
        out["web_commit_review_read"] = _html(await client.get(f"/tasks/{task}"))
    with _frozen_card(files="unreadable"):
        out["web_commit_review_unreadable"] = _html(await client.get(f"/tasks/{task}"))
    with _frozen_card(delivery="raises"):
        out["web_commit_review_delivery_raises"] = _html(
            await client.get(f"/tasks/{task}")
        )
    with _frozen_card():
        client.cookies.set(CSRF_COOKIE_NAME, "csrf-from-cookie")
        out["web_commit_review_cookie"] = _html(await client.get(f"/tasks/{task}"))
        client.cookies.delete(CSRF_COOKIE_NAME)
        out["web_commit_bad_csrf_403"] = _html(
            await client.post(
                f"/tasks/{task}/web-implementer-start", data={"csrf_token": "wrong"}
            )
        )

    # Последний отчёт несёт подтверждённые находки: карточка читает их, а
    # карта изменений получает их же (отчёты выше пусты и скрывают эту ветку).
    filled = await _submitted(client, "Карточка с находками", None)
    r = await client.post(f"/api/tasks/{filled}/machine-review", json=_REPORT_BODY)
    assert r.status_code == 200, r.text
    with _frozen_card():
        out["web_commit_review_findings"] = _html(await client.get(f"/tasks/{filled}"))

    await repo.create_executor_run(
        db,
        task_id=filled,
        submission_generation=1,
        agent_id="agent-x",
        run_id="run-x",
        model="m-1",
    )
    await db.commit()
    with _frozen_card(offers=True):
        out["web_commit_review_findings_runs_offers"] = _html(
            await client.get(f"/tasks/{filled}")
        )

    plain = await _create(client, title="Простая")
    with _frozen_card():
        out["web_commit_open"] = _html(await client.get(f"/tasks/{plain}"))
        out["web_missing"] = _html(await client.get("/tasks/999999"))
    with _frozen_card(offers=True):
        out["web_commit_open_offers_without_runs"] = _html(
            await client.get(f"/tasks/{plain}")
        )
    plain_done = await _create(client, title="Завершённая")
    await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (plain_done,))
    await db.commit()
    with _frozen_card():
        out["web_commit_completed"] = _html(await client.get(f"/tasks/{plain_done}"))
    with _frozen_card(delivery="raises"):
        out["web_commit_completed_delivery_raises"] = _html(
            await client.get(f"/tasks/{plain_done}")
        )

    state = await _create(client, title="Состояние")
    await client.post(f"/api/tasks/{state}/refine", json={"result_kind": "state"})
    with _frozen_card():
        out["web_state_open"] = _html(await client.get(f"/tasks/{state}"))
    await db.execute("UPDATE tasks SET status='review' WHERE id=?", (state,))
    await db.commit()
    with _frozen_card():
        out["web_state_review"] = _html(await client.get(f"/tasks/{state}"))
    return out


async def _machine_reports(client: AsyncClient, db, task_id: int) -> None:
    body = _REPORT_BODY
    r = await client.post(f"/api/tasks/{task_id}/machine-review", json=body)
    assert r.status_code == 200, r.text
    await _extra_report_rows(client, db, task_id)


_REPORT_BODY = {
    "harness_skill": "lite-diff-review",
    "agent_count": 1,
    "tokens_spent": 12000,
    "model": "grok-4.6",
    "raw_count": 2,
    "findings_confirmed": [
        {
            "locator": "file",
            "title": "граница",
            "severity": "medium",
            "file": "hub/a.py",
        }
    ],
    "findings_rejected": [{"title": "nit", "category": "style", "reason": "не дефект"}],
    "incomplete": False,
    "unresolved": [],
    "lost_dimensions": [],
    "agent": "cursor-cloud-reviewer",
}


async def _extra_report_rows(client: AsyncClient, db, task_id: int) -> None:
    row = await repo.get_task(db, task_id)
    gen = dict(row)["submission_generation"]
    rid = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=gen,
        raw_count=2,
        tokens_spent=500,
        profile="deep",
        principal_id=11,
    )
    await db.execute(
        "UPDATE machine_reviews SET provider_tokens=? WHERE id=?", (777000, rid)
    )
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=gen,
        raw_count=2,
        principal_id=11,
        carried_from_review_id=rid,
    )
    await db.commit()


def _text(result: Any) -> str:
    if isinstance(result, str):
        return result
    return "\n".join(b.text for b in result.content)


def _dashboard(kind: str) -> dict[str, Any]:
    if kind == "empty":
        return {
            k: []
            for k in (
                "draft_tasks",
                "needs_info_tasks",
                "review_tasks",
                "needs_decision_tasks",
                "active_tasks",
                "open_prs",
                "recent_commits",
                "recent_decisions",
            )
        }

    def task(i: int, status: str, **extra: Any) -> dict[str, Any]:
        return {
            "id": i,
            "title": f"Задача {i}",
            "status": status,
            "task_type": "task",
            "runtime": "auto",
            "parent_id": None,
            **extra,
        }

    return {
        "draft_tasks": [task(1, "draft"), task(2, "draft", source="agent")],
        "needs_info_tasks": [task(3, "needs_info")],
        "review_tasks": [
            task(4, "review", review_cycle=0),
            task(5, "review", review_cycle=2),
        ],
        "needs_decision_tasks": [task(6, "needs_decision")],
        "active_tasks": [task(7, "running"), task(8, "open")],
        "open_prs": [
            {"number": 11, "title": "PR", "headRefName": "task-7/x"},
            {"number": 12, "title": "PR2"},
        ],
        "recent_commits": [
            {"sha": f"abc{i}", "message": f"сообщение {i}\nвторая строка " + "х" * 100}
            for i in range(7)
        ],
        "recent_decisions": [{"title": f"Решение {i}"} for i in range(7)] + [{}],
    }


_QUEUE = {
    "projects": [
        {
            "mode": "shadow",
            "project": "default",
            "summary": "следующая #7",
            "skipped": [{"task_id": 9, "detail": "зависит"}, {"task_id": 10}],
        },
        {"project": "other", "summary": None},
        "мусор",
    ]
}


async def snapshot_mcp(_unused=None) -> dict[str, Any]:
    from hub.mcp_server import hub_prepare_developer_task, hub_project_status

    out: dict[str, Any] = {}

    async def run_status(name: str, dashboard: dict, queue: Any) -> None:
        async def fake_get(path: str, *a: Any, **k: Any):
            if path == "/api/dashboard":
                return dashboard
            if isinstance(queue, Exception):
                raise queue
            return queue

        with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=fake_get)):
            res = await hub_project_status()
        out[f"mcp_status_{name}"] = {
            "text": norm(_text(res)),
            "structured": json.loads(
                json.dumps(res.structuredContent, ensure_ascii=False)
            ),
        }

    await run_status("filled", _dashboard("full"), _QUEUE)
    await run_status("empty", _dashboard("empty"), {"projects": []})
    await run_status(
        "empty_queue_error", _dashboard("empty"), RuntimeError("очередь недоступна")
    )
    await run_status(
        "filled_queue_error", _dashboard("full"), ValueError("плохой ответ")
    )
    await run_status("queue_not_dict", _dashboard("empty"), ["x"])

    risk = {
        "kind": "security",
        "severity": "medium",
        "description": "d",
        "mitigation": "m",
    }
    risk2 = {
        "kind": "performance",
        "severity": "low",
        "description": "d2",
        "mitigation": "m2",
    }
    ac = [
        {"id": "AC-1", "given": "g", "when": "w", "then": "t", "verifiable_by": "test"}
    ]
    fields = {
        "work_type": "feature",
        "size": "M",
        "problem_statement": "проблема",
        "business_value": "ценность",
        "scope_in": ["a"],
        "scope_out": ["b"],
        "affected_areas": ["hub/app.py"],
        "validation_commands": ["uv run pytest -q"],
        "review_checklist": ["пункт"],
        "acceptance_criteria": ac,
        "risks": [risk, risk2],
    }
    current = {
        "id": 25,
        "risks": [risk],
        "problem_statement": "старая",
        "validation_commands": [],
        "work_type": "feature",
    }
    readiness = {
        "score": 80,
        "dor_passed": False,
        "missing_required": ["scope_in", "size"],
        "recommendations": [1, 2],
    }

    async def prep(
        name: str,
        kwargs: dict,
        cur: dict = current,
        ready: dict = readiness,
        fail: str = "",
    ):
        calls: list[Any] = []

        async def get(path: str, *a: Any, **k: Any):
            calls.append(["GET", path])
            if path.endswith("/acceptance_criteria"):
                return [{"id": "AC-0"}]
            if path.endswith("/readiness"):
                return ready
            return dict(cur)

        async def post(path: str, body: Any = None, *a: Any, **k: Any):
            calls.append(
                ["POST", path, json.loads(norm(json.dumps(body, ensure_ascii=False)))]
            )
            if fail and path.endswith(fail):
                raise RuntimeError(f"отказ {fail}")
            if path.endswith("/refine"):
                return {"updated_columns": ["size", "problem_statement"]}
            return {}

        async def put(path: str, body: Any = None, *a: Any, **k: Any):
            calls.append(["PUT", path, body])
            if fail and path.endswith(fail):
                raise RuntimeError(f"отказ {fail}")
            return [{"id": "AC-1"}, {"id": "AC-2"}]

        with (
            patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=get)),
            patch("hub.mcp_server._api_post", new=AsyncMock(side_effect=post)),
            patch("hub.mcp_server._api_put", new=AsyncMock(side_effect=put)),
        ):
            try:
                result: Any = norm(
                    await hub_prepare_developer_task(task_id=25, **kwargs)
                )
            except Exception as exc:  # noqa: BLE001 - ошибка тоже часть ответа
                result = f"{type(exc).__name__}: {exc}"
        out[f"mcp_prepare_{name}"] = {"result": result, "calls": calls}

    await prep("apply_all_dedupe", fields)
    await prep("apply_all_append", {**fields, "risk_mode": "append"})
    await prep("apply_all_replace", {**fields, "risk_mode": "replace"})
    await prep("preview_all", {**fields, "mode": "preview"})
    await prep(
        "apply_ready",
        {"work_type": "docs"},
        ready={**readiness, "dor_passed": True, "missing_required": []},
    )
    await prep("apply_empty_current_no_fields", {}, cur={"id": 25})
    await prep("apply_empty_validation_explicit", {"validation_commands": []})
    await prep("apply_feature_default_wip", {"work_type": "feature"})
    await prep("apply_non_feature_no_wip", {"work_type": "bugfix", "analyst": "a2"})
    await prep(
        "preview_no_ac", {"risks": [risk], "mode": "preview", "risk_mode": "append"}
    )
    await prep("fail_refine", fields, fail="/refine")
    await prep("fail_acceptance_criteria", fields, fail="/acceptance_criteria")
    await prep("fail_risks", fields, fail="/risks")
    await prep("fail_updates", fields, fail="/updates")
    await prep("bad_mode", {"mode": "x"})
    await prep("bad_risk_mode", {"risk_mode": "x"})
    return out


async def snapshot_all(client: AsyncClient, db) -> dict[str, Any]:
    # Имя машины и адрес хаба зависят от того, где идёт прогон (CI, ноутбук):
    # эхо экземпляра в MCP-ответах закреплено.
    echo = {"instance": "local", "base_url": "http://hub.test", "server_id": "host-1"}
    out: dict[str, Any] = {}
    with patch("hub.hub_instance.instance_echo_fields", return_value=echo):
        out.update(await snapshot_rest_context(client, db))
        out.update(await snapshot_web_cards(client, db))
        out.update(await snapshot_mcp())
    return out
