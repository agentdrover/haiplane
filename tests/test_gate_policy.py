"""Per-project gate policy: storage, human-only writes, default lock (#743).

Shadow step of feature #738: the policy is stored, validated and visible,
and deliberately decides NOTHING until #744 starts reading it. The default
project — the hub's own repo — refuses any 'auto' from any token: the hub
does not weaken oversight over itself.
"""

from __future__ import annotations

import json

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
