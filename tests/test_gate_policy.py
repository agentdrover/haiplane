"""Per-project gate policy: storage, human-only writes, default lock (#743).

Shadow step of feature #738: the policy is stored, validated and visible,
and deliberately decides NOTHING until #744 starts reading it. The default
project — the hub's own repo — refuses any 'auto' from any token: the hub
does not weaken oversight over itself.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import AsyncClient

from hub import config
from hub.config import TokenIdentity


async def _create_project(client: AsyncClient, slug: str, **headers) -> int:
    resp = await client.post(
        "/api/projects", json={"slug": slug, "name": slug.title()}, **headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_policy_stored_and_returned(client: AsyncClient):
    # AC-1 (#743): a set policy is visible in the API; absence reads as {}.
    pid = await _create_project(client, "spike-a")
    plain = await _create_project(client, "spike-b")

    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"dor": "auto"}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["gate_policy"] == {"dor": "auto"}

    listed = {p["slug"]: p for p in (await client.get("/api/projects")).json()}
    assert listed["spike-a"]["gate_policy"] == {"dor": "auto"}
    assert listed["spike-b"]["gate_policy"] == {}, (
        "no policy means {} — every gate human by default"
    )
    assert plain == listed["spike-b"]["id"]


async def test_policy_shape_is_validated(client: AsyncClient):
    # Unknown keys and values are mistakes worth refusing, not ignoring.
    pid = await _create_project(client, "spike-shape")
    for bad in (
        {"dor": "yolo"},
        {"unknown": "auto"},
        {"decision": "auto"},
    ):
        resp = await client.patch(f"/api/projects/{pid}", json={"gate_policy": bad})
        assert resp.status_code == 422, f"{bad} must be refused: {resp.text}"
    body = (await client.get("/api/projects")).json()
    row = next(p for p in body if p["id"] == pid)
    assert row["gate_policy"] == {}, "a refused write must leave nothing behind"


async def test_agent_token_cannot_set_policy(client: AsyncClient, monkeypatch):
    # AC-2 (#743): the write rides the human-only project PATCH — an agent
    # token gets a structured 403 and the policy stays untouched.
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-token": TokenIdentity("bot", "agent"),
            "human-token": TokenIdentity("denis", "human"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"headers": {"Authorization": "Bearer human-token"}}
    agent = {"headers": {"Authorization": "Bearer agent-token"}}

    pid = await _create_project(client, "spike-guard", **human)

    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"dor": "auto"}},
        **agent,
    )
    assert resp.status_code == 403

    listed = (await client.get("/api/projects", **human)).json()
    row = next(p for p in listed if p["id"] == pid)
    assert row["gate_policy"] == {}


async def test_default_project_locked(client: AsyncClient):
    # AC-3 (#743): the hub's own project refuses 'auto' at any gate from
    # any token — the system does not simplify its own rules.
    pid = await _create_project(client, "default")

    for payload in (
        {"dor": "auto"},
        {"verdict": "auto"},
        {"dor": "auto", "verdict": "human"},
    ):
        resp = await client.patch(f"/api/projects/{pid}", json={"gate_policy": payload})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "default_project_gate_locked"

    # An explicit all-human policy is fine — it changes nothing.
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"dor": "human", "verdict": "human"}},
    )
    assert resp.status_code == 200, resp.text


async def test_policy_is_inert_in_this_task(client: AsyncClient, monkeypatch):
    # AC-4 (#743): nothing reads the policy yet. With the global switch off
    # (today's default), a DoR-passed R0 draft stays waiting for the human
    # even though a project with full auto policy exists — the policy alone
    # activates nothing until #744.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    pid = await _create_project(client, "spike-inert")
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"dor": "auto", "verdict": "auto"}},
    )
    assert resp.status_code == 200, resp.text

    draft = await client.post(
        "/api/tasks", json={"title": "inert probe", "source": "agent"}
    )
    task_id = draft.json()["id"]
    resp = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "work_type": "feature",
            "user_story": "as a user, I want X so that Y",
            "problem_statement": "ps",
            "business_value": "bv",
            "scope_in": ["module"],
            "validation_commands": ["uv run pytest -q"],
            "size": "S",
            "wip_tag": "feature_work",
            "affected_areas": ["docs/notes.md"],
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
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["dor_passed"] is True
    assert body["risk_class"] == "R0"
    assert body["status"] == "draft", "the policy must decide nothing until #744"


async def test_risk_map_and_ceiling_are_human_only(client: AsyncClient, monkeypatch):
    """#760 AC-4: the new knobs ride the same human-only PATCH as the gates.

    They decide how far the DoR autopilot reaches, so an agent able to write
    them could widen its own gate — the conflict #743 removed for dor/verdict.
    """
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-token": TokenIdentity("bot", "agent"),
            "human-token": TokenIdentity("denis", "human"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"headers": {"Authorization": "Bearer human-token"}}
    agent = {"headers": {"Authorization": "Bearer agent-token"}}

    pid = await _create_project(client, "spike-knobs", **human)
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"dor": "auto", "risk_map": {"src/**": "code"}}},
        **agent,
    )
    assert resp.status_code == 403
    listed = (await client.get("/api/projects", **human)).json()
    assert next(p for p in listed if p["id"] == pid)["gate_policy"] == {}

    ok = await client.patch(
        f"/api/projects/{pid}",
        json={
            "gate_policy": {
                "dor": "auto",
                "dor_max_class": "r1",
                "risk_map": {"src/**": "code"},
            }
        },
        **human,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["gate_policy"]["risk_map"] == {"src/**": "code"}


async def test_risk_map_and_ceiling_refuse_nonsense(client: AsyncClient):
    """#760: a malformed knob is refused loudly, never stored half-understood."""
    pid = await _create_project(client, "spike-shapes")
    for payload in (
        {"risk_map": {"src/**": "whatever"}},
        {"risk_map": {"": "code"}},
        {"risk_map": ["src/**"]},
        {"dor_max_class": "r3"},
        {"dor_max_class": "R1 "},
    ):
        resp = await client.patch(f"/api/projects/{pid}", json={"gate_policy": payload})
        assert resp.status_code == 422, f"{payload} must be refused: {resp.text}"

    listed = (await client.get("/api/projects")).json()
    assert next(p for p in listed if p["id"] == pid)["gate_policy"] == {}, (
        "a refused write must leave nothing behind"
    )


# --- The review key (#805) ---------------------------------------------------


async def test_review_key_is_stored_and_validated(client: AsyncClient):
    # The key is part of the policy shape, so a typo is refused at the door
    # rather than stored as a knob nothing reads. (The dispatcher ALSO reads
    # an unknown value as off — a policy written straight into the DB must
    # not spend tokens either.)
    pid = await _create_project(client, "spike-review")

    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"review": "dispatch"}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["gate_policy"] == {"review": "dispatch"}

    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"review": "dispath"}}
    )
    assert resp.status_code == 422, resp.text


