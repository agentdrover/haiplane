"""Найденное расхождение доставки больше не ждёт, пока о нём спросят (#1198).

ЧТО ЗДЕСЬ НЕ СТРОИТСЯ, чтобы файл не читался как «хаб был слеп». Обнаружение
построено и верно: свип сверяет закрытые задачи с состоянием их PR, отделяет
«не доставлено» от «спросить не удалось» и помнит возраст. Алерт в карточку
тоже был, и гасился он правильно — один раз на состояние.

ЧЕГО НЕ ХВАТАЛО. Карточка ЗАКРЫТОЙ задачи — страница, на которую не
возвращаются: задача выпала из review, из running, из очереди решений. Замер,
из которого выросла постановка, показывает это буквально — на #1138 алерт лёг
03.09 в 08:18:34, через секунду после принятия, и остался непрочитанным.
Поэтому голос переезжает в ленту событий: её читают hub_wait_events и Stop-хук,
то есть она будит, а не ждёт, пока в неё заглянут. Соседний путь — ручное
принятие — событие писал всегда; свип, который находит расхождения САМ, не
писал ни одного.

И вторая половина, без которой первая вредна: у голоса должен быть способ
замолчать законно. Расхождение бывает намеренным, и если сказать об этом
нечем, первый же такой случай учит пропускать сигнал.
"""

from __future__ import annotations

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.services.delivery_state import (
    DISCREPANCY_EVENT,
    scan_completed_deliveries,
    undelivered_completed_tasks,
)
from tests.test_accept_without_delivery import (
    _alerts,
    _completed_task,
    _pr_states,
)


