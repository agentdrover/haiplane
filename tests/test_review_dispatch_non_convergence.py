"""Hub-dispatched cross-model reviews (#757): non_convergence (split of the former single file, #1670)."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud, local_reviewer
from hub.services.review_dispatch import (
    DEEP,
    LITE,
    sweep_review_dispatches,
)
from tests.review_dispatch_support import (
    _HARMLESS_DIFF,
    _RATE_LIMIT,
    _age_dispatches,
    _alerts_of,
    _any_dispatch_row,
    _author_closed_them,
    _confirmed,
    _DispatchRecorder,
    _generation_with_findings,
    _local_principal,
    _no_local_path,
    _pinned_setup,
    _report_on,
    _reporting_stub,
    _rows_of,
    _seed_report,
    _Sequence,
    _stub_reviewer,
    _submitted,
    _wire,
)


# ---------------------------------------------------------------------------
# #1255 — несходимость находок по поколениям: прогон не покупается
# ---------------------------------------------------------------------------

#: Размеченные траектории находок по поколениям. True — задача сошлась по
#: критерию постановки #1255 (на последнем поколении ноль либо убыло и
#: осталось не больше одной), False — нет.
#:
#: ДВА ИСТОЧНИКА, и у них разная мера — это названо, а не склеено:
#:
#: ``st-*`` — шесть траекторий из постановки #1255: замер machine_reviews
#:     прода 11.09.2026, счёт confirmed + unresolved. Единственный источник
#:     НЕСОШЕДШИХСЯ примеров.
#: ``feed-*`` — лента событий прода (GET /api/events, kind
#:     machine_review_completed, снято 23.09.2026; БАЗА НЕ ЧИТАЛАСЬ). Все
#:     задачи с тремя и более поколениями в ленте — 20. Ограничения:
#:     payload несёт только ЧИСЛО confirmed — ни unresolved, ни диспозиций,
#:     ни uid, поэтому несколько отчётов поколения свёрнуты максимумом (не
#:     объединением, как в коде: объединять нечего), а счёт ниже настоящего.
#:     Лента начинается с события 2657, старшие задачи (#1081, #1084, #1186)
#:     в неё не попали. #1208 по одной confirmed выглядит сошедшейся
#:     [3,0,0,…] — её несходимость целиком в unresolved; ровно поэтому
#:     правило в коде считает обе секции.
NON_CONVERGENCE_HISTORY: dict[str, tuple[tuple[int, ...], bool]] = {
    "st-1167": ((9, 5, 6, 3, 2, 1, 0), True),
    "st-1204": ((6, 2, 2, 2, 1, 3, 1, 0), True),
    "st-1208": ((3, 4, 2, 4, 2, 4), False),
    "st-1081": ((5, 6, 6), False),
    "st-1084": ((3, 6, 3), False),
    "st-1186": ((7, 2, 13), False),
    "feed-1155": ((0, 0, 0), True),
    "feed-1158": ((0, 0, 0, 0), True),
    "feed-1167": ((6, 5, 0, 3, 2, 0, 0), True),
    "feed-1168": ((3, 2, 1, 0), True),
    "feed-1169": ((5, 1, 1, 0), True),
    "feed-1171": ((3, 0, 0, 0), True),
    "feed-1172": ((0, 0, 0), True),
    "feed-1176": ((2, 1, 0), True),
    "feed-1195": ((1, 1, 0), True),
    "feed-1204": ((0, 1, 0, 2, 1, 0), True),
    "feed-1206": ((2, 1, 0, 0), True),
    "feed-1208": ((3, 0, 0, 0, 0, 0, 0), True),
    "feed-1233": ((0, 2, 0), True),
    "feed-1235": ((2, 1, 0), True),
    "feed-1249": ((1, 0, 0), True),
    "feed-1261": ((0, 1, 1, 1, 0), True),
    "feed-1265": ((0, 2, 2, 1, 0), True),
    "feed-1271": ((2, 2, 0), True),
    "feed-1273": ((2, 0, 0, 0), True),
    "feed-1287": ((1, 0, 0, 0), True),
}


def _calibrate(window: int) -> tuple[list[str], list[str]]:
    """(сошедшиеся, остановленные зря; несошедшиеся, пропущенные)."""
    from hub.services.review_dispatch import non_convergence_point

    false_stops = [
        key
        for key, (counts, converged) in NON_CONVERGENCE_HISTORY.items()
        if converged and non_convergence_point(counts, window) is not None
    ]
    misses = [
        key
        for key, (counts, converged) in NON_CONVERGENCE_HISTORY.items()
        if not converged and non_convergence_point(counts, window) is None
    ]
    return false_stops, misses


def test_the_threshold_is_measured_on_the_history_not_chosen(monkeypatch):
    """AC-1 (#1255): числа в коде — результат прогона по истории.

    26 траекторий (6 из постановки, 20 из ленты событий) прогоняются по
    окнам 2..5. Окно в коде обязано быть ЕДИНСТВЕННЫМ, которое не
    останавливает ни одной сошедшейся и ловит все несошедшиеся; рядом
    названо, скольких сошедшихся оно останавливает зря — ноль. Нижний край
    проверяется так же: без него плато единиц #1261 останавливается зря.
    """
    from hub.services import review_dispatch as rd

    table = {window: _calibrate(window) for window in range(2, 6)}
    assert table[2][0] == ["st-1204", "feed-1265", "feed-1271"], (
        "окно 2 останавливает плато"
    )
    assert table[4][1] == ["st-1081", "st-1084", "st-1186"], (
        "окно 4 пропускает короткие"
    )
    clean = [w for w, (stops, misses) in table.items() if not stops and not misses]
    assert clean == [rd.NON_CONVERGENCE_WINDOW] == [3], (
        f"замер даёт {clean}, в коде стоит {rd.NON_CONVERGENCE_WINDOW}"
    )
    false_stops, _ = table[rd.NON_CONVERGENCE_WINDOW]
    assert false_stops == [], "сошедшихся остановлено зря: 0 из 22"
    assert rd.non_convergence_point(NON_CONVERGENCE_HISTORY["st-1208"][0]) == 4, (
        "#1208 ловится на четвёртом поколении"
    )
    assert rd.NON_CONVERGENCE_FLOOR == 2
    monkeypatch.setattr(rd, "NON_CONVERGENCE_FLOOR", 1)
    assert _calibrate(rd.NON_CONVERGENCE_WINDOW)[0] == ["feed-1261"], (
        "без нижнего края плато из единиц останавливается зря"
    )


def _layer(generation: int, size: int) -> list[dict]:
    """``size`` РАЗНЫХ подтверждённых находок поколения: у каждой свой uid."""
    return [
        _confirmed(
            f"дефект {generation}.{n}", f"cat-{generation}", 100 * generation + n
        )
        for n in range(size)
    ]


async def _next_submission(db: aiosqlite.Connection, task_id: int, generation: int):
    """Сдача ``generation`` с НОВЫМ кодом, ещё без ревьюера."""
    await db.execute(
        "UPDATE tasks SET submission_generation=?, submission_sha=?, "
        "review_job_id=NULL, status='review' WHERE id=?",
        (generation, f"{generation:x}".rjust(40, "d"), task_id),
    )
    await db.commit()


async def _non_convergence_notices(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if "несходимость находок" in dict(u)["content"]
    ]


async def _walk_counts(db, task_id: int, counts: list[int]) -> None:
    """Положить отчёты по поколениям 1..N; каждый слой автор ЧИНИЛ."""
    previous: tuple[int, int, list[dict]] | None = None
    for generation, size in enumerate(counts, start=1):
        layer = _layer(generation, size)
        review_id = await _generation_with_findings(
            db, task_id, generation, confirmed=layer
        )
        if previous is not None:
            await _author_closed_them(
                db,
                task_id,
                previous[0],
                previous[1],
                confirmed=previous[2],
                unresolved=[],
            )
        previous = (review_id, generation, layer)


async def test_a_flat_finding_count_stops_the_next_run_and_asks_the_human(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1255): три поколения по три находки при настоящих правках.

    Проверяется НАБЛЮДАЕМОЕ отсутствие заказа у провайдера: подставка
    провайдера не получила второго вызова. Вопрос задан один раз и несёт
    саму последовательность; после ответа человека круги продолжаются.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1255"}, "run": {"id": "r-1255"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-1255-flat")
    assert len(recorder.calls) == 1, "предпосылка: первая сдача заказала прогон"

    await _walk_counts(db, task_id, [3, 3, 3])
    await _next_submission(db, task_id, 4)
    from hub.services.review_dispatch import maybe_dispatch_review

    assert await maybe_dispatch_review(db, task_id) is False
    assert len(recorder.calls) == 1, "прогон НЕ заказан у провайдера"
    notices = await _non_convergence_notices(db, task_id)
    assert len(notices) == 1
    assert "постановка или код" in notices[0]
    assert "сдача 1: 3, сдача 2: 3, сдача 3: 3" in notices[0]
    events = [
        json.loads(dict(r)["payload"])
        for r in await repo.list_events(
            db, since=0, kinds=["review_non_convergence"], limit=10
        )
    ]
    assert [e["counts"] for e in events] == [[3, 3, 3]]

    # Без ответа следующая сдача тоже стоит, но вопрос не повторяется.
    await _next_submission(db, task_id, 5)
    assert await maybe_dispatch_review(db, task_id) is False
    assert len(recorder.calls) == 1
    assert len(await _non_convergence_notices(db, task_id)) == 1, "вопрос один раз"

    # Человек ответил вердиктом по остановленной сдаче — круги продолжаются.
    await db.execute(
        "UPDATE tasks SET review_verdict_generation=5 WHERE id=?", (task_id,)
    )
    await _next_submission(db, task_id, 6)
    assert await maybe_dispatch_review(db, task_id) is True
    assert len(recorder.calls) == 2, "после ответа прогон снова покупается"


async def _stops_along(db, task_id: int, counts: tuple[int, ...]) -> list[int]:
    """Поколения, перед которыми правило остановило бы прогон."""
    from hub.services.review_dispatch import findings_stopped_converging

    stopped: list[int] = []
    for generation, size in enumerate(counts, start=1):
        await _generation_with_findings(
            db, task_id, generation, confirmed=_layer(generation, size)
        )
        await _next_submission(db, task_id, generation + 1)
        task = dict(await repo.get_task(db, task_id))
        if await findings_stopped_converging(db, task):
            stopped.append(generation + 1)
    return stopped


async def test_a_long_but_converging_task_is_not_stopped(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1255): #1167 и #1204 — длинные, с колебаниями, но сошлись.

    Настоящие последовательности прогоняются через базу поколение за
    поколением, как их видел бы хаб на каждой сдаче. Контроль — #1208 на
    том же пути останавливается, иначе «не сработало» ничего не доказывает.
    """
    _wire(monkeypatch, _DispatchRecorder({"agent": {"id": "bc-c"}, "run": {"id": "r"}}))
    for tid in ("st-1167", "st-1204", "feed-1204", "feed-1261", "feed-1265"):
        task_id = await _submitted(client, db, f"spike-1255-conv-{tid}")
        assert await _stops_along(db, task_id, NON_CONVERGENCE_HISTORY[tid][0]) == [], (
            f"{tid} сошлась сама — останавливать её нельзя"
        )
    control = await _submitted(client, db, "spike-1255-control")
    stops = await _stops_along(db, control, NON_CONVERGENCE_HISTORY["st-1208"][0])
    assert stops and stops[0] == 5, "#1208 стоит перед пятой сдачей: четвёртый отчёт"


async def test_dispositions_count_as_progress_and_an_incomplete_zero_does_not(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-4 (#1255): открытые находки, а не сырые; неполный ноль — не чисто."""
    from hub.services.review_dispatch import findings_stopped_converging

    _wire(monkeypatch, _DispatchRecorder({"agent": {"id": "bc-d"}, "run": {"id": "r"}}))

    async def _stopped(task_id: int, generation: int) -> bool:
        await _next_submission(db, task_id, generation)
        return await findings_stopped_converging(
            db, dict(await repo.get_task(db, task_id))
        )

    # Разбор: две из трёх находок третьего поколения автор отложил и
    # признал не дефектом — открытых 3, 3, 1, это схождение.
    for outcome, expected in (("deferred", False), ("fixed", True)):
        task_id = await _submitted(client, db, f"spike-1255-disp-{outcome}")
        await _walk_counts(db, task_id, [3, 3])
        third = _layer(3, 3)
        review_id = await _generation_with_findings(db, task_id, 3, confirmed=third)
        await _author_closed_them(
            db,
            task_id,
            review_id,
            3,
            confirmed=third[:2],
            unresolved=[],
            outcome_confirmed=outcome,
        )
        assert await _stopped(task_id, 4) is expected, (
            f"исход {outcome}: разбор — прогресс, починка проверяется отчётом"
        )

    # Неполный отчёт с нулём находок: 3, 4, 3 и затем «не дочитал».
    task_id = await _submitted(client, db, "spike-1255-incomplete")
    for generation, size in enumerate((3, 4, 3), start=1):
        await _generation_with_findings(
            db, task_id, generation, confirmed=_layer(generation, size)
        )
    await db.execute("UPDATE tasks SET submission_generation=4 WHERE id=?", (task_id,))
    await _seed_report(db, task_id, incomplete=True, reason=None)
    await db.commit()
    assert await _stopped(task_id, 5) is True, (
        "неполный ноль не схождение: последним известным остаётся 3 из 3"
    )


async def test_several_reports_of_one_generation_fold_into_their_union(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1255: lite и добор deep на одно поколение сворачиваются ОБЪЕДИНЕНИЕМ.

    Два случая, и каждый отличает объединение от своей альтернативы:
    максимум прятал бы находку, которую нашёл только один отчёт, сумма
    считала бы повтор дважды.
    """
    from hub.services.review_dispatch import finding_trajectory

    _wire(monkeypatch, _DispatchRecorder({"agent": {"id": "bc-u"}, "run": {"id": "r"}}))
    a, b, c = _layer(1, 3)
    task_id = await _submitted(client, db, "spike-1255-union-max")
    await _generation_with_findings(db, task_id, 1, confirmed=[a, b])
    await _generation_with_findings(db, task_id, 1, confirmed=[b, c])
    trajectory = await finding_trajectory(db, task_id, 2)
    assert trajectory.counts == (3,), "объединение: a, b, c — не максимум 2"

    task_id = await _submitted(client, db, "spike-1255-union-sum")
    await _generation_with_findings(db, task_id, 1, confirmed=[a, b])
    await _generation_with_findings(db, task_id, 1, confirmed=[a, b])
    trajectory = await finding_trajectory(db, task_id, 2)
    assert trajectory.counts == (2,), "повтор того же отчёта — не сумма 4"


async def _self_report(db, task_id: int, generation: int, size: int) -> None:
    """Самоотчёт исполнителя о своей работе: не независимое чтение."""
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill="deep-review",
        model="claude-fable-5",
        raw_count=size,
        findings_confirmed=json.dumps(_layer(generation, size), ensure_ascii=False),
        self_reviewed=True,
        submitted_by="dev-agent",
    )
    await db.commit()


async def test_a_self_report_is_neither_a_point_nor_a_reset_of_the_trajectory(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1255, находка e6250c505eb42e95, следствие 1: самоотчёт — не сброс.

    Тот же признак, которым страж новизны не считает самоотчёт чтением:
    полный самоотчёт с нулём находок не снимает остановку.
    """
    from hub.services.review_dispatch import findings_stopped_converging

    _wire(monkeypatch, _DispatchRecorder({"agent": {"id": "bc-s"}, "run": {"id": "r"}}))

    async def _stopped(task_id: int, generation: int) -> bool:
        await _next_submission(db, task_id, generation)
        return await findings_stopped_converging(
            db, dict(await repo.get_task(db, task_id))
        )

    task_id = await _submitted(client, db, "spike-1255-self-reset")
    await _walk_counts(db, task_id, [3, 3, 3])
    await _self_report(db, task_id, 4, 0)
    assert await _stopped(task_id, 5) is True, "чистый самоотчёт — не схождение"


async def test_flat_self_reports_do_not_stop_the_first_independent_order(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1255, находка e6250c505eb42e95, следствие 2: самоотчёт — не точка."""
    from hub.services.review_dispatch import findings_stopped_converging

    _wire(monkeypatch, _DispatchRecorder({"agent": {"id": "bc-s"}, "run": {"id": "r"}}))
    task_id = await _submitted(client, db, "spike-1255-self-point")
    for generation in (1, 2, 3):
        await _self_report(db, task_id, generation, 3)
    await _next_submission(db, task_id, 4)
    task = dict(await repo.get_task(db, task_id))
    assert await findings_stopped_converging(db, task) is False, (
        "плоские самоотчёты не останавливают первый независимый заказ"
    )


async def test_a_refused_call_is_asked_again(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1242): отказ провайдера НА ВЫЗОВЕ, отчёта нет — второй вызов.

    Путь синхронный: maybe_dispatch_review, агент не создался. Локальный путь
    не настроен, то есть вторая дверь (#1252) не открылась — ровно случай
    22.09. Следующий проход свипа после паузы зовёт ревьюера снова, и
    карточка называет причину ПЕРВОГО сбоя.
    """
    provider = _Sequence(
        [
            (None, _RATE_LIMIT),
            ({"agent": {"id": "bc-second"}, "run": {"id": "run-second"}}, None),
        ]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)

    task_id = await _submitted(
        client, db, "ask-again-refused", policy={"review": "dispatch"}
    )
    assert len(provider.calls) == 1, "первый вызов отказан"

    await sweep_review_dispatches(db)
    assert len(provider.calls) == 1, (
        "пауза: отказ по лимиту не переспрашивается в ту же минуту"
    )

    await _age_dispatches(db)
    await sweep_review_dispatches(db)

    assert len(provider.calls) == 2, "второй вызов ревью состоялся"
    rows = await _rows_of(db, task_id)
    assert rows[-1]["agent_id"] == "bc-second" and rows[-1]["status"] == "active"
    assert rows[-1]["replaces_dispatch_id"] == rows[0]["id"], (
        "переспрос продолжает ту же ступень, а не открывает новую (#879)"
    )
    asked = [a for a in await _alerts_of(db, task_id) if "Переспрос ревью" in a]
    assert len(asked) == 1, asked
    assert "HTTP 429" in asked[0], (
        f"причина первого сбоя обязана стоять в карточке: {asked[0]}"
    )
    assert "1 из" in asked[0], asked[0]
    assert await repo.count_review_dispatches(db, task_id, 1) == 1, (
        "повтор не съедает ступень лестницы"
    )


async def test_a_dead_run_is_asked_again(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1242): прогон кончился терминальным статусом без отчёта.

    Другая функция — sweep_review_dispatches, агент создан и оплачен. Первый
    проход закрывает прогон как упавший (алерт «отчёт НЕ сдан»), следующий
    зовёт ревьюера снова. Свежий ревьюер узнаёт СВОЙ отчёт: упавший заказ,
    замещённый повтором, ступенью в сопоставлении отчётов не считается.
    """
    provider = _Sequence(
        [
            ({"agent": {"id": "bc-dead"}, "run": {"id": "run-dead"}}, None),
            ({"agent": {"id": "bc-fresh"}, "run": {"id": "run-fresh"}}, None),
        ]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    reviewer_pid, _, _ = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(
        client, db, "ask-again-dead-run", policy={"review": "dispatch"}
    )
    await _age_dispatches(db)

    async def _errored(agent_id, run_id):
        if agent_id == "bc-dead":
            return {"id": run_id, "status": "ERROR"}
        return {"id": run_id, "status": "RUNNING"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    await sweep_review_dispatches(db)
    assert any("отчёт НЕ сдан" in a for a in await _alerts_of(db, task_id))
    await sweep_review_dispatches(db)

    assert len(provider.calls) == 2, "второй вызов ревью по мёртвому прогону"
    rows = await _rows_of(db, task_id)
    assert [r["agent_id"] for r in rows] == ["bc-dead", "bc-fresh"]
    assert rows[0]["status"] == "failed" and rows[1]["status"] == "active"
    asked = [a for a in await _alerts_of(db, task_id) if "Переспрос ревью" in a]
    assert len(asked) == 1 and "ERROR" in asked[0], asked
    assert await repo.count_review_dispatches(db, task_id, 1) == 1

    from hub.services.review_dispatch import _dispatch_report

    assert rows[1]["reviewer_principal_id"] == reviewer_pid, (
        "ревьюер закреплён — сопоставление идёт по ступеням, а не по поколению"
    )
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        model="grok-4.6",
        submitted_by="cursor-cloud-reviewer",
        principal_id=reviewer_pid,
    )
    await db.commit()
    own = await _dispatch_report(db, task_id, 1, rows[1])
    assert own is not None, (
        "отчёт свежего ревьюера — ЕГО отчёт: замещённый заказ не занимает "
        "ступень в сопоставлении (#1025)"
    )


async def test_an_existing_report_is_not_bought_twice(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1242): отчёт уже есть — пусть неполный — переспроса нет.

    Неполнота — предмет лестницы добора (#879). Отчёт здесь неполный
    намеренно: страж второй читки неполный отчёт покрытием не считает и
    пропустил бы повтор, то есть мутация «переспрашивать всегда» видна
    ТОЛЬКО этим тестом.
    """
    provider = _Sequence(
        [
            ({"agent": {"id": "bc-late"}, "run": {"id": "run-late"}}, None),
            ({"agent": {"id": "bc-extra"}, "run": {"id": "run-extra"}}, None),
        ]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    task_id = await _submitted(
        client, db, "ask-again-has-report", policy={"review": "dispatch"}
    )
    await _age_dispatches(db)

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    await sweep_review_dispatches(db)
    assert (await _rows_of(db, task_id))[0]["status"] == "failed"

    # Отчёт доехал ПОСЛЕ того, как прогон назван упавшим: заказ закрыт, а
    # отчёт этого поколения в базе есть.
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        model="grok-4.6",
        submitted_by="someone-else",
        incomplete=True,
        lost_dimensions='["hub/services/big.py"]',
    )
    await db.commit()

    await sweep_review_dispatches(db)
    await sweep_review_dispatches(db)

    assert len(provider.calls) == 1, "второго ревьюера на сданный отчёт не купили"
    assert len(await _rows_of(db, task_id)) == 1
    assert not [a for a in await _alerts_of(db, task_id) if "Переспрос ревью" in a]


async def test_exhausted_retries_name_themselves_to_a_human(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-4 (#1242): потолок на поколение исчерпан — к человеку, с числом.

    Отказ постоянный (тот же 429 на каждом вызове). После потолка третий
    переспрос не покупается, а карточка называет число попыток и причину
    последнего сбоя — один раз, а не на каждом проходе свипа.
    """
    from hub.services.review_dispatch import REVIEW_ASK_AGAIN_MAX

    provider = _Sequence([(None, _RATE_LIMIT)])
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    task_id = await _submitted(
        client, db, "ask-again-exhausted", policy={"review": "dispatch"}
    )

    for _ in range(REVIEW_ASK_AGAIN_MAX + 3):
        await _age_dispatches(db)
        await sweep_review_dispatches(db)

    assert len(provider.calls) == 1 + REVIEW_ASK_AGAIN_MAX, (
        f"вызовов {len(provider.calls)}: потолок {REVIEW_ASK_AGAIN_MAX} "
        "переспросов на поколение"
    )
    alerts = await _alerts_of(db, task_id)
    asked = [a for a in alerts if "Переспрос ревью" in a]
    assert len(asked) == REVIEW_ASK_AGAIN_MAX, asked
    exhausted = [a for a in alerts if "исчерпан" in a]
    assert len(exhausted) == 1, exhausted
    assert f"{REVIEW_ASK_AGAIN_MAX}" in exhausted[0]
    assert "HTTP 429" in exhausted[0], exhausted[0]
    assert "человек" in exhausted[0], exhausted[0]


async def test_a_blind_retry_is_not_repeated(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Ограничение #1199 в переспросе (#1242): вслепую не повторять.

    Переспрос не дошёл до ответа, и спросить провайдера, создался ли агент,
    тоже не вышло — строки заказа нет. Следующий проход не покупает ещё
    одного поверх, возможно, уже оплаченного: он называет это человеку.
    """
    provider = _Sequence(
        [(None, _RATE_LIMIT), (None, cursor_cloud.Refusal(status=0, detail="timeout"))]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)

    async def _cannot_ask(name, pages=3):
        return cursor_cloud.Reconciliation("", "", False)

    monkeypatch.setattr(cursor_cloud, "find_agent_by_name", _cannot_ask)
    task_id = await _submitted(
        client, db, "ask-again-blind", policy={"review": "dispatch"}
    )
    for _ in range(3):
        await _age_dispatches(db)
        await sweep_review_dispatches(db)

    assert len(provider.calls) == 2, "после слепого переспроса — ни одного вызова"
    stopped = [a for a in await _alerts_of(db, task_id) if "исчерпан" in a]
    assert len(stopped) == 1 and "не оставил заказа" in stopped[0], stopped


async def test_a_mixed_chain_is_still_one_ladder_step(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка b9943fc18e24a30c (#1242, сдача 1): смешанная цепочка повторов.

    Оплаченный прогон упал (ERROR) → переспрос словил 429, осталась
    бесплатная заглушка → следующий переспрос успешен. Все три строки — одна
    ступень: успех продолжает заглушку, заглушка — оплаченный прогон. Счёт,
    глядящий только на непосредственного предка, называл успех второй
    ступенью, и лестница ложно отказывала в DEEP-доборе («потолок достигнут»).
    """
    provider = _Sequence(
        [
            ({"agent": {"id": "bc-dead"}, "run": {"id": "run-dead"}}, None),
            (None, _RATE_LIMIT),
            ({"agent": {"id": "bc-fresh"}, "run": {"id": "run-fresh"}}, None),
        ]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    task_id = await _submitted(
        client, db, "ask-again-mixed-chain", policy={"review": "dispatch"}
    )

    async def _runs(agent_id, run_id):
        return {"id": run_id, "status": "ERROR" if agent_id == "bc-dead" else "RUNNING"}

    monkeypatch.setattr(cursor_cloud, "get_run", _runs)
    for _ in range(3):
        await _age_dispatches(db)
        await sweep_review_dispatches(db)

    rows = await _rows_of(db, task_id)
    assert [r["agent_id"] for r in rows] == ["bc-dead", "", "bc-fresh"], rows
    assert rows[1]["replaces_dispatch_id"] == rows[0]["id"]
    assert rows[2]["replaces_dispatch_id"] == rows[1]["id"]
    assert await repo.count_review_dispatches(db, task_id, 1) == 1, (
        "оплаченный упал, заглушка бесплатна, успех — продолжение той же ступени"
    )


async def test_a_retry_that_left_no_order_does_not_claim_blindness(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка c82b1af523ba9dd7 (#1242, сдача 1): остановка не выдумывает причину.

    Переспрос отказан по конфигурации — наблюдённый отказ, строки заказа нет.
    Остановка верна (исход неизвестен переспросу, повтор ничего не изменит),
    но называть его «слепотой #1199» — ложь в карточке: причина стоит в
    алерте диспетчера, и остановка обязана на него сослаться, а не подменять.
    """
    provider = _Sequence(
        [({"agent": {"id": "bc-dead"}, "run": {"id": "run-dead"}}, None)]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    task_id = await _submitted(
        client, db, "ask-again-no-config", policy={"review": "dispatch"}
    )

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    await _age_dispatches(db)
    await sweep_review_dispatches(db)
    monkeypatch.setattr(config, "CURSOR_API_KEY", "")
    for _ in range(3):
        await _age_dispatches(db)
        await sweep_review_dispatches(db)

    assert len(provider.calls) == 1
    alerts = await _alerts_of(db, task_id)
    assert any("не хватает конфигурации" in a for a in alerts), alerts
    stopped = [a for a in alerts if "исчерпан" in a]
    assert len(stopped) == 1, stopped
    assert "не оставил заказа" in stopped[0], stopped[0]
    assert "вслепую нельзя" not in stopped[0], (
        f"отказ конфигурации — не слепой исход #1199: {stopped[0]}"
    )


async def test_a_report_landing_while_the_retry_is_prepared_buys_nothing(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка 17a9ca6ea3451163 (#1242, сдача 1): последнее слово перед тратой.

    Отчёт доезжает, пока переспрос готовит заказ (дифф, правила — сеть).
    Проверка в начале переспроса его не видит; облако не должно покупаться,
    как не покупает его вторая дверь в том же окне (#1266).
    """
    from hub.services import review_dispatch as rd

    provider = _Sequence(
        [
            ({"agent": {"id": "bc-dead"}, "run": {"id": "run-dead"}}, None),
            ({"agent": {"id": "bc-extra"}, "run": {"id": "run-extra"}}, None),
        ]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    task_id = await _submitted(
        client, db, "ask-again-late-report", policy={"review": "dispatch"}
    )

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    await _age_dispatches(db)
    await sweep_review_dispatches(db)

    real_prepare = rd.prepare_review_order

    async def _report_arrives(db_, task, **kw):
        await repo.insert_machine_review(
            db_,
            task_id=task_id,
            submission_generation=1,
            model="grok-4.6",
            submitted_by="late-reviewer",
        )
        await db_.commit()
        return await real_prepare(db_, task, **kw)

    monkeypatch.setattr(rd, "prepare_review_order", _report_arrives)
    await _age_dispatches(db)
    await sweep_review_dispatches(db)

    assert len(provider.calls) == 1, "отчёт уже есть — облако не покупается"
    assert len(await _rows_of(db, task_id)) == 1
    # Находка 2007bee302b71bf4 (сдача 2): запись о переспросе стоит ДО
    # вызова и обещает ревьюера. Отменённая трата обязана быть названа, иначе
    # карточка говорит о прогоне, которого не было.
    alerts = await _alerts_of(db, task_id)
    asked = [i for i, a in enumerate(alerts) if "Переспрос ревью 1 из" in a]
    assert asked, alerts
    after = alerts[asked[-1] + 1 :]
    assert any("отменён" in a and "отчёт" in a for a in after), (
        f"отменённый переспрос не назван в карточке: {after}"
    )


async def test_a_submission_moved_during_preparation_buys_no_cloud_run(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка dcd7da1fa88023c5 (#1242, сдача 2): свежесть сдачи перед тратой.

    Подготовка заказа ходит в сеть; за это время задачу могут вернуть в
    работу или пересдать. Облачный агент тогда читал бы код, которого на
    живой сдаче уже нет. Проверка общая для ВСЕХ облачных заказов, не только
    для переспроса: здесь — самый первый вызов при сдаче.
    """
    from hub.services import review_dispatch as rd

    provider = _Sequence(
        [({"agent": {"id": "bc-stale"}, "run": {"id": "run-stale"}}, None)]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    real_prepare = rd.prepare_review_order

    async def _task_leaves_review(db_, task, **kw):
        order = await real_prepare(db_, task, **kw)
        # Сдачу переставили на другую ветку, пока шла подготовка.
        await repo.update_task(db_, int(task["id"]), branch="task-x/other")
        await db_.commit()
        return order

    monkeypatch.setattr(rd, "prepare_review_order", _task_leaves_review)
    task_id = await _submitted(
        client, db, "cloud-stale-submission", policy={"review": "dispatch"}
    )

    assert provider.calls == [], "сдача сменилась — облако не покупается"
    assert not await _rows_of(db, task_id)


async def test_a_retry_cancelled_by_a_moved_submission_says_so(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка 40a8fd8b727c82ec (#1242, сдача 3): отмена по свежести названа.

    Переспрос уже записал «ревьюер зовётся снова», а проверка свежести перед
    облачной тратой его отменила: сдачу переставили на другую ветку, пока
    готовился заказ. Своего алерта у этого отказа нет, поэтому карточка
    обязана назвать отмену — иначе она обещает прогон, которого не было, а
    остановка потом ссылается на алерт, которого нет.
    """
    from hub.services import review_dispatch as rd

    provider = _Sequence(
        [
            ({"agent": {"id": "bc-dead"}, "run": {"id": "run-dead"}}, None),
            ({"agent": {"id": "bc-extra"}, "run": {"id": "run-extra"}}, None),
        ]
    )
    _wire(monkeypatch, provider)
    _no_local_path(monkeypatch)
    task_id = await _submitted(
        client, db, "ask-again-moved", policy={"review": "dispatch"}
    )

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    await _age_dispatches(db)
    await sweep_review_dispatches(db)

    real_prepare = rd.prepare_review_order

    async def _branch_moves(db_, task, **kw):
        order = await real_prepare(db_, task, **kw)
        await repo.update_task(db_, task_id, branch="task-x/moved")
        await db_.commit()
        return order

    monkeypatch.setattr(rd, "prepare_review_order", _branch_moves)
    await _age_dispatches(db)
    await sweep_review_dispatches(db)

    assert len(provider.calls) == 1, "сдача сменилась — облако не покупается"
    alerts = await _alerts_of(db, task_id)
    asked = [i for i, a in enumerate(alerts) if "Переспрос ревью 1 из" in a]
    assert asked, alerts
    after = alerts[asked[-1] + 1 :]
    assert any("отменён" in a and "сдача сменилась" in a for a in after), (
        f"отмена по свежести сдачи не названа в карточке: {after}"
    )


# --- #1357: попытка Docker до «среда отказала», и только там, где она нужна ---


def _prompt_for(profile: str, needs_container: bool) -> str:
    from hub.services.review_dispatch import _review_prompt

    return _review_prompt(
        1357,
        "task-1357/x",
        "grok-4.6",
        profile,
        "RULES",
        "DIFF",
        "PREPASS",
        needs_container=needs_container,
    )


@pytest.mark.parametrize("profile", [LITE, DEEP])
def test_prompt_orders_docker_attempt_when_task_needs_container(profile: str) -> None:
    """AC-1: задача с docker compose в проверках получает попытку поднять демон,
    и отсутствие команды docker до попытки отказом среды не называется."""
    from hub.services.review_dispatch import task_needs_container

    task = {
        "validation_commands": json.dumps(
            ["docker compose up -d --build && curl -fsS http://localhost:8080/healthz"]
        ),
        "review_checklist": "[]",
    }
    assert task_needs_container(task)

    prompt = _prompt_for(profile, task_needs_container(task))
    assert "sudo apt-get install -y docker.io docker-compose-v2" in prompt
    assert "sudo docker info" in prompt
    assert "это ещё не отказ среды" in prompt
    # Ловушка пробы 24.09: на порт ответил хостовый процесс, а не контейнер.
    assert "sudo docker compose ps" in prompt
    assert "ports: !override" in prompt
    # Порядок: попытка идёт ДО итогового правила об отказе среды.
    assert prompt.index("docker.io") < prompt.index('incomplete_reason="environment"')


@pytest.mark.parametrize("profile", [LITE, DEEP])
def test_prompt_requires_named_step_when_docker_attempt_fails(profile: str) -> None:
    """AC-2: провал попытки — incomplete с причиной environment и названным шагом."""
    prompt = _prompt_for(profile, needs_container=True)
    assert "incomplete=true" in prompt
    assert 'incomplete_reason="environment"' in prompt
    for step in (
        "нет sudo",
        "пакет не встал",
        "dockerd не стартовал",
        "контейнер не поднялся",
    ):
        assert step in prompt, step


@pytest.mark.parametrize("profile", [LITE, DEEP])
def test_prompt_forbids_repo_changes_but_allows_tool_install(profile: str) -> None:
    """AC-3: запрет на коммит, пуш и правку файлов репозитория остаётся, а
    установка инструмента во временную машину названа частью проверки. Для
    задачи без контейнерных проверок абзаца про Docker нет вовсе."""
    from hub.services.review_dispatch import task_needs_container

    prompt = _prompt_for(profile, needs_container=False)
    assert "НИЧЕГО не коммить, не пушить и не менять в файлах репозитория" in prompt
    assert "Поставить во временную машину недостающий инструмент" in prompt
    assert "docker.io" not in prompt
    # Блок #1238 про pytest и базу на месте в обоих случаях.
    assert "python -m pip install -q pytest" in prompt

    # Слово Docker в чеклисте — не команда: правка самого промпта не должна
    # покупать себе установку демона.
    assert not task_needs_container(
        {
            "validation_commands": json.dumps(["uv run pytest -q"]),
            "review_checklist": json.dumps(
                ["Попытка Docker условная: без контейнерных проверок не ставить"]
            ),
        }
    )
    assert task_needs_container(
        {"validation_commands": None, "review_checklist": ["docker build ."]}
    )
    # Имя файла и пакета — тоже не команда (ревью #1357, поколение 1).
    for text in ("Не править docker-compose.yml", "пакет docker-compose-v2"):
        assert not task_needs_container(
            {"validation_commands": "[]", "review_checklist": json.dumps([text])}
        ), text
    assert task_needs_container(
        {"validation_commands": json.dumps(["docker-compose up -d"])}
    )


async def test_dispatched_prompt_carries_docker_attempt_only_for_container_tasks(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1357, место вызова: флаг доезжает от задачи до промпта, уходящего в
    облако. Тесты выше зовут _review_prompt напрямую и пропустили бы
    prepare_review_order, забывший передать признак."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-d"}, "run": {"id": "run-d"}})
    _wire(monkeypatch, recorder)

    await _submitted(
        client,
        db,
        "spike-container",
        validation_commands=["docker compose up -d --build"],
    )
    await _submitted(
        client, db, "spike-plain", validation_commands=["uv run pytest -q"]
    )

    assert len(recorder.calls) == 2
    with_docker, plain = (c["prompt_text"] for c in recorder.calls)
    assert "sudo apt-get install -y docker.io docker-compose-v2" in with_docker
    assert "docker.io" not in plain
    assert "не менять в файлах репозитория" in plain


# ---- #1254: символы «только тесты» доезжают до того, кто судит ----------
#
# call_sites.analyse вычислял ONLY_TESTS и отдавал его одной фразе брифа,
# которую судящая сторона не читает: 11.09.2026 две сдачи (#1231, #1234) ушли
# к человеку с механизмом, недостижимым из прода. Анализатор здесь подменён
# готовым отчётом: его точность — не предмет этой задачи, предмет — адресат.


def _only_tests_report(*symbols: str):
    from hub.services import call_sites

    reports = [
        call_sites.SymbolReport(
            symbol=name,
            defined_in="hub/services/steward_corridor.py",
            state=call_sites.ONLY_TESTS,
            sites=[
                call_sites.CallSite(
                    file="tests/test_steward_corridor.py",
                    line=10,
                    caller="test_it",
                    touched=True,
                )
            ],
        )
        for name in symbols
    ]
    reports.append(
        call_sites.SymbolReport(
            symbol="ordinary_helper",
            defined_in="hub/services/digest.py",
            state=call_sites.ALL_TOUCHED,
        )
    )
    return call_sites.CallSiteReport(call_sites.ANALYSED, symbols=reports)


def _analyse_returns(monkeypatch, report) -> None:
    from hub.services import call_sites

    monkeypatch.setattr(call_sites, "analyse", lambda root, diff: report)


def _callers_of(tree: ast.Module, name: str) -> set[str]:
    callers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == name
                ):
                    callers.add(node.name)
    return callers


async def test_every_review_path_carries_the_only_tests_symbols(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-1 (#1254): символы only_tests уезжают судящей стороне по КАЖДОМУ пути.

    Пути перечислены поимённо, а не показаны на одном:
    1) облачный заказ (и добор лестницы, и вторая дверь — они собирают заказ
       той же ``prepare_review_order``, что доказывает перечисление вызовов);
    2) локальный заказ — промт в том виде, в каком его получает ревьюер после
       перевыписки кода доступа в момент старта (#1208);
    3) бриф ревью — то, что читает ревьюер, пришедший через MCP или HTTP.
    """
    from hub.services import review_dispatch as rd
    from hub.services.review_dispatch import wait_for_local_runs

    _analyse_returns(monkeypatch, _only_tests_report("mechanical_step"))

    # Перечисление: ревьюер получает промт ТОЛЬКО из prepare_review_order, а
    # её зовут ровно два транспорта. Новый путь, собравший промт мимо неё,
    # уронит эту строку, а не проедет молча.
    tree = ast.parse(Path(rd.__file__).read_text())
    assert _callers_of(tree, "_review_prompt") == {"prepare_review_order"}
    assert _callers_of(tree, "prepare_review_order") == {
        "_prepare_claimed_order",
        "dispatch_local_review",
    }
    # #1399: облачный транспорт собирает заказ через бронь — и только он.
    assert _callers_of(tree, "_prepare_claimed_order") == {"maybe_dispatch_review"}
    # Добор лестницы (#879), переспрос (#1242), вторая ось каскада (#1243) и
    # вторая дверь (#1252) — те же два транспорта.
    assert _callers_of(tree, "maybe_dispatch_review") == {
        "maybe_top_up_incomplete",
        "_ask_again",
        "_ask_a_stronger_model",
        # #1561: обратная вторая дверь — облако после отказа локального deep.
        "_cloud_after_local",
    }
    assert _callers_of(tree, "dispatch_local_review") == {
        "maybe_dispatch_review",
        "open_second_door",
        # #1561: deep по политике проекта идёт в локальный путь первым.
        "_local_first_order",
    }

    # 1) облако
    recorder = _DispatchRecorder({"agent": {"id": "bc-ot"}, "run": {"id": "r-ot"}})
    _wire(monkeypatch, recorder)
    cloud_id = await _submitted(client, db, "spike-only-tests-cloud")
    cloud_prompt = recorder.calls[0]["prompt_text"]
    assert "mechanical_step" in cloud_prompt
    assert "hub/services/steward_corridor.py" in cloud_prompt
    assert "ordinary_helper" not in cloud_prompt, "назван только only_tests"
    assert 'category="only_tests"' in cloud_prompt, "ревьюеру сказано, как ответить"

    # 3) бриф — тот, что читает ревьюер через MCP/HTTP
    brief = await client.get(f"/api/tasks/{cloud_id}/review-brief")
    assert brief.status_code == 200, brief.text
    section = brief.json()["call_sites"]
    assert section["only_tests_state"] == "no_report", section
    assert [o["symbol"] for o in section["only_tests"]] == ["mechanical_step"]

    # 2) локально, промт — в момент старта
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    seen: list[str] = []

    async def _capturing(prompt, *, timeout=None, prompt_at_slot=None):
        if prompt_at_slot is not None:
            prompt = await prompt_at_slot(prompt)
        seen.append(prompt)
        return None

    monkeypatch.setattr(local_reviewer, "run_review", _capturing)
    await _submitted(
        client,
        db,
        "spike-only-tests-local",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    assert len(seen) == 1, "локальный прогон обязан был стартовать"
    assert "mechanical_step" in seen[0]
    assert 'category="only_tests"' in seen[0]


async def test_an_empty_only_tests_list_leaves_the_order_as_it_was(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1254, риск «общий канал»: без кандидатов заказ не получает ни строки.

    И «анализ не состоялся», и «анализ прошёл, кандидатов нет» оставляют
    промт тем же, каким он был до этой задачи — сравнивается с промтом,
    собранным вовсе без анализа.
    """
    from hub.services import call_sites

    prompts: list[str] = []
    for slug, report in (
        ("spike-ot-none", _only_tests_report()),
        (
            "spike-ot-unknown",
            call_sites.CallSiteReport(call_sites.UNKNOWN, reason="no tree"),
        ),
    ):
        _analyse_returns(monkeypatch, report)
        recorder = _DispatchRecorder(
            {"agent": {"id": f"bc-{slug}"}, "run": {"id": "r"}}
        )
        _wire(monkeypatch, recorder)
        await _submitted(client, db, slug)
        prompts.append(recorder.calls[0]["prompt_text"])

    for prompt in prompts:
        assert "only_tests" not in prompt
        assert "ПРОВЕРЬ ДОСТИЖИМОСТЬ" not in prompt
    # Две задачи отличаются номером — его и снимаем, остальное байт в байт.
    first, second = (
        re.sub(r"#\d+|task_id=\d+|/tasks/\d+|task-\d+", "N", p) for p in prompts
    )
    assert first == second


async def _only_tests_submission(client, db, monkeypatch, slug, *symbols) -> int:
    _analyse_returns(monkeypatch, _only_tests_report(*symbols))
    _wire(
        monkeypatch,
        _DispatchRecorder({"agent": {"id": f"bc-{slug}"}, "run": {"id": "r"}}),
    )
    return await _submitted(client, db, slug)


async def _only_tests_section(client, task_id: int) -> dict:
    brief = await client.get(f"/api/tasks/{task_id}/review-brief")
    assert brief.status_code == 200, brief.text
    return brief.json()["call_sites"]


async def test_every_named_symbol_comes_back_with_an_outcome(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1254): у каждого названного символа — исход, молчание исходом нет.

    Четыре символа, четыре ответа отчёта: подтверждён, снят с путём, «снят»
    без пути, не упомянут. Засчитываются только первые два.
    """
    task_id = await _only_tests_submission(
        client,
        db,
        monkeypatch,
        "spike-ot-outcomes",
        "mechanical_step",
        "hub_submit_probe",
        "self_approval_for",
        "approve_without_a_human",
    )
    await _report_on(
        client,
        task_id,
        confirmed=[
            {
                "title": "mechanical_step",
                "category": "only_tests",
                "severity": "high",
                "locator": "file",
                "file": "hub/services/steward_corridor.py",
                "detail": "ни одна строка прода его не зовёт",
            }
        ],
        rejected=[
            {
                "title": "hub_submit_probe",
                "category": "only_tests",
                "reason": "hub/mcp_server.py: зарегистрирован @mcp.tool, зовётся по имени через tools/call",
            },
            {"title": "self_approval_for", "category": "only_tests", "reason": ""},
        ],
    )

    section = await _only_tests_section(client, task_id)
    outcomes = {o["symbol"]: o for o in section["only_tests"]}
    assert section["only_tests_state"] == "named"
    assert set(outcomes) == {
        "mechanical_step",
        "hub_submit_probe",
        "self_approval_for",
        "approve_without_a_human",
    }, "каждый названный символ обязан быть в итоге, отвеченный или нет"
    assert outcomes["mechanical_step"]["outcome"] == "unreachable"
    assert outcomes["hub_submit_probe"]["outcome"] == "cleared"
    assert "tools/call" in outcomes["hub_submit_probe"]["call_path"]
    assert outcomes["self_approval_for"]["outcome"] == "silent", (
        "снятие без названного пути исходом не считается"
    )
    assert outcomes["approve_without_a_human"]["outcome"] == "silent", (
        "символ, о котором отчёт молчит, исходом не считается"
    )
    assert "без исхода 2" in section["only_tests_summary"]


async def test_an_empty_list_is_not_sold_as_a_clean_verdict(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1254): «символов не названо» ≠ «символы названы и сняты» (#750)."""
    from hub.services import call_sites

    empty_id = await _only_tests_submission(client, db, monkeypatch, "spike-ot-empty")
    await _report_on(client, empty_id)
    empty = await _only_tests_section(client, empty_id)

    cleared_id = await _only_tests_submission(
        client,
        db,
        monkeypatch,
        "spike-ot-cleared",
        "hub_submit_probe",
        "hub_other_tool",
    )
    await _report_on(
        client,
        cleared_id,
        rejected=[
            {
                "title": name,
                "category": "only_tests",
                "reason": f"hub/mcp_server.py: @mcp.tool {name}",
            }
            for name in ("hub_submit_probe", "hub_other_tool")
        ],
    )
    cleared = await _only_tests_section(client, cleared_id)

    # Третий ответ: разбор при заказе не состоялся вовсе.
    _analyse_returns(
        monkeypatch, call_sites.CallSiteReport(call_sites.UNKNOWN, reason="no tree")
    )
    _wire(
        monkeypatch, _DispatchRecorder({"agent": {"id": "bc-unk"}, "run": {"id": "r"}})
    )
    unknown_id = await _submitted(client, db, "spike-ot-unknown-walk")
    await _report_on(client, unknown_id)
    unknown = await _only_tests_section(client, unknown_id)

    assert empty["only_tests_state"] == "none_named"
    assert empty["only_tests"] == []
    assert cleared["only_tests_state"] == "named"
    assert [o["outcome"] for o in cleared["only_tests"]] == ["cleared", "cleared"]
    assert unknown["only_tests_state"] == "not_analysed", (
        "не смотрели — это третий ответ, а не «кандидатов нет»"
    )
    summaries = {
        empty["only_tests_summary"],
        cleared["only_tests_summary"],
        unknown["only_tests_summary"],
    }
    assert len(summaries) == 3, "три разных итога обязаны читаться по-разному"
    assert "отсутствие данных" in empty["only_tests_summary"]


async def test_the_readout_scores_exactly_what_the_order_named(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1254, находки c7488ab8 / d848b4a0 / 80985368: бриф судит по ЗАКАЗУ.

    Второй разбор при чтении брифа мог дать другой набор: база протухла и
    бриф разбор выключил, дерево сдвинулось между вызовами, потолок списка
    обрезал промт. Набор названных символов запоминается в строке заказа, и
    итог считается ровно по нему.
    """
    from hub.services import call_sites

    many = [f"tool_{i:02d}" for i in range(25)]
    task_id = await _only_tests_submission(
        client, db, monkeypatch, "spike-ot-pinned", *many
    )
    recorder_prompt = (await _any_dispatch_row(db, task_id))["only_tests"]
    assert json.loads(recorder_prompt) == many, "заказ помнит ровно названное"

    # Дрейф: второй разбор видит другой символ; и протухшая база: не видит ничего.
    _analyse_returns(monkeypatch, _only_tests_report("drifted_symbol"))
    drifted = await _only_tests_section(client, task_id)
    _analyse_returns(
        monkeypatch, call_sites.CallSiteReport(call_sites.UNKNOWN, reason="stale")
    )
    stale = await _only_tests_section(client, task_id)

    for section in (drifted, stale):
        assert [o["symbol"] for o in section["only_tests"]] == many, section
        assert section["only_tests_state"] == "no_report"


async def test_the_order_names_every_candidate(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1254, находка 80985368: промт называет ВСЕ символы, которые потом судят."""
    many = [f"tool_{i:02d}" for i in range(25)]
    recorder = _DispatchRecorder({"agent": {"id": "bc-many"}, "run": {"id": "r"}})
    _analyse_returns(monkeypatch, _only_tests_report(*many))
    _wire(monkeypatch, recorder)
    await _submitted(client, db, "spike-ot-many")
    prompt = recorder.calls[0]["prompt_text"]
    missing = [name for name in many if f"- {name} (" not in prompt]
    assert missing == [], f"в заказе не названы: {missing}"


async def test_no_current_report_is_said_as_such(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1254, находка 33559ae2: нет отчёта — это не «ревьюер промолчал»."""
    task_id = await _only_tests_submission(
        client, db, monkeypatch, "spike-ot-noreport", "mechanical_step"
    )
    before = await _only_tests_section(client, task_id)
    assert before["only_tests_state"] == "no_report"
    assert [o["outcome"] for o in before["only_tests"]] == ["pending"]
    assert "актуального отчёта нет" in before["only_tests_summary"]

    await _report_on(client, task_id)
    after = await _only_tests_section(client, task_id)
    assert after["only_tests_state"] == "named"
    assert [o["outcome"] for o in after["only_tests"]] == ["silent"], (
        "а вот отчёт, который промолчал, — это уже «без исхода»"
    )


async def test_the_card_shows_the_only_tests_outcome(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1254, находка bf42c7bf: итог виден там, куда смотрит человек — в карточке."""
    task_id = await _only_tests_submission(
        client, db, monkeypatch, "spike-ot-card", "mechanical_step"
    )
    await _report_on(
        client,
        task_id,
        rejected=[
            {
                "title": "mechanical_step",
                "category": "only_tests",
                "reason": "hub/poller.py: _sweep зовёт его по имени",
            }
        ],
    )
    page = await client.get(f"/tasks/{task_id}")
    assert page.status_code == 200
    assert "снято с путём вызова 1" in page.text, "итог only_tests — на панели"


async def test_the_readout_follows_the_order_of_the_shown_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1254, находка 63a14461: набор берётся у заказа ПОКАЗАННОГО отчёта.

    Лестница (#879): лёгкий заказ назвал одно, добор deep — другое, а
    показан пока отчёт лёгкого. Последний заказ поколения здесь чужой.
    """
    task_id = await _only_tests_submission(
        client, db, monkeypatch, "spike-ot-ladder", "lite_sym"
    )
    reviewer = 77
    await db.execute(
        "UPDATE review_dispatches SET reviewer_principal_id = ? WHERE task_id = ?",
        (reviewer, task_id),
    )
    await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="bc-deep",
        run_id="r-deep",
        model="grok-4.6",
        profile=DEEP,
        reviewer_principal_id=reviewer,
        only_tests=["deep_sym"],
    )
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        raw_count=1,
        incomplete=True,
        findings_rejected=json.dumps(
            [
                {
                    "title": "lite_sym",
                    "category": "only_tests",
                    "reason": "hub/poller.py: зовётся по имени",
                }
            ]
        ),
        principal_id=reviewer,
    )
    await db.commit()
    # У запасного пути (раздел брифа) — свой, третий набор: итог обязан прийти
    # из заказа отчёта, а не совпасть с ним случайно (находка 4a226dcf).
    _analyse_returns(monkeypatch, _only_tests_report("fallback_sym"))

    section = await _only_tests_section(client, task_id)
    assert section["only_tests"] == [
        {
            "symbol": "lite_sym",
            "outcome": "cleared",
            "call_path": "hub/poller.py: зовётся по имени",
        }
    ], section


async def test_a_report_of_the_previous_submission_is_no_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1254, находка 6e76db6b: пересдача без нового отчёта — это no_report.

    Отчёт прошлого поколения снял символ; о новом коде он не говорит ничего.
    """
    task_id = await _only_tests_submission(
        client, db, monkeypatch, "spike-ot-resubmit", "mechanical_step"
    )
    await _report_on(
        client,
        task_id,
        rejected=[
            {
                "title": "mechanical_step",
                "category": "only_tests",
                "reason": "hub/poller.py: _sweep зовёт его по имени",
            }
        ],
    )
    await db.execute(
        "UPDATE tasks SET submission_generation = 2, submission_sha = ? WHERE id = ?",
        ("d" * 40, task_id),
    )
    await db.commit()

    section = await _only_tests_section(client, task_id)
    assert section["only_tests_state"] == "no_report", section
    assert [o["outcome"] for o in section["only_tests"]] == ["pending"]


@pytest.mark.parametrize("case", ["no_context", "empty_diff", "walk_raises"])
async def test_a_walk_that_did_not_run_is_recorded_as_null(case, monkeypatch):
    """#1254, находка 0b1110ec: «не смотрели» — это None, а не пустой список.

    Пустой список в строке заказа читается как «разбор прошёл и никого не
    назвал»; ранний выход обязан оставить NULL.
    """
    from hub.services import call_sites
    from hub.services import review_dispatch as rd

    def _boom(root, diff):
        raise RuntimeError("walk failed")

    monkeypatch.setattr(call_sites, "analyse", _boom)
    ctx, diff = {
        "no_context": (None, _HARMLESS_DIFF),
        "empty_diff": (("/tmp/ws", "develop"), ""),
        "walk_raises": (("/tmp/ws", "develop"), _HARMLESS_DIFF),
    }[case]
    assert await rd._only_tests_of(ctx, diff) is None


async def test_a_failed_walk_leaves_null_in_the_order_row(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1254, находка 0b1110ec: сквозь заказ — NULL в строке, а не '[]'."""
    from hub.services import call_sites

    def _boom(root, diff):
        raise RuntimeError("walk failed")

    monkeypatch.setattr(call_sites, "analyse", _boom)
    _wire(
        monkeypatch, _DispatchRecorder({"agent": {"id": "bc-null"}, "run": {"id": "r"}})
    )
    task_id = await _submitted(client, db, "spike-ot-null")
    assert (await _any_dispatch_row(db, task_id))["only_tests"] is None


async def test_a_refused_call_stub_carries_the_orders_only_tests(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1337): заглушка синхронного отказа несёт only_tests заказа.

    #1242 перенесла создание заглушки в ``_owe_the_refused_call``; строка
    #1254 ``only_tests=order.only_tests`` там потеряла точку приложения. Без
    сопряжения заглушка осела бы с NULL, и бриф не узнал бы, что заказ
    называл ревьюеру. Локальный путь выключен: вторая дверь не открылась, и
    заглушка — единственная строка заказа.
    """
    _analyse_returns(monkeypatch, _only_tests_report("mechanical_step"))
    _wire(monkeypatch, _Sequence([(None, _RATE_LIMIT)]))
    _no_local_path(monkeypatch)

    task_id = await _submitted(
        client, db, "refused-stub-only-tests", policy={"review": "dispatch"}
    )

    rows = await _rows_of(db, task_id)
    assert len(rows) == 1, rows
    stub = rows[0]
    assert stub["agent_id"] == "", "это заглушка отказа, а не созданный агент"
    assert stub["only_tests"] is not None, "заглушка потеряла only_tests заказа"
    assert json.loads(stub["only_tests"]) == ["mechanical_step"]
