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

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
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