async def test_default_project_may_ask_for_review(client: AsyncClient):
    # The #743 lock is about handing the hub's own gates to the autopilot.
    # Calling a reviewer does the opposite: the human keeps the gate and
    # finally has something to read at it (#804). Refusing this would have
    # meant the hub's own code is the one code nobody reviews.
    pid = await _create_project(client, "default")

    resp = await client.patch(
        f"/api/projects/{pid}",
        json={
            "gate_policy": {"dor": "human", "verdict": "human", "review": "dispatch"}
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["gate_policy"]["review"] == "dispatch"

    # The lock itself is untouched.
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"verdict": "auto", "review": "dispatch"}},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "default_project_gate_locked"


# ---------------------------------------------------------------------------
# #1151 — verdict=steward: композиция с автопилотом, а не замена его
# ---------------------------------------------------------------------------


def test_every_policy_consumer_is_enumerated():
    """Все читатели gate_policy.verdict спрашивают ОДИН перечень.

    Значение политики читают четыре независимых места, и каждое решает
    своё. Достаточно одному прочитать «verdict больше не auto» как «здесь
    теперь всё ручное» — и проект тихо теряет автовердикт на чистых
    сдачах. Обнаружится это не отказом, а очередью у человека.

    Проверяется перечислением: тест, трогающий одного потребителя, не
    отличает композицию от совпадения.
    """
    from hub.services.digest import _policy_delegates
    from hub.services.project_policy import (
        DELEGATED_VERDICTS,
        review_dispatch_enabled,
        verdict_is_delegated,
    )
    from hub.services.steward_dispatch import _policy_wants_steward

    assert DELEGATED_VERDICTS == frozenset({"auto", "steward"})

    steward = {"verdict": "steward"}
    assert verdict_is_delegated(steward), (
        "автовердикт обязан считать это делегированием"
    )
    assert review_dispatch_enabled(steward), (
        "стюард судит ПО ОТЧЁТУ: проект без заказанного ревью судил бы вслепую"
    )
    assert _policy_delegates(json.dumps(steward)), "дайджест обязан выйти"

    class _Row(dict):
        pass

    assert _policy_wants_steward(_Row(gate_policy=json.dumps(steward))), (
        "диспетчер обязан заказать прогон"
    )

    # Зеркало: чисто человеческая политика не включает НИЧЕГО. Проверка,
    # умеющая только разрешать, неотличима от выключателя.
    human = {"verdict": "human"}
    assert not verdict_is_delegated(human)
    assert not review_dispatch_enabled(human)
    assert not _policy_delegates(json.dumps(human))
    assert not _policy_wants_steward(_Row(gate_policy=json.dumps(human)))

    # Нераспознанное значение — это человек, а не «кто-нибудь» (#835).
    assert not verdict_is_delegated({"verdict": "stewrad"})

    # Пятый потребитель, который читает тот же вопрос раньше всех: валидатор
    # API. Пока он знал только human и auto, «verdict=steward» нельзя было
    # СОХРАНИТЬ вовсе — рычаг перевода проекта на стюарда не существовал, и
    # политику можно было положить только прямо в базу, мимо всех проверок.
    from hub.services.project_policy import GATE_VALUES

    assert GATE_VALUES == frozenset({"human"}) | DELEGATED_VERDICTS, (
        "принимаемые значения и делегирующие обязаны идти одним перечнем: "
        "иначе слово можно научиться понимать, не научив API его принимать"
    )


async def test_steward_composes_with_auto(client: AsyncClient):
    """AC-1 (#1151): перевод на steward ничего не выключает.

    Проверяется на живом проекте через API, а не на словаре в памяти:
    политика хранится строкой, и путь от неё до потребителя проходит через
    разбор JSON, где и теряются такие вещи.
    """
    from hub.services.project_policy import (
        gate_policy_of,
        review_dispatch_enabled,
        verdict_is_delegated,
    )

    pid = await _create_project(client, "spike-steward")
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"dor": "auto", "verdict": "steward"}},
    )
    assert resp.status_code == 200, resp.text

    listed = {p["id"]: p for p in (await client.get("/api/projects")).json()}
    stored = listed[pid]["gate_policy"]
    assert stored == {"dor": "auto", "verdict": "steward"}

    policy = gate_policy_of({"gate_policy": json.dumps(stored)})
    assert verdict_is_delegated(policy), (
        "автовердикт на чистой сдаче обязан продолжать работать: стюард "
        "добавлен на грязный путь, а не поставлен вместо автопилота"
    )
    assert review_dispatch_enabled(policy)


async def test_default_project_lock_covers_every_delegating_value(client: AsyncClient):
    """AC-3 (#1151): замок #743 закрывает КАЖДОЕ делегирующее значение.

    Замок сравнивался ровно со строкой «auto». Появление второго
    делегирующего слова сделало бы его обходимым одной буквой: verdict=
    steward на default включил бы на репозитории самого хаба ту автоматику,
    которую замок и запрещает. Перебор по перечню, а не два примера: третье
    слово, добавленное в перечень и забытое здесь, повторит ту же дыру.
    """
    from hub.services.project_policy import DELEGATED_VERDICTS

    pid = await _create_project(client, "default")

    for gate in ("dor", "verdict"):
        for value in sorted(DELEGATED_VERDICTS):
            resp = await client.patch(
                f"/api/projects/{pid}", json={"gate_policy": {gate: value}}
            )
            assert resp.status_code == 422, (
                f"{gate}={value} обязано быть отвергнуто на проекте default: {resp.text}"
            )
            assert resp.json()["detail"]["error"] == "default_project_gate_locked"

    # Человеческая политика по-прежнему сохраняется — замок не запрещает всё.
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"dor": "human", "verdict": "human"}},
    )
    assert resp.status_code == 200, resp.text


async def test_an_unknown_gate_value_is_still_refused(client: AsyncClient):
    """Расширение словаря не превратило его в «что угодно».

    Зеркало к предыдущему: раз API теперь принимает новое слово, надо
    показать, что он по-прежнему отвергает НЕ слово. Опечатка в политике не
    имеет права сохраниться и потом читаться как «человек» — тихо, без
    единого признака, что владелец промахнулся.
    """
    pid = await _create_project(client, "spike-typo")

    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"verdict": "stewrad"}}
    )

    assert resp.status_code == 422, resp.text
    assert "gate_policy" in resp.text


