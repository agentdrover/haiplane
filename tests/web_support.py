from __future__ import annotations

import pytest
from httpx import AsyncClient
from hub import repository as repo


@pytest.fixture(autouse=True)
def _form_carries_policy_version(request):
    """Настоящая страница всегда несёт скрытое policy_version (#1593).

    Тесты формы политики шлют то, что отправил бы браузер: текущую версию.
    Явно переданное (в том числе пустое) значение не трогается — отказ без
    версии проверяет test_web_form_without_policy_version_is_refused.
    """
    if "client" not in request.fixturenames or "db" not in request.fixturenames:
        yield
        return
    client = request.getfixturevalue("client")
    db = request.getfixturevalue("db")
    original = client.post

    async def post(url, *args, **kwargs):
        data = kwargs.get("data")
        if (
            isinstance(data, dict)
            and str(url).endswith("/web-edit")
            and "policy_version" not in data
            and any(str(k).startswith("gate_policy_") for k in data)
        ):
            from hub.services import policy_change

            await db.commit()
            row = await repo.get_project(db, int(str(url).split("/")[2]))
            kwargs["data"] = {
                **data,
                "policy_version": policy_change.policy_version(row),
            }
        return await original(url, *args, **kwargs)

    client.post = post
    yield
    client.post = original


# ---- Web review verdict panel (#321) ----


async def _web_task_in_review(client: AsyncClient) -> int:
    resp = await client.post("/api/tasks", json={"title": "Web review task"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.json()["status"] == "review"
    return task_id


# ---- Draft queue ranking (#253) ----


async def _draft_with_readiness(client: AsyncClient, title: str, *, ready: bool) -> int:
    resp = await client.post(
        "/api/tasks", json={"title": title, "source": "agent", "agent": "bot"}
    )
    task_id = resp.json()["id"]
    if ready:
        resp = await client.post(
            f"/api/tasks/{task_id}/refine",
            json={
                "work_type": "feature",
                "user_story": "as a user, I want X so that Y",
                "problem_statement": "ps",
                "business_value": "bv",
                "scope_in": ["m"],
                "validation_commands": ["uv run pytest -q"],
                "size": "S",
                "wip_tag": "feature_work",
                "affected_areas": ["hub/services/dor.py"],
                "acceptance_criteria": [
                    {
                        "id": "AC-1",
                        "given": "g",
                        "when": "w",
                        "then": "t",
                        "verifiable_by": "test",
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text
    return task_id


def _web_project_tokens():
    from hub.config import TokenIdentity

    return {
        "agent-token": TokenIdentity("bot", "agent"),
        "human-token": TokenIdentity("op", "human"),
    }


# --- The verdict gate reads a report, not a diff (#808) ----------------------
#
# The owner does not read code at this gate, so what the panel says IS the
# decision's basis — and what it leaves out silently is the failure mode.


async def _machine_report(client: AsyncClient, task_id: int, **overrides) -> None:
    body = {
        "harness_skill": "lite-diff-review",
        "agent_count": 1,
        "tokens_spent": 12000,
        "model": "grok-4.6",
        "raw_count": 2,
        "findings_confirmed": [
            {
                "locator": "file",
                "title": "boundary lost",
                "severity": "medium",
                "file": "hub/a.py",
            }
        ],
        "findings_rejected": [
            {"title": "style nit", "category": "style", "reason": "не дефект"}
        ],
        "incomplete": False,
        "unresolved": [],
        "lost_dimensions": [],
        "agent": "cursor-cloud-reviewer",
    }
    body.update(overrides)
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=body)
    assert resp.status_code == 200, resp.text


# ---- #823: the evidence panel at the verdict gate ----


async def _web_task_in_review_with_test_ac(client: AsyncClient, db) -> int:
    """A submitted task carrying one test-AC with a recorded result."""
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "панель собрана",
                    "when": "открыта карточка",
                    "then": "критерий виден со своей проверкой",
                    "verifiable_by": "test",
                    "test_ref": "tests/test_web.py::test_evidence_panel_renders_at_the_verdict_gate",
                }
            ]
        },
    )
    row = await repo_module.get_task(db, task_id)
    await repo_module.upsert_ac_test_result(
        db,
        task_id=task_id,
        ac_id="AC-1",
        generation=dict(row)["submission_generation"],
        status="pass",
    )
    await db.commit()
    return task_id


# --- Форма и запись спрашивают одно: чем ревью можно добыть (#1188) ----------
#
# До #1180 ответ сводился к форжу, и каждый спрашивал его сам. #1180 доставила
# локальный путь, работающий на любом форже, — и инвариант записи, оставшийся
# на прежнем признаке, стал отказывать В ЗАПИСИ политике, которую хаб уже умел
# ИСПОЛНЯТЬ. Форма же предлагала dispatch всем подряд, не спросив никого.


def _card(page: str, slug: str) -> str:
    """Кусок разметки от карточки этого проекта до начала следующей.

    Резать приходится потому, что утверждения здесь — о МЕСТЕ: «предлагается
    на этом проекте» и «отказ показан у этой карточки» на целой странице
    неотличимы от «есть где-то на странице», а это ровно тот дефект, который
    задача закрывает.
    """
    start = page.index(slug)
    tail = page[start:]
    nxt = tail.find("project-admin-actions", tail.find("project-admin-actions") + 1)
    return tail if nxt < 0 else tail[:nxt]
