"""Круг починки на слоте k8 после changes_requested (#1998).

После вердикта «на доработку» задача стоит в running. Кнопка «Запустить на k8»
и сервис слота работали только с open, и каждый круг начинался ручным
return-to-work. Теперь running без признаков работы допускается тем же правилом,
что облачный круг починки (#1783): возврат в open и заказ слоту — ОДНА
транзакция под ЕДИНЫМ SAVEPOINT, все отказы — до возврата.
"""

# ruff: noqa: F811 - фикстура world приходит импортом, как в соседних тестах слота
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from hub import config
from hub import db as hub_db
from hub import repository as repo
from hub.csrf import token_for_session_cookie
from hub.services import admin as admin_svc
from hub.services import executor_launch as el
from hub.services import executor_orders as orders
from hub.services import launch_feedback
from hub.services import rework_admission, return_running
from tests.test_executor_dispatch import (
    _CONFIRMED,
    _CREATED,
    _UNRESOLVED,
    _creator,
    _launch_config,
)
from tests.test_executor_orders import (  # noqa: F401 - фикстуры и помощники слота
    HUB_URL,
    _claim_and_start,
    _code_of,
    _launch,
    _orders,
    _redeem,
    _rows,
    _task,
    keys,
    world,
)

SHA = "b" * 40
HUMAN_FINDING = "Гонка двух нажатий в слоте не закрыта"
FINDINGS_JSON = json.dumps(
    [
        {
            "id": 1,
            "severity": "high",
            "message": HUMAN_FINDING,
            "file": "hub/x.py",
            "line": 3,
        }
    ]
)


async def _after_cr(
    world, title: str, *, findings: str = "human", runs: bool = True
) -> int:
    """Задача в running после вердикта changes_requested: прогон был, держателя нет."""
    task_id = await _task(world, title, status="running")
    fields: dict = {
        "submission_generation": 1,
        "submission_sha": SHA,
        "review_verdict": "changes_requested",
        "review_verdict_generation": 1,
        "review_cycle": 2,
    }
    if findings == "human":
        fields["review_findings"] = FINDINGS_JSON
    await repo.update_task(world.db, task_id, **fields)
    if runs:
        run = await repo.create_executor_run(
            world.db,
            task_id=task_id,
            submission_generation=1,
            agent_id="bc-old",
            run_id="run-old",
            model="m",
        )
        await repo.update_executor_run(
            world.db, run, tokens=1000, cents=10.0, outcome="finished", finish=True
        )
    if findings == "machine":
        await world.db.execute(
            "INSERT INTO machine_reviews (task_id, submission_generation, "
            "harness_skill, findings_confirmed, unresolved) "
            "VALUES (?, 1, 'multi-agent-review', ?, ?)",
            (task_id, json.dumps([_CONFIRMED]), json.dumps([_UNRESOLVED])),
        )
    await world.db.commit()
    return task_id


async def _footprint(world, task_id: int) -> dict:
    """Всё, что запуск мог оставить за собой: статус, прогоны, коды, заказы, ленту."""
    await world.db.commit()
    task = dict(await repo.get_task(world.db, task_id))  # type: ignore[arg-type]

    async def count(sql: str) -> int:
        return int((await _rows(world.db, sql, (task_id,)))[0]["n"])

    return {
        "status": task["status"],
        "review_cycle": task["review_cycle"],
        "runs": await count("SELECT COUNT(*) n FROM executor_runs WHERE task_id = ?"),
        "codes": await count(
            "SELECT COUNT(*) n FROM chat_pair_codes WHERE bound_task_id = ?"
        ),
        "orders": await count(
            "SELECT COUNT(*) n FROM executor_orders WHERE task_id = ?"
        ),
        "updates": await count("SELECT COUNT(*) n FROM task_updates WHERE task_id = ?"),
    }