# ---------------------------------------------------------------------------
# #1264: лимит очереди review — вход новой работы, а не её сдача
# ---------------------------------------------------------------------------
#
# Все тесты ниже строят очередь напрямую в базе: задача ставится в review
# записью статуса, потому что вопрос здесь не «как задача туда попала», а
# «что делает вход в работу, когда она там». Проект задачи — через
# project_id на самой строке: resolve_project_for_task читает его с любой.


async def _project_with_policy(db, slug: str, policy: dict) -> int:
    from hub import repository as repo

    existing = await repo.get_project_by_slug(db, slug)
    if existing is not None:
        pid = int(existing["id"])
    else:
        pid = await repo.create_project(db, slug=slug, name=slug.title())
    await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    await db.commit()
    return pid


async def _task_in(db, project_id: int, *, status: str, title: str = "t") -> int:
    from hub import repository as repo
    from hub import services
    from hub.models import TaskCreate

    tv = await services.create_task(db, TaskCreate(title=title))
    await repo.update_task(db, tv.id, project_id=project_id)
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: work")
    if status != "open":
        await repo.update_task(db, tv.id, status=status)
    await db.commit()
    return tv.id


async def _feed(db, task_id: int) -> str:
    from hub import repository as repo

    return " ".join(
        (u["content"] or "") for u in await repo.get_task_updates(db, task_id)
    )


async def test_a_full_review_queue_refuses_to_open_new_work(client: AsyncClient, db):
    """AC-1 (#1264): K=3, в review три задачи проекта — pair_start отказывает.

    Отказ называет K, текущее число и номера задач очереди; статус и claim
    задачи не меняются, а карточка говорит то же вслух — один раз.
    """
    from hub import repository as repo

    pid = await _project_with_policy(db, "wip-a", {"review_limit": 3})
    queue = [await _task_in(db, pid, status="review") for _ in range(3)]
    task_id = await _task_in(db, pid, status="open", title="new work")
    resp = await client.post(f"/api/tasks/{task_id}/claim", json={"agent": "dev"})
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "review_queue_full"
    assert detail["review_limit"] == 3
    assert detail["in_review"] == 3
    assert detail["queue"] == sorted(queue)
    row = await repo.get_task(db, task_id)
    assert row["status"] == "claimed", "отказ не открывает задачу"
    assert row["claimed_by"] == "dev", "и не снимает claim"
    feed = await _feed(db, task_id)
    assert all(f"#{q}" in feed for q in queue), "карточка называет очередь"
    assert "лимите 3" in feed

    # Повтор не засоряет карточку: та же очередь — та же запись.
    before = len(await repo.get_task_updates(db, task_id))
    again = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert again.status_code == 409
    assert len(await repo.get_task_updates(db, task_id)) == before


async def test_a_full_review_queue_refuses_dispatch_start_too(client: AsyncClient, db):
    """AC-1 (#1264), второй вход: start_task (диспетчерский) держит тот же лимит."""
    from hub import repository as repo

    pid = await _project_with_policy(db, "wip-start", {"review_limit": 2})
    queue = [await _task_in(db, pid, status="review") for _ in range(2)]
    task_id = await _task_in(db, pid, status="open", title="dispatch me")

    resp = await client.post(f"/api/tasks/{task_id}/start", json={})

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "review_queue_full"
    assert detail["queue"] == sorted(queue)
    assert (await repo.get_task(db, task_id))["status"] == "open"


