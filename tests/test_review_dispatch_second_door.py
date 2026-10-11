"""Hub-dispatched cross-model reviews (#757): second_door (split of the former single file, #1670)."""

from __future__ import annotations

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import auth as hub_auth
from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud, local_reviewer
from hub.services.review_dispatch import (
    DEEP,
    LITE,
    maybe_dispatch_review,
    sweep_review_dispatches,
)
from tests.review_dispatch_support import (
    _LIMIT_REFUSAL,
    _LOCAL_REPORT,
    _a_report_from,
    _agent_key,
    _any_dispatch_row,
    _DispatchRecorder,
    _local_dispatches,
    _local_principal,
    _no_local_path,
    _pinned_setup,
    _reporting_stub,
    _stub_reviewer,
    _submitted,
    _wire,
)


async def test_a_refused_cloud_call_still_gets_a_report_from_the_local_path(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-1 (#1252): облако отказало НА СОЗДАНИИ — отчёт всё равно есть.

    Форж github, то есть облако сюда дотягивается и пробуется ПЕРВЫМ; это и
    есть случай, у которого второй двери не было вовсе. Проверяется
    НАБЛЮДАЕМЫМ СОСТОЯНИЕМ карточки — строка в machine_reviews текущего
    поколения под принципалом ревьюера, — а не тем, что функцию позвали.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
    _wire(monkeypatch, recorder)
    reviewer_pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-second-door",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await wait_for_local_runs()
    await db.commit()

    assert len(recorder.calls) == 1, (
        "порядок не меняется: облако пробуется ПЕРВЫМ и ровно один раз "
        "(повтор на 400 покупает тот же отказ)"
    )
    reports = [
        dict(r) for r in await repo.machine_reviews_of_generation(db, task_id, 1)
    ]
    assert len(reports) == 1, (
        "ровно это и не получалось 10.09.2026: сдача оставалась без отчёта "
        "навсегда, потому что второго способа не было"
    )
    assert reports[0]["principal_id"] == reviewer_pid, (
        "отчёт ложится под принципалом РЕВЬЮЕРА, иначе гейт его не засчитает"
    )
    assert not reports[0]["self_reviewed"], (
        "self_reviewed=1 означал бы оплаченный впустую прогон (#1128)"
    )
    dispatches = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=? ORDER BY id", (task_id,)
        )
    ]
    assert [d["channel"] for d in dispatches] == ["local"], (
        "облачной строки нет — агент не создался; локальная одна"
    )
    assert dispatches[0]["status"] == "done"