async def _events(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = [dict(e) for e in await repo.list_events(db, since=0)]
    return [
        e for e in rows if e["kind"] == DISCREPANCY_EVENT and e["task_id"] == task_id
    ]


async def _age(db: aiosqlite.Connection, task_id: int, hours: int) -> None:
    """Состарить расхождение: возраст считается от закрытия задачи."""
    await db.execute(
        "UPDATE tasks SET completed_at = datetime('now', ?) WHERE id=?",
        (f"-{hours} hours", task_id),
    )
    await db.commit()


# ---- AC-1: расхождение заявляет о себе само ----


async def test_an_undelivered_task_speaks_without_being_asked(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await _age(db, task_id, 5)

    await scan_completed_deliveries(db)

    events = await _events(db, task_id)
    assert len(events) == 1, (
        "лента событий — единственный канал, который будит; карточка "
        "закрытой задачи ждёт, пока на неё зайдут, и на #1138 не дождалась"
    )
    import json

    payload = json.loads(events[0]["payload"] or "{}")
    assert payload["pr"] == 444, "без номера PR сигнал требует той же ручной сверки"
    assert payload["age_hours"] >= 5, "возраст обязан ехать вместе с сигналом"
    assert payload["state"] == "pr_open"
    # Реестр при этом не спрашивали ни разу — а он всё равно заговорил.
    assert "не доставлена" in " ".join(await _alerts(db, task_id)).lower()


# ---- AC-2: один голос, а не метроном ----


async def test_the_same_discrepancy_is_not_repeated_every_cycle(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})

    await scan_completed_deliveries(db)
    await scan_completed_deliveries(db)
    await scan_completed_deliveries(db)

    assert len(await _events(db, task_id)) == 1
    assert len(await _alerts(db, task_id)) == 1


async def test_crossing_an_age_threshold_speaks_once_more_and_no_more(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Рубеж — повод сказать «это длится дольше, чем вы думали». Один раз.

    Проверяется ОБА конца правила: что рубеж вообще звучит и что он не звучит
    дважды. Без второй половины «сообщать при переходе рубежа» выродилось бы
    в «сообщать, пока возраст больше суток», то есть на каждом тике.
    """
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)
    assert len(await _events(db, task_id)) == 1

    await _age(db, task_id, 30)  # первый рубеж — сутки
    await scan_completed_deliveries(db)
    await scan_completed_deliveries(db)

    events = await _events(db, task_id)
    assert len(events) == 2, "переход рубежа — новость, и она одна"
    import json

    assert json.loads(events[1]["payload"])["age_hours"] >= 24


# ---- AC-3: признанное молчит, но остаётся видимым ----


async def test_an_acknowledged_discrepancy_goes_quiet_but_stays_visible(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)
    before = len(await _events(db, task_id))

    resp = await client.post(
        f"/api/delivery/discrepancies/{task_id}/acknowledge",
        json={"reason": "PR держим открытым намеренно до релиза платы"},
    )
    assert resp.status_code == 200

    # Молчит даже там, где иначе заговорил бы: и на следующем тике, и на
    # переходе возрастного рубежа.
    await _age(db, task_id, 200)
    await scan_completed_deliveries(db)
    await scan_completed_deliveries(db)

    assert len(await _events(db, task_id)) == before, (
        "признанное законным расхождение больше не звучит"
    )
    row = next(
        r
        for r in (await undelivered_completed_tasks(db))["undelivered"]
        if r["task_id"] == task_id
    )
    assert row["ack_reason"] == "PR держим открытым намеренно до релиза платы", (
        "заткнуть можно, стереть нельзя: причина остаётся в реестре"
    )
    assert row["acknowledged_at"], "видно, что это решение, а не забывчивость"


async def test_acknowledging_without_a_reason_is_refused(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Признание без причины — выключатель, а не решение.

    Риск назван в самой постановке: способ признать законным легко
    превращается в способ глушить неудобное. Причина — единственное, что
    отличает одно от другого, поэтому её отсутствие отвергается схемой.
    """
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)

    resp = await client.post(
        f"/api/delivery/discrepancies/{task_id}/acknowledge", json={"reason": ""}
    )

    assert resp.status_code == 422
    await scan_completed_deliveries(db)
    row = await repo.get_delivery_discrepancy(db, task_id)
    assert not (row["acknowledged_at"] or ""), "пустая причина ничего не заглушила"


# ---- AC-4: незнание называется незнанием ----


async def test_unknown_is_not_dressed_as_a_discrepancy(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Состояние PR узнать не удалось» — не «работа не доставлена».

    Это не будущая функция, а живой дефект: свип объявлял UNKNOWN тем же
    текстом, что и настоящее расхождение, то есть выдавал незнание за факт —
    ровно то, что выдача реестра делать отказывается с самого начала (#897).
    """
    task_id = await _completed_task(db, client, title="Provider silent", pr=444)
    _pr_states(monkeypatch, {444: ""})  # провайдер не ответил

    await scan_completed_deliveries(db)

    said = " ".join(await _alerts(db, task_id))
    assert "не доставлена" not in said.lower(), (
        "незнание, названное фактом, — это ложь в самом громком месте"
    )
    assert "подтвердить не удалось" in said.lower()
    registry = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in registry["undelivered"]] == [], (
        "unknown не попадает в список расхождений"
    )
    assert task_id in [r["task_id"] for r in registry["unknown"]]
    events = await _events(db, task_id)
    assert len(events) == 1 and events[0]["kind"] == DISCREPANCY_EVENT
    import json

    assert json.loads(events[0]["payload"])["state"] == "unknown", (
        "событие называет состояние, а не сваливает оба в одно"
    )


# ---- Кнопка там, где строку читают ----


async def test_the_owner_can_acknowledge_from_the_inbox(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Признание живёт рядом со строкой, а не в отдельном месте.

    Владелец читает расхождения в инбоксе, а не в каталоге MCP. Кнопка,
    доступная только оттуда, куда он не ходит, — это отсутствующая кнопка.
    """
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)

    page = await client.get("/partials/inbox")
    assert f"/tasks/{task_id}/web-acknowledge-delivery" in page.text, (
        "признать законным можно там же, где расхождение видно"
    )

    resp = await client.post(
        f"/tasks/{task_id}/web-acknowledge-delivery",
        data={"reason": "PR держим открытым до релиза платы"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await _age(db, task_id, 200)
    await scan_completed_deliveries(db)
    assert len(await _events(db, task_id)) == 1, "после признания голос смолк"

    page = await client.get("/partials/inbox")
    assert "признано законным" in page.text and "релиза платы" in page.text, (
        "строка осталась видимой, и видно, чьё это решение и почему"
    )
    assert f"/tasks/{task_id}/web-acknowledge-delivery" not in page.text, (
        "признавать дважды нечего"
    )


async def test_the_inbox_button_refuses_an_empty_reason(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустая форма — выключатель. Отказ здесь, а не только в схеме API."""
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)

    resp = await client.post(
        f"/tasks/{task_id}/web-acknowledge-delivery",
        data={"reason": "   "},
        follow_redirects=False,
    )

    assert resp.status_code == 422
    row = await repo.get_delivery_discrepancy(db, task_id)
    assert not (row["acknowledged_at"] or "")


# ---- Находки машинного ревью по сдаче #1 ----


async def test_a_discrepancy_found_already_old_speaks_once_not_twice(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Расхождение, впервые увиденное УЖЕ старше рубежа, звучит один раз.

    Находка ревью, severity high, и она верна: INSERT не перечислял
    alerted_age_bucket, поэтому первая запись ложилась с нулём независимо от
    того, какой рубеж посчитал голос. Следующий свип видел «рубеж не
    отзвучал» и говорил второй раз. Случай не выдуманный: хаб смотрит на
    закрытые задачи за 30 дней назад, так что первое знакомство со старым
    расхождением — обычное дело, а не край.
    """
    task_id = await _completed_task(db, client, title="Old and open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await _age(db, task_id, 100)  # старше суток и старше трёх

    await scan_completed_deliveries(db)
    await scan_completed_deliveries(db)

    assert len(await _events(db, task_id)) == 1, (
        "первый же голос обязан запомнить пройденный рубеж"
    )
    row = await repo.get_delivery_discrepancy(db, task_id)
    assert row["alerted_age_bucket"] == 72, "рубеж записан тем же, что произнесён"


async def test_a_voice_that_fails_to_land_is_not_marked_as_said(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отметка «сказано» не должна опережать сам голос.

    Находка ревью: record_delivery_discrepancy коммитит, и в прежнем порядке
    упавшая запись события оставляла строку уже помеченной — сигнал терялся
    навсегда, потому что второй попытки правило не даёт. Проверяется
    поведением: ломаем запись события и смотрим, что следующий свип
    ГОВОРИТ, а не молчит.
    """
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})

    boom = {"fail": True}
    real_insert = repo.insert_event

    async def flaky_insert(db_, **kw):
        if boom["fail"] and kw.get("kind") == DISCREPANCY_EVENT:
            raise RuntimeError("лента событий недоступна")
        return await real_insert(db_, **kw)

    monkeypatch.setattr(repo, "insert_event", flaky_insert)
    await scan_completed_deliveries(db)  # свип не падает: строка одна из многих
    assert await _events(db, task_id) == []

    boom["fail"] = False
    await scan_completed_deliveries(db)

    assert len(await _events(db, task_id)) == 1, (
        "неудавшийся голос не считается сказанным и повторяется"
    )


async def test_acknowledging_a_task_with_no_discrepancy_is_a_404(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    """Признать нечего — так и сказать, а не сделать вид, что признали.

    Находка ревью: ветки отказа не исполнялись ни одним тестом. Молчаливое
    «ок» на признание несуществующей строки — это ложное чувство, что
    расхождение закрыто.
    """
    task_id = (await client.post("/api/tasks", json={"title": "No PR here"})).json()[
        "id"
    ]

    resp = await client.post(
        f"/tasks/{task_id}/web-acknowledge-delivery",
        data={"reason": "нечего признавать"},
        follow_redirects=False,
    )
    assert resp.status_code == 404

    api = await client.post(
        f"/api/delivery/discrepancies/{task_id}/acknowledge",
        json={"reason": "нечего признавать"},
    )
    assert api.status_code == 404


async def test_an_acknowledged_row_stops_pushing_the_badge(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Признанное перестаёт кричать и цветом, и счётчиком.

    Находка ревью: строку было видно, но она по-прежнему считалась опасной.
    Счётчик, который никогда не возвращается к нулю, перестают читать — то
    есть ровно та смерть сигнала, против которой задача и заведена.
    """
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)

    page = await client.get("/partials/inbox")
    assert "badge-failed" in page.text

    await client.post(
        f"/tasks/{task_id}/web-acknowledge-delivery",
        data={"reason": "PR держим открытым до релиза платы"},
        follow_redirects=False,
    )

    page = await client.get("/partials/inbox")
    assert "badge-muted" in page.text, "это уже не тревога, а запись о решении"
    assert 'class="inbox-count inbox-count-danger">0<' in page.text, (
        "счётчик обязан вернуться к нулю, иначе его перестанут читать"
    )