async def _go(db, world, task_id: int, host: str = "k8"):
    return await orders.launch_on_slot(
        db,
        await _project_of(db, world),
        host=host,
        task_id=task_id,
        issuer_principal_id=world.human_id,
        issuer_human=True,
    )


async def _project_of(db, world) -> dict:
    return dict(await repo.get_project(db, world.pid))  # type: ignore[arg-type]


async def _cookie(world) -> tuple[dict, str]:
    session = await admin_svc.create_browser_session(world.db, world.human_id)
    await world.db.commit()
    return (
        {"Cookie": f"{config.HUB_COOKIE_NAME}={session}", "Origin": HUB_URL},
        token_for_session_cookie(session),
    )


async def _feed(world, task_id: int) -> str:
    await world.db.commit()
    return "\n".join(
        str(r["content"])
        for r in await _rows(
            world.db, "SELECT content FROM task_updates WHERE task_id = ?", (task_id,)
        )
    )


# ---------------------------------------------------------------------------
# AC-1
# ---------------------------------------------------------------------------


async def test_slot_repair_returns_and_orders_in_one_transaction(world, monkeypatch):
    """Находки человека и машинные: кнопка видна, возврат и заказ — один commit."""
    cookie, csrf = await _cookie(world)

    # (а) находки вердикта человека: нажатие с карточки, как у владельца.
    human = await _after_cr(world, "круг по вердикту", findings="human")
    page = await world.client.get(f"/tasks/{human}", headers=cookie)
    assert f"/tasks/{human}/web-executor-slot" in page.text
    assert "Запустить на k8" in page.text and "Запустить на k9" in page.text
    assert 'class="c2-btn" href="#executor-repair-form"' in page.text
    pressed = await world.client.post(
        f"/tasks/{human}/web-executor-slot",
        data={"host": "k8", "csrf_token": csrf},
        headers=cookie,
        follow_redirects=False,
    )
    assert pressed.status_code == 303, pressed.text
    after = await _footprint(world, human)
    assert after["status"] == "open" and after["orders"] == 1
    assert after["runs"] == 2 and after["codes"] == 1
    assert after["review_cycle"] == 0, "возврат сбрасывает цикл ревью (принято)"
    placed = await _rows(
        world.db,
        "SELECT generation, order_text FROM executor_orders WHERE task_id=?",
        (human,),
    )
    assert placed[0]["generation"] == 2
    assert HUMAN_FINDING in placed[0]["order_text"], "находка вердикта — в заказе"
    assert "замечание вердикта №1" in placed[0]["order_text"]
    assert "в finding_outcomes их не передавай" in placed[0]["order_text"]
    feed = await _feed(world, human)
    assert "owner1712" in feed and "слот k8" in feed and "круг починки" in feed

    # (б) машинные находки: возврат и заказ видны другим читателям одним commit.
    machine = await _after_cr(world, "круг по отчёту", findings="machine")
    commits: list[tuple[str, int]] = []
    real_commit = world.db.commit

    async def counted() -> None:
        row = await _rows(
            world.db,
            "SELECT t.status s, (SELECT COUNT(*) FROM executor_orders "
            "WHERE task_id = t.id) n FROM tasks t WHERE t.id = ?",
            (machine,),
        )
        commits.append((row[0]["s"], row[0]["n"]))
        await real_commit()

    monkeypatch.setattr(world.db, "commit", counted)
    result = await _go(world.db, world, machine, "k9")
    monkeypatch.undo()
    assert result.launched, result
    assert commits == [("open", 1)], "один commit: уже open и уже с заказом"
    order = await _rows(
        world.db, "SELECT order_text FROM executor_orders WHERE task_id=?", (machine,)
    )
    assert "Отмена не сверяет поколение" in order[0]["order_text"]

    # Читатели после заказа: плашка называет новый прогон, повтор отказан номером.
    plaque = await launch_feedback.plaque(world.db, machine)
    assert plaque and plaque["active"]
    again = await _go(world.db, world, machine, "k8")
    assert not again.launched and "заказ #" in again.reason


