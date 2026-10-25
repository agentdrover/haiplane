"""Решение «на доработку» закрывает окно одобрения — и это видно (#1286).

22.09.2026 на проде дважды подряд: #1162 и #1206 возвращены человеком на
доработку и продолжали числиться одобренными для своей сдачи. Возврат сбрасывал
цикл ревью и метку арбитра, а вердикт не трогал вовсе — при том что комментарий
в самой ветке обещал, что «the stale verdict cannot count as current» (#422).

Здесь проверяется ВИДИМАЯ сторона починки, а не только поведение гейта: окно
одобрения закрывается, но сам вердикт из карточки не исчезает. Стереть
review_verdict было бы самым коротким способом получить «не одобрено» — и самым
дорогим: latest_review строится из этих же полей, так что вместе с окном ушли бы
и находки, и то, ЧТО именно было одобрено. Человек, читающий карточку через
неделю, должен видеть обе вещи сразу: одобрение было, и его закрыло решение.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import aiosqlite
import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from hub import db as db_module
from hub import repository as repo
from hub import services
from hub.models import TaskReturnToWork
from tests.test_pair_merge_gate import _approved_pair_task, _git


async def test_rework_closes_the_verdict_window_visibly(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    # AC-3: карточка после возврата показывает и прошлый вердикт, и то, что он
    # больше не действует. История не стёрта — закрыто окно.
    _git()
    task_id = await _approved_pair_task(db)
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/decide",
        json={"action": "rework", "instructions": "Дописать проверку AC-2."},
    )
    assert resp.status_code == 200, resp.text

    card = (await client.get(f"/api/tasks/{task_id}")).json()
    assert card["review_verdict"] == "approved", (
        "вердикт остаётся в карточке: решение закрывает окно, а не стирает историю"
    )
    assert card["review_verdict_generation"] == 1, (
        "поколение вердикта показывает, какую сдачу одобряли"
    )
    latest = card["latest_review"]
    assert latest and latest["verdict"] == "approved", (
        f"проекция последнего ревью обязана пережить возврат: {latest}"
    )
    assert latest["submission_generation"] == 1

    assert card["review_approved_current"] is False, (
        "после возврата задача не числится одобренной — иначе её доставит свип"
    )
    assert latest["is_current"] is False, (
        "карточка не может одновременно говорить «вердикт текущий» и "
        "«не одобрено»: правило одно, читатель один"
    )
    assert latest["closed_by_decision"] is True, (
        "«не текущий» само по себе не отличает возврат от пересдачи — "
        "причина должна быть названа"
    )

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    feed = " ".join(u.get("content") or "" for u in updates)
    assert "Human requested rework" in feed, "само решение остаётся в ленте"
    assert "одобрение" in feed.lower() and "закрыт" in feed.lower(), (
        f"лента обязана сказать, что одобрение закрыто этим решением: {feed}"
    )
    assert "hub_submit_for_review" in feed, (
        "и назвать выход: работа доедет только новой сдачей"
    )

    events = [dict(e) for e in await repo.list_events(db, since=0)]
    decided = [
        e for e in events if e["kind"] == "task_decided" and e["task_id"] == task_id
    ]
    assert decided, "решение остаётся событием"
    assert "closed_verdict_generation" in (decided[-1].get("payload") or ""), (
        "сколько одобрений закрыто возвратом — должно считаться по событию, "
        "а не разбором текста ленты"
    )


async def test_rework_without_an_approval_closes_nothing(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    # Обратная сторона AC-3: гасить нечего — и в ленте не появляется записи о
    # закрытом одобрении. Иначе каждая доработка после changes_requested
    # рассказывала бы о вердикте, которого не было.
    task_id = (await client.post("/api/tasks", json={"title": "No verdict"})).json()[
        "id"
    ]
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/decide",
        json={"action": "rework", "instructions": "Переделать."},
    )
    assert resp.status_code == 200, resp.text

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    feed = " ".join(u.get("content") or "" for u in updates)
    assert "закрыт" not in feed.lower(), (
        f"нечего закрывать — нечего и рассказывать: {feed}"
    )
    card = (await client.get(f"/api/tasks/{task_id}")).json()
    assert card["latest_review"] is None


async def _changes_requested_pair_task(db: aiosqlite.Connection) -> int:
    """Пара, дошедшая до CHANGES_REQUESTED на текущей сдаче.

    Это состояние достижимо без подъёма поколения: отправка в починку может не
    состояться (dispatch_fix уводит задачу в needs_decision), и арбитр
    заканчивает на том же поколении. Вердикт при этом лежит на текущей сдаче —
    ровно вход, на котором возврат на доработку рассказывал про «одобрение».
    """
    from hub import services
    from hub.models import TaskCreate, TaskReviewVerdict

    tv = await services.create_task(db, TaskCreate(title="Sent back"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await services.submit_for_review(db, tv.id)
    await services.record_review_verdict(
        db,
        tv.id,
        TaskReviewVerdict(
            verdict="changes_requested",
            agent="reviewer",
            comments="Дописать проверку AC-2.",
        ),
    )
    return tv.id


async def test_rework_after_changes_requested_closes_no_approval(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    # Находка 1ef5b666cf5597bb. Закрывать нужно ПРАВО НА ДОСТАВКУ, а его даёт
    # только approved. Отказ ревью его и не давал: закрывать нечего, и
    # рассказывать про «закрытое одобрение» — говорить неправду о том, что
    # лежит в карточке.
    _git()
    task_id = await _changes_requested_pair_task(db)
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/decide",
        json={"action": "rework", "instructions": "Доделать."},
    )
    assert resp.status_code == 200, resp.text

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    feed = " ".join(u.get("content") or "" for u in updates)
    assert "Одобрение ревью" not in feed, (
        f"вердикт был отказом — лента не вправе называть его одобрением: {feed}"
    )
    assert "больше не доставляется" not in feed, (
        f"отказ ревью ничего не доставлял, отменять у него нечего: {feed}"
    )

    card = (await client.get(f"/api/tasks/{task_id}")).json()
    latest = card["latest_review"]
    assert latest and latest["verdict"] == "changes_requested", (
        f"вердикт остаётся в карточке как был: {latest}"
    )
    assert latest["closed_by_decision"] is False, (
        "решение «на доработку» не закрывает отказ ревью: оно с ним согласно"
    )
    assert latest["is_current"] is True, (
        "отказ по-прежнему говорит о том же коде — сдачи не было"
    )

    events = [dict(e) for e in await repo.list_events(db, since=0)]
    decided = [
        e for e in events if e["kind"] == "task_decided" and e["task_id"] == task_id
    ]
    assert decided, "решение остаётся событием"
    assert '"closed_verdict_generation": null' in (decided[-1].get("payload") or ""), (
        "счётчик закрытых одобрений не должен расти на возвратах после отказа"
    )


async def test_a_resubmission_supersedes_the_rework_closure(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    # Находка 4b2c49c86e446ba7. Запись о закрытии относится к той сдаче,
    # которую закрыли. После пересдачи вердикт перестаёт быть текущим по другой
    # причине — работа изменилась, — и свежесть обязана называть эту причину, а
    # не звать пересдать то, что уже пересдано.
    from hub import services
    from hub.models import latest_review_freshness

    _git()
    task_id = await _approved_pair_task(db)
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()
    resp = await client.post(
        f"/api/tasks/{task_id}/decide",
        json={"action": "rework", "instructions": "Доделать."},
    )
    assert resp.status_code == 200, resp.text

    await services.pair_start_task(db, task_id, caller="dev")
    await services.submit_for_review(db, task_id)

    card = (await client.get(f"/api/tasks/{task_id}")).json()
    assert card["submission_generation"] == 2, (
        "исполнитель сдал заново — поколение обязано подняться"
    )
    latest = card["latest_review"]
    assert latest and latest["verdict"] == "approved", "история вердикта на месте"
    assert latest["is_current"] is False, "вердикт судил другой код"
    assert latest["closed_by_decision"] is False, (
        "закрытие относилось к сдаче #1; новую сдачу решение человека не "
        "закрывало — иначе карточка требует пересдать уже пересданное"
    )
    assert (
        latest_review_freshness(latest["is_current"], latest["closed_by_decision"])
        == "stale — work resubmitted"
    ), "после пересдачи причина именно такая"


# ---- #1356: вернуть в работу сдачу, чей исполнитель пропал ----
#
# 23.09.2026: #1241 стоит в review с подтверждённой находкой, держатель —
# облачная сессия 17.09, и hub_claim_task отвечает 400 «can only claim open
# tasks». Из review и fix_requested не было пути ни в open, ни к другому
# исполнителю — даже решением человека.


def _return_tokens() -> dict:
    from hub.config import TokenIdentity

    return {
        # A — ушедший держатель, под своим принципалом.
        "a-token": TokenIdentity("agent-a", "agent", principal_id=7),
        # B — преемник: агентский токен без принципала. Так видно, остался ли
        # на задаче принципал A: у B нечем его перезаписать.
        "b-token": TokenIdentity("agent-b", "agent"),
        "human-token": TokenIdentity("denis", "human"),
    }


_HUMAN = {"Authorization": "Bearer human-token"}
_AGENT_A = {"Authorization": "Bearer a-token"}
_AGENT_B = {"Authorization": "Bearer b-token"}


async def _abandoned_review(db: aiosqlite.Connection, *, approved: bool = True) -> int:
    """Сдача в review у агента A (сессия S, принципал 7); одобрена по желанию."""
    if approved:
        task_id = await _approved_pair_task(db)
    else:
        task_id = await _changes_requested_pair_task(db)
    await repo.update_task(
        db,
        task_id,
        status="review",
        claimed_by="agent-a",
        claim_session_id="sess-a",
        claimed_at="2026-09-17T10:00:00+00:00",
        implementer_principal_id=7,
        assigned_agent="agent-a",
        branch=f"task-{task_id}/return-review-to-work",
    )
    await db.commit()
    return task_id


async def test_a_human_returns_an_abandoned_review_to_open(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-1: статус open, захват снят, окно одобрения закрыто, гейт не
    # доставляет, лента называет кто/почему/у кого; ветка, PR и sha прежние.
    from hub import config
    from hub.poller import _sweep_pair_delivery

    monkeypatch.setattr(config, "HUB_TOKENS", _return_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    g = _git()
    task_id = await _abandoned_review(db)
    before = dict(await repo.get_task(db, task_id))
    assert before["review_verdict"] == "approved"

    resp = await client.post(
        f"/api/tasks/{task_id}/return-to-work",
        json={"reason": "Держатель молчит с 17.09, находка ждёт с 06:35."},
        headers=_HUMAN,
    )
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert card["status"] == "open"
    assert card["claimed_by"] is None
    assert card["claim_session_id"] is None
    assert card["claimed_at"] is None
    assert card["review_approved_current"] is False, (
        "одобрение отозванной работы не может оставаться текущим"
    )
    assert card["review_cycle"] == 0

    after = dict(await repo.get_task(db, task_id))
    assert after["implementer_principal_id"] is None, (
        "принципал A снят вместе с захватом"
    )
    assert after["branch"] == before["branch"], "ветка остаётся"
    assert after["pr_number"] == before["pr_number"] == 77, "PR остаётся"
    assert after["submission_sha"] == before["submission_sha"], "sha сдачи остаётся"
    assert after["submission_generation"] == before["submission_generation"]
    assert after["review_verdict"] == "approved", "вердикт остаётся историей"

    feed = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    returned = [u for u in feed if "Возвращена в работу" in (u.get("content") or "")]
    assert returned, f"лента обязана назвать возврат: {feed}"
    text = returned[-1]["content"]
    assert returned[-1]["agent"] == "denis", "кто вернул — из токена"
    assert "Держатель молчит с 17.09" in text, "почему"
    assert "agent-a" in text and "sess-a" in text and "7" in text, (
        f"у кого был захват: имя, сессия, принципал — {text}"
    )
    closed = " ".join(u.get("content") or "" for u in feed)
    assert "Одобрение ревью" in closed and "закрыто" in closed, (
        "закрытое одобрение названо тем же текстом, что у rework"
    )

    events = [
        dict(e)
        for e in await repo.list_events(db, since=0)
        if e["kind"] == "task_returned_to_work" and e["task_id"] == task_id
    ]
    assert events, "возврат — событие, а не только строка ленты"
    assert '"closed_verdict_generation": 1' in events[-1]["payload"]

    # Гейт доставки: даже если задачу снова поставят в running без пересдачи,
    # закрытое одобрение ничего не везёт.
    await repo.update_task(db, task_id, status="running")
    await db.commit()
    await _sweep_pair_delivery(db)
    assert g.merge_pr.await_count == 0, "одобрение отозванной работы не доставляется"
    assert dict(await repo.get_task(db, task_id))["status"] == "running"


async def test_another_agent_resubmits_a_returned_task(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-2: B берёт задачу штатно — claim, pair_start(remote, тот же slug),
    # пересдача — и получает следующее поколение с заказом ревью. Бывший
    # держатель A после этого — посторонний: его вердикт не саморевью.
    from hub import config
    from hub.services import review_dispatch

    monkeypatch.setattr(config, "HUB_TOKENS", _return_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    _git()
    task_id = await _abandoned_review(db)

    resp = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "agent-b", "session_id": "sess-b"},
        headers=_AGENT_B,
    )
    assert resp.status_code == 400, "до возврата задача в review не берётся"

    resp = await client.post(
        f"/api/tasks/{task_id}/return-to-work",
        json={"reason": "Исполнитель пропал"},
        headers=_HUMAN,
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "agent-b", "session_id": "sess-b"},
        headers=_AGENT_B,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={
            "assigned_agent": "agent-b",
            "session_id": "sess-b",
            "git_mode": "remote",
            "branch_slug": "return-review-to-work",
        },
        headers=_AGENT_B,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    assert resp.json()["branch"] == f"task-{task_id}/return-review-to-work"

    dispatched: list[int] = []

    async def _spy(db_, task_id_, **kwargs):
        dispatched.append(task_id_)
        return False

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", _spy)
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"agent": "agent-b", "model": "claude-opus-5.5"},
        headers=_AGENT_B,
    )
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert card["status"] == "review"
    assert card["submission_generation"] == 2, "следующее поколение"
    assert dispatched == [task_id], "пересдача заказывает ревью"

    task = dict(await repo.get_task(db, task_id))
    assert task["implementer_principal_id"] != 7, (
        "работу B нельзя записывать на принципал A"
    )
    # Бывший держатель теперь посторонний этой сдаче: владелец под тем же
    # токеном (#1241) не должен упираться в запрет саморевью.
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "agent-a",
            "comments": "нужна правка блокировки",
        },
        headers=_AGENT_A,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_verdict"] == "changes_requested"


async def test_returning_to_work_is_human_only_and_status_bound(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-3: агент — 403, без причины — 422, чужой статус — 400; задача не
    # меняется ни в одном из отказов. Работает из review И из fix_requested.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _return_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    _git()
    task_id = await _abandoned_review(db)
    snapshot = dict(await repo.get_task(db, task_id))

    async def _unchanged() -> None:
        now = dict(await repo.get_task(db, task_id))
        for field in (
            "status",
            "claimed_by",
            "claim_session_id",
            "implementer_principal_id",
            "review_verdict_closed_generation",
        ):
            assert now[field] == snapshot[field], field

    for headers in (_AGENT_A, _AGENT_B):
        resp = await client.post(
            f"/api/tasks/{task_id}/return-to-work",
            json={"reason": "перехват"},
            headers=headers,
        )
        assert resp.status_code == 403, resp.text
        await _unchanged()

    for body in ({}, {"reason": ""}, {"reason": "   "}):
        resp = await client.post(
            f"/api/tasks/{task_id}/return-to-work", json=body, headers=_HUMAN
        )
        assert resp.status_code == 422, (body, resp.text)
        await _unchanged()

    # #1675: running вынесен из этого списка - из него commit-задача возвращается,
    # когда у неё нет ни одного признака работы (test_running_*).
    for status in (
        "open",
        "needs_info",
        "needs_decision",
        "completed",
        "claimed",
    ):
        await repo.update_task(db, task_id, status=status)
        await db.commit()
        snapshot["status"] = status
        resp = await client.post(
            f"/api/tasks/{task_id}/return-to-work",
            json={"reason": "не отсюда"},
            headers=_HUMAN,
        )
        assert resp.status_code == 400, (status, resp.text)
        await _unchanged()

    # fix_requested в том виде, в каком его ставит сервис: всегда с job_id.
    # Здесь job завершён — возврат проходит без флага.
    _job_status(monkeypatch, "completed")
    await repo.update_task(db, task_id, status="fix_requested", job_id="job-fix-1")
    await db.commit()
    resp = await client.post(
        f"/api/tasks/{task_id}/return-to-work",
        json={"reason": "из fix_requested тоже"},
        headers=_HUMAN,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"
    assert resp.json()["claimed_by"] is None
    assert resp.json()["job_id"] is None


def _job_status(monkeypatch, status: str | None) -> None:
    """The registry's word on every job: ``None`` is a job it never heard of."""
    from hub.integrations.registry import plugins

    monkeypatch.setattr(
        plugins.dispatch,
        "get_job",
        lambda job_id: None if status is None else {"id": job_id, "status": status},
    )


