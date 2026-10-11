"""Общие подставки для тестов задачи-состояния (#1647).

Здесь нет ни одного импорта из нового кода хаба: файл обязан загружаться и на
кодовой базе ДО задачи, чтобы тесты падали на поведении (нет ветки/нет
доказательств/нет поколения у вердикта), а не на ImportError подставок.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.models import ACVerifiableBy, AcceptanceCriterion, TaskCreate

#: Метка, которую тесты кладут в «значения»: если она оказалась в ответе или в
#: логе при ошибке контракта, значит ошибка вернула вход.
MARKER = "ЗНАЧЕНИЕ-ИЗ-ЗАПРОСА-7731"

ROLLBACK = "Вернуть запись DNS на прежний адрес из карточки"


class GitSpy:
    """Подставка git-адаптера: ЛЮБОЕ обращение записывается и роняет вызов.

    Записывается именно обращение, а не успешный вызов: код, который ловит
    исключение и идёт дальше, всё равно оставит имя в ``calls``.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        self.calls.append(name)

        def _refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError(f"git-вызов у задачи-состояния: {name}")

        return _refuse


def human_and_agents() -> dict[str, TokenIdentity]:
    """Токены: исполнитель-агент, ревьюер-агент, человек (все с principal_id)."""
    return {
        "impl-token": TokenIdentity("dev", "agent", principal_id=7),
        "rev-token": TokenIdentity("reviewer", "agent", principal_id=9),
        "human-token": TokenIdentity("denis", "human", principal_id=8),
    }


def auth(monkeypatch) -> dict[str, dict[str, str]]:
    """Включить закрытый режим с тремя токенами; вернуть заголовки по ролям."""
    monkeypatch.setattr(config, "HUB_TOKENS", human_and_agents())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {
        "impl": {"Authorization": "Bearer impl-token"},
        "rev": {"Authorization": "Bearer rev-token"},
        "human": {"Authorization": "Bearer human-token"},
    }


def _ac(idx: int, kind: str = "manual", test_ref: str | None = None):
    return AcceptanceCriterion(
        id=f"AC-{idx}",
        given=f"given {idx}",
        when=f"when {idx}",
        then=f"then {idx}",
        verifiable_by=ACVerifiableBy(kind),
        test_ref=test_ref,
    )


async def _node(db, title: str, task_type: str, parent_id: int | None) -> int:
    return await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )


async def make_project(
    db: aiosqlite.Connection, slug: str, policy: dict | None = None
) -> int:
    pid = await repo.create_project(
        db, slug=slug, name=slug.title(), workspace_path="/tmp/ws"
    )
    if policy:
        await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    await db.commit()
    return pid


async def make_state_task(
    db: aiosqlite.Connection,
    *,
    title: str = "Переключить DNS",
    kinds: tuple[str, ...] = ("manual", "log_check"),
    rollback: str | None = ROLLBACK,
    project_id: int | None = None,
    under_feature: bool = False,
    status: str = "open",
    work_type: str = "feature",
    result_kind: str = "state",
    plan: bool = True,
) -> int:
    """Лист-задача с полной базовой постановкой, rollback и AC.

    ``project_id=None`` — задача без эпика (проект default). С проектом задача
    сидит под фичей под эпиком, как на проде; ``under_feature`` без проекта
    ставит ей только фичу-родителя (для свёртки родителя).
    """
    parent_id: int | None = None
    if project_id is not None:
        epic = await _node(db, "epic", "epic", None)
        await repo.update_task(db, epic, project_id=project_id)
        parent_id = await _node(db, "feature", "feature", epic)
    elif under_feature:
        parent_id = await _node(db, "feature", "feature", None)
    payload = TaskCreate(
        title=title,
        task_type="task",
        parent_id=parent_id,
        work_type=work_type,
        user_story="Как владелец хочу, чтобы DNS смотрел на новый сервер",
        problem_statement="Старый адрес уходит из эксплуатации",
        business_value="Переезд без простоя",
        scope_in=["запись A у регистратора"],
        size="S",
        wip_tag="feature_work",
        result_kind=result_kind,
        rollback=rollback or "",
    )
    task_id = await repo.create_task_full(db, payload, status=status)
    for idx, kind in enumerate(kinds, start=1):
        await repo.add_acceptance_criterion(
            db, task_id, _ac(idx, kind, "tests/x.py::t" if kind == "test" else None)
        )
    if plan:
        await repo.add_task_update(db, task_id, "dev", "status", "Plan: поменять DNS")
    await db.commit()
    return task_id


def evidence_for(
    ac_ids: list[str] | tuple[str, ...] = ("AC-1", "AC-2"), **over: Any
) -> list[dict[str, str]]:
    """По записи на каждый AC; ``over`` переопределяет поля КАЖДОЙ записи."""
    items = []
    for ac_id in ac_ids:
        item = {
            "ac_id": ac_id,
            "action": f"dig +short A example.org  # {ac_id}",
            "observed": f"отвечает 203.0.113.7 ({ac_id})",
            "target": "ns1.registrar.example",
            "observed_at": "2026-10-09T12:00:00Z",
        }
        item.update(over)
        items.append(item)
    return items


