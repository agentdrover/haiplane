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
    note_completion_without_delivery,
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


@pytest.mark.parametrize("reason", ["   ", "  a", "\t\n "])
async def test_whitespace_never_buys_an_acknowledgement(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    """Пробелы не проходят порог причины ни одним из двух входов.

    Валидатор ревью назвал дыру точнее, чем сама находка: схема считала длину
    ДО обрезки, поэтому три пробела проходили порог и получали 404 «строки
    нет» — отказ, называющий не ту причину, — а «  a» записывало причину в
    один символ. Форма в инбоксе обрезала сама, API нет: два входа в один
    глагол вели себя по-разному, и это худший вид расхождения.
    """
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)

    api = await client.post(
        f"/api/delivery/discrepancies/{task_id}/acknowledge", json={"reason": reason}
    )
    assert api.status_code == 422, "порог считается по обрезанной причине"

    web = await client.post(
        f"/tasks/{task_id}/web-acknowledge-delivery",
        data={"reason": reason},
        follow_redirects=False,
    )
    assert web.status_code == 422

    row = await repo.get_delivery_discrepancy(db, task_id)
    assert not (row["acknowledged_at"] or ""), "пробелами расхождение не заткнуть"


# ---- находки ревью №281: обещание в комментарии против обещания в коде ----