async def test_a_run_that_ended_without_a_report_opens_the_second_door(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-2 (#1252): ВТОРОЕ место применения — прогон кончился без отчёта.

    Отказ здесь асинхронный и лежит в другом пути кода: агент создался,
    деньги потрачены, прогон дошёл до ERROR, а machine_review так и не
    пришёл (замерено 10.09.2026 на агентах bc-6e7cc3e1 и bc-9b077f68).
    Закрыть только синхронный случай значит оставить эту половину сдач без
    отчёта ровно так же, как сегодня.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-6e7cc3e1"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    reviewer_pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-dead-run",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    assert len(recorder.calls) == 1, "облако пробуется первым и агент СОЗДАЁТСЯ"
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert any("отчёт НЕ сдан" in u for u in updates), (
        "отказ облака остаётся наблюдённым фактом в карточке"
    )
    reports = [
        dict(r) for r in await repo.machine_reviews_of_generation(db, task_id, 1)
    ]
    assert len(reports) == 1, (
        "второй дверью открывается и этот путь: одного потребителя правила недостаточно"
    )
    assert reports[0]["principal_id"] == reviewer_pid
    assert not reports[0]["self_reviewed"]
    channels = [
        dict(r)["channel"]
        for r in await db.execute_fetchall(
            "SELECT channel FROM review_dispatches WHERE task_id=? ORDER BY id",
            (task_id,),
        )
    ]
    assert channels == ["cloud", "local"], (
        "облачный прогон был оплачен и остаётся в истории; локальный — второй"
    )


async def test_an_unconfigured_local_path_changes_nothing_on_github(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1252): настройки нет — поведение байт в байт прежнее.

    Тот же форж и тот же отказ облака, но локальный путь не настроен. Тогда
    в карточке ровно тот же единственный алерт об отказе облака, ни одной
    новой записи и ни одной попытки запуска. Настройка, которой нет, не
    имеет права менять сегодняшний путь — и это проверяется ПОПЫТКОЙ, а не
    чтением кода: подставка на месте запуска процесса обязана остаться
    нетронутой.
    """
    recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
    _wire(monkeypatch, recorder)
    _no_local_path(monkeypatch)
    launched: list[str] = []

    async def _never(prompt):
        launched.append(prompt)
        raise AssertionError("локального прогона тут быть не должно")

    monkeypatch.setattr(local_reviewer, "run_review", _never)

    task_id = await _submitted(
        client,
        db,
        "spike-no-local",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await db.commit()

    assert launched == [], "ненастроенный путь не запускает НИЧЕГО"
    alerts = [
        dict(r)["content"]
        for r in await db.execute_fetchall(
            "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
            (task_id,),
        )
    ]
    about_review = [a for a in alerts if "ревью НЕ вызвано" in a]
    assert len(about_review) == 1, (
        "алерт об отказе облака остаётся ОДИН — второго объяснения одному "
        "состоянию не заводим (#1188)"
    )
    assert "провайдер отказал" in about_review[0], about_review[0]
    assert "HTTP 400, usage_limit_exceeded" in about_review[0], about_review[0]
    # #1242 уточняет «ни одной строки»: след сорвавшегося вызова остаётся —
    # одна облачная заглушка, закрытая в failed, ничего не стоившая. Без неё
    # переспросу не за что зацепиться, и сдача стоит без отчёта навсегда
    # (22.09: #1162, #1281, HTTP 429). Локальной строки по-прежнему нет.
    rows = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=?", (task_id,)
        )
    ]
    assert [(r["channel"], r["agent_id"], r["status"]) for r in rows] == [
        ("cloud", "", "failed")
    ], rows
    assert await repo.count_review_dispatches(db, task_id, 1) == 0
    assert await repo.get_review_dispatch_for_generation(db, task_id, 1) is None, (
        "осевшая заглушка не выдаёт себя за запущенного ревьюера"
    )
    assert not await repo.machine_reviews_of_generation(db, task_id, 1)
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert not any("ЛОКАЛЬНО" in u for u in updates), (
        "карточка не обещает того, чего не было"
    )


async def test_the_card_names_which_provider_gave_the_report_and_why(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-4 (#1252): карточка называет, КТО дал отчёт и ПОЧЕМУ не первый.

    Названной причиной отказа облака, а не общими словами. Отчёт без
    указания автора читается как облачный и вводит человека в заблуждение
    ровно так же, как сегодня вводит молчание. И «форж облаку недоступен»
    здесь было бы ЛОЖЬЮ: форж github, облако до него дотягивается.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-named-author",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await wait_for_local_runs()
    await db.commit()

    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    started = [u for u in updates if "запущено ЛОКАЛЬНО" in u]
    assert len(started) == 1, "запуск второй двери называется в карточке"
    note = started[0]
    assert "ВТОРЫМ поставщиком" in note and "локальным" in note, (
        "кто дал отчёт — сказано поимённо"
    )
    assert "usage_limit_exceeded" in note and "HTTP 400" in note, (
        "почему не первый — НАЗВАННОЙ причиной отказа облака, с кодом "
        "провайдера, а не «облако недоступно»"
    )
    assert "недоступен" not in note, (
        "форж github облаку доступен; старый текст на этом месте был бы ложью"
    )


async def _cloud_dispatch_that_died(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path, slug: str
) -> int:
    """Задача с облачным заказом, чей прогон кончился ERROR без отчёта."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-stale"}, "run": {"id": "r-s"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    task_id = await _submitted(
        client, db, slug, policy={"review": "dispatch", "local_review_fallback": "on"}
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    return task_id


async def test_the_second_door_does_not_buy_a_run_for_a_superseded_submission(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1252: свип разбирает заказ ПОЗЖЕ, и сдача могла успеть смениться.

    Заказ был на поколение 1, автор с тех пор пересдал. Купить второго
    ревьюера по мёртвому поколению — это оплатить чтение кода, которого на
    ветке уже нет, и положить в карточку отчёт не про ту сдачу.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    task_id = await _cloud_dispatch_that_died(
        client, db, monkeypatch, tmp_path, "spike-superseded"
    )
    await db.execute(
        "UPDATE tasks SET submission_generation = 2 WHERE id = ?", (task_id,)
    )
    await db.commit()

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert await _local_dispatches(db, task_id) == [], (
        "вторая дверь открывается по ЖИВОЙ сдаче, а не по той, которую заказ "
        "успел пережить"
    )


async def test_the_second_door_stays_shut_on_a_task_that_left_review(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1252: задача ушла из review — отчёт больше некому засчитывать.

    Свип бежит по расписанию, и между заказом и его разбором задачу могли
    вернуть в работу. Прогон по ней был бы куплен впустую: гейта, который
    ждёт этот отчёт, больше нет.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    task_id = await _cloud_dispatch_that_died(
        client, db, monkeypatch, tmp_path, "spike-left-review"
    )
    await db.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
    await db.commit()

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert await _local_dispatches(db, task_id) == [], (
        "ревьюер не покупается для задачи, которая ревью больше не ждёт"
    )


# --- #1252, второй заход ревью: находки 1–3 --------------------------------


async def test_a_blind_creation_does_not_open_the_second_door(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1252, находка 1: СЛЕПОЙ исход — не отказ, и дверь на нём не открывается.

    Ответ на создание не дошёл, и спросить провайдера, создался ли агент,
    тоже не вышло. Это состояние ``_Started.blind``: агент, возможно, УЖЕ
    создан и оплачен. Локальный путь на нём купил бы ВТОРОГО ревьюера на ту
    же сдачу и принёс бы два соперничающих отчёта — ровно то, ради чего
    запрет на слепой повтор и стоит двумя строками выше по коду.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder(
        None, refusal=cursor_cloud.Refusal(status=0, detail="таймаут ответа")
    )
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    async def _cannot_ask(name, pages=3):
        return cursor_cloud.Reconciliation("", "", False)

    monkeypatch.setattr(cursor_cloud, "find_agent_by_name", _cannot_ask)

    task_id = await _submitted(
        client,
        db,
        "spike-blind-door",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await wait_for_local_runs()
    await db.commit()

    assert await _local_dispatches(db, task_id) == [], (
        "агент, возможно, УЖЕ создан — локальный путь купил бы второго судью"
    )


async def test_the_replacement_keeps_the_profile_the_top_up_ordered(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1252, находка 2: замена упавшего добора обязана остаться deep.

    Принудительный добор лестницы (#879) заказал deep, прогон кончился без
    отчёта, и локальная замена считала профиль ЗАНОВО — на сдаче низкого
    риска это lite. Хуже понижения его последствие: упавший заказ и его
    замена вместе выводят счёт заходов за REVIEW_LADDER_MAX_STEPS, и
    неполный отчёт lite нового deep уже не позовёт. Лестница ломается молча
    и в сторону более дешёвого прогона. Тот же класс, что находка 7ed386a8.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-deep"}, "run": {"id": "r-deep"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-deep-replacement",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    first = await _any_dispatch_row(db, task_id)
    assert first["profile"] == LITE, "исходный прогон дешёвый — иначе добора нет"
    await repo.set_review_dispatch_status(db, first["id"], "done")
    await db.commit()

    assert await maybe_dispatch_review(db, task_id, force_profile=DEEP)
    top_up = await _any_dispatch_row(db, task_id)
    assert top_up["profile"] == DEEP and top_up["id"] != first["id"]

    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    local = await _local_dispatches(db, task_id)
    assert len(local) == 1, "замена должна быть"
    assert local[0]["profile"] == DEEP, (
        "добор заказывали ТЕМ ЖЕ профилем: понижение до lite ломает лестницу молча"
    )


async def test_the_sync_second_door_rechecks_the_submission_is_still_live(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1252, находка 3: свежесть перечитывает и СИНХРОННЫЙ путь.

    Сторожа свежести стояли только в свипе, а мест применения два: пока
    летел запрос на создание облачного агента, автор успел пересдать.
    Синхронный путь передавал локальному диспетчеру устаревшие задачу,
    ветку и поколение — и покупал ревью по замещённой сдаче. Один
    потребитель правила ≠ все.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    monkeypatch.setattr(config, "CURSOR_API_KEY", "test-key")
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "reviewer-token")

    async def _no_usage(agent_id, run_id=None):
        return None

    monkeypatch.setattr(cursor_cloud, "get_usage", _no_usage)

    async def _refuse_and_resubmit(**kwargs):
        # Пока летел запрос, автор пересдал: поколение уже другое.
        await db.execute(
            "UPDATE tasks SET submission_generation = 2 "
            "WHERE submission_generation = 1 AND status = 'review'"
        )
        return None, _LIMIT_REFUSAL

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", _refuse_and_resubmit)

    task_id = await _submitted(
        client,
        db,
        "spike-sync-stale",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await wait_for_local_runs()
    await db.commit()

    assert await _local_dispatches(db, task_id) == [], (
        "сдача сменилась, пока летел запрос: локальный прогон купил бы чтение "
        "кода, которого на этой сдаче уже нет"
    )


# --- #1252, сдача №3: находки внешнего ревьюера по коммиту c939276 ----------


async def test_the_ceiling_counts_what_the_failed_run_already_cost(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 1 (P1): потолок расходов не видел денег УПАВШЕГО прогона.

    На асинхронном пути свип строкой выше спрашивает у провайдера счёт за
    прогон и кладёт его в ``review_dispatches.provider_tokens`` — сумма уже
    НАЗВАНА. А потолок считал ``_tokens_already_spent``, и та суммировала
    только ``machine_reviews``, где строки нет: отчёта-то не было. Оплаченный
    прогон учитывался как ноль ровно в тот момент, когда открывается вторая
    дверь, и ``LOCAL_REVIEW_TOKEN_CEILING`` (deploy/LOCAL-REVIEW.md)
    обходился.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-costly"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    monkeypatch.setattr(config, "LOCAL_REVIEW_TOKEN_CEILING", 1_000)

    task_id = await _submitted(
        client,
        db,
        "spike-ceiling-blind",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    async def _billed(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 5_000}}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    monkeypatch.setattr(cursor_cloud, "get_usage", _billed)

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    cloud = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=? AND channel='cloud'",
            (task_id,),
        )
    ]
    assert cloud and cloud[0]["provider_tokens"] == 5_000, (
        "счёт за упавший прогон СКАЗАН провайдером и записан — это не незнание"
    )
    assert await _local_dispatches(db, task_id) == [], (
        "потолок в 1000 токенов уже пробит оплаченным прогоном: вторая дверь "
        "не имеет права покупать ещё один"
    )
    alerts = [
        dict(r)["content"]
        for r in await repo.get_task_updates(db, task_id)
        if dict(r)["kind"] == "alert"
    ]
    assert any("потолок стоимости исчерпан" in a for a in alerts), (
        "отказ по потолку называет причину, а не молчит (#1152)"
    )


async def test_a_crash_before_the_second_door_does_not_lose_it(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 2 (P2): вторая попытка терялась НАВСЕГДА.

    Строка облачного заказа переводилась в ``failed`` и коммитилась ДО вызова
    второй двери. Любой сбой после этого коммита — упавший процесс хаба или
    исключение в подготовке заказа (git-операции ходят в сеть) — оставлял
    сдачу без локального заказа, а свипы грузят только активные строки.
    Обещанной второй двери больше не существовало: поллер глотает исключение
    (hub/poller.py: «the sweep must not kill the loop») и идёт дальше.
    """
    from hub.services import review_dispatch as rd
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-crash"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-crash-second-door",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    real_order = rd.prepare_review_order
    attempts = {"n": 0}

    async def _flaky_order(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("подготовка заказа сорвалась: git-операция не прошла")
        return await real_order(*args, **kwargs)

    monkeypatch.setattr(rd, "prepare_review_order", _flaky_order)

    try:
        await sweep_review_dispatches(db)
    except RuntimeError:
        pass  # ровно то, что поллер глотает и забывает
    await db.commit()

    # Перезапуск: следующий проход свипа обязан ЗАСТАТЬ долг второй двери.
    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert len(await _local_dispatches(db, task_id)) == 1, (
        "долг второй двери переживает сбой: иначе сдача теряет обещанный "
        "второй способ добыть отчёт безвозвратно"
    )
    reports = [
        dict(r) for r in await repo.machine_reviews_of_generation(db, task_id, 1)
    ]
    assert len(reports) == 1
    alerts = [
        dict(r)["content"]
        for r in await repo.get_task_updates(db, task_id)
        if dict(r)["kind"] == "alert" and "отчёт НЕ сдан" in dict(r)["content"]
    ]
    assert len(alerts) == 1, (
        "повтор долга не превращается в поток алертов: отказ облака назван один раз"
    )


@pytest.mark.parametrize(
    "reads_before_revocation",
    # Чтений принципала на пути второй двери ТРИ, и отзыв токена между любыми
    # двумя из них даёт разное состояние. 1 — отозван после проверки
    # готовности у зовущего: ways вызываемого уже без локального канала.
    # 2 — отозван после ЕГО собственного review_reach: ways ещё с локальным
    # каналом, а принципала уже нет. Второй случай ловится только
    # перечитыванием принципала, и без него первый сторож пропускает.
    (1, 2),
    ids=("revoked-before-the-callee-looks", "revoked-between-reach-and-order"),
)
async def test_the_local_order_refuses_without_the_reviewers_principal(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    tmp_path,
    reads_before_revocation: int,
):
    """Находка 3 (P2): заказ мог уехать БЕЗ принципала ревьюера.

    ``open_second_door`` требует LOCAL_CHANNEL в ways, а вызываемый
    ``dispatch_local_review`` делал СВОЙ ``review_reach`` и смотрел только на
    ``runnable``. На github ``runnable`` истинно из-за облака даже тогда,
    когда локального канала уже нет, — и ``principal_id`` мог оказаться
    None. Между двумя чтениями лежит await, и токен ревьюера успевает быть
    отозванным: заказ создавался под NULL, то есть отчёт переставал быть
    чужим автору — ровно то, что обесценивает прогон (#1128).
    """
    from hub.services import review_dispatch as rd
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
    _wire(monkeypatch, recorder)
    pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    real_principal = rd.local_reviewer_principal_id
    reads = {"n": 0}

    async def _revoked_after_the_first_read(conn):
        reads["n"] += 1
        got = await real_principal(conn)
        if reads["n"] == reads_before_revocation:
            # Токен ревьюера отозвали ПОСЛЕ того, как готовность уже сказали.
            await conn.execute(
                "UPDATE api_keys SET revoked_at = datetime('now') "
                "WHERE principal_id = ?",
                (pid,),
            )
            await conn.commit()
        return got

    monkeypatch.setattr(
        rd, "local_reviewer_principal_id", _revoked_after_the_first_read
    )

    task_id = await _submitted(
        client,
        db,
        "spike-no-principal",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await wait_for_local_runs()
    await db.commit()

    _rows = await _local_dispatches(db, task_id)
    assert [(r["id"], r["reviewer_principal_id"]) for r in _rows] == [], (
        "заказ без принципала независимого ревьюера не создаётся вовсе: "
        "отчёт под NULL читается как свой собственному вызову"
    )
    alerts = [
        dict(r)["content"]
        for r in await repo.get_task_updates(db, task_id)
        if dict(r)["kind"] == "alert"
    ]
    assert any("LOCAL_REVIEWER_HUB_TOKEN" in a for a in alerts), (
        "отказ называет, ЧЕГО не хватает, именем настройки (#1083)"
    )


async def test_the_local_order_refuses_a_forge_where_only_the_cloud_is_ready(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка 3, вторая половина: правило живёт в ВЫЗЫВАЕМОЙ функции.

    Проверяется собственный вход ``dispatch_local_review``, а не путь через
    ``open_second_door``: сегодня сторож стоит у зовущего, и достаточность
    этого — свойство сегодняшнего списка зовущих, а не функции. На форже из
    CLOUD_REVIEW_FORGES ``reach.runnable`` истинно ИЗ-ЗА ОБЛАКА даже тогда,
    когда локального канала нет вовсе, и старая проверка пропускала заказ
    дальше: строка диспетчера создавалась под путь, которым исполнить её
    нечем.
    """
    from hub.services.review_dispatch import dispatch_local_review

    await _local_principal(db, monkeypatch)
    # Токен ревьюера есть и разрешается, а CLI и песочницы нет: локального
    # канала нет, но ways на github непуст — там стоит облако.
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", "")

    task_id = await _submitted(client, db, "spike-cloud-only-reach")
    task = dict(await repo.get_task(db, task_id))

    assert not await dispatch_local_review(db, task, "github", task["branch"], 1), (
        "локального канала нет — заказывать нечем"
    )
    assert await _local_dispatches(db, task_id) == [], (
        "строка диспетчера под путь, которым исполнить нечем, — оплаченный "
        "заказ в никуда"
    )
    alerts = [
        dict(r)["content"]
        for r in await repo.get_task_updates(db, task_id)
        if dict(r)["kind"] == "alert"
    ]
    assert any("LOCAL_REVIEW_CMD" in a for a in alerts), (
        "отказ называет отсутствующие настройки по именам (#1083)"
    )


# --- #1252, сдача №4: находки внешнего ревьюера по коммиту 6bdc607 ----------


def _contract_reporting_stub(db, principal_id: int, report: dict | None = None):
    """Ревьюер, сдающий отчёт ПО КОНТРАКТУ и не оставляющий блока в тексте.

    Основной путь локального ревьюера — ``hub_submit_machine_review`` под
    своим токеном (#1180); блок в выводе — запасной (#1036). Заглушки этого
    файла до сих пор изображали только запасной, и контрактный путь второй
    двери ни одним тестом не проходился.
    """

    async def _run(prompt: str, *, timeout: int | None = None, prompt_at_slot=None):
        from hub.models import MachineReviewSubmit
        from hub.services.machine_review_intake import record_machine_review

        payload = {**(report or _LOCAL_REPORT)}
        payload.pop("orchestrator", None)
        await record_machine_review(
            db,
            _run.task_id,
            MachineReviewSubmit(**payload),
            principal_id=principal_id,
            username="local-reviewer",
        )
        await db.commit()
        return local_reviewer.LocalRun(
            rc=0, output="", dropped=0, timed_out=False, duration_ms=1_000
        )

    return _run


async def test_a_contract_report_from_the_local_reviewer_closes_its_dispatch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """P1-1: отчёт локального ревьюера не опознавался СВОИМ заказом.

    Ступень заказа считалась по ВСЕМ заказам поколения, а отчёты отбирались
    по принципалу заказа. На пути второй двери заказов два и принципалы
    разные: облачный — ступень 0, локальный — ступень 1, а у локального
    принципала отчёт всего один, с индексом 0. Годный отчёт не опознавался,
    локальный заказ помечался упавшим, и пустое ревью не могло дать
    автовердикт: ни один заказ не закрылся.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-contract"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    reviewer_pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-contract-report",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    stub = _contract_reporting_stub(db, reviewer_pid)
    stub.task_id = task_id
    monkeypatch.setattr(local_reviewer, "run_review", stub)

    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    local = await _local_dispatches(db, task_id)
    assert len(local) == 1
    assert local[0]["status"] == "done", (
        "отчёт сдан по контракту под принципалом ревьюера — заказ обязан "
        "закрыться им, а не быть объявленным упавшим"
    )
    alerts = [
        dict(r)["content"]
        for r in await repo.get_task_updates(db, task_id)
        if dict(r)["kind"] == "alert"
    ]
    assert not any("Локальное машинное ревью" in a for a in alerts), (
        "«ревью не состоялось» по годному отчёту — ложь в карточке"
    )


async def test_a_late_cloud_report_closes_the_dispatch_instead_of_the_second_door(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """P1-2: гонка с поздним отчётом облака покупала второго ревьюера.

    Между первой проверкой отчёта и открытием второй двери свип успевает
    сходить в сеть дважды — за терминальным состоянием прогона и за расходом.
    Контрактный отчёт, пришедший в это окно, блок не останавливал: писался
    алерт «отчёт НЕ сдан», открывалась вторая дверь, и на одну сдачу
    приезжали два соперничающих отчёта за лишние деньги.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-late"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-late-cloud",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    submitted: list[int] = []

    async def _usage_and_a_late_report(agent_id, run_id=None):
        # Пока свип ходит за расходом, облачный ревьюер сдаёт отчёт по
        # контракту: сеть и MCP работают параллельно. Сдаёт ОДИН раз —
        # ревьюер не отчитывается дважды.
        from hub.models import MachineReviewSubmit
        from hub.services.machine_review_intake import record_machine_review

        if submitted:
            return None
        submitted.append(1)
        payload = {**_LOCAL_REPORT}
        payload.pop("orchestrator", None)
        await record_machine_review(
            db,
            task_id,
            MachineReviewSubmit(**payload),
            principal_id=cloud_pid,
            username="cloud-reviewer",
        )
        await db.commit()
        return None

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    monkeypatch.setattr(cursor_cloud, "get_usage", _usage_and_a_late_report)

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()
    # Доехавший отчёт закрывает заказ СЛЕДУЮЩИМ проходом, обычным путём — со
    # сверкой расхода по данным провайдера (#1026), а не вторым автором
    # закрытия здесь.
    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert await _local_dispatches(db, task_id) == [], (
        "отчёт УЖЕ есть: второй ревьюер здесь — лишние деньги и второй "
        "отчёт-соперник на ту же сдачу"
    )
    reports = [
        dict(r) for r in await repo.machine_reviews_of_generation(db, task_id, 1)
    ]
    assert len(reports) == 1
    cloud = dict(
        (
            await db.execute_fetchall(
                "SELECT * FROM review_dispatches WHERE task_id=? AND channel='cloud'",
                (task_id,),
            )
        )[0]
    )
    assert cloud["status"] == "done", "заказ закрывается СВОИМ доехавшим отчётом"
    alerts = [
        dict(r)["content"]
        for r in await repo.get_task_updates(db, task_id)
        if dict(r)["kind"] == "alert"
    ]
    assert not any("отчёт НЕ сдан" in a for a in alerts), (
        "алерт об отсутствии отчёта при доехавшем отчёте — ложь в карточке"
    )


async def test_the_debt_is_paid_once_even_after_a_death_mid_settlement(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """P2: отдача долга второй двери была неидемпотентна.

    Хаб остановился после того, как локальный заказ закоммичен, но до того,
    как облачный долг помечен failed. При рестарте видны обе строки, а
    отдача долга существующего локального заказа не замечала: облачная
    разбиралась первой и покупала ВТОРОЙ прогон на то же поколение.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-twice"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    # Принципал облачного ревьюера настоящий, как на проде. Без него заказ
    # пишется с NULL, сопоставление отчёта падает на старое правило «любой
    # отчёт этого поколения», и отчёт ЛОКАЛЬНОГО прогона закрывал бы дверь
    # вместо сторожа идемпотентности — то есть проверялся бы не тот сторож.
    await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-debt-twice",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    cloud_id = (await _any_dispatch_row(db, task_id))["id"]
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    real_set = repo.set_review_dispatch_status
    died = {"n": 0}

    async def _dies_closing_the_debt(conn, dispatch_id, status):
        if int(dispatch_id) == int(cloud_id) and status == "failed":
            died["n"] += 1
            raise RuntimeError("хаб остановлен между двумя записями")
        return await real_set(conn, dispatch_id, status)

    monkeypatch.setattr(repo, "set_review_dispatch_status", _dies_closing_the_debt)

    try:
        await sweep_review_dispatches(db)
    except RuntimeError:
        pass
    await wait_for_local_runs()
    await db.commit()
    assert died["n"] == 1, "предпосылка: смерть случилась именно на закрытии долга"
    assert len(await _local_dispatches(db, task_id)) == 1, (
        "предпосылка: локальный заказ уже создан и закоммичен"
    )

    monkeypatch.setattr(repo, "set_review_dispatch_status", real_set)
    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert len(await _local_dispatches(db, task_id)) == 1, (
        "долг отдаётся ОДИН раз: второй прогон на то же поколение — "
        "оплаченный дважды отчёт"
    )
    assert (
        dict(
            (
                await db.execute_fetchall(
                    "SELECT * FROM review_dispatches WHERE id=?", (cloud_id,)
                )
            )[0]
        )["status"]
        == "failed"
    ), "отданный долг закрывается"


async def test_the_rung_is_counted_inside_its_own_channel(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1252: канал в мерке ступени нужен САМ ПО СЕБЕ.

    Отдельный токен у локального ревьюера — требование выката, а не свойство
    кода: ничто не мешает поставить на обоих каналах ОДИН токен. Тогда
    принципал у двух заказов поколения общий, и мерки «свои заказы того же
    принципала» не хватает — локальный заказ снова оказывается ступенью 1
    при единственном отчёте.
    """
    from hub.services.review_dispatch import _dispatch_report

    monkeypatch.setattr(hub_auth, "_is_open_mode", lambda: False)
    shared_pid, _ = await _agent_key(db, "one-token-for-both")
    task_id = await _submitted(client, db, "spike-shared-token")

    await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="bc-shared",
        run_id="r-1",
        model="grok-4.6",
        reviewer_principal_id=shared_pid,
        channel="cloud",
    )
    local_id = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="local:shared",
        run_id="r-2",
        model="grok-4.6",
        reviewer_principal_id=shared_pid,
        channel="local",
    )
    await db.commit()
    report_id = await _a_report_from(db, task_id, shared_pid, "one-token-for-both")

    local = dict(
        (
            await db.execute_fetchall(
                "SELECT * FROM review_dispatches WHERE id=?", (local_id,)
            )
        )[0]
    )
    matched = await _dispatch_report(db, task_id, 1, local)
    assert matched is not None and int(matched["id"]) == report_id, (
        "единственный отчёт принадлежит локальному заказу: ступень считается "
        "внутри своего канала, иначе общий токен ломает сопоставление"
    )


async def test_the_rung_is_counted_inside_its_own_principal(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1252: принципал в мерке ступени нужен САМ ПО СЕБЕ.

    Два заказа одного канала в одном поколении бывают и без второй двери:
    добор лестницы (#879). Если между ними токен ревьюера сменили, отчёт
    нового принципала — первый ЕГО отчёт, а заказ стоит вторым в канале.
    Мерки «свои заказы того же канала» тут не хватает.
    """
    from hub.services.review_dispatch import _dispatch_report

    monkeypatch.setattr(hub_auth, "_is_open_mode", lambda: False)
    old_pid, _ = await _agent_key(db, "reviewer-before-rotation")
    new_pid, _ = await _agent_key(db, "reviewer-after-rotation")
    task_id = await _submitted(client, db, "spike-rotated-token")

    await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="bc-old",
        run_id="r-1",
        model="grok-4.6",
        reviewer_principal_id=old_pid,
        channel="cloud",
    )
    top_up_id = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="bc-new",
        run_id="r-2",
        model="grok-4.6",
        reviewer_principal_id=new_pid,
        channel="cloud",
    )
    await db.commit()
    report_id = await _a_report_from(db, task_id, new_pid, "reviewer-after-rotation")

    top_up = dict(
        (
            await db.execute_fetchall(
                "SELECT * FROM review_dispatches WHERE id=?", (top_up_id,)
            )
        )[0]
    )
    matched = await _dispatch_report(db, task_id, 1, top_up)
    assert matched is not None and int(matched["id"]) == report_id, (
        "отчёт нового принципала — ПЕРВЫЙ его отчёт: ступень считается "
        "внутри заказов того же принципала"
    )


async def test_a_report_arriving_while_the_debt_settles_still_stops_the_order(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1252: последнее слово перед заказом говорится ПЕРЕД заказом.

    Одной проверки мало. Отчёт может доехать уже после того, как свип назвал
    его отсутствующим, — пока пишется долг, читается задача и читается
    проект, а на возобновлении ещё и между проходами. Деньги тратятся в
    момент заказа, и смотреть надо там.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-mid"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-report-mid-settle",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    real_resolve = repo.resolve_project_for_task
    landed: list[int] = []

    async def _report_lands_mid_settlement(conn, tid):
        # Отчёт доехал ПОСЛЕ первой проверки: долг уже записан, заказ ещё нет.
        if not landed:
            landed.append(-1)
            landed[0] = await _a_report_from(db, task_id, cloud_pid, "cloud-reviewer")
        return await real_resolve(conn, tid)

    monkeypatch.setattr(repo, "resolve_project_for_task", _report_lands_mid_settlement)

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert landed and landed[0] > 0, "предпосылка: отчёт доехал именно в это окно"
    assert await _local_dispatches(db, task_id) == [], (
        "отчёт уже сдан: заказ второго ревьюера — оплаченный отчёт-соперник"
    )


# --- #1252: находки внешнего ревьюера по коммиту e69a3d5 --------------------


_INCOMPLETE_LOCAL_REPORT = {
    **_LOCAL_REPORT,
    "incomplete": True,
    "lost_dimensions": ["hub/services/big.py"],
}


async def test_a_deep_top_up_is_not_silenced_by_an_earlier_lite_debt(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 1 (P1): ключ идемпотентности не различает РАЗНЫЕ долги.

    Первый облачный вызов отказывает на создании -> открывается первая
    вторая дверь -> локальный LITE-прогон сдаёт отчёт incomplete=true ->
    лестница (#879) заказывает тяжёлый добор -> второй облачный вызов
    СОЗДАЁТСЯ, но кончается без отчёта -> открывается ВТОРАЯ вторая дверь.

    ``_second_door_already_opened`` спрашивал только task_id+generation+
    channel='local'+status, не различая, какой ИМЕННО долг локальный заказ
    оплатил. Он находил первый (LITE, уже done) заказ и считал ВТОРОЙ
    (DEEP) долг уже оплаченным — тяжёлый добор так и не заказывался, а
    сдача оставалась с одним неполным отчётом.
    """
    from hub.services.review_dispatch import DEEP, wait_for_local_runs

    calls: list[dict] = []

    async def _create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            # Первый вызов (первичная сдача): облако отказывает НА СОЗДАНИИ.
            return None, _LIMIT_REFUSAL
        # Второй вызов (добор лестницы): облако СОЗДАЁТ агента на этот раз.
        return {"agent": {"id": "bc-deep"}, "run": {"id": "r-deep"}}, None

    monkeypatch.setattr(config, "CURSOR_API_KEY", "test-key")
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", _create)

    async def _no_usage(agent_id, run_id=None):
        return None

    monkeypatch.setattr(cursor_cloud, "get_usage", _no_usage)
    # Принципал облачного ревьюера настоящий, как на проде (без него отчёт
    # сопоставляется по старому правилу «любой отчёт этого поколения», и
    # LITE-отчёт замаскировал бы находку 1 под находку рунг-сопоставления).
    await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub(_INCOMPLETE_LOCAL_REPORT))

    task_id = await _submitted(
        client,
        db,
        "spike-topup-after-lite-debt",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    # Первый (синхронный) отказ уже случился внутри _submitted; локальный
    # LITE-прогон запущен асинхронно.
    await wait_for_local_runs()
    await db.commit()

    local_after_first_door = await _local_dispatches(db, task_id)
    assert len(local_after_first_door) == 1
    assert local_after_first_door[0]["status"] == "done"
    assert local_after_first_door[0]["profile"] == "lite"
    # Предпосылка: неполный LITE-отчёт купил тяжёлый добор ВТОРЫМ облачным
    # вызовом (лестница #879 срабатывает автоматически при приёме отчёта).
    assert len(calls) == 2, "неполный отчёт обязан купить добор лестницы"

    deep_cloud = dict(
        (
            await db.execute_fetchall(
                "SELECT * FROM review_dispatches WHERE task_id=? AND channel='cloud'",
                (task_id,),
            )
        )[0]
    )
    assert deep_cloud["profile"] == DEEP

    # Тяжёлый добор дошёл до терминального статуса без отчёта.
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes') "
        "WHERE id = ?",
        (deep_cloud["id"],),
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    local_after_second_door = await _local_dispatches(db, task_id)
    assert len(local_after_second_door) == 2, (
        "тяжёлый добор кончился без отчёта — ему причитается СВОЯ вторая "
        "дверь, а не долг, уже оплаченный чужим (LITE) заказом"
    )
    newest = max(local_after_second_door, key=lambda d: d["id"])
    assert newest["profile"] == DEEP, (
        "замена обязана доехать тем же (тяжёлым) профилем, каким заказывали "
        "упавший добор"
    )


async def test_a_late_report_during_debt_settlement_settles_its_own_dispatch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 2 (P1): доехавший отчёт обязан закрыть СВОЙ заказ как done.

    Сценарий тот же, что и в ``test_a_report_arriving_while_the_debt_settles_
    still_stops_the_order`` — отчёт доезжает, пока отдаётся долг второй
    двери, и локальный соперник верно не покупается. Но ``_settle_second_
    door`` безусловно закрывал СТРОКУ ОБЛАЧНОГО заказа в ``failed`` — даже
    когда его же отчёт только что нашёлся. ``failed``-строки свип больше не
    разбирает, а ``get_settled_review_dispatch`` берёт только ``done``, то
    есть годный отчёт никогда не сверяется как успешный заказ.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-late2"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-late-settles-done",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    real_resolve = repo.resolve_project_for_task
    landed: list[int] = []

    async def _report_lands_mid_settlement(conn, tid):
        if not landed:
            landed.append(-1)
            landed[0] = await _a_report_from(db, task_id, cloud_pid, "cloud-reviewer")
        return await real_resolve(conn, tid)

    monkeypatch.setattr(repo, "resolve_project_for_task", _report_lands_mid_settlement)

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert landed and landed[0] > 0, "предпосылка: отчёт доехал именно в это окно"
    assert await _local_dispatches(db, task_id) == [], (
        "отчёт уже сдан: заказ второго ревьюера — оплаченный отчёт-соперник"
    )
    cloud = dict(
        (
            await db.execute_fetchall(
                "SELECT * FROM review_dispatches WHERE task_id=? AND channel='cloud'",
                (task_id,),
            )
        )[0]
    )
    assert cloud["status"] == "done", (
        "доехавший отчёт принадлежит ЭТОМУ заказу — он обязан закрыться им, "
        "а не быть объявленным упавшим одновременно с годным отчётом"
    )
    settled = await repo.get_settled_review_dispatch(db, task_id, 1)
    assert settled is not None and int(settled["id"]) == int(cloud["id"]), (
        "get_settled_review_dispatch обязана видеть этот заказ доставленным, "
        "иначе доказательство пустого ревью (#769) никогда не соберётся"
    )


async def test_a_report_arriving_while_the_local_order_is_being_prepared_stops_it(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 3 (P1): последнее слово должно звучать НЕПОСРЕДСТВЕННО перед заказом.

    Рунг-проверка в ``_second_door_after_run`` — только ПЕРВАЯ из двух:
    после неё ``open_second_door`` перечитывает свежесть сдачи и достижимость
    (несколько await в базу), а ``dispatch_local_review`` считает потолок
    стоимости и готовит заказ через ``prepare_review_order`` (диф/правила) —
    и только потом вставляет строку и запускает прогон. Отчёт, доехавший в
    ЭТО окно, эту проверку не видит вовсе и покупает оплаченного соперника.
    """
    from hub.services import review_dispatch as review_dispatch_module
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-prep"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-report-during-prep",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    real_prepare = review_dispatch_module.prepare_review_order
    landed: list[int] = []

    async def _report_lands_during_prep(
        db_conn, task, *, branch, generation, force_profile, principal_id, **more
    ):
        # Отчёт доезжает ПОСЛЕ рунг-проверки в _second_door_after_run (она
        # уже прошла — отчёта тогда не было), но ДО вставки строки локального
        # заказа: подготовка заказа (git/дифф) идёт прямо сейчас.
        if not landed:
            landed.append(-1)
            landed[0] = await _a_report_from(db, task_id, cloud_pid, "cloud-reviewer")
        return await real_prepare(
            db_conn,
            task,
            branch=branch,
            generation=generation,
            force_profile=force_profile,
            principal_id=principal_id,
            **more,
        )

    monkeypatch.setattr(
        review_dispatch_module, "prepare_review_order", _report_lands_during_prep
    )

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert landed and landed[0] > 0, "предпосылка: отчёт доехал именно в это окно"
    assert await _local_dispatches(db, task_id) == [], (
        "отчёт доехал во время подготовки заказа: локальный заказ здесь — "
        "оплаченный отчёт-соперник"
    )
    cloud = dict(
        (
            await db.execute_fetchall(
                "SELECT * FROM review_dispatches WHERE task_id=? AND channel='cloud'",
                (task_id,),
            )
        )[0]
    )
    assert cloud["status"] == "done", (
        "доехавший отчёт принадлежит этому заказу — закрывается им"
    )


# --- #1266: пять остаточных находок доставки #1252 --------------------------


async def test_a_replacement_does_not_spend_the_ladder_step(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-1: локальная замена упавшего облачного LITE не тратит шаг лестницы.

    Облачный LITE СОЗДАЁТСЯ (в отличие от test_a_deep_top_up_is_not_
    silenced_by_an_earlier_lite_debt, где он отказывает на создании и строки
    не пишет) и кончается без отчёта. Вторая дверь заказывает локальную
    замену, её отчёт неполный. count_review_dispatches сегодня считает ОБЕ
    строки (облачную и локальную замену) как два шага, REVIEW_LADDER_MAX_
    STEPS=2 достигнут — DEEP-добор молча не заказывается.
    """
    from hub.services.review_dispatch import DEEP, wait_for_local_runs

    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-ladder"}, "run": {"id": "r-ladder"}}
    )
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub(_INCOMPLETE_LOCAL_REPORT))

    task_id = await _submitted(
        client,
        db,
        "spike-ladder-replacement",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    cloud_before = dict(
        (
            await db.execute_fetchall(
                "SELECT * FROM review_dispatches WHERE task_id=? AND channel='cloud'",
                (task_id,),
            )
        )[0]
    )
    assert cloud_before["profile"] == "lite"

    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes') "
        "WHERE id = ?",
        (cloud_before["id"],),
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    locals_ = await _local_dispatches(db, task_id)
    assert len(locals_) == 1 and locals_[0]["status"] == "done"
    assert locals_[0]["profile"] == "lite"

    deep_rows = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=? AND channel='cloud' "
            "AND profile=?",
            (task_id, DEEP),
        )
    ]
    assert deep_rows, (
        "замена упавшего облачного LITE не должна тратить шаг лестницы: "
        "DEEP-добор обязан быть заказан по неполному отчёту локальной замены"
    )


async def test_a_sync_refusal_keeps_the_second_door_debt_through_a_crash(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-2: синхронный отказ облака обязан оставить долг ДО риска сломаться.

    Облако отказывает НА СОЗДАНИИ (синхронно, строки не бывает вовсе).
    Подготовка локального заказа (``prepare_review_order`` для локального
    принципала) падает — ровно то, что может случиться из-за сети внутри
    неё. Сегодня ``open_second_door`` зовётся БЕЗ строки долга, и падение
    внутри нею не оставляет НИЧЕГО, что свип мог бы повторить: вторая дверь
    потеряна навсегда.
    """
    from hub.services import review_dispatch as rd
    from hub.services.review_dispatch import wait_for_local_runs

    _wire(monkeypatch, _DispatchRecorder(None, _LIMIT_REFUSAL))

    local_pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    real_order = rd.prepare_review_order
    local_attempts = {"n": 0}

    async def _flaky_for_local(*args, **kwargs):
        if kwargs.get("principal_id") == local_pid:
            local_attempts["n"] += 1
            if local_attempts["n"] == 1:
                raise RuntimeError(
                    "подготовка локального заказа сорвалась: git-операция не прошла"
                )
        return await real_order(*args, **kwargs)

    monkeypatch.setattr(rd, "prepare_review_order", _flaky_for_local)

    task_id = await _submitted(
        client,
        db,
        "spike-sync-debt-crash",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )

    rows_after_crash = list(
        await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=?", (task_id,)
        )
    )
    assert rows_after_crash, (
        "долг второй двери обязан быть записан ДО рискованного вызова: иначе "
        "падение внутри подготовки локального заказа теряет обещанную вторую "
        "дверь безвозвратно"
    )

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    locals_ = await _local_dispatches(db, task_id)
    assert len(locals_) == 1 and locals_[0]["status"] == "done", (
        "долг второй двери обязан быть отдан на следующем проходе свипа, "
        "как и в асинхронном случае (test_a_crash_before_the_second_door_"
        "does_not_lose_it)"
    )


async def test_a_submission_moved_during_preparation_buys_no_local_run(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-4: сдачу пересдали, пока готовился локальный заказ — прогон не покупается.

    ``_submission_still_live`` сегодня перечитывается только в начале
    ``open_second_door`` — ДО ``review_reach``, счёта потраченного и
    медленной ``prepare_review_order``. Пересдача, случившаяся ВНУТРИ
    подготовки заказа, этой ранней проверке не видна вовсе, а
    ``late_report_recheck`` перед вставкой строки смотрит только на поздний
    отчёт, не на актуальность сдачи.
    """
    from hub.services import review_dispatch as review_dispatch_module
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-move"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-resubmit-during-prep",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    real_prepare = review_dispatch_module.prepare_review_order
    moved: list[bool] = []

    async def _resubmit_during_prep(
        db_conn, task, *, branch, generation, force_profile, principal_id, **more
    ):
        if not moved:
            moved.append(True)
            # Пересдача поднимает submission_generation — ровно то, что
            # увидел бы _submission_still_live, если бы его спросили ЗДЕСЬ,
            # а не только в самом начале open_second_door.
            await repo.update_task(
                db_conn, task_id, submission_generation=generation + 1
            )
            await db_conn.commit()
        return await real_prepare(
            db_conn,
            task,
            branch=branch,
            generation=generation,
            force_profile=force_profile,
            principal_id=principal_id,
            **more,
        )

    monkeypatch.setattr(
        review_dispatch_module, "prepare_review_order", _resubmit_during_prep
    )

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert moved, "предпосылка: пересдача произошла именно во время подготовки заказа"
    assert await _local_dispatches(db, task_id) == [], (
        "сдачу пересдали, пока готовился локальный заказ — прогон по уехавшей "
        "сдаче не покупается"
    )


async def test_a_late_cloud_report_wins_over_an_existing_fallback(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-5: поздний СВОЙ отчёт закрывает заказ как done, а не failed.

    Локальная замена уже закоммичена (долг «отдан»), но финальную запись
    статуса ОБЛАЧНОГО заказа обрывает «падение процесса» — строка остаётся
    ``second_door``. Поздний отчёт ЭТОГО облачного заказа доезжает до
    следующего прохода свипа. Сегодня ``_settle_second_door`` спрашивает
    ``_second_door_already_opened`` РАНЬШЕ своего отчёта — находит локальную
    замену и БЕЗУСЛОВНО закрывает облачный заказ ``failed``, хотя его
    собственный отчёт уже лежит в базе.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-late5"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-late-cloud-wins",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    real_set_status = repo.set_review_dispatch_status
    crashed = {"done": False}

    async def _crash_on_first_status_write(db_conn, dispatch_id, status):
        if not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("падение процесса перед закрытием долга")
        return await real_set_status(db_conn, dispatch_id, status)

    monkeypatch.setattr(
        repo, "set_review_dispatch_status", _crash_on_first_status_write
    )

    try:
        await sweep_review_dispatches(db)
    except RuntimeError:
        pass
    await db.commit()

    monkeypatch.setattr(repo, "set_review_dispatch_status", real_set_status)
    await wait_for_local_runs()
    await db.commit()

    cloud = dict(
        (
            await db.execute_fetchall(
                "SELECT * FROM review_dispatches WHERE task_id=? AND channel='cloud'",
                (task_id,),
            )
        )[0]
    )
    assert cloud["status"] == "second_door", (
        "предпосылка: долг остался неотданным — процесс упал перед записью статуса"
    )
    assert await _local_dispatches(db, task_id) != [], (
        "предпосылка: локальная замена уже закоммичена к моменту падения"
    )

    await _a_report_from(db, task_id, cloud_pid, "cloud-reviewer")

    await sweep_review_dispatches(db)
    await db.commit()

    cloud_after = dict(
        (
            await db.execute_fetchall(
                "SELECT * FROM review_dispatches WHERE id=?", (cloud["id"],)
            )
        )[0]
    )
    assert cloud_after["status"] == "done", (
        "СВОЙ поздний отчёт обязан закрыть заказ как done, а не failed — иначе "
        "get_settled_review_dispatch никогда не сверит этот отчёт как заказ (#769)"
    )


async def test_two_real_cloud_runs_still_hit_the_ladder_ceiling(
    client: AsyncClient, db: aiosqlite.Connection
):
    """Мутационный щит: правка count_review_dispatches не расширяет потолок.

    Два НАСТОЯЩИХ облачных прогона (agent_id непуст, replaces_dispatch_id
    пуст у обоих — ни один не замена) обязаны по-прежнему считаться ДВУМЯ
    шагами лестницы, а не одним: #1266 чинит замену, а не потолок.
    """
    from hub.services.review_dispatch import REVIEW_LADDER_MAX_STEPS

    task_id = await _submitted(client, db, "spike-ceiling-two-real-cloud")
    generation = 1
    for i in range(REVIEW_LADDER_MAX_STEPS):
        await repo.create_review_dispatch(
            db,
            task_id=task_id,
            submission_generation=generation,
            agent_id=f"bc-real-{i}",
            run_id=f"r-{i}",
            model="grok-4.6",
            profile="lite" if i == 0 else "deep",
            channel="cloud",
        )
    await db.commit()

    steps = await repo.count_review_dispatches(db, task_id, generation)
    assert steps == REVIEW_LADDER_MAX_STEPS, (
        "два настоящих облачных прогона обязаны считаться двумя шагами — "
        "починка счёта логического шага не должна расширить потолок"
    )


async def test_a_sync_refusal_names_the_refusal_not_a_finished_run(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """F3 (раунд 2, 7a78080de93938ae): синхронный отказ — не «прогон кончился».

    _second_door_after_run строит текст шаблоном «прогон облачного агента
    ... кончился статусом ... и отчёта не оставил», и после унификации
    синхронного и асинхронного путей (#1266) этот же шаблон достаётся и
    заглушке долга синхронного отказа — с пустым agent_id, где не было ни
    агента, ни рана. Карточка и second_door_reason обязаны назвать
    НАБЛЮДЁННЫЙ отказ провайдера НА СОЗДАНИИ, а не сочинённое «кончился
    статусом» про событие, которого не было.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    _wire(monkeypatch, _DispatchRecorder(None, _LIMIT_REFUSAL))
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-sync-refusal-wording",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await wait_for_local_runs()
    await db.commit()

    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    started = [u for u in updates if "запущено ЛОКАЛЬНО" in u]
    assert len(started) == 1, "запуск второй двери называется в карточке"
    note = started[0]
    assert "кончился статусом" not in note, (
        "создание агента отказало СИНХРОННО — рана, который бы «кончился», не было"
    )
    assert "usage_limit_exceeded" in note and "HTTP 400" in note, (
        "причина — наблюдённый отказ провайдера НА СОЗДАНИИ, с кодом"
    )

    local = (await _local_dispatches(db, task_id))[0]
    assert "кончился статусом" not in local["second_door_reason"], (
        "second_door_reason — тот же текст, что и в карточке; третьей копии "
        "не заводится"
    )
    assert "usage_limit_exceeded" in local["second_door_reason"]
