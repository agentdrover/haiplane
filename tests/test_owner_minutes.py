"""Оценка минут владельца, шаг 1 (#1874): журнал начислений по решениям человека.

Хаб считает минуты владельца ПО НОРМАТИВАМ: каждое записанное решение человека
на гейте (approve постановки, review-verdict, decide, answer, запуск исполнителя)
даёт одно начисление. Начисления строит поллер из событий гейтов; сам гейт
журнал не пишет. Здесь проверены пять свойств:

* AC-1 — на каждое учитываемое событие ровно одно начисление, по каждой
  поверхности (REST, веб-форма, HTMX, batch, compatibility);
* AC-2 — нечеловеческий автор начислений не даёт, права гейтов прежние;
* AC-3 — пас идемпотентен и перезапускаем, очистка events не съедает хвост;
* AC-4 — суммы по задаче и по локальному дню, версия нормативов;
* AC-5 — карточка, REST, MCP и CLI показывают одно и то же число.

Тесты ходят через публичные двери (REST, веб-форма, пас поллера из ``SWEEPS``,
таблицы журнала), а не через внутренние функции нового модуля.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.integrations import cursor_cloud
from hub.services import admin as admin_svc
from hub.services import chat_pair as cp
from tests.state_support import make_state_task

AGENT_TOKEN = "agent-token-1874"  # pragma: allowlist secret
ENV_HUMAN_TOKEN = "env-human-token-1874"  # pragma: allowlist secret
#: Не похоже на настоящий ключ нарочно: secret_scan читает дерево.
_FAKE_KEY = "not-a-real-cursor-key-1874"

#: Нормативы первой версии (секунды на действие) — из постановки.
NORMS = {
    "approve": 180,
    "review_verdict": 300,
    "decide": 240,
    "answer": 180,
    "launch": 60,
}


@pytest.fixture(autouse=True)
def _cursor_key(monkeypatch):
    monkeypatch.setattr(config, "CURSOR_API_KEY", _FAKE_KEY)


@pytest.fixture(autouse=True)
def _clean_limiter():
    cp.chat_pair_limiter._buckets.clear()
    yield
    cp.chat_pair_limiter._buckets.clear()


def _make_key() -> str:
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


@pytest.fixture(scope="module")
def host_keys() -> SimpleNamespace:
    return SimpleNamespace(k8=_make_key(), k9=_make_key())


# ---------------------------------------------------------------------------
# Мир: двое людей, машинный принципал-админ, агент, env-человек без принципала
# ---------------------------------------------------------------------------


async def _rows(db, sql: str, params: tuple = ()) -> list[dict]:
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


def _bearer(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


@pytest.fixture
async def world(client, db, monkeypatch):
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            AGENT_TOKEN: TokenIdentity("bot", "agent"),
            # Человек по роли, но без принципала в базе: автора не восстановить.
            ENV_HUMAN_TOKEN: TokenIdentity("env-denis", "human"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    from hub.db import seed_default_skills

    await seed_default_skills(db)
    owner = await admin_svc.create_principal(
        db, kind="human", username="owner-1874", role_slug="operator"
    )
    second = await admin_svc.create_principal(
        db, kind="human", username="second-1874", role_slug="operator"
    )
    machine = await admin_svc.create_principal(
        db, kind="service", username="svc-admin-1874", role_slug="admin"
    )
    keys = {}
    for name, principal in (("owner", owner), ("second", second), ("machine", machine)):
        keys[name] = await admin_svc.create_api_key(db, principal["id"], name=name)
    await db.commit()
    world = SimpleNamespace(
        client=client,
        db=db,
        owner_id=int(owner["id"]),
        second_id=int(second["id"]),
        machine_id=int(machine["id"]),
        human=_bearer(keys["owner"]["plaintext_key"]),
        second=_bearer(keys["second"]["plaintext_key"]),
        machine=_bearer(keys["machine"]["plaintext_key"]),
        agent=_bearer(AGENT_TOKEN),
        env_human=_bearer(ENV_HUMAN_TOKEN),
    )
    # Учёт включён до тестовых действий: первый проход создаёт курсор, и история
    # до него не считается (сама инициализация проверена в AC-3).
    await _pass(db)
    return world


async def _pass(db) -> None:
    """Один проход поллера для учёта минут — ровно тот, что стоит в ``SWEEPS``."""
    from hub.poller import SWEEPS

    sweep = next(s for s in SWEEPS if s.name == "owner_minutes")
    await sweep.run(db)


async def _accruals(db) -> list[dict]:
    return await _rows(
        db,
        "SELECT task_id, principal_id, kind, seconds, norms_version, source_event_id,"
        " occurred_at FROM owner_minute_accruals ORDER BY source_event_id",
    )


def _by(rows: list[dict]) -> list[tuple[int, str]]:
    return sorted((r["task_id"], r["kind"]) for r in rows)


async def _mk_task(db, status: str, title: str = "t", **updates) -> int:
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status=status,
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    if updates:
        await repo.update_task(db, task_id, **updates)
    await db.commit()
    return task_id


_REFINE = {
    "work_type": "feature",
    "user_story": "as a user, I want X so that Y",
    "problem_statement": "ps",
    "business_value": "bv",
    "scope_in": ["a"],
    "validation_commands": ["uv run pytest"],
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
}


async def _ready_draft(world, title: str = "draft") -> int:
    """Черновик с пройденным DoR: одобряется без force."""
    created = await world.client.post(
        "/api/tasks",
        json={"title": title, "source": "agent", "agent": "test"},
        headers=world.human,
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]
    refined = await world.client.post(
        f"/api/tasks/{task_id}/refine", json=_REFINE, headers=world.human
    )
    assert refined.status_code == 200, refined.text
    return task_id


# ---------------------------------------------------------------------------
# Подставки запусков исполнителя (облако, слот, повтор, слияние)
# ---------------------------------------------------------------------------

_CREATED = {"agent": {"id": "bc-exec-9", "latestRunId": "run-9"}, "run": {}}


def _executor_config(monkeypatch) -> list[dict]:
    monkeypatch.setattr(config, "EXECUTOR_MODEL", "claude-4.6-sonnet")
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL", "")
    monkeypatch.setattr(config, "STEWARD_MODEL", "gpt-5.3-codex")
    monkeypatch.setattr(config, "EXECUTOR_LAUNCH_PAUSE_S", 0)
    monkeypatch.setattr(config, "EXECUTOR_LAUNCH_MAX_ATTEMPTS", 3)
    calls: list[dict] = []

    async def _create(**kw):
        calls.append(kw)
        return _CREATED, None

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", _create)
    return calls


async def _cloud_project(db, slug: str) -> tuple[dict, int]:
    from tests.test_executor_dispatch import _launch_project

    return await _launch_project(db, slug=slug)


async def _launch_cloud_rest(world, slug: str) -> int:
    project, task_id = await _cloud_project(world.db, slug)
    resp = await world.client.post(
        f"/api/projects/{project['slug']}/executor-launch", headers=world.human
    )
    assert resp.status_code == 200, resp.text
    return task_id


async def _launch_cloud_web(world, slug: str) -> int:
    project, task_id = await _cloud_project(world.db, slug)
    resp = await world.client.post(
        f"/projects/{project['slug']}/web-executor-launch",
        data={"target": "cloud"},
        headers=world.human,
    )
    assert resp.status_code in (200, 303), resp.text
    assert dict(await repo.get_task(world.db, task_id)), "задача на месте"
    assert await repo.list_executor_runs(world.db, task_id), "прогон заказан"
    return task_id


async def _slot_world(world, host_keys, slug: str) -> dict:
    """Проект manual, два хоста; возвращает проект."""
    pid = await repo.create_project(world.db, slug=slug, name=slug)
    await repo.update_project(
        world.db,
        pid,
        gate_policy=json.dumps({"executor_launch": "manual"}),
        forge="github",
        repo="agentdrover/haiplane",
    )
    await world.db.commit()
    for name, pem in (("k8", host_keys.k8), ("k9", host_keys.k9)):
        created = await world.client.post(
            "/api/admin/executor-hosts",
            json={"name": name, "public_key_pem": pem},
            headers=world.human,
        )
        assert created.status_code == 200, created.text
    return {"id": pid, "slug": slug}


async def _slot_task(world, project: dict, title: str) -> int:
    task_id = await _mk_task(world.db, "open", title)
    await repo.update_task(
        world.db, task_id, project_id=project["id"], branch=f"task-{task_id}/work"
    )
    await world.db.commit()
    return task_id


# ---------------------------------------------------------------------------
# AC-1: одно начисление на каждое учитываемое событие, по каждой поверхности
# ---------------------------------------------------------------------------


async def test_each_counted_gate_accrues_once_per_surface(
    world, db, monkeypatch, host_keys
):
    """AC-1: матрица действие × поверхность; сопутствующие события не считаются."""
    client = world.client
    calls = _executor_config(monkeypatch)
    expected: list[tuple[int, str]] = []
    human = world.human
    htmx = {**human, "HX-Request": "true"}

    # --- approve: одиночный (REST, веб), batch (REST, веб), compatibility ----
    rest_approve = await _ready_draft(world, "approve REST")
    resp = await client.post(
        f"/api/tasks/{rest_approve}/approve", json={}, headers=human
    )
    assert resp.status_code == 200, resp.text
    expected.append((rest_approve, "approve"))

    web_approve = await _ready_draft(world, "approve web")
    resp = await client.post(
        f"/tasks/{web_approve}/web-approve", data={"comment": ""}, headers=human
    )
    assert resp.status_code in (200, 303), resp.text
    assert dict(await repo.get_task(db, web_approve))["status"] == "open"
    expected.append((web_approve, "approve"))

    batch_rest = [await _ready_draft(world, f"batch REST {i}") for i in range(2)]
    resp = await client.post(
        "/api/tasks/batch-approve", json={"task_ids": batch_rest}, headers=human
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["approved"]) == sorted(batch_rest)
    expected += [(t, "approve") for t in batch_rest]

    batch_web = [await _ready_draft(world, f"batch web {i}") for i in range(2)]
    resp = await client.post(
        "/tasks/web-batch-approve-selected",
        data={"task_ids": batch_web},
        headers=htmx,
    )
    assert resp.status_code in (200, 303), resp.text
    for t in batch_web:
        assert dict(await repo.get_task(db, t))["status"] == "open"
    expected += [(t, "approve") for t in batch_web]

    compat_rest = await _ready_draft(world, "compat REST")
    resp = await client.post(
        f"/api/proposals/{compat_rest}/action",
        json={"action": "approved"},
        headers=human,
    )
    assert resp.status_code == 200, resp.text
    expected.append((compat_rest, "approve"))

    compat_web = await _ready_draft(world, "compat web")
    resp = await client.post(
        f"/proposals/{compat_web}/approve", data={"comment": ""}, headers=human
    )
    assert resp.status_code in (200, 303), resp.text
    expected.append((compat_web, "approve"))

    # --- review-verdict: commit (REST, веб, HTMX) и state (REST, веб) --------
    for label, headers, path in (
        ("verdict REST", human, "rest"),
        ("verdict web", human, "web"),
        ("verdict htmx", htmx, "web"),
    ):
        task_id = await _mk_task(
            db,
            "review",
            label,
            submission_generation=1,
            submission_sha="a" * 40,
        )
        if path == "rest":
            resp = await client.post(
                f"/api/tasks/{task_id}/review-verdict",
                json={
                    "verdict": "changes_requested",
                    "comments": "поправить",
                    "agent": "owner",
                },
                headers=headers,
            )
        else:
            resp = await client.post(
                f"/tasks/{task_id}/web-review-verdict",
                data={"verdict": "changes_requested", "comments": "поправить"},
                headers=headers,
            )
        assert resp.status_code in (200, 303), resp.text
        assert dict(await repo.get_task(db, task_id))["status"] == "running"
        expected.append((task_id, "review_verdict"))

    for label, path in (("state REST", "rest"), ("state web", "web")):
        task_id = await make_state_task(db, title=label, status="review")
        await repo.update_task(db, task_id, submission_generation=1)
        await db.commit()
        if path == "rest":
            resp = await client.post(
                f"/api/tasks/{task_id}/review-verdict",
                json={
                    "verdict": "changes_requested",
                    "comments": "нужны правки",
                    "agent": "owner",
                    "expected_generation": 1,
                },
                headers=human,
            )
        else:
            resp = await client.post(
                f"/tasks/{task_id}/web-review-verdict",
                data={
                    "verdict": "changes_requested",
                    "comments": "нужны правки",
                    "expected_generation": "1",
                },
                headers=human,
            )
        assert resp.status_code in (200, 303), resp.text
        assert dict(await repo.get_task(db, task_id))["status"] == "running"
        expected.append((task_id, "review_verdict"))

    # state APPROVED завершает задачу: task_completed рядом не считается.
    state_done = await make_state_task(db, title="state approved", status="review")
    await repo.update_task(db, state_done, submission_generation=1)
    await db.commit()
    resp = await client.post(
        f"/api/tasks/{state_done}/review-verdict",
        json={"verdict": "approved", "agent": "owner", "expected_generation": 1},
        headers=human,
    )
    assert resp.status_code == 200, resp.text
    expected.append((state_done, "review_verdict"))

    # --- decide: accept и rework, REST, веб, HTMX ----------------------------
    for label, body, via in (
        ("decide accept REST", {"action": "accept"}, "rest"),
        ("decide rework REST", {"action": "rework", "instructions": "fix"}, "rest"),
        ("decide accept web", {"action": "accept"}, "web"),
        ("decide rework web", {"action": "rework", "instructions": "fix"}, "web"),
        ("decide accept htmx", {"action": "accept"}, "htmx"),
    ):
        task_id = await _mk_task(db, "needs_decision", label)
        if via == "rest":
            resp = await client.post(
                f"/api/tasks/{task_id}/decide", json=body, headers=human
            )
        else:
            resp = await client.post(
                f"/tasks/{task_id}/web-decide",
                data=body,
                headers=htmx if via == "htmx" else human,
            )
        assert resp.status_code in (200, 303), resp.text
        expected.append((task_id, "decide"))

    # --- answer: REST, веб, HTMX; resume и без --------------------------------
    for label, via, resume in (
        ("answer REST", "rest", True),
        ("answer REST no resume", "rest", False),
        ("answer web", "web", True),
        ("answer htmx", "htmx", True),
    ):
        task_id = await _mk_task(db, "needs_info", label)
        if via == "rest":
            resp = await client.post(
                f"/api/tasks/{task_id}/answer",
                json={"answer": "да", "resume": resume},
                headers=human,
            )
        else:
            resp = await client.post(
                f"/tasks/{task_id}/web-answer",
                data={"answer": "да", "resume": "true"},
                headers=htmx if via == "htmx" else human,
            )
        assert resp.status_code in (200, 303), resp.text
        expected.append((task_id, "answer"))

    # --- запуск: облако REST/веб, слот REST/веб, повтор и слияние REST/веб ----
    expected.append((await _launch_cloud_rest(world, "ac1-cloud-rest"), "launch"))
    expected.append((await _launch_cloud_web(world, "ac1-cloud-web"), "launch"))
    assert len(calls) == 2

    from tests.test_executor_dispatch import (
        _task_in_base_conflict,
        _task_with_findings,
    )

    for slug, via in (("ac1-repair-rest", "rest"), ("ac1-repair-web", "web")):
        _, task_id = await _task_with_findings(db, slug=slug)
        if via == "rest":
            resp = await client.post(
                f"/api/tasks/{task_id}/executor-repair", headers=human
            )
        else:
            resp = await client.post(
                f"/tasks/{task_id}/web-executor-repair", headers=human
            )
        assert resp.status_code in (200, 303), resp.text
        expected.append((task_id, "launch"))
    for slug, via in (("ac1-merge-rest", "rest"), ("ac1-merge-web", "web")):
        _, task_id = await _task_in_base_conflict(db, slug=slug)
        if via == "rest":
            resp = await client.post(
                f"/api/tasks/{task_id}/executor-merge", headers=human
            )
        else:
            resp = await client.post(
                f"/tasks/{task_id}/web-executor-merge", headers=human
            )
        assert resp.status_code in (200, 303), resp.text
        expected.append((task_id, "launch"))

    slot_project = await _slot_world(world, host_keys, "ac1-slot")
    rest_slot = await _slot_task(world, slot_project, "slot REST")
    resp = await client.post(
        f"/api/projects/{slot_project['slug']}/executor-launch",
        json={"target": "slot:k8", "task_id": rest_slot},
        headers=human,
    )
    assert resp.status_code == 200, resp.text
    expected.append((rest_slot, "launch"))
    web_slot = await _slot_task(world, slot_project, "slot web")
    resp = await client.post(
        f"/tasks/{web_slot}/web-executor-slot", data={"host": "k9"}, headers=human
    )
    assert resp.status_code in (200, 303), resp.text
    expected.append((web_slot, "launch"))

    # Отказ ДО заказа начисления не даёт: проект без политики manual.
    off = await repo.create_project(db, slug="ac1-off", name="off")
    await db.commit()
    refused = await client.post("/api/projects/ac1-off/executor-launch", headers=human)
    assert refused.status_code == 409, refused.text
    del off

    # --- пас: ровно одно начисление на событие, с нормативом своего типа ------
    await _pass(db)
    rows = await _accruals(db)
    assert _by(rows) == sorted(expected)
    assert len({r["source_event_id"] for r in rows}) == len(rows)
    for row in rows:
        assert row["principal_id"] == world.owner_id
        assert row["seconds"] == NORMS[row["kind"]], row
    # Повторный пас ничего не добавляет.
    await _pass(db)
    assert _by(await _accruals(db)) == sorted(expected)
    # Сопутствующее task_completed (decide accept, state APPROVED) в журнал не попало.
    completed = await _rows(db, "SELECT id FROM events WHERE kind='task_completed'")
    assert completed
    ids = {r["source_event_id"] for r in rows}
    assert not ids & {c["id"] for c in completed}


# ---------------------------------------------------------------------------
# AC-2: нечеловеческие авторы начислений не дают
# ---------------------------------------------------------------------------


async def test_non_human_actors_never_accrue(world, db, monkeypatch):
    """AC-2: (а) разрешённое нечеловеческое, (б) запрещённое агенту, (в) подделки."""
    client = world.client
    _executor_config(monkeypatch)

    # (а) policy-автоодобрение: результат прежний (задача открыта), начисления нет.
    from tests.test_auto_approve import _draft_in_project, _project, _refine_to_dor

    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", True)
    pid = await _project(db, "spike-1874", {"dor": "auto"})
    auto_task = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, auto_task, ["docs/notes.md"])
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    assert body["status"] == "open", "автоодобрение работает как раньше"

    # (а) стюард применил вердикт, даже с принципалом человека в вызове; агент-
    # ревьюер поставил вердикт через REST: вердикт записан, начисления нет.
    from hub.models import ReviewVerdict, TaskReviewVerdict
    from hub.services.lifecycle import record_review_verdict

    steward_review = await _mk_task(
        db, "review", "steward", submission_generation=1, submission_sha="a" * 40
    )
    await record_review_verdict(
        db,
        steward_review,
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested, agent="steward", comments="c"
        ),
        principal_id=world.owner_id,
        applied_by_steward=True,
    )
    agent_review = await _mk_task(
        db, "review", "by agent", submission_generation=1, submission_sha="a" * 40
    )
    resp = await client.post(
        f"/api/tasks/{agent_review}/review-verdict",
        json={"verdict": "changes_requested", "comments": "c", "agent": "rev"},
        headers=world.agent,
    )
    assert resp.status_code == 200, resp.text
    assert dict(await repo.get_task(db, agent_review))["status"] == "running"

    # (а) автопродолжения хаба (только сдай, продолжение после ответа) заказаны,
    # но события «заказ принят» у них нет.
    from tests.test_executor_dispatch import _door_continue, _door_submit_only

    for door in (_door_submit_only, _door_continue):
        _, run = await door(db, world.owner_id)
        launched = await run()
        assert launched.launched, launched
    assert (
        await _rows(db, "SELECT id FROM events WHERE kind='executor_order_accepted'")
        == []
    )

    # (б) агент не проходит гейты: прежний отказ, ни решения, ни начисления.
    draft = await _ready_draft(world, "agent tries")
    waiting = await _mk_task(db, "needs_info", "agent answers")
    deciding = await _mk_task(db, "needs_decision", "agent decides")
    for url, body in (
        (f"/api/tasks/{draft}/approve", {}),
        ("/api/tasks/batch-approve", {"task_ids": [draft]}),
        (f"/api/tasks/{waiting}/answer", {"answer": "x"}),
        (f"/api/tasks/{deciding}/decide", {"action": "accept"}),
        (f"/api/proposals/{draft}/action", {"action": "approved"}),
        ("/api/projects/spike-1874/executor-launch", {}),
    ):
        refused = await client.post(url, json=body, headers=world.agent)
        assert refused.status_code == 403, (url, refused.text)
    assert dict(await repo.get_task(db, draft))["status"] == "draft"
    assert dict(await repo.get_task(db, waiting))["status"] == "needs_info"
    assert dict(await repo.get_task(db, deciding))["status"] == "needs_decision"

    # (в) подделки: чужое имя и principal в теле, машинный принципал с ролью
    # admin, env-токен человека без принципала.
    forged = await _ready_draft(world, "forged body")
    resp = await client.post(
        f"/api/tasks/{forged}/approve",
        json={"actor": "someone", "principal_id": 999, "human": True},
        headers=world.human,
    )
    assert resp.status_code == 200, resp.text
    machine_task = await _ready_draft(world, "machine admin")
    resp = await client.post(
        f"/api/tasks/{machine_task}/approve", json={}, headers=world.machine
    )
    assert resp.status_code == 200, resp.text
    env_task = await _ready_draft(world, "env human")
    resp = await client.post(
        f"/api/tasks/{env_task}/approve", json={}, headers=world.env_human
    )
    assert resp.status_code == 200, resp.text
    # Событие с подделанной личностью прямо в ленте: принципал не human в базе.
    await _event_at(db, world.machine_id, forged, "2026-10-19 09:00:00")
    await _event_at(db, 999, forged, "2026-10-19 09:00:01")

    await _pass(db)
    rows = await _accruals(db)
    assert [(r["task_id"], r["kind"], r["principal_id"]) for r in rows] == [
        (forged, "approve", world.owner_id)
    ], "начисление только настоящему человеку; остальные пути молчат"


# ---------------------------------------------------------------------------
# AC-3: идемпотентность и перезапуск
# ---------------------------------------------------------------------------


async def _human_event(db, owner_id: int, task_id: int, *, kind="task_approved") -> int:
    event_id = await repo.insert_event(
        db,
        kind=kind,
        task_id=task_id,
        actor="human",
        payload={"principal_id": owner_id, "human": True},
    )
    await db.commit()
    return event_id


async def test_accrual_pass_is_idempotent_and_restartable(
    world, db, db_dsn, monkeypatch
):
    """AC-3: история до включения, дубли, рестарт, падение и очистка events."""
    task_id = await _mk_task(db, "open", "old history")
    # Учёт ещё не включён: состояния нет, а история уже есть.
    await db.execute("DELETE FROM owner_minutes_state")
    await db.commit()
    await _human_event(db, world.owner_id, task_id)

    await _pass(db)  # первая инициализация
    state = (await _rows(db, "SELECT * FROM owner_minutes_state"))[0]
    top = (await _rows(db, "SELECT MAX(id) AS m FROM events"))[0]["m"]
    assert state["cursor_event_id"] == top and state["tracking_since"]
    assert await _accruals(db) == [], "история до tracking_since не начислена"

    # Событие в момент включения: после инициализации — считается.
    first = await _human_event(db, world.owner_id, task_id)
    await _pass(db)
    await _pass(db)
    assert [r["source_event_id"] for r in await _accruals(db)] == [first]

    # Рестарт: новое соединение к той же базе, состояние читается из БД.
    other = await aiosqlite.connect(db_dsn)
    other.row_factory = aiosqlite.Row
    try:
        second = await _human_event(db, world.owner_id, task_id)
        await _pass(other)
    finally:
        await other.close()
    assert [r["source_event_id"] for r in await _accruals(db)] == [first, second]

    # Падение между вставкой начислений и курсором, затем отмена coroutine:
    # в базе ни начислений, ни сдвига курсора.
    third = await _human_event(db, world.owner_id, task_id)
    cursor_before = (
        await _rows(db, "SELECT cursor_event_id FROM owner_minutes_state")
    )[0]["cursor_event_id"]
    for failure in (RuntimeError("boom"), asyncio.CancelledError()):
        with patch(
            "hub.repository_owner_minutes.advance_cursor",
            AsyncMock(side_effect=failure),
        ):
            with pytest.raises((RuntimeError, asyncio.CancelledError)):
                await _pass(db)
        await db.rollback()
        assert [r["source_event_id"] for r in await _accruals(db)] == [first, second]
        now = (await _rows(db, "SELECT cursor_event_id FROM owner_minutes_state"))[0]
        assert now["cursor_event_id"] == cursor_before

    # Падение паса, затем очистка events, затем восстановление: непрочитанный
    # хвост старше 14 дней пережил очистку и начислен позже.
    await db.execute(
        "UPDATE events SET created_at = datetime('now', '-30 days') WHERE id >= ?",
        (first,),
    )
    await db.commit()
    from hub.poller import _sweep_events_retention

    await _sweep_events_retention(db)
    survivors = {r["id"] for r in await _rows(db, "SELECT id FROM events")}
    assert third in survivors, "непрочитанный хвост очисткой не тронут"
    assert first not in survivors, "прочитанное старше 14 дней очищается как раньше"
    await _pass(db)
    assert [r["source_event_id"] for r in await _accruals(db)] == [
        first,
        second,
        third,
    ]

    # Пакеты по 200: курсор идёт по всем событиям, включая неучитываемые.
    noise = 450
    await db.executemany(
        "INSERT INTO events (kind, task_id, actor, payload) VALUES ('noise', ?, 'hub', '{}')",
        [(task_id,)] * noise,
    )
    tail = await _human_event(db, world.owner_id, task_id)
    await _pass(db)
    assert (await _accruals(db))[-1]["source_event_id"] == tail
    state = (await _rows(db, "SELECT cursor_event_id FROM owner_minutes_state"))[0]
    assert state["cursor_event_id"] == tail

    # Записанные решения не изменились: задача на месте, события целы.
    assert dict(await repo.get_task(db, task_id))["status"] == "open"
    assert len(await _accruals(db)) == 4


# ---------------------------------------------------------------------------
# AC-4: суммы по задаче и локальному дню
# ---------------------------------------------------------------------------


async def _event_at(db, owner_id, task_id, created_at: str, kind="task_approved"):
    cursor = await db.execute(
        "INSERT INTO events (kind, task_id, actor, payload, created_at) "
        "VALUES (?, ?, 'human', ?, ?)",
        (
            kind,
            task_id,
            json.dumps({"principal_id": owner_id, "human": True}),
            created_at,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def test_sums_by_task_and_local_day(world, db, monkeypatch):
    """AC-4: день по локальной дате пояса, задача с разбивкой, версия нормативов."""
    client = world.client
    t1 = await _mk_task(db, "open", "sum 1")
    t2 = await _mk_task(db, "running", "sum 2")
    quiet = await _mk_task(db, "open", "no accruals")

    # 23:59:59 и 00:00:00 по Москве (UTC+3) вокруг локальной полуночи 18/19.10.
    await _event_at(db, world.owner_id, t1, "2026-10-18 20:59:59")
    await _event_at(db, world.owner_id, t1, "2026-10-18 21:00:00")
    # Второй человек на той же задаче.
    await _event_at(db, world.second_id, t1, "2026-10-19 08:00:00")
    await _event_at(db, world.owner_id, t2, "2026-10-19 09:00:00", kind="task_decided")
    await _pass(db)

    async def day(date: str, principal: int | None = None):
        query = f"?date={date}" + (f"&principal_id={principal}" if principal else "")
        resp = await client.get(f"/api/owner-minutes/day{query}", headers=world.human)
        assert resp.status_code == 200, resp.text
        return resp.json()

    d18 = await day("2026-10-18", world.owner_id)
    d19 = await day("2026-10-19", world.owner_id)
    assert (d18["total_seconds"], d18["accruals"]) == (180, 1)
    assert (d19["total_seconds"], d19["accruals"]) == (180 + 240, 2)
    assert d19["timezone"] == "Europe/Moscow"
    assert d19["status"] == "ok" and d19["caught_up"] is True
    assert d19["tracking_since"] and d19["processed_through_event_id"] > 0
    nobody = await day("2026-10-20", world.owner_id)
    assert nobody["total_seconds"] is None and nobody["status"] == "no_data"

    # Сумма по задаче: оба человека, с разбивкой по principal.
    resp = await client.get(f"/api/tasks/{t1}/owner-minutes", headers=world.human)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_seconds"] == 180 * 3 and body["accruals"] == 3
    split = {p["principal_id"]: p["seconds"] for p in body["by_principal"]}
    assert split == {world.owner_id: 360, world.second_id: 180}
    empty = (
        await client.get(f"/api/tasks/{quiet}/owner-minutes", headers=world.human)
    ).json()
    assert empty["total_seconds"] is None and empty["status"] == "no_data"

    # Смена часового пояса переносит день, но не переписывает начисления.
    before = await _accruals(db)
    monkeypatch.setattr(config, "OWNER_MINUTES_TIMEZONE", "UTC")
    utc19 = await day("2026-10-19", world.owner_id)
    assert utc19["timezone"] == "UTC" and utc19["total_seconds"] == 240
    assert (await day("2026-10-18", world.owner_id))["total_seconds"] == 360
    assert await _accruals(db) == before
    monkeypatch.setattr(config, "OWNER_MINUTES_TIMEZONE", "Europe/Moscow")

    # Смена версии нормативов не меняет старые начисления.
    from hub.services import owner_minutes

    monkeypatch.setattr(owner_minutes, "NORMS_VERSION", 2)
    monkeypatch.setitem(owner_minutes.NORMS, "approve", 600)
    await _event_at(db, world.owner_id, t1, "2026-10-19 10:00:00")
    await _pass(db)
    rows = [r for r in await _accruals(db) if r["task_id"] == t1]
    assert sorted((r["norms_version"], r["seconds"]) for r in rows) == [
        (1, 180),
        (1, 180),
        (1, 180),
        (2, 600),
    ]

    # Удалённая задача: сумма читается и входит в день.
    from hub import services

    await services.delete_task_tree(db, t1)
    gone = await client.get(f"/api/tasks/{t1}/owner-minutes", headers=world.human)
    assert gone.status_code == 200 and gone.json()["total_seconds"] == 180 * 3 + 600
    after = await day("2026-10-19", world.owner_id)
    assert after["total_seconds"] == 180 + 240 + 600


# ---------------------------------------------------------------------------
# AC-5: карточка, REST, MCP, CLI показывают одно и то же
# ---------------------------------------------------------------------------


async def test_card_rest_mcp_and_cli_show_the_same_estimate(world, db, capsys):
    """AC-5: одна оценка на всех поверхностях; нет данных и отставание — разные."""
    from hub import cli, mcp_server

    client = world.client
    t1 = await _mk_task(db, "open", "card with accruals")
    t2 = await _mk_task(db, "open", "card without")
    await _event_at(db, world.owner_id, t1, "2026-10-19 09:00:00")
    await _event_at(db, world.owner_id, t1, "2026-10-19 09:05:00", kind="task_decided")
    await _pass(db)

    rest = (await client.get(f"/api/tasks/{t1}", headers=world.human)).json()
    estimate = rest["owner_minutes"]
    assert estimate["total_seconds"] == 420 and estimate["status"] == "ok"
    assert estimate["caught_up"] is True
    assert (await client.get(f"/api/tasks/{t2}", headers=world.human)).json()[
        "owner_minutes"
    ]["status"] == "no_data"

    # HTML: строка с оценкой и подписью.
    page = await client.get(f"/tasks/{t1}", headers=world.human)
    assert page.status_code == 200
    text = " ".join(page.text.split())
    assert "оценка, с начала учёта" in text and "7 мин" in text
    quiet = " ".join(
        (await client.get(f"/tasks/{t2}", headers=world.human)).text.split()
    )
    assert "оценка, с начала учёта" in quiet and "нет данных" in quiet

    # MCP: compact и full — тот же объект, что в REST.
    for full in (False, True):
        with (
            patch.object(mcp_server, "_api_post", AsyncMock(return_value={})),
            patch.object(mcp_server, "_api_get", AsyncMock(return_value=rest)),
        ):
            out = await mcp_server.hub_task_status(t1, full=full)
        structured = out.structuredContent["task"]
        assert structured["owner_minutes"] == estimate
        assert "7 мин" in "\n".join(b.text for b in out.content)

    # CLI: status печатает тот же объект и строку; дневная команда — день.
    with patch.object(cli, "_api", return_value=rest):
        assert cli.cmd_status(argparse.Namespace(task_id=t1)) == 0
    captured = capsys.readouterr()
    assert json.dumps(estimate["total_seconds"]) in captured.out
    assert "7 мин" in captured.err
    day = (
        await client.get(
            f"/api/owner-minutes/day?date=2026-10-19&principal_id={world.owner_id}",
            headers=world.human,
        )
    ).json()
    with patch.object(cli, "_api", return_value=day) as api:
        rc = cli.cmd_owner_minutes_day(
            argparse.Namespace(principal_id=world.owner_id, date="2026-10-19")
        )
    assert rc == 0
    assert "/api/owner-minutes/day" in api.call_args.args[1]
    assert f"principal_id={world.owner_id}" in api.call_args.args[1]
    assert "date=2026-10-19" in api.call_args.args[1]
    assert "7 мин" in capsys.readouterr().out

    # Отставший пас: событие записано, а пас его ещё не прочитал.
    await _event_at(db, world.owner_id, t1, "2026-10-19 09:10:00")
    lagging = (await client.get(f"/api/tasks/{t1}", headers=world.human)).json()
    assert lagging["owner_minutes"]["status"] == "behind"
    assert lagging["owner_minutes"]["caught_up"] is False
    assert lagging["owner_minutes"]["status"] not in ("no_data", "ok")