async def test_a_failed_voice_leaves_nothing_behind(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Голос и отметка о нём — одно целое, и это держит откат, а не комментарий.

    add_task_update и insert_event не коммитят; коммитит record_delivery_
    discrepancy. Значит исключение МЕЖДУ голосом и отметкой оставляло алерт
    незакоммиченным, но живым в открытой транзакции, — и первый же коммит
    следующего кандидата записывал его БЕЗ отметки. Расхождение звучало бы
    дважды, а обещание «либо сказано и помечено, либо не случилось ничего»
    оказалось бы неправдой.

    Проверяется двумя кандидатами в одном проходе: на первом голос падает,
    второй проходит и коммитит. Без отката алерт первого уезжает в базу на
    чужом коммите — одного кандидата для этого мало, и потому его здесь два.

    Исключение намеренно ПИТОНОВСКОЕ, а не ошибка SQLite: та оборвала бы
    транзакцию сама, и дыры не было бы видно. Здоровая грязная транзакция —
    ровно тот случай, ради которого откат и стоит.
    """
    broken = await _completed_task(db, client, title="Voice fails", pr=555)
    intact = await _completed_task(db, client, title="Voice lands", pr=556)
    _pr_states(monkeypatch, {555: "open", 556: "open"})

    real_insert = repo.insert_event

    async def _fail_for_the_first(db_, **kwargs):
        if kwargs.get("task_id") == broken:
            raise RuntimeError("голос не записался")
        return await real_insert(db_, **kwargs)

    monkeypatch.setattr(repo, "insert_event", _fail_for_the_first)
    await scan_completed_deliveries(db)

    assert await _alerts(db, broken) == [], (
        "упавший голос не оставляет следов: алерт без отметки прозвучал бы "
        "снова следующим проходом"
    )
    assert await repo.get_delivery_discrepancy(db, broken) is None, (
        "и строки реестра тоже нет — откат откатывает целиком"
    )
    assert len(await _alerts(db, intact)) == 1, (
        "соседний кандидат не пострадал: один плохой ряд не валит свип"
    )


async def test_an_acknowledged_row_stops_pushing_the_glance_badge(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Счётчик на ГЛАВНОЙ, а не только в разделе инбокса.

    Исключение признанных строк живёт в двух местах: в секции и в топбаре на
    «/». Тесты трогали лишь /partials/inbox, поэтому вернуть исключение в
    топбаре можно было, не уронив ни одного теста, — и бейдж продолжал бы
    считать то, что владелец уже разобрал. Счётчик, который не возвращается
    к нулю, никто не читает: это та самая смерть сигнала, против которой
    задача и заведена.
    """
    task_id = await _completed_task(db, client, title="Left open", pr=777)
    _pr_states(monkeypatch, {777: "open"})
    await scan_completed_deliveries(db)

    page = await client.get("/")
    assert page.status_code == 200
    before = page.text

    await repo.acknowledge_delivery_discrepancy(
        db, task_id, by="denis", reason="PR оставлен открытым намеренно"
    )

    page = await client.get("/")
    assert page.status_code == 200
    assert page.text != before, "признание обязано быть видно на главной"
    # Считаем не текст бейджа, а сам факт: до признания строка в счётчике
    # была, после — нет. Сравнение по числу устойчивее, чем по вёрстке.
    assert _inbox_badge(page.text) < _inbox_badge(before), (
        "признанная строка продолжает толкать бейдж на главной"
    )


def _inbox_badge(html: str) -> int:
    """Число в топбаре у ярлыка Inbox на «/».

    Читается прицельно из блока самого счётчика, а не поиском ближайшей
    цифры за словом: соседние числа на дашборде сделали бы тест зелёным
    по случайности.
    """
    import re

    block = re.search(r"topbar-stat--inbox.*?</a>", html, re.S)
    assert block, "на главной нет счётчика Inbox — разметка изменилась"
    number = re.search(r'topbar-stat-value">\s*(\d+)', block.group(0))
    assert number, "у счётчика Inbox нет значения"
    return int(number.group(1))


# ---- Находки ревью #294, закрытые кодом ----


async def test_a_flapping_provider_does_not_turn_the_card_into_a_metronome(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дребезг провайдера — не новость, а помеха. Говорить по разу на факт.

    Память о сказанном была ОДНОЙ ячейкой: UNKNOWN затирал в ней «про PR_OPEN
    уже сказали», и возврат в PR_OPEN снова считался новостью. При нестабильном
    GitHub (пустой ответ gh читается как UNKNOWN — штатный случай, #802) это
    давало голос на КАЖДОМ проходе свипа: алерт на карточке закрытой задачи и
    событие в ленте, которая будит агентов.

    Шесть проходов с чередованием обязаны дать ровно два голоса: по одному на
    каждый настоящий факт. Возраст здесь не растёт, рубеж не переходится —
    значит всё, что сверх двух, есть повтор.
    """
    task_id = await _completed_task(db, client, title="Дребезг", pr=920)
    for tick in range(6):
        _pr_states(monkeypatch, {920: "open"} if tick % 2 == 0 else {})
        await scan_completed_deliveries(db)

    alerts = await _alerts(db, task_id)
    assert len(alerts) == 2, alerts
    assert sum("НЕ доставлена" in a for a in alerts) == 1, alerts
    assert sum("НЕ УДАЛОСЬ" in a for a in alerts) == 1, alerts
    # Будящих событий ровно столько же: лента не должна будить чаще, чем есть
    # о чём разбудить.
    assert len(await _events(db, task_id)) == 2


async def test_an_acknowledged_fact_does_not_silence_a_different_one(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Признание относится к ФАКТУ, а не к задаче навсегда.

    Решение владельца 08.09.2026. «PR держим открытым намеренно» — суждение о
    том, что PR открыт. Когда хаб перестаёт видеть состояние PR, факт другой:
    «доставку подтвердить НЕ УДАЛОСЬ». Его никто не одобрял, и молчать о нём
    значит выдавать старое решение за оценку новой обстановки.
    """
    task_id = await _completed_task(db, client, title="Признанное", pr=910)
    _pr_states(monkeypatch, {910: "open"})
    await scan_completed_deliveries(db)
    assert len(await _alerts(db, task_id)) == 1

    await repo.acknowledge_delivery_discrepancy(
        db, task_id, by="denis", reason="держим открытым до релиза платы"
    )
    # Признанный факт молчит и на следующем проходе — это по-прежнему верно.
    await scan_completed_deliveries(db)
    assert len(await _alerts(db, task_id)) == 1

    # Провайдер замолчал: факт сменился на тот, которого не признавали.
    _pr_states(monkeypatch, {})
    await scan_completed_deliveries(db)
    alerts = await _alerts(db, task_id)
    assert any("НЕ УДАЛОСЬ" in a for a in alerts), alerts
    # И признание не стёрто: оно всё ещё относится к своему факту.
    row = await repo.get_delivery_discrepancy(db, task_id)
    assert row["ack_reason"] == "держим открытым до релиза платы"
    assert row["acknowledged_state"] == "pr_open"


async def test_the_last_counter_lets_the_board_say_all_clear(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Счётчиков внимания три, и разойтись им нельзя.

    Топбар и заголовок секции признанные строки уже исключали, а total_inbox —
    единственный вход в блок «All clear» — считал их по-прежнему. Разобрав
    последнее дело, владелец не получал подтверждения, что дел не осталось:
    счётчик, который не возвращается к нулю, никто не читает.
    """
    task_id = await _completed_task(db, client, title="Единственное дело", pr=930)
    _pr_states(monkeypatch, {930: "open"})
    await scan_completed_deliveries(db)
    assert "All clear" not in (await client.get("/partials/inbox")).text

    await repo.acknowledge_delivery_discrepancy(
        db, task_id, by="denis", reason="PR держим открытым намеренно"
    )
    body = (await client.get("/partials/inbox")).text
    assert "All clear" in body
    # Строка при этом НЕ исчезла: заткнуть можно, стереть нельзя.
    assert f"inbox-undelivered-{task_id}" in body


async def test_crossing_an_age_threshold_earns_every_fact_a_fresh_voice(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Рубеж обнуляет память о сказанном — иначе он молчит про второй факт.

    Набор озвученных состояний копится ВНУТРИ рубежа: это и глушит метроном.
    Но сам рубеж существует ради права сказать «это длится дольше, чем вы
    думали», и право это принадлежит КАЖДОМУ факту, а не первому успевшему.
    Без обнуления состояние, прозвучавшее сутки назад, на новом рубеже
    считалось бы уже сказанным и не звучало никогда.

    Состояние достижимо обычной жизнью расхождения: строка живёт неделями, а
    свип ходит по ней каждые DELIVERY_SCAN_MINUTES — возраст растёт сам, руками
    его подкручивать не нужно ни на одном продакшн-пути.
    """
    task_id = await _completed_task(db, client, title="Долгое", pr=950)

    # Первые сутки: оба факта уже прозвучали по разу.
    _pr_states(monkeypatch, {950: "open"})
    await scan_completed_deliveries(db)
    _pr_states(monkeypatch, {})
    await scan_completed_deliveries(db)
    assert len(await _alerts(db, task_id)) == 2

    # Рубеж суток пройден. Провайдер отвечает — звучит «не доставлено».
    await _age(db, task_id, 30)
    _pr_states(monkeypatch, {950: "open"})
    await scan_completed_deliveries(db)
    assert len(await _alerts(db, task_id)) == 3

    # На том же рубеже провайдер снова замолчал: это ВТОРОЙ факт, и он тоже
    # заслужил голос — сутки назад его сказали про другой возраст.
    _pr_states(monkeypatch, {})
    await scan_completed_deliveries(db)
    alerts = await _alerts(db, task_id)
    assert len(alerts) == 4, alerts
    assert "30 ч" in alerts[-1], alerts[-1]


# ---- Второй раунд ревью: находки, внесённые самой починкой ----


async def test_a_row_that_speaks_is_never_drawn_as_settled(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Голос и доска обязаны спрашивать ОДНО И ТО ЖЕ.

    Правило «признан только тот факт, который признавали» стояло в алертере, а
    три места, решающие, ВИДЕН ли сигнал человеку, спрашивали грубее — просто
    «признание было». Разъехавшись, они дали худшее из возможного: карточка
    будила агентов, а доска рисовала строку приглушённой, без кнопки, и
    считала её нулём. Счётчик возвращался к нулю раньше, чем факт закрыт.
    """
    task_id = await _completed_task(db, client, title="Разъезд", pr=970)
    # Провайдер молчит: расхождение начинается как «подтвердить не удалось».
    _pr_states(monkeypatch, {})
    await scan_completed_deliveries(db)
    await repo.acknowledge_delivery_discrepancy(
        db, task_id, by="denis", reason="GitHub лежит, разберусь как встанет"
    )

    # Провайдер ожил: PR открыт. Это ДРУГОЙ факт, его никто не признавал.
    _pr_states(monkeypatch, {970: "open"})
    await scan_completed_deliveries(db)
    assert any("НЕ доставлена" in a for a in await _alerts(db, task_id))

    body = (await client.get("/partials/inbox")).text
    assert f"inbox-undelivered-{task_id}" in body
    # Раз голос прозвучал — строка обязана выглядеть требующей внимания и
    # снова предлагать кнопку, иначе заткнуть её человеку нечем.
    assert "badge-muted" not in body, body[body.find("inbox-undelivered") :][:400]
    assert "web-acknowledge-delivery" in body
    assert "All clear" not in body
    # И прежнее решение человека с доски не пропадает: иначе он видит крик и
    # не понимает, куда делось то, что он уже решал.
    assert "признавалось для другого факта" in body
    assert "GitHub лежит" in body

    # И топбар на «/» — третий счётчик внимания. Он живёт в другом файле и
    # именно поэтому уже дважды отставал от остальных.
    assert _inbox_badge((await client.get("/")).text) == 1


async def test_settled_rows_never_crowd_a_live_one_out_of_the_board(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Признанные строки не вытесняют непризнанные за край выдачи.

    Признанные живут вечно и они же самые старые. При сортировке только по
    возрасту они занимали всё окно, а счёт, взятый с усечённой страницы,
    говорил «ноль» — то есть доска УТВЕРЖДАЛА «всё чисто» при живом
    неразобранном расхождении. Промолчать было бы честнее, чем соврать.
    """
    answers: dict[int, str] = {}
    for n in range(20):
        old_id = await _completed_task(db, client, title=f"Старое {n}", pr=800 + n)
        answers[800 + n] = "open"
        _pr_states(monkeypatch, answers)
        await _age(db, old_id, 500 + n)
        await scan_completed_deliveries(db)
        await repo.acknowledge_delivery_discrepancy(
            db, old_id, by="denis", reason="держим открытым намеренно"
        )

    fresh_id = await _completed_task(db, client, title="Свежее и живое", pr=899)
    answers[899] = "open"
    _pr_states(monkeypatch, answers)
    await scan_completed_deliveries(db)

    body = (await client.get("/partials/inbox")).text
    assert "All clear" not in body, "доска соврала при живом расхождении"
    assert f"inbox-undelivered-{fresh_id}" in body, "живая строка вытеснена за LIMIT"


async def test_one_name_written_by_the_neighbour_reads_as_a_set_of_one(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Соседний писатель кладёт ОДНО имя — читатель ждёт множество.

    ``note_completion_without_delivery`` (ручное принятие, force-complete,
    done-отчёт без ревью) пишет в память о сказанном одно имя состояния, а
    свип читает её как множество через запятую. Стык двух писателей одного
    поля — ровно тот класс, на котором эта задача спотыкалась дважды, и
    держать его на честном слове нельзя.
    """
    task_id = await _completed_task(db, client, title="Ручное", pr=960)
    _pr_states(monkeypatch, {960: "open"})
    await note_completion_without_delivery(db, task_id, via="human_accept")
    said = (await repo.get_delivery_discrepancy(db, task_id))["alerted_state"]
    assert said == "pr_open", said

    spoken = len(await _alerts(db, task_id))
    await scan_completed_deliveries(db)
    assert len(await _alerts(db, task_id)) == spoken, "свип повторил сказанное соседом"


async def test_an_unsettled_fact_keeps_escalating_even_beside_a_settled_one(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Рубеж принадлежит ФАКТУ, а не строке.

    Я сперва написал здесь обратное — «признавшему рубежи не положены» — и это
    оказалось неверно ровно наоборот. Проверка признания возвращает молчание
    раньше, значит до рубежей доходит ТОЛЬКО факт, которого никто не одобрял.
    Глушить его эскалацию — второй раз построить «одно суждение отнимает голос
    у другого», от чего задача и защищает. Признанный факт при этом молчит
    по-прежнему: у него своя, ранняя дверь.
    """
    task_id = await _completed_task(db, client, title="Разобрано", pr=980)
    _pr_states(monkeypatch, {980: "open"})
    await scan_completed_deliveries(db)
    await repo.acknowledge_delivery_discrepancy(
        db, task_id, by="denis", reason="держим открытым до релиза платы"
    )

    # Провайдер замолчал: факт другой, его никто не признавал — голос один.
    _pr_states(monkeypatch, {})
    await scan_completed_deliveries(db)
    after_news = len(await _alerts(db, task_id))
    assert any("НЕ УДАЛОСЬ" in a for a in await _alerts(db, task_id))

    # Сутки, трое суток, неделя — по одному напоминанию на рубеж: факт живой и
    # неодобренный, а «это длится дольше, чем вы думали» — про него.
    for hours in (30, 80, 200):
        await _age(db, task_id, hours)
        await scan_completed_deliveries(db)
    assert len(await _alerts(db, task_id)) == after_news + 3

    # А признанный факт молчит и через неделю: рубежи ему не открывают дверь.
    _pr_states(monkeypatch, {980: "open"})
    await scan_completed_deliveries(db)
    settled = len(await _alerts(db, task_id))
    await _age(db, task_id, 400)
    await scan_completed_deliveries(db)
    assert len(await _alerts(db, task_id)) == settled


async def test_a_fact_heard_before_the_decision_still_speaks_after_it(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Решение человека — точка отсчёта заново, а не пломба на всю историю.

    Память о сказанном копит ВСЕ когда-либо озвученные состояния, а признание
    навсегда снимает возрастные рубежи, которые её сбрасывали. Значит факт,
    прозвучавший ДО признания, при возврате читался как «уже сказанный» и
    молчал вечно — хотя одобряли не его. Хуже всего то, что на доске строка
    при этом выглядит правильно: кнопка вернулась, счётчик считает. Молчал бы
    только канал, который будит, — то есть тот единственный, ради которого
    задача заведена.
    """
    task_id = await _completed_task(db, client, title="Два факта", pr=990)
    _pr_states(monkeypatch, {990: "open"})
    await scan_completed_deliveries(db)
    _pr_states(monkeypatch, {})
    await scan_completed_deliveries(db)
    assert len(await _alerts(db, task_id)) == 2

    # Владелец признаёт то, что видит СЕЙЧАС: «подтвердить не удалось».
    await repo.acknowledge_delivery_discrepancy(
        db, task_id, by="denis", reason="GitHub лежит, разберусь как встанет"
    )

    # Сутки спустя провайдер ожил: PR открыт. Этого никто не признавал.
    await _age(db, task_id, 30)
    _pr_states(monkeypatch, {990: "open"})
    await scan_completed_deliveries(db)
    alerts = await _alerts(db, task_id)
    assert len(alerts) == 3, alerts
    assert "НЕ доставлена" in alerts[-1]
    # Дальше он эскалирует как обычный неодобренный факт — по разу на рубеж.
    for hours in (80, 200):
        await _age(db, task_id, hours)
        await scan_completed_deliveries(db)
    assert len(await _alerts(db, task_id)) == 5


async def test_a_decision_restarts_the_record_of_what_was_said(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Решение человека — точка отсчёта заново, и ждать рубежа оно не должно.

    Без обнуления памяти факт, звучавший ДО признания, при возврате читался
    как «уже сказанный» и молчал до ближайшего возрастного рубежа — то есть до
    суток. Это та же болезнь, от которой заведена задача, просто в меньшей
    дозе: недоставленная работа стоит непрозвучавшей, пока не состарится.
    Здесь возраст НЕ подкручивается намеренно — проверяется именно тот же день.
    """
    task_id = await _completed_task(db, client, title="В тот же день", pr=991)
    _pr_states(monkeypatch, {991: "open"})
    await scan_completed_deliveries(db)
    _pr_states(monkeypatch, {})
    await scan_completed_deliveries(db)
    assert len(await _alerts(db, task_id)) == 2

    await repo.acknowledge_delivery_discrepancy(
        db, task_id, by="denis", reason="GitHub лежит, разберусь как встанет"
    )
    said = (await repo.get_delivery_discrepancy(db, task_id))["alerted_state"]
    assert said == "", f"память о сказанном не обнулена: {said!r}"

    # Провайдер ожил в тот же час. Рубеж не пройден — но факт неодобренный.
    _pr_states(monkeypatch, {991: "open"})
    await scan_completed_deliveries(db)
    alerts = await _alerts(db, task_id)
    assert len(alerts) == 3, alerts
    assert "НЕ доставлена" in alerts[-1]


# ---- #1210: разбор списком не теряет список ----


async def test_acknowledging_keeps_the_project_filter(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Фильтр проекта переживает признание (#1210, AC-1).

    Расхождения разбирают списком, и фильтр в нём несущий: возврат на голый
    "/" отправлял человека в общий перечень, где следующую строку надо искать
    заново. Соседние действия того же инбокса это уже умеют — batch-approve
    везёт проект скрытым полем, ссылка «Разобрать» кладёт его в адрес.
    """
    await db.execute("INSERT INTO projects (id, slug, name) VALUES (1, 'alpha', 'A')")
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    await db.execute("UPDATE tasks SET project_id=1 WHERE id=?", (task_id,))
    await db.commit()
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)

    page = await client.get("/partials/inbox?project=alpha")
    assert "web-acknowledge-delivery" in page.text, (
        "строка видна под фильтром — иначе тест проверяет пустоту"
    )
    assert 'name="return_project" value="alpha"' in page.text, (
        "форма обязана знать, из какого вида её нажали"
    )

    resp = await client.post(
        f"/tasks/{task_id}/web-acknowledge-delivery",
        data={
            "reason": "PR держим открытым до релиза платы",
            "return_project": "alpha",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/?project=alpha", (
        "возврат в тот же отфильтрованный вид, а не в общий список"
    )


async def test_acknowledging_without_a_filter_still_lands_on_the_board(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без фильтра поведение прежнее — и без пустого ?project= в адресе (#1210, AC-2)."""
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)

    resp = await client.post(
        f"/tasks/{task_id}/web-acknowledge-delivery",
        data={"reason": "PR держим открытым до релиза платы", "return_project": "  "},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/", (
        "пустой фильтр не становится пустым параметром: адрес прежний"
    )
    row = await repo.get_delivery_discrepancy(db, task_id)
    assert row["acknowledged_at"], "признание записано и без фильтра"


async def test_the_project_field_steers_the_redirect_and_nothing_else(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Поле приходит от клиента и не смеет быть ничем, кроме адреса (#1210, AC-3).

    Два разных вопроса, и оба обязаны иметь ответ «нет». Первый: может ли
    подменённое значение увести на чужой хост — слаг подставляется в один
    известный путь и экранируется, поэтому не может. Второй: может ли оно
    сменить строку, которую признают, — запись идёт по task_id из адреса
    маршрута, а не по чему-либо из формы.
    """
    mine = await _completed_task(db, client, title="Left open", pr=444)
    other = await _completed_task(db, client, title="Someone else", pr=445)
    _pr_states(monkeypatch, {444: "open", 445: "open"})
    await scan_completed_deliveries(db)

    resp = await client.post(
        f"/tasks/{mine}/web-acknowledge-delivery",
        data={
            "reason": "PR держим открытым до релиза платы",
            "return_project": "//evil.example/steal",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    where = resp.headers["location"]
    assert where.startswith("/?project="), "адрес возврата остаётся внутренним"
    assert "/" not in where[len("/?project=") :], (
        "слаг едет экранированным ЦЕЛИКОМ: он данные, а не кусок пути"
    )

    assert (await repo.get_delivery_discrepancy(db, mine))["acknowledged_at"], (
        "признана строка из адреса маршрута"
    )
    assert not (
        (await repo.get_delivery_discrepancy(db, other))["acknowledged_at"] or ""
    ), "поле формы не выбирает, чью строку признать"