async def test_a_full_review_queue_never_blocks_draining_it(db):
    """AC-2 (#1264): пересдача, вердикт, исправление, доставка — как без лимита.

    Очередь держится выше лимита на КАЖДОМ шаге (четыре при K=3): иначе шаг,
    который прошёл бы только потому, что сама задача на миг вышла из review,
    ничего бы не доказал.
    """
    from hub import repository as repo
    from hub import services
    from hub.models import TaskCreate, TaskReviewVerdict, TaskUpdateCreate

    # Задачи без проекта относятся к default (resolve_project_for_task).
    pid = await _project_with_policy(db, "default", {})

    # Разбираемая задача открыта ДО включения лимита — как на проде: очередь
    # уже стоит, когда владелец лимит включает.
    tv = await services.create_task(db, TaskCreate(title="in the queue"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await services.submit_for_review(db, tv.id)
    for _ in range(4):
        await _task_in(db, pid, status="review")
    await _project_with_policy(db, "default", {"review_limit": 3})

    # Пересдача из review.
    await services.submit_for_review(db, tv.id)
    assert (await repo.get_task(db, tv.id))["status"] == "review"
    # Вердикт «вернуть» — задача уходит в исправление.
    await services.record_review_verdict(
        db,
        tv.id,
        TaskReviewVerdict(
            verdict="changes_requested", agent="reviewer", comments="Поправить тест."
        ),
    )
    assert (await repo.get_task(db, tv.id))["status"] == "running"
    # Исправление сдаётся снова.
    await services.submit_for_review(db, tv.id)
    assert (await repo.get_task(db, tv.id))["status"] == "review"
    # Вердикт «принять».
    await services.record_review_verdict(
        db, tv.id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )
    # Доставка.
    await services.add_update(
        db, tv.id, TaskUpdateCreate(agent="dev", kind="done", content="Готово")
    )
    assert (await repo.get_task(db, tv.id))["status"] == "completed"
    assert "review_queue_full" not in await _feed(db, tv.id)


async def test_no_review_limit_means_todays_behaviour(client: AsyncClient, db):
    """AC-3 (#1264): без ключа 15 задач в review ничего не держат."""
    pid = await _project_with_policy(db, "wip-none", {})
    for _ in range(15):
        await _task_in(db, pid, status="review")
    task_id = await _task_in(db, pid, status="open", title="new work")

    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    assert "review" not in (await _feed(db, task_id)).replace("Plan: work", "")


async def test_review_limit_counts_only_the_projects_own_queue(client: AsyncClient, db):
    """AC-4 (#1264): две свои в review и пять чужих при K=3 — задача открывается."""
    pid_a = await _project_with_policy(db, "wip-own", {"review_limit": 3})
    pid_b = await _project_with_policy(db, "wip-other", {})
    for _ in range(2):
        await _task_in(db, pid_a, status="review")
    for _ in range(5):
        await _task_in(db, pid_b, status="review")
    task_id = await _task_in(db, pid_a, status="open", title="new work")

    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"


async def test_expedite_is_not_held_by_the_review_limit(client: AsyncClient, db):
    """AC-5 (#1264): expedite открывается при полной очереди, обход — в ленте."""
    from hub import repository as repo

    pid = await _project_with_policy(db, "wip-exp", {"review_limit": 1})
    queue = [await _task_in(db, pid, status="review") for _ in range(2)]
    task_id = await _task_in(db, pid, status="open", title="hotfix")
    await repo.update_task(db, task_id, class_of_service="expedite")
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    feed = await _feed(db, task_id)
    assert "expedite" in feed, "обход виден в ленте"
    assert all(f"#{q}" in feed for q in queue)


async def test_warn_mode_opens_and_says_what_it_would_have_held(
    client: AsyncClient, db
):
    """#1264, выход для включения: режим warn не держит, а пишет в ленту.

    Владелец включает лимит на проекте, где очередь уже выше K (default: 20+
    сдач) — enforce сразу остановил бы всех, включая стюарда. warn даёт
    увидеть, кого лимит держал бы, прежде чем держать.
    """
    pid = await _project_with_policy(
        db, "wip-warn", {"review_limit": 1, "review_limit_mode": "warn"}
    )
    queue = [await _task_in(db, pid, status="review") for _ in range(2)]
    task_id = await _task_in(db, pid, status="open", title="new work")

    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )

    assert resp.status_code == 200, resp.text
    feed = await _feed(db, task_id)
    assert "warn" in feed
    assert all(f"#{q}" in feed for q in queue)


async def test_review_limit_keys_are_validated(client: AsyncClient, db):
    """#1264: лимит — целое ≥ 1, режим — enforce или warn; прочее отказ."""
    pid = await _create_project(client, "wip-shape")
    for bad in (
        {"review_limit": 0},
        {"review_limit": -2},
        {"review_limit": "3"},
        {"review_limit": True},
        {"review_limit": 3, "review_limit_mode": "shadow"},
    ):
        resp = await client.patch(f"/api/projects/{pid}", json={"gate_policy": bad})
        assert resp.status_code == 422, f"{bad} must be refused: {resp.text}"
    good = {"review_limit": 3, "review_limit_mode": "warn"}
    resp = await client.patch(f"/api/projects/{pid}", json={"gate_policy": good})
    assert resp.status_code == 200, resp.text
    assert resp.json()["gate_policy"] == good
    # Проект default принимает лимит: он не делегирует гейт (#743 не о нём).
    default_id = await _project_with_policy(db, "default", {})
    resp = await client.patch(
        f"/api/projects/{default_id}", json={"gate_policy": {"review_limit": 20}}
    )
    assert resp.status_code == 200, resp.text


# Находки ce6159d160b95a4b и a4cf336071a7be3b: create_task(run_immediately) и
# approve_task(run=true) переводят задачу в running — лимит на них тот же, что
# на start и pair_start (/start тоже только человеческий, и он держится).
# Вторая половина входа — создать, одобрить — не теряется: задача остаётся
# open, карточка и ответ API говорят, что запуск удержан.


async def _default_queue_over_limit(
    db, *, limit: int, size: int, mode: str = ""
) -> list[int]:
    policy: dict = {"review_limit": limit}
    if mode:
        policy["review_limit_mode"] = mode
    pid = await _project_with_policy(db, "default", policy)
    return [await _task_in(db, pid, status="review") for _ in range(size)]


async def test_create_run_immediately_creates_but_does_not_run_over_the_limit(
    client: AsyncClient, db
):
    from hub import repository as repo

    queue = await _default_queue_over_limit(db, limit=2, size=3)

    resp = await client.post(
        "/api/tasks", json={"title": "run now", "run_immediately": True}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "open", "ответ честно говорит: создана, не запущена"
    row = await repo.get_task(db, body["id"])
    assert row["status"] == "open"
    assert not row["job_id"], "диспетчер не вызывался"
    feed = await _feed(db, body["id"])
    assert (
        "Запуск удержан лимитом review (2, сейчас 3) — задача создана, "
        "но не запущена" in feed
    )
    assert all(f"#{q}" in feed for q in queue)


async def test_approve_with_run_approves_but_does_not_run_over_the_limit(
    client: AsyncClient, db
):
    from hub import repository as repo
    from hub import services
    from hub.models import TaskCreate

    queue = await _default_queue_over_limit(db, limit=2, size=3)
    draft = await services.create_task(db, TaskCreate(title="draft", source="agent"))

    resp = await client.post(
        f"/api/tasks/{draft.id}/approve", json={"run": True, "force": True}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open", "одобрение не потеряно, запуск удержан"
    row = await repo.get_task(db, draft.id)
    assert row["status"] == "open"
    assert not row["job_id"]
    feed = await _feed(db, draft.id)
    assert (
        "Запуск удержан лимитом review (2, сейчас 3) — задача одобрена, "
        "но не запущена" in feed
    )
    assert all(f"#{q}" in feed for q in queue)


async def test_run_entrances_run_below_the_limit(client: AsyncClient, db):
    """Зеркало: ниже лимита запуск идёт, как сегодня, и карточка молчит."""
    await _default_queue_over_limit(db, limit=5, size=3)

    resp = await client.post(
        "/api/tasks", json={"title": "run now", "run_immediately": True}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    assert "удержан" not in await _feed(db, resp.json()["id"])


async def test_run_entrances_honour_expedite_and_warn(client: AsyncClient, db):
    """expedite и warn работают на этих входах так же, как на pair_start."""
    from hub import services
    from hub.models import TaskCreate

    await _default_queue_over_limit(db, limit=2, size=3)
    resp = await client.post(
        "/api/tasks",
        json={
            "title": "hotfix",
            "run_immediately": True,
            "class_of_service": "expedite",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    assert "expedite" in await _feed(db, resp.json()["id"])

    await _default_queue_over_limit(db, limit=2, size=0, mode="warn")
    draft = await services.create_task(db, TaskCreate(title="draft", source="agent"))
    resp = await client.post(
        f"/api/tasks/{draft.id}/approve", json={"run": True, "force": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    assert "Режим warn" in await _feed(db, draft.id)


async def test_a_held_create_run_is_never_running_not_even_between_commits(
    client: AsyncClient, db, monkeypatch
):
    """Находка 7d3317471e3e0f61: решение о запуске — ДО вставки строки.

    Вставка в running с последующим откатом в open оставляла окно, где задача
    жила в running без job. Здесь перехвачены все пути записи статуса: строка
    вставляется сразу open, и ни одна запись не ставит running.
    """
    from hub import repository as repo

    await _default_queue_over_limit(db, limit=2, size=3)
    statuses: list[str] = []

    real_insert = repo.create_task_full
    real_update = repo.update_task
    real_transition = repo.transition_status_if

    async def insert(conn, body, *, status, **kw):
        statuses.append(f"insert:{status}")
        return await real_insert(conn, body, status=status, **kw)

    async def update(conn, task_id, **fields):
        if "status" in fields:
            statuses.append(f"update:{fields['status']}")
        return await real_update(conn, task_id, **fields)

    async def transition(conn, task_id, *, expected_from, new_status, **kw):
        statuses.append(f"transition:{expected_from}->{new_status}")
        return await real_transition(
            conn, task_id, expected_from=expected_from, new_status=new_status, **kw
        )

    monkeypatch.setattr(repo, "create_task_full", insert)
    monkeypatch.setattr(repo, "update_task", update)
    monkeypatch.setattr(repo, "transition_status_if", transition)

    resp = await client.post(
        "/api/tasks", json={"title": "run now", "run_immediately": True}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"
    assert statuses == ["insert:open"], statuses


# ---------------------------------------------------------------------------
# #1593: отложенные правки политики — создание, права, устаревшая форма
# ---------------------------------------------------------------------------


def _fake_reach(state: dict):
    """Подмена читателя достижимости ревью: состояние переключает сам тест."""
    from hub.services.review_dispatch import ReviewReach

    async def reach(_db, _forge):
        if state["runnable"]:
            return ReviewReach(("local",), "", ())
        return ReviewReach((), "локальная конфигурация снята", ("нет принципала",))

    return reach


async def test_policy_schedule_creation_is_validated_and_human_only(
    client: AsyncClient, db, monkeypatch
):
    """AC-3 (#1593): создание принято/отказано с причиной; после исполнения
    записи PATCH, форма и PATCH только forge не откатывают и не обходят её."""
    from datetime import UTC, datetime, timedelta

    from hub import repository as repo
    from hub.services import policy_change, review_dispatch

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-token": TokenIdentity("bot", "agent"),
            "human-token": TokenIdentity("denis", "human"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"headers": {"Authorization": "Bearer human-token"}}
    agent = {"headers": {"Authorization": "Bearer agent-token"}}
    base = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    clock = {"now": base}
    monkeypatch.setattr(policy_change, "utcnow", lambda: clock["now"])
    reach = {"runnable": True}
    monkeypatch.setattr(review_dispatch, "review_reach", _fake_reach(reach))

    pid = await _create_project(client, "sched-ok", **human)
    default_pid = await _create_project(client, "default", **human)
    at = (base + timedelta(days=7)).isoformat()
    url = "/api/projects/sched-ok/policy-schedule"

    # Корректная запись принята, видна в списке и в сводке действующей политики.
    ok = await client.post(
        url,
        json={"at": at, "patch": {"deep_daily_cap": 4}, "note": "вернуть"},
        **human,
    )
    assert ok.status_code == 201, ok.text
    first_id = ok.json()["id"]
    assert ok.json()["state"] == "pending"
    listed = (await client.get(url, **human)).json()
    assert [r["id"] for r in listed] == [first_id]
    summary = (
        await client.get("/api/projects/sched-ok/effective-policy", **human)
    ).json()
    cap = next(r for r in summary["keys"] if r["key"] == "deep_daily_cap")
    assert cap["scheduled"][0]["value"] == 4
    assert cap["scheduled"][0]["id"] == first_id

    # Остальное — конкретный отказ с причиной, а не 404.
    past = await client.post(
        url,
        json={
            "at": (base - timedelta(hours=1)).isoformat(),
            "patch": {"deep_daily_cap": 1},
        },
        **human,
    )
    assert past.status_code == 422
    assert past.json()["detail"]["error"] == "schedule_at_in_past"

    locked = await client.post(
        "/api/projects/default/policy-schedule",
        json={"at": at, "patch": {"verdict": "auto"}},
        **human,
    )
    assert locked.status_code == 422, locked.text
    assert locked.json()["detail"]["error"] == "default_project_gate_locked"
    assert default_pid

    invalid = await client.post(url, json={"at": at, "patch": {"dor": "yolo"}}, **human)
    assert invalid.status_code == 422
    assert "dor" in invalid.text

    reach["runnable"] = False
    unrunnable = await client.post(
        url, json={"at": at, "patch": {"review": "dispatch"}}, **human
    )
    assert unrunnable.status_code == 422, unrunnable.text
    assert unrunnable.json()["detail"]["error"] == "review_unrunnable_here"
    reach["runnable"] = True

    monkeypatch.setattr(policy_change, "MAX_PENDING_PER_PROJECT", 1)
    over = await client.post(url, json={"at": at, "patch": {"wip_limit": 2}}, **human)
    assert over.status_code == 422
    assert over.json()["detail"]["error"] == "schedule_limit_reached"
    monkeypatch.setattr(policy_change, "MAX_PENDING_PER_PROJECT", 50)

    # Агент не создаёт и не отменяет: 403 с причиной human-only.
    by_agent = await client.post(
        url, json={"at": at, "patch": {"wip_limit": 2}}, **agent
    )
    assert by_agent.status_code == 403
    cancel_by_agent = await client.post(f"{url}/{first_id}/cancel", **agent)
    assert cancel_by_agent.status_code == 403
    assert (await client.get(url, **agent)).status_code == 200, "чтение открыто"
    assert len(await repo.list_scheduled_policy_changes(db, pid)) == 1

    # --- после исполнения записи ---
    await repo.update_project(
        db, pid, gate_policy=json.dumps({"deep_daily_cap": 2, "review": "dispatch"})
    )
    await db.commit()
    version_before = policy_change.policy_version(await repo.get_project(db, pid))
    clock["now"] = base + timedelta(days=7, minutes=1)
    outcomes = await policy_change.run_due(db)
    assert [o["outcome"] for o in outcomes] == ["applied"]
    stored = json.loads((await repo.get_project(db, pid))["gate_policy"])
    assert stored["deep_daily_cap"] == 4

    # PATCH ДРУГОГО ключа не возвращает старое значение.
    other = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"wip_limit": 3}}, **human
    )
    assert other.status_code == 200, other.text
    assert other.json()["gate_policy"]["deep_daily_cap"] == 4

    # Явный PATCH того же ключа применяется.
    same = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"deep_daily_cap": 9}}, **human
    )
    assert same.json()["gate_policy"]["deep_daily_cap"] == 9

    # Устаревшая форма (открыта до исполнения) отказывает по версии политики.
    stale_form = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "policy_version": version_before,
            "gate_policy_dor": "human",
            "gate_policy_verdict": "human",
            "gate_policy_review": "dispatch",
        },
        follow_redirects=False,
        **human,
    )
    assert stale_form.status_code == 303
    from urllib.parse import unquote

    assert "политика проекта изменилась" in unquote(stale_form.headers["location"])
    assert (
        json.loads((await repo.get_project(db, pid))["gate_policy"])["deep_daily_cap"]
        == 9
    )

    # Свежая форма проходит — проверка версии не ломает честную правку.
    fresh_version = policy_change.policy_version(await repo.get_project(db, pid))
    fresh_form = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "policy_version": fresh_version,
            "gate_policy_dor": "human",
            "gate_policy_verdict": "human",
            "gate_policy_review": "dispatch",
        },
        follow_redirects=False,
        **human,
    )
    assert "project_error" not in fresh_form.headers["location"], fresh_form.headers

    # PATCH только forge, делающий сохранённый review=dispatch неисполнимым,
    # по-прежнему отказывает (защита от регресса общего пути).
    reach["runnable"] = False
    forge_only = await client.patch(
        f"/api/projects/{pid}", json={"forge": "gitverse"}, **human
    )
    assert forge_only.status_code == 422, forge_only.text
    assert forge_only.json()["detail"]["error"] == "review_unrunnable_here"


