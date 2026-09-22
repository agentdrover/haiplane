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