async def pair_start(
    client: AsyncClient,
    task_id: int,
    *,
    headers: dict[str, str] | None = None,
    git_mode: str = "hub",
    session_id: str = "s-state-1647",
):
    return await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={
            "assigned_agent": "dev",
            "plan": "Plan: поменять DNS",
            "git_mode": git_mode,
            "session_id": session_id,
        },
        headers=headers or {},
    )


async def submit(
    client: AsyncClient,
    task_id: int,
    evidence: Any,
    *,
    headers: dict[str, str] | None = None,
    **extra: Any,
):
    body: dict[str, Any] = {
        "model": "claude-opus-5-5",
        "summary": "DNS переключён, наблюдения приложены",
        "agent": "dev",
    }
    if evidence is not None:
        body["evidence"] = evidence
    body.update(extra)
    return await client.post(
        f"/api/tasks/{task_id}/submit-review", json=body, headers=headers or {}
    )


async def row(db: aiosqlite.Connection, task_id: int) -> dict[str, Any]:
    found = await repo.get_task(db, task_id)
    assert found is not None
    return dict(found)


async def evidence_rows(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    """Строки task_evidence; пусто, если таблицы ещё нет (код до задачи)."""
    tables = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_evidence'"
    )
    if not tables:
        return []
    rows = await db.execute_fetchall(
        "SELECT * FROM task_evidence WHERE task_id=? ORDER BY generation, ac_id",
        (task_id,),
    )
    return [dict(r) for r in rows]


async def feed(db: aiosqlite.Connection, task_id: int) -> list[dict[str, Any]]:
    return [dict(u) for u in await repo.get_task_updates(db, task_id)]


async def events(
    db: aiosqlite.Connection, task_id: int, kind: str | None = None
) -> list[dict]:
    sql = "SELECT * FROM events WHERE task_id=?"
    args: list[Any] = [task_id]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return [dict(r) for r in await db.execute_fetchall(sql + " ORDER BY id", args)]


async def fingerprint(db: aiosqlite.Connection, task_id: int) -> dict[str, Any]:
    """Всё, что обязано остаться прежним после отказа без побочных эффектов."""
    task = await row(db, task_id)
    submissions = await db.execute_fetchall(
        "SELECT COUNT(*) FROM submissions WHERE task_id=?", (task_id,)
    )
    return {
        "status": task["status"],
        "generation": task["submission_generation"],
        "verdict": task.get("review_verdict"),
        "submissions": submissions[0][0],
        "evidence": len(await evidence_rows(db, task_id)),
        "feed": len(await feed(db, task_id)),
        "events": len(await events(db, task_id)),
    }


async def drive_to_review(
    client: AsyncClient,
    db: aiosqlite.Connection,
    task_id: int,
    *,
    headers: dict[str, str] | None = None,
    ac_ids: tuple[str, ...] = ("AC-1", "AC-2"),
) -> None:
    """pair-start + сдача с доказательствами; ожидает status=review."""
    started = await pair_start(client, task_id, headers=headers)
    assert started.status_code == 200, started.text
    sent = await submit(client, task_id, evidence_for(ac_ids), headers=headers)
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "review", sent.text


async def human_verdict(
    client: AsyncClient,
    task_id: int,
    verdict: str,
    *,
    generation: int | None,
    headers: dict[str, str] | None = None,
    comments: str = "",
):
    """Вердикт человека по REST; ``generation`` — то, что назвала форма."""
    body: dict[str, Any] = {"verdict": verdict, "agent": "denis", "comments": comments}
    if generation is not None:
        body["expected_generation"] = generation
    return await client.post(
        f"/api/tasks/{task_id}/review-verdict", json=body, headers=headers or {}
    )


async def drive_to_second_generation(
    client: AsyncClient,
    db: aiosqlite.Connection,
    task_id: int,
    *,
    headers: dict[str, str] | None = None,
    human_headers: dict[str, str] | None = None,
) -> None:
    """Сдача №1, отказ человека, возврат в работу, сдача №2: ожидает review/2.

    Доказательства поколения 1 остаются в таблице (insert-only), поэтому
    читатель обязан брать только поколение 2.
    """
    await drive_to_review(client, db, task_id, headers=headers)
    sent_back = await human_verdict(
        client,
        task_id,
        "changes_requested",
        generation=1,
        headers=human_headers or headers,
        comments="AC-2: нет наблюдения с резолвера",
    )
    assert sent_back.status_code == 200, sent_back.text
    returned = await client.post(
        f"/api/tasks/{task_id}/return-to-work",
        json={"reason": "уточняем"},
        headers=human_headers or headers or {},
    )
    assert returned.status_code == 200, returned.text
    started = await pair_start(client, task_id, headers=headers)
    assert started.status_code == 200, started.text
    second = await submit(
        client,
        task_id,
        evidence_for(("AC-1", "AC-2"), observed="повторное наблюдение, поколение 2"),
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["submission_generation"] == 2, second.text