async def test_manual_patch_and_scheduled_execution_do_not_clobber_each_other(
    client: AsyncClient, db, db_dsn, monkeypatch
):
    """#1593: PATCH читает и пишет политику под тем же write-локом, что поллер.

    Поллер пытается исполнить запись В ТОТ МОМЕНТ, когда PATCH уже прочитал
    политику. Если чтение стоит до транзакции, PATCH запишет слияние от
    устаревшей политики и вернёт deep_daily_cap=2 поверх исполненной правки.
    """
    import asyncio
    from datetime import UTC, datetime, timedelta

    from hub import repository as repo
    from hub.db import connect
    from hub.services import policy_change

    base = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    clock = {"now": base}
    monkeypatch.setattr(policy_change, "utcnow", lambda: clock["now"])
    pid = await _create_project(client, "sched-race")
    await repo.update_project(db, pid, gate_policy=json.dumps({"deep_daily_cap": 2}))
    await db.commit()
    created = await client.post(
        "/api/projects/sched-race/policy-schedule",
        json={
            "at": (base + timedelta(hours=1)).isoformat(),
            "patch": {"deep_daily_cap": 4},
        },
    )
    assert created.status_code == 201, created.text
    clock["now"] = base + timedelta(hours=2)

    poller_conn = await connect(db_dsn)
    real_get = repo.get_project
    armed = {"on": True}
    racers: list[asyncio.Task] = []

    async def get_then_race(conn, project_id):
        row = await real_get(conn, project_id)
        if armed["on"]:
            armed["on"] = False
            racers.append(asyncio.create_task(policy_change.run_due(poller_conn)))
            await asyncio.wait(racers, timeout=0.5)
        return row

    monkeypatch.setattr(repo, "get_project", get_then_race)
    try:
        resp = await client.patch(
            f"/api/projects/{pid}", json={"gate_policy": {"wip_limit": 3}}
        )
        assert resp.status_code == 200, resp.text
        await asyncio.gather(*racers)
    finally:
        await poller_conn.close()
    monkeypatch.setattr(repo, "get_project", real_get)
    stored = json.loads((await repo.get_project(db, pid))["gate_policy"])
    assert stored == {"deep_daily_cap": 4, "wip_limit": 3}, (
        "ручная правка и исполненное расписание не затирают друг друга"
    )