# ---------------------------------------------------------------------------
# AC-2
# ---------------------------------------------------------------------------


async def test_busy_or_failed_issue_leaves_the_task_running(world, monkeypatch):
    """Признаки работы — отказ с названием; сбой выпуска и commit снаружи — ничего."""
    cookie, _csrf = await _cookie(world)

    async def refused_with(task_id: int, needle: str) -> None:
        before = await _footprint(world, task_id)
        resp = await _launch(world, task_id)
        assert resp.status_code == 409 and needle in resp.text, resp.text
        assert await _footprint(world, task_id) == before
        card = await world.client.get(f"/tasks/{task_id}", headers=cookie)
        assert f"/tasks/{task_id}/web-executor-slot" not in card.text

    held = await _after_cr(world, "держатель")
    await repo.update_task(world.db, held, claimed_by="alice")
    await world.db.commit()
    await refused_with(held, "claimed_by")

    live = await _after_cr(world, "живой прогон")
    await repo.create_executor_run(
        world.db,
        task_id=live,
        submission_generation=2,
        agent_id="bc-live",
        run_id="r",
        model="m",
    )
    await world.db.commit()
    await refused_with(live, "прогон")

    online = await _after_cr(world, "сессия захвата онлайн")
    await repo.upsert_agent_session(
        world.db, session_id="s-online", principal_id=None, agent="bot"
    )
    await repo.update_task(world.db, online, claim_session_id="s-online")
    await world.db.commit()
    await refused_with(online, "executor_online")

    await refused_with(
        await _after_cr(world, "нет находок", findings="none"), "нет находок"
    )
    parent = await _after_cr(world, "не лист")
    child = await _task(world, "дочерняя")
    await repo.update_task(world.db, child, parent_id=parent)
    await world.db.commit()
    await refused_with(parent, "не лист")

    broken = await _after_cr(world, "порча находок", findings="none")
    await repo.update_task(world.db, broken, review_findings="{broken")
    await world.db.commit()
    await refused_with(broken, "не читаются")

    # Машинные находки есть, а замечания вердикта не читаются: чинить половину
    # молча нельзя — отказ, а не заказ с одними машинными находками.
    half = await _after_cr(world, "порча при машинных", findings="machine")
    await repo.update_task(world.db, half, review_findings="{broken")
    await world.db.commit()
    await refused_with(half, "не читаются")
    await refused_with(
        await _after_cr(world, "прогонов не было", runs=False), "не запускался"
    )

    # Сбой выпуска при ВНЕШНЕЙ транзакции, которую потом коммитят: ни возврата,
    # ни кода, ни прогона — ни исключением, ни строковым отказом (HostError).
    for failure in (RuntimeError("seal"), orders.HostError(400, "ключ хоста")):
        task_id = await _after_cr(world, f"сбой выпуска {type(failure).__name__}")
        before = await _footprint(world, task_id)

        def boom(*_a, _failure=failure, **_k):
            raise _failure

        monkeypatch.setattr(orders, "encrypt_code", boom)
        await world.db.execute("BEGIN IMMEDIATE")
        try:
            result = await _go(world.db, world, task_id)
            assert not result.launched
        except RuntimeError:
            pass
        await world.db.commit()  # внешний владелец транзакции коммитит после отказа
        monkeypatch.undo()
        assert await _footprint(world, task_id) == before, failure

    # Задача ушла из running между проверкой и возвратом: отказ, следов нет.
    raced = await _after_cr(world, "ушла из running")

    async def lost(*_a, **_k):
        raise HTTPException(409, "left running")

    before = await _footprint(world, raced)
    monkeypatch.setattr(return_running, "apply_return_to_open", lost)
    result = await _go(world.db, world, raced)
    monkeypatch.undo()
    assert not result.launched and "left running" in result.reason
    assert await _footprint(world, raced) == before