async def _fix_requested_with_job(db: aiosqlite.Connection) -> int:
    """fix_requested как его ставит сервис (decide rework, dispatch_fix): с job_id."""
    task_id = await _abandoned_review(db, approved=False)
    await repo.update_task(db, task_id, status="fix_requested", job_id="job-fix-1")
    await db.commit()
    return task_id


async def test_a_live_job_refuses_the_return_without_the_abandon_flag(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # Находка 8582271d7f92112c: у убитого исполнителя job в реестре навсегда
    # «running». Без флага — 409, который называет job, статус и сам флаг.
    _git()
    _job_status(monkeypatch, "running")
    task_id = await _fix_requested_with_job(db)
    before = dict(await repo.get_task(db, task_id))

    resp = await client.post(
        f"/api/tasks/{task_id}/return-to-work", json={"reason": "исполнитель умер"}
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "job-fix-1" in detail and "running" in detail, detail
    assert "abandon_active_job=true" in detail, "отказ называет выход"
    after = dict(await repo.get_task(db, task_id))
    for field in ("status", "job_id", "claimed_by", "claim_session_id"):
        assert after[field] == before[field], field


async def test_the_abandon_flag_returns_a_task_over_a_live_job(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    _git()
    _job_status(monkeypatch, "running")
    task_id = await _fix_requested_with_job(db)

    resp = await client.post(
        f"/api/tasks/{task_id}/return-to-work",
        json={"reason": "исполнитель умер", "abandon_active_job": True},
    )
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert card["status"] == "open"
    assert card["job_id"] is None, "брошенный job отвязан от задачи"
    assert card["claimed_by"] is None
    feed = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    abandoned = [u for u in feed if "Брошен job_id job-fix-1" in (u["content"] or "")]
    assert abandoned, f"лента называет брошенный job: {feed}"
    text = abandoned[-1]["content"]
    assert "running" in text and "не остановлен" in text and "#509" in text, text


async def test_a_finished_job_does_not_block_the_return(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    _git()
    for status in ("completed", "failed", None):
        _job_status(monkeypatch, status)
        task_id = await _fix_requested_with_job(db)
        resp = await client.post(
            f"/api/tasks/{task_id}/return-to-work", json={"reason": "job закончился"}
        )
        assert resp.status_code == 200, (status, resp.text)
        assert resp.json()["status"] == "open"
        assert resp.json()["job_id"] is None
        feed = " ".join(
            dict(u).get("content") or ""
            for u in await repo.get_task_updates(db, task_id)
        )
        assert "Брошен" not in feed, "бросать было нечего"


async def test_return_to_work_loses_the_race_to_the_delivery_gate(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # Гонка из рисков: сервис прочёл задачу в review, а гейт доставки в это
    # время уже увёл её (доставил и завершил). Переход обязан проверить
    # статус В ЗАПИСИ: возврат получает 409 и не трогает ничего — ни статус
    # доставленной задачи, ни захват, ни окно одобрения.
    _git()
    task_id = await _abandoned_review(db)
    stale = await repo.get_task(db, task_id)
    await repo.update_task(db, task_id, status="completed")
    await db.commit()

    real_get_task = repo.get_task
    served = {"stale": False}

    async def _stale_first(db_, tid):
        if tid == task_id and not served["stale"]:
            served["stale"] = True
            return stale
        return await real_get_task(db_, tid)

    monkeypatch.setattr(repo, "get_task", _stale_first)
    resp = await client.post(
        f"/api/tasks/{task_id}/return-to-work", json={"reason": "гонка"}
    )
    assert served["stale"], "сервис должен был прочесть устаревший снимок"
    assert resp.status_code == 409, resp.text

    task = dict(await real_get_task(db, task_id))
    assert task["status"] == "completed", "доставленная задача не возвращается в open"
    assert task["claimed_by"] == "agent-a", "захват не снят проигравшим возвратом"
    assert task["implementer_principal_id"] == 7
    assert task["review_verdict_closed_generation"] in (None, 0), (
        "окно одобрения не закрыто проигравшим возвратом"
    )
    feed = " ".join(
        dict(u).get("content") or "" for u in await repo.get_task_updates(db, task_id)
    )
    assert "Возвращена в работу" not in feed


async def test_a_late_machine_review_lands_on_a_returned_task_without_approving_it(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # Риск «ревью, уже заказанное по сдаче, отчитается задаче в open». Приём
    # отчёта статус не проверяет: отчёт ложится на то же поколение как
    # свидетельство о той же сдаче. Но всё, что из отчёта СЛЕДУЕТ —
    # автовердикт, лестница профилей, — спрашивает status == review, и задача
    # в open остаётся без одобрения. Без возврата тот же отчёт одобряет
    # (test_auto_verdict::test_clean_submission_gets_policy_approved).
    from hub import config
    from tests.test_auto_verdict import _post_review, _submitted_task

    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _submitted_task(client, db, "late-report", {"verdict": "auto"})
    resp = await client.post(
        f"/api/tasks/{task_id}/return-to-work", json={"reason": "исполнитель пропал"}
    )
    assert resp.status_code == 200, resp.text

    await _post_review(client, task_id)

    card = (await client.get(f"/api/tasks/{task_id}")).json()
    assert card["status"] == "open", "отчёт не двигает возвращённую задачу"
    assert card["review_verdict"] is None, "автовердикт не выдан задаче в open"
    assert card["review_approved_current"] is False
    rows = await db.execute_fetchall(
        "SELECT submission_generation FROM machine_reviews WHERE task_id=?",
        (task_id,),
    )
    assert [r[0] for r in rows] == [1], "отчёт сохранён за той сдачей, которую читал"


# ---- #1675: возврат running-задачи без признаков работы ----

_PAST = "2020-01-01 00:00:00"
_FUTURE = "2999-01-01 00:00:00"


async def _running_commit(client: AsyncClient, db: aiosqlite.Connection) -> int:
    task_id = (await client.post("/api/tasks", json={"title": "Running idle"})).json()[
        "id"
    ]
    await repo.update_task(db, task_id, status="running")
    await db.commit()
    return task_id


async def _sql(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> None:
    await db.execute(sql, params)
    await db.commit()


async def _pair_session(db, task_id, *, kind="implementer", bound=None, **cols):
    principal = (await (await db.execute("SELECT id FROM principals")).fetchone())[0]
    fields = {
        "principal_id": principal,
        "kind": kind,
        "bound_task_id": task_id if bound is None else bound,
        "token_hash": f"hash-{kind}-{task_id}-{len(cols)}-{cols.get('tag', '')}",
        "expires_at": _FUTURE,
        **{k: v for k, v in cols.items() if k != "tag"},
    }
    await db.execute(
        f"INSERT INTO chat_pair_sessions ({', '.join(fields)}) "  # nosec B608
        f"VALUES ({', '.join('?' * len(fields))})",
        tuple(fields.values()),
    )
    await db.commit()


async def _register_session(db, session_id: str, *, last_seen: str | None = None):
    await repo.upsert_agent_session(
        db, session_id=session_id, principal_id=None, agent="agent-a"
    )
    if last_seen:
        await db.execute(
            "UPDATE agent_sessions SET last_seen_at=? WHERE session_id=?",
            (last_seen, session_id),
        )
    await db.commit()


async def _run(db, task_id, *, outcome="running", agent_id="bc-1", run_id="r-1"):
    await _sql(
        db,
        "INSERT INTO executor_runs (task_id, agent_id, run_id, outcome) "
        "VALUES (?, ?, ?, ?)",
        (task_id, agent_id, run_id, outcome),
    )


async def _slot(db, task_id, channel, *, released=None):
    await _sql(
        db,
        "INSERT INTO executor_slots (task_id, channel, slot, released_at) "
        "VALUES (?, ?, 's1', ?)",
        (task_id, channel, released),
    )


async def _set(db, task_id, **fields):
    await repo.update_task(db, task_id, **fields)
    await db.commit()


# Признак -> (как его создать, имя признака в отказе). Каждый - один и только он.
_SIGNS = {
    "claimed_by": lambda db, t: _set(db, t, claimed_by="agent-a"),
    "job_id": lambda db, t: _set(db, t, job_id="job-live-1"),
    "review_job_id": lambda db, t: _set(db, t, review_job_id="job-rev-1"),
    "open_slot": lambda db, t: _slot(db, t, "slot"),
    "open_slot_cloud": lambda db, t: _slot(db, t, "cloud"),
    "executor_run": lambda db, t: _run(db, t),
    "executor_run_booking": lambda db, t: _run(db, t, agent_id="", run_id=""),
    "implementer_session": lambda db, t: _pair_session(db, t),
    "executor_online": lambda db, t: _online_claim(db, t),
    "declared_wait": lambda db, t: _set(
        db, t, waiting_for="CI", waiting_until=_FUTURE, waiting_declared_by="a"
    ),
}
_SIGN_CODE = {
    "open_slot_cloud": "open_slot",
    "executor_run_booking": "executor_run",
}


async def _online_claim(db, task_id):
    await _register_session(db, "sess-live")
    await _set(db, task_id, claim_session_id="sess-live")


async def _snapshot(db, task_id):
    task = dict(await repo.get_task(db, task_id))
    feed = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    events = [dict(e) for e in await repo.list_events(db, since=0)]
    return task, feed, len(events)


async def test_running_task_without_work_returns_to_open(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    # AC-1: ни одного признака - open, захват снят, решение и событие записаны.
    task_id = await _running_commit(client, db)
    # claim_session_id указывает на сессию, которой нет в реестре: не признак.
    await _set(db, task_id, claim_session_id="sess-gone")

    resp = await client.post(
        f"/api/tasks/{task_id}/return-to-work", json={"reason": "исполнитель ушёл"}
    )
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert card["status"] == "open"
    assert card["claimed_by"] is None and card["claim_session_id"] is None
    assert card["job_id"] is None and card["review_job_id"] is None
    feed = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    decision = [u for u in feed if u["kind"] == "decision"]
    assert decision, f"решение в ленте: {feed}"
    text = decision[-1]["content"]
    assert "исполнитель ушёл" in text and "running" in text, text
    events = [
        dict(e)
        for e in await repo.list_events(db, since=0)
        if dict(e)["kind"] == "task_returned_to_work"
    ]
    assert len(events) == 1 and events[0]["task_id"] == task_id
    payload = json.loads(events[0]["payload"])
    assert payload["from_status"] == "running"
    assert payload["reason"] == "исполнитель ушёл"
    assert payload["previous_session"] == "sess-gone"


async def test_running_with_any_sign_of_work_is_not_returned(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    # AC-2: каждый признак по отдельности - 409 с его именем, задача цела,
    # abandon_active_job отказ не обходит; обратные случаи - возврат проходит.
    for name, make in _SIGNS.items():
        for abandon in (False, True):
            task_id = await _running_commit(client, db)
            await make(db, task_id)
            before = await _snapshot(db, task_id)
            resp = await client.post(
                f"/api/tasks/{task_id}/return-to-work",
                json={"reason": "проверка", "abandon_active_job": abandon},
            )
            assert resp.status_code == 409, (name, abandon, resp.text)
            detail = resp.json()["detail"]
            assert detail["reason"] == "return_running_has_work", (name, detail)
            code = _SIGN_CODE.get(name, name)
            assert [s["sign"] for s in detail["signs"]] == [code], (name, detail)
            assert await _snapshot(db, task_id) == before, (name, abandon)

    async def _closed_slot(db, t):
        await _slot(db, t, "slot", released=_PAST)

    async def _terminal_run(db, t):
        await _run(db, t, outcome="completed")

    async def _expired(db, t):
        await _pair_session(db, t, expires_at=_PAST)

    async def _revoked(db, t):
        await _pair_session(db, t, revoked_at=_PAST)

    async def _foreign(db, t):
        other = await _running_commit(client, db)
        await _pair_session(db, t, bound=other)

    async def _intake(db, t):
        await _pair_session(db, t, kind="intake")

    async def _reviewer(db, t):
        await _pair_session(db, t, kind="reviewer")

    async def _offline(db, t):
        await _register_session(db, "sess-old", last_seen=_PAST)
        await _set(db, t, claim_session_id="sess-old")

    async def _past_wait(db, t):
        await _set(db, t, waiting_for="CI", waiting_until=_PAST)

    for name, make in {
        "closed_slot": _closed_slot,
        "terminal_run": _terminal_run,
        "expired_session": _expired,
        "revoked_session": _revoked,
        "foreign_session": _foreign,
        "intake_session": _intake,
        "reviewer_session": _reviewer,
        "offline_claim": _offline,
        "expired_wait": _past_wait,
    }.items():
        task_id = await _running_commit(client, db)
        await make(db, task_id)
        resp = await client.post(
            f"/api/tasks/{task_id}/return-to-work", json={"reason": "обратный"}
        )
        assert resp.status_code == 200, (name, resp.text)
        assert resp.json()["status"] == "open", name


async def test_expired_wait_is_closed_on_return(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    # AC-3: просроченное (и неполное) ожидание очищается с записью аудита, но
    # только если других признаков нет.
    for fields in (
        {"waiting_for": "CI main", "waiting_until": _PAST, "waiting_declared_by": "x"},
        {"waiting_for": "CI main", "waiting_until": "", "waiting_declared_by": "x"},
    ):
        task_id = await _running_commit(client, db)
        await _set(db, task_id, **fields)
        resp = await client.post(
            f"/api/tasks/{task_id}/return-to-work", json={"reason": "вернуть"}
        )
        assert resp.status_code == 200, resp.text
        card = resp.json()
        assert card["status"] == "open"
        assert (card["waiting_for"], card["waiting_until"]) == ("", "")
        assert card["waiting_declared_by"] == ""
        feed = " ".join(
            dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
        )
        assert "CI main" in feed and "ожидани" in feed.lower(), feed

    task_id = await _running_commit(client, db)
    await _set(
        db,
        task_id,
        waiting_for="CI main",
        waiting_until=_PAST,
        waiting_declared_by="x",
        claimed_by="agent-a",
    )
    before = await _snapshot(db, task_id)
    resp = await client.post(
        f"/api/tasks/{task_id}/return-to-work", json={"reason": "вернуть"}
    )
    assert resp.status_code == 409, resp.text
    assert [s["sign"] for s in resp.json()["detail"]["signs"]] == ["claimed_by"]
    assert await _snapshot(db, task_id) == before, "ожидание не тронуто, аудита нет"


async def test_return_running_rechecks_inside_the_write(
    client: AsyncClient, db: aiosqlite.Connection, db_dsn, monkeypatch
) -> None:
    # AC-4: признак, появившийся после внешнего чтения, ловится перечиткой под
    # write-локом: 409, конкурентная запись сохранена, задача не изменилась.
    from hub.services import return_running

    concurrent_writes = {
        "slot": (
            "INSERT INTO executor_slots (task_id, channel, slot) "
            "VALUES (?, 'slot', 's9')",
            "open_slot",
        ),
        "wait": (
            "UPDATE tasks SET waiting_for='CI', waiting_until='2999-01-01 00:00:00',"
            " waiting_declared_by='a' WHERE id=?",
            "declared_wait",
        ),
    }
    for name, (sql, code) in concurrent_writes.items():
        task_id = await _running_commit(client, db)
        before = (await _snapshot(db, task_id))[0]
        outer_read_done, committed = asyncio.Event(), asyncio.Event()
        real = return_running.write_transaction

        @asynccontextmanager
        async def _gated(conn, _real=real):
            outer_read_done.set()  # внешнее чтение позади, лока ещё нет
            await committed.wait()
            async with _real(conn):
                yield

        monkeypatch.setattr(return_running, "write_transaction", _gated)

        async def _writer(sql=sql, task_id=task_id):
            await outer_read_done.wait()
            other = await db_module.connect(db_dsn)
            try:
                await other.execute(sql, (task_id,))
                await other.commit()
            finally:
                await other.close()
            committed.set()

        writer = asyncio.create_task(_writer())
        conn = await db_module.connect(db_dsn)
        try:
            with pytest.raises(HTTPException) as caught:
                await services.return_to_work(
                    conn,
                    task_id,
                    TaskReturnToWork(reason="гонка"),
                    actor="denis",
                )
        finally:
            await conn.close()
        await writer
        assert caught.value.status_code == 409, name
        assert caught.value.detail["reason"] == "return_running_has_work"
        assert [s["sign"] for s in caught.value.detail["signs"]] == [code], name
        after = dict(await repo.get_task(db, task_id))
        assert after["status"] == "running", name
        for field in ("claimed_by", "claim_session_id", "job_id", "status_entered_at"):
            assert after[field] == before[field], (name, field)
        if name == "slot":
            rows = await (
                await db.execute(
                    "SELECT COUNT(*) FROM executor_slots WHERE task_id=? "
                    "AND released_at IS NULL",
                    (task_id,),
                )
            ).fetchone()
            assert rows[0] == 1, "конкурентная запись сохранена"
        else:
            assert after["waiting_for"] == "CI", "объявленное ожидание сохранено"
        monkeypatch.undo()


async def test_return_running_for_state_and_needs_info_keeps_old_rules(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    # AC-5 (дополнение): needs_info commit - 400 и после этой задачи; state из
    # running идёт прежним путём, даже с признаком работы (claimed_by); общая
    # константа не расширена - её читает оркестратор (executor_launch).
    from hub.services import lifecycle, return_running

    assert lifecycle.RETURN_TO_WORK_STATUSES == {"review", "fix_requested"}
    assert return_running.RETURN_RUNNING_STATUSES == {"running"}
    task_id = await _running_commit(client, db)
    await _set(db, task_id, status="needs_info")
    resp = await client.post(
        f"/api/tasks/{task_id}/return-to-work", json={"reason": "нет"}
    )
    assert resp.status_code == 400, resp.text

    state_id = await _running_commit(client, db)
    await _set(db, state_id, result_kind="state", claimed_by="agent-a")
    resp = await client.post(
        f"/api/tasks/{state_id}/return-to-work", json={"reason": "state как раньше"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"


async def _session_row(db, session_id: str) -> dict:
    cur = await db.execute(
        "SELECT current_task_id, last_seen_at FROM agent_sessions WHERE session_id=?",
        (session_id,),
    )
    return dict(await cur.fetchone())


async def test_return_running_leaves_the_session_alive_signs_alone(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    # Находка ревью P2-1: возврат не освежает last_seen_at (offline не станет
    # online) и не стирает указатель сессии на ДРУГУЮ задачу.
    other = await _running_commit(client, db)
    task_id = await _running_commit(client, db)
    await _register_session(db, "sess-elsewhere", last_seen=_PAST)
    await _sql(
        db,
        "UPDATE agent_sessions SET current_task_id=? WHERE session_id=?",
        (other, "sess-elsewhere"),
    )
    await _set(db, task_id, claim_session_id="sess-elsewhere")
    before = await _session_row(db, "sess-elsewhere")
    resp = await client.post(
        f"/api/tasks/{task_id}/return-to-work", json={"reason": "чужой указатель"}
    )
    assert resp.status_code == 200, resp.text
    assert await _session_row(db, "sess-elsewhere") == before
    assert before["current_task_id"] == other and before["last_seen_at"] == _PAST

    mine = await _running_commit(client, db)
    await _register_session(db, "sess-mine", last_seen=_PAST)
    await _sql(
        db,
        "UPDATE agent_sessions SET current_task_id=? WHERE session_id=?",
        (mine, "sess-mine"),
    )
    await _set(db, mine, claim_session_id="sess-mine")
    resp = await client.post(
        f"/api/tasks/{mine}/return-to-work", json={"reason": "свой указатель"}
    )
    assert resp.status_code == 200, resp.text
    after = await _session_row(db, "sess-mine")
    assert after == {"current_task_id": None, "last_seen_at": _PAST}


async def test_return_running_writer_waits_for_the_write_lock(
    client: AsyncClient, db: aiosqlite.Connection, db_dsn, monkeypatch
) -> None:
    # Находка ревью P2-2(а): между чтением признаков и переходом чужая запись
    # (открыть слот) обязана ЖДАТЬ write-лок, а не вклиниваться. Без
    # BEGIN IMMEDIATE писатель коммитится сразу и задача уходит в open при
    # открытом слоте.
    from hub.services import return_running

    task_id = await _running_commit(client, db)
    real_signs = return_running.work_signs
    state: dict = {"writer_done_in_window": None}

    async def _signs_then_a_writer_tries(conn, task):
        signs = await real_signs(conn, task)
        done = asyncio.Event()

        async def _writer():
            other = await db_module.connect(db_dsn)
            try:
                await other.execute(
                    "INSERT INTO executor_slots (task_id, channel, slot) "
                    "VALUES (?, 'slot', 's7')",
                    (task_id,),
                )
                await other.commit()
            finally:
                await other.close()
            done.set()

        state["writer"] = asyncio.create_task(_writer())
        await asyncio.sleep(0.5)  # окно между чтением признаков и записью
        state["writer_done_in_window"] = done.is_set()
        return signs

    monkeypatch.setattr(return_running, "work_signs", _signs_then_a_writer_tries)
    conn = await db_module.connect(db_dsn)
    try:
        card = await services.return_to_work(
            conn, task_id, TaskReturnToWork(reason="окно"), actor="denis"
        )
    finally:
        await conn.close()
    await state["writer"]
    assert state["writer_done_in_window"] is False, "писатель ждёт лок возврата"
    assert card.status.value == "open"
    row = await (
        await db.execute(
            "SELECT COUNT(*) FROM executor_slots WHERE task_id=? "
            "AND released_at IS NULL",
            (task_id,),
        )
    ).fetchone()
    assert row[0] == 1, "запись писателя пришла после возврата и сохранена"


async def test_return_running_rolls_back_every_step_on_a_late_failure(
    client: AsyncClient, db: aiosqlite.Connection, db_dsn, monkeypatch
) -> None:
    # Находка ревью P2-2(б): сбой ПОСЛЕ последнего шага (запись activity) откатывает
    # всё: статус, ожидание, окно одобрения, указатель сессии, decision, событие,
    # activity. Проверка - с отдельного соединения, то есть по закоммиченному.
    task_id = await _running_commit(client, db)
    await _register_session(db, "sess-rb", last_seen=_PAST)
    await _sql(
        db,
        "UPDATE agent_sessions SET current_task_id=? WHERE session_id=?",
        (task_id, "sess-rb"),
    )
    await _set(
        db,
        task_id,
        claim_session_id="sess-rb",
        waiting_for="CI",
        waiting_until=_PAST,
        waiting_declared_by="x",
        review_verdict="approved",
        review_verdict_generation=1,
        submission_generation=1,
        claimed_at=_PAST,
    )

    async def _state(c):
        task = dict(await repo.get_task(c, task_id))
        feed = [dict(u)["content"] for u in await repo.get_task_updates(c, task_id)]
        events = [dict(e)["kind"] for e in await repo.list_events(c, since=0)]
        acts = await (await c.execute("SELECT COUNT(*) FROM activity_log")).fetchone()
        return (
            {
                k: task[k]
                for k in (
                    "status",
                    "waiting_for",
                    "waiting_until",
                    "waiting_declared_by",
                    "review_verdict_closed_generation",
                    "claim_session_id",
                    "claimed_at",
                    "review_cycle",
                )
            },
            feed,
            events,
            acts[0],
            await _session_row(c, "sess-rb"),
        )

    before = await _state(db)
    real_log = db_module.log_activity

    async def _log_then_fail(*args, **kwargs):
        await real_log(*args, **kwargs)
        raise RuntimeError("сбой после записи activity")

    monkeypatch.setattr(db_module, "log_activity", _log_then_fail)
    conn = await db_module.connect(db_dsn)
    try:
        with pytest.raises(RuntimeError):
            await services.return_to_work(
                conn, task_id, TaskReturnToWork(reason="откат"), actor="denis"
            )
    finally:
        await conn.close()
    monkeypatch.undo()
    fresh = await db_module.connect(db_dsn)
    try:
        assert await _state(fresh) == before
        assert before[0]["status"] == "running"
    finally:
        await fresh.close()