async def test_web_form_without_policy_version_is_refused(client: AsyncClient, db):
    """#1593: форма политики без версии (старая страница) ничего не сохраняет."""
    from urllib.parse import unquote

    from hub import repository as repo

    pid = await _create_project(client, "sched-noversion")
    await repo.update_project(db, pid, gate_policy=json.dumps({"review_limit": 4}))
    await db.commit()
    for extra in ({}, {"policy_version": ""}):
        resp = await client.post(
            f"/projects/{pid}/web-edit",
            data={"gate_policy_dor": "human", "gate_policy_verdict": "human", **extra},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "обновите страницу" in unquote(resp.headers["location"]), extra
        stored = json.loads((await repo.get_project(db, pid))["gate_policy"])
        assert stored == {"review_limit": 4}, "ничего не сохранено"
    # Форма без полей политики (только имя) версии не требует.
    ok = await client.post(
        f"/projects/{pid}/web-edit", data={"name": "Renamed"}, follow_redirects=False
    )
    assert "project_error" not in ok.headers["location"]


# --- Заморозка проекта (#1594): допуск новой работы -------------------------

FREEZE = {
    "until": None,
    "allow_work_types": ["bug", "chore"],
    "note": "до MS-A2: только ошибки и качество",
}


async def _ready_draft(
    client: AsyncClient,
    db,
    pid: int,
    *,
    work_type: str,
    rationale: str,
    ready: bool = True,
) -> int:
    """Черновик проекта; ``ready`` доводит его до DoR (иначе force-случай)."""
    from tests.test_auto_approve import _DOR_READY, _draft_in_project

    tid = await _draft_in_project(client, db, pid)
    payload: dict = {"work_type": work_type, "freeze_rationale": rationale}
    if ready:
        payload.update(_DOR_READY, work_type=work_type, affected_areas=["docs/n.md"])
    resp = await client.post(f"/api/tasks/{tid}/refine", json=payload)
    assert resp.status_code == 200, resp.text
    return tid


async def _status(client: AsyncClient, tid: int) -> str:
    return (await client.get(f"/api/tasks/{tid}")).json()["status"]


def _assert_freeze_refusal(resp, *, until_text: str = "до снятия") -> dict:
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "freeze_refused"
    assert until_text in detail["message"]
    assert FREEZE["note"] in detail["message"], "note приезжает вместе с причиной"
    return detail


async def test_freeze_admits_only_justified_allowed_work(client: AsyncClient, db):
    # AC-1 (#1594): на каждой двери одобрения — один и тот же допуск.
    from tests.test_auto_approve import _project

    pid = await _project(db, "frozen", {"freeze": FREEZE})
    good = await _ready_draft(
        client, db, pid, work_type="bug", rationale="сбой в проде"
    )
    good2 = await _ready_draft(client, db, pid, work_type="chore", rationale="чистка")
    blank = await _ready_draft(client, db, pid, work_type="bug", rationale="   ")
    feature = await _ready_draft(client, db, pid, work_type="feature", rationale="надо")
    unready = await _ready_draft(
        client, db, pid, work_type="feature", rationale="срочно", ready=False
    )
    web_blank = await _ready_draft(client, db, pid, work_type="bug", rationale=" \n")
    compat = await _ready_draft(client, db, pid, work_type="docs", rationale="доки")

    # одиночное одобрение: пускает только допустимый тип с обоснованием
    ok = await client.post(f"/api/tasks/{good}/approve")
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "open"
    for tid in (blank, feature):
        detail = _assert_freeze_refusal(await client.post(f"/api/tasks/{tid}/approve"))
        assert detail["missing"] == (
            "freeze_rationale" if tid == blank else "work_type"
        )
        assert await _status(client, tid) == "draft", "отказ не меняет состояние"

    # force обходит только DoR, заморозку — нет; override-запись не пишется
    forced = await client.post(f"/api/tasks/{unready}/approve", json={"force": True})
    _assert_freeze_refusal(forced)
    assert await _status(client, unready) == "draft"
    updates = (await client.get(f"/api/tasks/{unready}/updates")).json()
    assert updates is not None
    assert not [u for u in updates if "override" in u["content"]]

    # batch: допустимое одобрено, отказ несёт текст причины
    result = (
        await client.post(
            "/api/tasks/batch-approve", json={"task_ids": [good2, blank, feature]}
        )
    ).json()
    assert result["approved"] == [good2]
    skipped = {s["task_id"]: s for s in result["skipped"]}
    assert set(skipped) == {blank, feature}
    for item in skipped.values():
        assert item["reason"] == "freeze_refused"
        assert "до снятия" in item["detail"] and FREEZE["note"] in item["detail"]
    assert await _status(client, blank) == "draft"

    # web: человек видит причину, а не голый JSON; HTMX получает фрагмент
    web = await client.post(f"/tasks/{web_blank}/web-approve", follow_redirects=False)
    assert web.status_code == 303
    assert web.headers["location"].endswith("approve_error=freeze_refused")
    page = await client.get(web.headers["location"])
    assert "Заморозка проекта" in page.text and FREEZE["note"] in page.text
    htmx = await client.post(
        f"/tasks/{web_blank}/web-approve", headers={"HX-Request": "true"}
    )
    assert htmx.status_code == 200 and "Заморозка проекта" in htmx.text
    assert await _status(client, web_blank) == "draft"

    # compatibility-маршрут
    legacy = await client.post(
        f"/api/proposals/{compat}/action", json={"action": "approved"}
    )
    assert legacy.status_code == 422, "docs не в allow_work_types"
    assert await _status(client, compat) == "draft"

    # черновики создаются как раньше, и пачку черновиков заморозка не трогает
    epic_child = (await client.get(f"/api/tasks/{good}")).json()["parent_id"]
    draft = await client.post(
        "/api/tasks",
        json={"title": "d", "source": "agent", "parent_id": epic_child},
    )
    assert draft.status_code == 200 and draft.json()["status"] == "draft"
    bulk = await client.post(
        f"/api/tasks/{epic_child}/subtasks",
        json={"items": [{"title": "b1"}], "task_type": "task", "source": "agent"},
    )
    assert bulk.status_code == 200, bulk.text

    # без freeze и с прошедшим until — поведение прежнее
    plain = await _project(db, "plain-freeze", {})
    gone = await _project(
        db, "gone-freeze", {"freeze": dict(FREEZE, until="2020-01-01T00:00:00+00:00")}
    )
    for other in (plain, gone):
        tid = await _ready_draft(client, db, other, work_type="feature", rationale="")
        resp = await client.post(f"/api/tasks/{tid}/approve")
        assert resp.status_code == 200, resp.text


def test_freeze_holds_strictly_before_until_and_fails_closed():
    # Граница: при now < until заморозка действует, при now == until — нет.
    from datetime import UTC, datetime, timedelta

    from hub.services.project_policy import freeze_admission

    until = datetime(2026, 10, 26, tzinfo=UTC)
    project = {
        "gate_policy": json.dumps({"freeze": dict(FREEZE, until=until.isoformat())})
    }
    args = ("feature", "есть обоснование")
    assert freeze_admission(project, *args, now=until - timedelta(seconds=1))
    assert freeze_admission(project, *args, now=until) is None
    assert freeze_admission(project, *args, now=until + timedelta(days=1)) is None
    # запись, мимо валидатора попавшая в базу нечитаемой, не читается как «можно»
    broken = {"gate_policy": json.dumps({"freeze": {"until": "завтра"}})}
    assert freeze_admission(broken, "bug", "x") is not None
    assert freeze_admission({"gate_policy": "{}"}, "feature", "") is None
    assert freeze_admission(None, "feature", "") is None


async def test_freeze_rationale_is_stored_and_refined(client: AsyncClient):
    created = await client.post(
        "/api/tasks", json={"title": "t", "freeze_rationale": "почему можно"}
    )
    assert created.status_code == 200, created.text
    assert created.json()["freeze_rationale"] == "почему можно"
    tid = created.json()["id"]
    resp = await client.post(f"/api/tasks/{tid}/refine", json={"freeze_rationale": "x"})
    assert resp.status_code == 200
    assert (await client.get(f"/api/tasks/{tid}")).json()["freeze_rationale"] == "x"
    cleared = await client.post(
        f"/api/tasks/{tid}/refine", json={"freeze_rationale": ""}
    )
    assert cleared.json()["freeze_rationale"] == ""


# --- #1594, круг 2: нечитаемая политика, типы, атомарность -------------------


@pytest.mark.parametrize("raw", ["{broken", "[]", '"frozen"', "42", "null"])
async def test_unreadable_policy_closes_admission_instead_of_opening_it(
    client: AsyncClient, db, raw
):
    # Нечитаемая запись - не «заморозки нет». Решение только для допуска:
    # остальные читатели политики по-прежнему читают её как пустую.
    from hub import repository as repo
    from tests.test_auto_approve import _project

    default = await _project(db, "default", {})
    await repo.update_project(db, default, gate_policy=raw)
    pid = await _project(db, "broken-policy", {})
    draft = await _ready_draft(client, db, pid, work_type="bug", rationale="сбой")
    await repo.update_project(db, pid, gate_policy=raw)
    await db.commit()

    top = await client.post(
        "/api/tasks",
        json={"title": "t", "work_type": "bug", "freeze_rationale": "сбой"},
    )
    assert top.status_code == 422, top.text
    assert top.json()["detail"]["error"] == "freeze_refused"
    approve = await client.post(f"/api/tasks/{draft}/approve")
    assert approve.status_code == 422, approve.text
    assert approve.json()["detail"]["missing"] == "policy"
    assert await _status(client, draft) == "draft"
    # пустая политика («» и {}) - это «политики нет», допуск открыт
    from hub.services.project_policy import freeze_admission

    for fine in ("", "{}"):
        assert freeze_admission({"gate_policy": fine}, "feature", "") is None


@pytest.mark.parametrize("bad", [[{}], [[]], [1], [None], ["bug", {}]])
async def test_malformed_work_type_element_is_422_and_reads_fail_closed(
    client: AsyncClient, db, bad
):
    from hub.services.project_policy import freeze_admission

    pid = await _create_project(client, "bad-types")
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"freeze": {"allow_work_types": bad}}},
    )
    assert resp.status_code == 422, resp.text
    # запись, попавшая в базу мимо валидатора, читателя не роняет и не открывает
    stored = {"gate_policy": json.dumps({"freeze": {"allow_work_types": bad}})}
    refusal = freeze_admission(stored, "bug", "сбой")
    assert refusal is not None