# ---------------------------------------------------------------------------
# AC-3
# ---------------------------------------------------------------------------


async def _cloud_ready(world, monkeypatch) -> list[dict]:
    """Облачному кругу нужны наблюдение прав токена и провайдер-заглушка."""
    _launch_config(monkeypatch)
    witness = await repo.create_task(
        world.db,
        title="свидетель",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.insert_live_check(
        world.db, task_id=witness, sha="", outcome="done", observation="GH013"
    )
    await repo.update_project(
        world.db,
        world.pid,
        gate_policy=json.dumps(
            {"executor_launch": "manual", "executor_push_rights_task": witness}
        ),
    )
    await world.db.commit()
    return _creator(monkeypatch, [_CREATED, _CREATED])


async def _cloud(db, task_id: int, world):
    return await el.repair_executor(
        db, task_id, issuer_principal_id=world.human_id, issuer="cloud-owner"
    )


async def test_one_winner_across_slot_and_cloud_repair(world, db_dsn, monkeypatch):
    """Два соединения: победитель один, проигравший не оставляет следов."""
    calls = await _cloud_ready(world, monkeypatch)
    other = await hub_db.connect(db_dsn)
    try:
        # двойное нажатие на один хост и на разные
        for hosts in (("k8", "k8"), ("k8", "k9")):
            for task in [await _after_cr(world, f"двойное {hosts}")]:
                got = await asyncio.gather(
                    _go(world.db, world, task, hosts[0]),
                    _go(other, world, task, hosts[1]),
                )
                assert sorted(r.launched for r in got) == [False, True], got
                fp = await _footprint(world, task)
                assert (fp["status"], fp["runs"], fp["codes"], fp["orders"]) == (
                    "open",
                    2,
                    1,
                    1,
                ), fp
                await world.db.execute(
                    "UPDATE executor_orders SET state='closed', close_reason='t' "
                    "WHERE state <> 'closed'"
                )
                await world.db.commit()

        # слот первым, облако вторым — и наоборот
        first = await _after_cr(world, "слот, затем облако")
        assert (await _go(world.db, world, first)).launched
        assert not (await _cloud(other, first, world)).launched
        assert calls == [], "облако не куплено поверх заказа слоту"
        await world.db.execute("UPDATE executor_orders SET state='closed'")
        await world.db.commit()

        second = await _after_cr(world, "облако, затем слот")
        assert (await _cloud(world.db, second, world)).launched and len(calls) == 1
        before = await _footprint(world, second)
        lost = await _go(other, world, second)
        assert not lost.launched
        assert await _footprint(world, second) == before, "проигравший слот без следов"

        # одновременно: слот и облако на одну задачу
        third = await _after_cr(world, "слот и облако вместе")
        calls.clear()
        both = await asyncio.gather(
            _go(world.db, world, third, "k9"), _cloud(other, third, world)
        )
        assert sorted(r.launched for r in both) == [False, True], both
        fp = await _footprint(world, third)
        assert fp["runs"] == 2 and fp["codes"] == 1 and fp["status"] == "open", fp
        assert fp["orders"] + len(calls) == 1
    finally:
        await other.close()


async def test_foreign_claim_and_own_slot_session(world):
    """Чужой захват до брони — отказ; после брони — отказ; своя сессия слота проходит."""
    claimed = await _after_cr(world, "чужой захват до брони")
    await repo.update_task(world.db, claimed, status="claimed", claimed_by="alice")
    await world.db.commit()
    before = await _footprint(world, claimed)
    assert (await _launch(world, claimed)).status_code == 409
    assert await _footprint(world, claimed) == before

    task_id = await _after_cr(world, "после брони")
    assert (await _launch(world, task_id)).status_code == 200
    manual = {"agent": "bot", "session_id": "s-other"}
    stolen = await world.client.post(
        f"/api/tasks/{task_id}/claim", json=manual, headers=world.agent_auth
    )
    assert stolen.status_code == 409, "чужой claim после брони отказан"
    order = (await _orders(world)).json()["orders"][0]
    redeemed = await _redeem(world, _code_of(world, order))
    assert redeemed.status_code == 200, redeemed.text
    session = {"Authorization": f"Bearer {redeemed.json()['token']}"}
    await _claim_and_start(world, task_id, session)  # своя сессия слота проходит
    assert (await _footprint(world, task_id))["status"] == "running"


# ---------------------------------------------------------------------------
# Прежнее поведение и общее правило
# ---------------------------------------------------------------------------


async def test_open_task_and_cloud_repair_keep_working(world, monkeypatch):
    """Open-задача — как раньше (без записи о возврате); облако читает то же правило."""
    plain = await _task(world, "первая")
    assert (await _launch(world, plain)).status_code == 200
    assert "круг починки" not in await _feed(world, plain)

    task = await _after_cr(world, "облако по вердикту человека")
    assert await el.repair_state(
        world.db, dict(await repo.get_task(world.db, task))
    ) == (
        True,
        "",
    )
    calls = await _cloud_ready(world, monkeypatch)
    result = await _cloud(world.db, task, world)
    assert result.launched and len(calls) == 1
    assert HUMAN_FINDING in calls[0]["prompt_text"]

    bare = await _after_cr(world, "без находок", findings="none")
    offered, why = await el.repair_state(
        world.db, dict(await repo.get_task(world.db, bare))
    )
    assert not offered and why == el.REASON_NO_FINDINGS

    # Допуск под локом брони (общий для слота и облака) перечитывает всё сам.
    async with hub_db.write_transaction(world.db):
        fresh = dict(await repo.get_task(world.db, bare))
        assert await el._admit_rework(world.db, fresh, "o")
    assert return_running.RETURN_RUNNING_STATUSES == {"running"}


async def test_admission_rereads_policy_run_and_verdict(world):
    """Общий допуск сам читает режим, идущий прогон и текущесть вердикта."""
    task_id = await _after_cr(world, "допуск")

    async def why() -> str:
        row = dict(await repo.get_task(world.db, task_id))  # type: ignore[arg-type]
        return await rework_admission.refusal(world.db, row)

    assert await why() == ""

    live = await repo.create_executor_run(
        world.db,
        task_id=task_id,
        submission_generation=2,
        agent_id="bc-live",
        run_id="r",
        model="m",
    )
    assert (await why()).startswith(el.REASON_ALREADY_RUNNING)
    await repo.update_executor_run(world.db, live, outcome="failed", finish=True)

    # В находки идут только замечания этой задачи: вне области их чинит не круг.
    mixed = json.loads(FINDINGS_JSON) + [
        {"id": 2, "severity": "low", "message": "вынесено", "scope": "out_of_scope"}
    ]
    await repo.update_task(world.db, task_id, review_findings=json.dumps(mixed))
    row = dict(await repo.get_task(world.db, task_id))  # type: ignore[arg-type]
    assert [f["title"] for f in rework_admission.verdict_findings(row)] == [
        HUMAN_FINDING
    ]

    # Вердикт про прежнюю сдачу уже не находка этого круга.
    await repo.update_task(world.db, task_id, submission_generation=2)
    assert await why() == el.REASON_NO_FINDINGS

    await repo.update_project(
        world.db, world.pid, gate_policy=json.dumps({"executor_launch": "off"})
    )
    await world.db.commit()
    assert await why() == el.REASON_OFF


async def test_open_task_keeps_machine_findings_when_the_verdict_is_unreadable(world):
    """Задача уже open: порча вердикта не отнимает у заказа машинные находки (как раньше)."""
    task_id = await _after_cr(world, "open с порчей", findings="machine")
    await repo.update_task(world.db, task_id, status="open", review_findings="{broken")
    await world.db.commit()
    assert (await _launch(world, task_id)).status_code == 200
    text = (
        await _rows(
            world.db,
            "SELECT order_text FROM executor_orders WHERE task_id = ?",
            (task_id,),
        )
    )[0]["order_text"]
    assert "Отмена не сверяет поколение" in text


async def test_cloud_order_carries_the_findings_read_under_the_lock(world, monkeypatch):
    """Допуск и заказ видят один снимок: вердикт заменён перед бронью — в промпте новый."""
    calls = await _cloud_ready(world, monkeypatch)
    task_id = await _after_cr(world, "снимок находок")
    newer = "Находка B после замены вердикта"
    real = el._reserve

    async def swapped(*args, **kwargs):
        body = [{"id": 1, "severity": "high", "message": newer}]
        await repo.update_task(world.db, task_id, review_findings=json.dumps(body))
        await world.db.commit()
        return await real(*args, **kwargs)

    monkeypatch.setattr(el, "_reserve", swapped)
    result = await _cloud(world.db, task_id, world)
    assert result.launched, result
    prompt = calls[0]["prompt_text"]
    assert newer in prompt and HUMAN_FINDING not in prompt


async def test_new_order_supersedes_the_old_outcome_and_the_plaque_names_it(world):
    """Читатели после нового заказа: старый итог superseded, плашка — про новый заказ."""
    task_id = await _after_cr(world, "второй круг")
    assert (await _go(world.db, world, task_id)).launched
    first = (await _rows(world.db, "SELECT id FROM executor_orders"))[0]["id"]
    await world.db.execute(
        "UPDATE executor_orders SET state='closed', close_reason='test'"
    )
    await world.db.execute(
        "UPDATE executor_runs SET outcome='failed', finished_at=datetime('now') "
        "WHERE outcome='running'"
    )
    await repo.update_task(world.db, task_id, status="running")
    await world.db.commit()
    second = await _go(world.db, world, task_id)
    assert second.launched and second.order_id != first
    old = await orders.read_order_outcome(world.db, "k8", first)
    assert old and old["superseded"] is True
    plaque = await launch_feedback.plaque(world.db, task_id)
    assert plaque and f"#{second.order_id}" in plaque["headline"]
    assert f"#{first}" not in plaque["headline"]


async def test_cloud_repair_of_an_open_task_refuses_an_empty_snapshot(
    world, monkeypatch
):
    """Чистый отчёт пришёл перед бронью: под локом находок нет — заказ не покупается."""
    calls = await _cloud_ready(world, monkeypatch)
    task_id = await _after_cr(world, "open, отчёт очистился", findings="machine")
    await repo.update_task(world.db, task_id, status="open")
    await world.db.commit()
    real = el._reserve

    async def cleaned(*args, **kwargs):
        await world.db.execute(
            "INSERT INTO machine_reviews (task_id, submission_generation, "
            "harness_skill, findings_confirmed, unresolved) "
            "VALUES (?, 1, 'multi-agent-review', '[]', '[]')",
            (task_id,),
        )
        await world.db.commit()
        return await real(*args, **kwargs)

    monkeypatch.setattr(el, "_reserve", cleaned)
    result = await _cloud(world.db, task_id, world)
    assert not result.launched and calls == []
    assert (await _footprint(world, task_id))["runs"] == 1


@pytest.mark.parametrize("status", ["review", "fix_requested"])
async def test_refusal_between_check_and_booking_leaves_review_untouched(
    world, monkeypatch, status
):
    """Чистый отчёт перед бронью: возврат из review/fix_requested не коммитится отдельно."""
    calls = await _cloud_ready(world, monkeypatch)
    task_id = await _after_cr(world, f"возврат из {status}", findings="machine")
    await repo.update_task(
        world.db, task_id, status=status, job_id="job-1", review_cycle=2
    )
    await world.db.commit()
    before = await _footprint(world, task_id)
    real = el._reserve

    async def cleaned(*args, **kwargs):
        await world.db.execute(
            "INSERT INTO machine_reviews (task_id, submission_generation, "
            "harness_skill, findings_confirmed, unresolved) "
            "VALUES (?, 1, 'multi-agent-review', '[]', '[]')",
            (task_id,),
        )
        await world.db.commit()
        return await real(*args, **kwargs)

    monkeypatch.setattr(el, "_reserve", cleaned)
    result = await _cloud(world.db, task_id, world)
    assert not result.launched and calls == []
    row = dict(await repo.get_task(world.db, task_id))  # type: ignore[arg-type]
    assert row["job_id"] == "job-1"
    assert await _footprint(world, task_id) == before


@pytest.mark.parametrize("status", ["review", "fix_requested"])
async def test_cloud_repair_returns_and_orders_from_review_in_one_transaction(
    world, monkeypatch, status
):
    """Без отказа: возврат и бронь вместе, заказ один, статус open."""
    calls = await _cloud_ready(world, monkeypatch)
    task_id = await _after_cr(world, f"круг из {status}", findings="machine")
    await repo.update_task(world.db, task_id, status=status, review_cycle=2)
    if status == "fix_requested":
        await repo.update_task(world.db, task_id, job_id="job-1")
    await world.db.commit()
    result = await _cloud(world.db, task_id, world)
    assert result.launched and len(calls) == 1
    fp = await _footprint(world, task_id)
    assert fp["status"] == "open" and fp["review_cycle"] == 0 and fp["runs"] == 2
    assert "owner" in await _feed(world, task_id) or "cloud-owner" in await _feed(
        world, task_id
    )


@pytest.mark.parametrize("status", ["review", "fix_requested"])
async def test_failure_after_the_return_rolls_the_return_back(
    world, monkeypatch, status
):
    """Сбой выпуска кода после возврата: статус, цикл ревью и job_id прежние."""
    await _cloud_ready(world, monkeypatch)
    task_id = await _after_cr(
        world, f"сбой после возврата {status}", findings="machine"
    )
    await repo.update_task(
        world.db, task_id, status=status, job_id="job-1", review_cycle=2
    )
    await world.db.commit()
    before = await _footprint(world, task_id)

    async def boom(*_a, **_k):
        raise RuntimeError("code")

    monkeypatch.setattr(el.chat_pair, "issue_run_code", boom)
    with pytest.raises(RuntimeError):
        await _cloud(world.db, task_id, world)
    row = dict(await repo.get_task(world.db, task_id))  # type: ignore[arg-type]
    assert row["job_id"] == "job-1"
    assert await _footprint(world, task_id) == before


@pytest.mark.parametrize("status", ["review", "fix_requested"])
async def test_failure_under_an_outer_transaction_leaves_no_booking(
    world, monkeypatch, status
):
    """Внешняя транзакция коммитится после сбоя кода: возврата и брони нет, повтор проходит."""
    calls = await _cloud_ready(world, monkeypatch)
    task_id = await _after_cr(world, f"внешняя транзакция {status}", findings="machine")
    await repo.update_task(
        world.db, task_id, status=status, job_id="job-1", review_cycle=2
    )
    await world.db.commit()
    before = await _footprint(world, task_id)

    async def boom(*_a, **_k):
        raise RuntimeError("code")

    with monkeypatch.context() as patched:
        patched.setattr(el.chat_pair, "issue_run_code", boom)
        await world.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError):
            await _cloud(world.db, task_id, world)
        await world.db.commit()  # внешний владелец коммитит после перехваченной ошибки
    row = dict(await repo.get_task(world.db, task_id))  # type: ignore[arg-type]
    assert row["job_id"] == "job-1"
    assert await _footprint(world, task_id) == before
    again = await _cloud(world.db, task_id, world)
    assert again.launched and len(calls) == 1