async def _second_connection_racer(db_dsn, sql: str, args: tuple):
    """Писатель на втором соединении; блокируется, пока чужой write-лок держится."""
    from hub.db import connect

    conn = await connect(db_dsn)

    async def _write():
        try:
            await conn.execute(sql, args)
            await conn.commit()
        finally:
            await conn.close()

    return asyncio.create_task(_write())


async def _lock_is_held_meanwhile(db_dsn, sql: str, args: tuple) -> bool:
    """Запустить писателя и сказать, не смог ли он пробиться: True - лок держат."""
    racer = await _second_connection_racer(db_dsn, sql, args)
    await asyncio.wait([racer], timeout=0.5)
    held = not racer.done()
    _RACERS.append(racer)
    return held


_RACERS: list = []


async def test_approval_decides_freeze_under_the_write_lock(
    client: AsyncClient, db, db_dsn, monkeypatch
):
    # #1594: конкурентный refine между расчётом DoR и переходом не должен
    # превращать разрешённый bug в открытую feature без обоснования.
    from hub import repository as repo
    from hub.services import recommendations
    from tests.test_auto_approve import _project

    pid = await _project(db, "frozen-race", {"freeze": FREEZE})
    tid = await _ready_draft(client, db, pid, work_type="bug", rationale="сбой")
    real = recommendations.calculate_readiness_with_recommendations

    async def calc_then_refine(conn, task_id):
        report = await real(conn, task_id)
        resp = await client.post(
            f"/api/tasks/{task_id}/refine",
            json={"work_type": "feature", "freeze_rationale": ""},
        )
        assert resp.status_code == 200, resp.text
        return report

    monkeypatch.setattr(
        recommendations, "calculate_readiness_with_recommendations", calc_then_refine
    )
    resp = await client.post(f"/api/tasks/{tid}/approve")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "freeze_refused"
    assert await _status(client, tid) == "draft"

    # и в момент перехода write-лок уже держат: писатель не пробивается
    monkeypatch.undo()
    await client.post(
        f"/api/tasks/{tid}/refine",
        json={"work_type": "bug", "freeze_rationale": "сбой"},
    )
    seen: dict = {}
    real_transition = repo.transition_status_if

    async def transition(conn, task_id, **kw):
        seen["held"] = await _lock_is_held_meanwhile(
            db_dsn, "UPDATE tasks SET freeze_rationale='' WHERE id=?", (task_id,)
        )
        return await real_transition(conn, task_id, **kw)

    monkeypatch.setattr(repo, "transition_status_if", transition)
    ok = await client.post(f"/api/tasks/{tid}/approve")
    assert ok.status_code == 200, ok.text
    assert seen["held"], "между проверкой и переходом write-лок должен держаться"
    await asyncio.gather(*_RACERS)
    _RACERS.clear()


async def test_creation_checks_freeze_under_the_write_lock(
    client: AsyncClient, db, db_dsn, monkeypatch
):
    from hub import repository as repo
    from tests.test_auto_approve import _project

    pid = await _project(db, "frozen-create", {})
    tid = await _ready_draft(client, db, pid, work_type="bug", rationale="сбой")
    parent = (await client.get(f"/api/tasks/{tid}")).json()["parent_id"]
    seen: dict = {}
    real_insert = repo.create_task_full

    async def insert(conn, payload, **kw):
        seen["held"] = await _lock_is_held_meanwhile(
            db_dsn,
            "UPDATE projects SET gate_policy=? WHERE id=?",
            (json.dumps({"freeze": FREEZE}), pid),
        )
        return await real_insert(conn, payload, **kw)

    monkeypatch.setattr(repo, "create_task_full", insert)
    resp = await client.post(
        "/api/tasks", json={"title": "t", "task_type": "task", "parent_id": parent}
    )
    assert resp.status_code == 200, resp.text
    assert seen["held"], "проверка допуска и вставка - одна транзакция"
    await asyncio.gather(*_RACERS)
    _RACERS.clear()


async def test_bulk_reads_the_policy_after_taking_the_write_lock(
    client: AsyncClient, db, db_dsn, monkeypatch
):
    from hub.services import lifecycle
    from tests.test_auto_approve import _project

    pid = await _project(db, "frozen-bulk", {})
    tid = await _ready_draft(client, db, pid, work_type="bug", rationale="сбой")
    parent = (await client.get(f"/api/tasks/{tid}")).json()["parent_id"]
    real_guard = lifecycle._guard_ac_locator

    def guard_then_freeze(acs):
        import sqlite3

        raw = sqlite3.connect(db_dsn)
        raw.execute(
            "UPDATE projects SET gate_policy=? WHERE id=?",
            (json.dumps({"freeze": FREEZE}), pid),
        )
        raw.commit()
        raw.close()
        return real_guard(acs)

    monkeypatch.setattr(lifecycle, "_guard_ac_locator", guard_then_freeze)
    resp = await client.post(
        f"/api/tasks/{parent}/subtasks",
        json={
            "task_type": "task",
            "source": "human",
            "items": [
                {
                    "title": "feature",
                    "acceptance_criteria": [
                        {
                            "id": "AC-1",
                            "given": "g",
                            "when": "w",
                            "then": "t",
                            "verifiable_by": "manual",
                        }
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "freeze_refused"


async def test_bulk_policy_read_happens_while_the_write_lock_is_held(
    client: AsyncClient, db, db_dsn, monkeypatch
):
    from hub.services import lifecycle
    from tests.test_auto_approve import _project

    pid = await _project(db, "frozen-bulk-lock", {})
    tid = await _ready_draft(client, db, pid, work_type="bug", rationale="сбой")
    parent = (await client.get(f"/api/tasks/{tid}")).json()["parent_id"]
    real = lifecycle._project_of_new_task
    seen: dict = {}

    async def read_then_race(conn, parent_id, bound=None):
        row = await real(conn, parent_id, bound)
        seen["held"] = await _lock_is_held_meanwhile(
            db_dsn,
            "UPDATE projects SET gate_policy=? WHERE id=?",
            (json.dumps({"freeze": FREEZE}), pid),
        )
        return row

    monkeypatch.setattr(lifecycle, "_project_of_new_task", read_then_race)
    resp = await client.post(
        f"/api/tasks/{parent}/subtasks",
        json={"task_type": "task", "source": "human", "items": [{"title": "t"}]},
    )
    assert resp.status_code == 200, resp.text
    assert seen["held"], "политика читается под write-локом, а не до него"
    await asyncio.gather(*_RACERS)
    _RACERS.clear()
