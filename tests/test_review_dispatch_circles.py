"""Hub-dispatched cross-model reviews (#757): circles (split of the former single file, #1670)."""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from tests.review_dispatch_support import (
    _author_closed_them,
    _confirmed,
    _DispatchRecorder,
    _generation_with_findings,
    _submitted,
    _walk_the_circle,
    _wire,
)


def _unresolved(title: str) -> dict:
    """Неразрешённая находка: категории у неё нет по модели (#1085)."""
    return {"title": title, "why": "адъюдикаторы разошлись"}


async def _circle_notices(db: aiosqlite.Connection, task_id: int) -> list[str]:
    """Всё, что карточка сказала о круге."""
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if "идёт по кругу" in dict(u)["content"]
    ]


async def test_a_task_going_in_circles_is_named_and_escalated(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1235): три захода — круг назван, и человека зовут решать.

    Образец взят с прода 09.09.2026 без округления. #1171: отчёт #307 дал
    три подтверждённые и пять неразрешённых, все восемь разобраны и
    закрыты, пересдача — и отчёт #323 принёс ПЯТЬ НОВЫХ неразрешённых. То
    же на #1208 и #1169. Ни один существующий потолок не сработал:
    review_cycle на #1171 оставался нулём при третьем заходе, потому что
    работу автору никто не возвращал — он пересдавал сам.

    Последний отчёт приходит НАСТОЯЩИМ путём приёма, а не записью в
    таблицу: круг обязан называться в тот момент, когда очередной отчёт
    принесли, иначе он не позовёт никого.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1235"}, "run": {"id": "r-1235"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-circle")

    # Три первых слоя: каждый закрыт, следующий принёс новые находки.
    layer1 = ([_confirmed("гонка на записи", "concurrency", 10)], [_unresolved("A")])
    layer2 = ([_confirmed("необработанный код возврата", "error-handling", 20)], [])
    layer3 = ([_confirmed("тест не убивает мутацию", "test-adequacy", 30)], [])
    await _walk_the_circle(db, task_id, [layer1, layer2, layer3])

    from hub.services.review_dispatch import review_circle

    assert (await review_circle(db, task_id)).count == 2, (
        "предпосылка: до четвёртого отчёта заходов два, круг ещё не назван"
    )
    assert await _circle_notices(db, task_id) == [], "порог ещё не достигнут"

    # Третий заход: закрываем слой 3 и принимаем ЧЕТВЁРТЫЙ отчёт как отчёт.
    await _author_closed_them(
        db,
        task_id,
        (await _last_review_id(db, task_id)),
        3,
        confirmed=layer3[0],
        unresolved=layer3[1],
    )
    await db.execute("UPDATE tasks SET submission_generation=4 WHERE id=?", (task_id,))
    await db.commit()
    from hub.services.machine_review_intake import record_machine_review
    from hub.models import MachineReviewSubmit

    await record_machine_review(
        db,
        task_id,
        MachineReviewSubmit(
            harness_skill="deep-review",
            model="grok-4.6",
            raw_count=1,
            incomplete=False,
            findings_confirmed=[_confirmed("утечка дескриптора", "resource-leak", 40)],
        ),
        principal_id=None,
        username="cursor-cloud-reviewer",
    )

    circle = await review_circle(db, task_id)
    assert circle.count == 3, "три захода: 1→2, 2→3, 3→4"

    notices = await _circle_notices(db, task_id)
    assert len(notices) == 1, (
        "круг обязан быть назван в карточке: пока он не назван, его не видит "
        "ни автор, ни человек, ни метрики"
    )
    said = notices[0]
    assert "3-й раз" in said, "число заходов названо"
    for ordinal in (1, 2, 3):
        assert f"заход {ordinal}" in said, "разбивка приводится по КАЖДОМУ заходу"
    assert "закрыто 2, пришло новых 1" in said, (
        "первый заход закрыл две находки (одну подтверждённую и одну "
        "неразрешённую) и получил одну новую — оба числа стоят рядом"
    )
    for outcome in ("принять как есть", "отпустить", "продолжать"):
        assert outcome in said, (
            "три исхода названы явно: сигнал о круге легко прочесть как "
            "разрешение перестать чинить настоящие дефекты"
        )
    assert "Ревью НЕ выключено и пересдача не запрещена" in said

    events = [
        dict(r)
        for r in await repo.list_events(
            db, since=0, kinds=["review_circle_named"], limit=10
        )
    ]
    assert len(events) == 1, "событие в ленте, а не только строка в карточке"
    assert json.loads(events[0]["payload"])["laps"] == 3

    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "сигнал не двигает задачу сам"
    assert row["review_cycle"] == 0, (
        "и это ровно тот случай, который существующий потолок не видит: "
        "циклов ревью ноль при третьем заходе (наблюдено на #1171)"
    )

    from hub.services.review_brief import build_review_brief

    brief = await build_review_brief(db, task_id)
    assert brief.review_circle.laps == 3 and brief.review_circle.named, (
        "число заходов видно и в брифе ревью, тем же счётом, что в карточке"
    )
    assert len(brief.review_circle.breakdown) == 3

    from hub.mcp_server import _review_circle_line

    rendered = _review_circle_line(brief.model_dump())
    assert "Круг ревью: заходов 3" in rendered and "заход 1" in rendered, (
        "ревьюер читает бриф ТЕКСТОМ: число, не дошедшее до строки, не прочитает никто"
    )


async def _last_review_id(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT id FROM machine_reviews WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    return int(dict(rows[0])["id"])


async def test_a_plain_resubmission_is_not_a_circle(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1235): столько же пересдач, но находок между ними не было.

    МУТАЦИЯ, которую этот тест обязан ловить: считать заходом любую
    пересдачу. Счётчик, растущий на каждое поколение, назвал бы кругом
    обычную работу — задачу, которую пересдавали четыре раза с чистыми
    отчётами, — и позвал бы человека к тому, где решать нечего.

    Проверяется НОЛЬ заходов, а не отсутствие алерта: алерта нет и при
    пороге, которого не достигли, так что одно только молчание карточки
    отличить эти два случая не может.
    """
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-plain"}, "run": {"id": "r-plain"}}
    )
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-plain")
    for generation in (1, 2, 3, 4):
        await _generation_with_findings(db, task_id, generation)

    from hub.services.review_dispatch import name_the_circle, review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 0, (
        "четыре поколения с чистыми отчётами — это не круг, а работа: "
        "находок не было, закрывать было нечего"
    )
    assert not circle.named
    assert await name_the_circle(db, task_id) is False
    assert await _circle_notices(db, task_id) == []


async def test_a_closed_layer_without_new_findings_ends_the_circle(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Контроль к AC-2: находки были и закрыты, но нового слоя не пришло.

    Отдельный тест, потому что «находок не было вовсе» и «находки были,
    круг кончился» — разные состояния, и правило, потерявшее второе,
    прошло бы предыдущий тест целиком. Здесь же проверяется, что серия
    считается ПОДРЯД: чистый отчёт обнуляет счёт, а не откладывается в
    заслугу, по которой человека позовут к уже вышедшей задаче.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-end"}, "run": {"id": "r-end"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-ended")
    layer1 = ([_confirmed("гонка", "concurrency", 10)], [])
    layer2 = ([_confirmed("код возврата", "error-handling", 20)], [])
    layer3 = ([_confirmed("мутация выжила", "test-adequacy", 30)], [])
    await _walk_the_circle(db, task_id, [layer1, layer2, layer3])

    from hub.services.review_dispatch import review_circle

    assert (await review_circle(db, task_id)).count == 2, "предпосылка: два захода"

    # Автор закрыл третий слой, и новый отчёт пришёл чистым.
    await _author_closed_them(
        db,
        task_id,
        await _last_review_id(db, task_id),
        3,
        confirmed=layer3[0],
        unresolved=layer3[1],
    )
    await _generation_with_findings(db, task_id, 4)

    circle = await review_circle(db, task_id)
    assert circle.count == 0, (
        "круг кончился на чистом отчёте — считается хвостовая серия подряд"
    )
    assert not circle.named


async def test_a_repeating_category_is_named_apart_from_the_count(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1235): повтор категории отмечен ОТДЕЛЬНО от числа заходов.

    Признак другой и важнее: три захода с находками разного рода — работа,
    идущая вглубь, а повтор категории означает, что харнесс ходит по одному
    и тому же месту. Поэтому проверяется не только наличие слов, но и то,
    что при РАЗНЫХ категориях отметки нет — иначе «отмечено отдельно» было
    бы совместимо с «отмечается всегда».
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-cat"}, "run": {"id": "r-cat"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-category")
    same = "test-adequacy"
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("мутация выжила в А", same, 10)], []),
            ([_confirmed("мутация выжила в Б", same, 20)], []),
            ([_confirmed("мутация выжила в В", same, 30)], []),
        ],
    )

    from hub.services.review_dispatch import name_the_circle, review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 2, "заходов два"
    assert circle.repeated_categories == (same,), (
        "категория повторяется — это отдельный факт, а не следствие счёта"
    )
    assert await name_the_circle(db, task_id) is True
    said = (await _circle_notices(db, task_id))[0]
    assert "ОТДЕЛЬНО" in said and same in said, "повтор назван своим абзацем"
    assert "харнесс ходит по одному" in said, (
        "названо, ЧТО этот признак означает, а не только что он есть"
    )

    # Контроль: те же два захода, но категории разные — отметки нет.
    other = await _submitted(client, db, "spike-1235-varied")
    await _walk_the_circle(
        db,
        other,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )
    varied = await review_circle(db, other)
    assert varied.count == 2, "заходов столько же"
    assert varied.repeated_categories == (), (
        "категории разные — повтора нет, и число заходов этого не подменяет"
    )
    assert await name_the_circle(db, other) is True
    assert "ОТДЕЛЬНО" not in (await _circle_notices(db, other))[0]


async def test_the_circle_threshold_comes_from_the_config(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Порог читается из конфига и проверен на трёх значениях.

    Он выбран по трём наблюдениям одного дня — для числа это недобор, и
    менять его придётся без правки кода. Значение, зашитое в коде, стоило
    бы выката на каждое уточнение.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-thr"}, "run": {"id": "r-thr"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-1235-threshold")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    from hub.services.review_dispatch import review_circle

    for threshold, expected in ((2, True), (3, False), (5, False)):
        monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", threshold)
        circle = await review_circle(db, task_id)
        assert circle.count == 2, "заходов два при любом пороге"
        assert circle.threshold == threshold
        assert circle.named is expected, (
            f"порог {threshold}: круг называется только когда заходов не меньше"
        )


async def test_the_circle_is_named_once_per_generation(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Дедуп в пределах поколения: второй отчёт заходов не прибавляет.

    Лестница добора (#879) кладёт на одно поколение два отчёта. Без дедупа
    карточка получила бы вторую одинаковую запись про тот же круг.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-once"}, "run": {"id": "r-once"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-once")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    from hub.services.review_dispatch import name_the_circle

    assert await name_the_circle(db, task_id) is True
    assert await name_the_circle(db, task_id) is False
    assert len(await _circle_notices(db, task_id)) == 1


async def test_findings_left_unclosed_are_not_a_lap(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Заход требует, чтобы находки БЫЛИ ЗАКРЫТЫ правкой.

    Пересдача, на которой автор объявил находки ложными или отложил их, —
    не круг: работы по коду на ней не было, а разговор про ложные находки
    — это разговор про точность харнесса, и у него свои метрики. Без этого
    теста мутация «считать заходом любую пересдачу, где отчёты были»
    осталась бы живой: предыдущий контроль ловит только пересдачу вообще
    без находок.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-open"}, "run": {"id": "r-open"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-unclosed")
    layers = [
        ([_confirmed("гонка", "concurrency", 10)], [_unresolved("A")]),
        ([_confirmed("код возврата", "error-handling", 20)], [_unresolved("B")]),
        ([_confirmed("утечка", "resource-leak", 30)], [_unresolved("C")]),
    ]
    previous: tuple[int, int, list[dict], list[dict]] | None = None
    for generation, (confirmed, unresolved) in enumerate(layers, start=1):
        review_id = await _generation_with_findings(
            db, task_id, generation, confirmed=confirmed, unresolved=unresolved
        )
        if previous is not None:
            prev_review, prev_generation, prev_conf, prev_unres = previous
            await _author_closed_them(
                db,
                task_id,
                prev_review,
                prev_generation,
                confirmed=prev_conf,
                unresolved=prev_unres,
                outcome_confirmed="false_positive",
                outcome_unresolved="not_a_defect",
            )
        previous = (review_id, generation, confirmed, unresolved)

    from hub.services.review_dispatch import name_the_circle, review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 0, (
        "находки были и новые приходили, но чинить автор ничего не стал — "
        "заходом это не считается"
    )
    assert await name_the_circle(db, task_id) is False
    assert await _circle_notices(db, task_id) == []


async def test_a_circle_of_uncategorised_findings_claims_no_repeat(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Круг из ОДНИХ неразрешённых находок не объявляет повтор категории.

    Это форма круга, который оплачивался живьём: #1171 — ноль
    подтверждённых и пять НОВЫХ неразрешённых, #1208 — четыре
    неразрешённые. У неразрешённой записи категории нет по модели (#1085),
    и здесь она остаётся пустой.

    МУТАЦИЯ, которую тест обязан ловить: снять отсев пустой категории в
    сравнении на повтор. Тогда «неизвестно» склеилось бы с «то же самое», и
    САМЫЙ СИЛЬНЫЙ сигнал этой задачи — «харнесс ходит по одному месту, и
    это важнее числа заходов» — срабатывал бы на каждом круге, собранном из
    неразрешённых находок, то есть на самом частом. Человека звали бы
    разбираться с повтором, которого никто не наблюдал, да ещё и с пустым
    именем категории в тексте.
    """
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-nocat"}, "run": {"id": "r-nocat"}}
    )
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-uncategorised")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([], [_unresolved("адъюдикаторы разошлись про А")]),
            ([], [_unresolved("адъюдикаторы разошлись про Б")]),
            ([], [_unresolved("адъюдикаторы разошлись про В")]),
        ],
    )

    from hub.services.review_dispatch import name_the_circle, review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 2, (
        "заходы считаются и по неразрешённым: круг с прода состоял в "
        "основном из них, и счёт по одним подтверждённым не увидел бы его"
    )
    assert circle.repeated_categories == (), (
        "категории у неразрешённых находок нет — это «неизвестно», а не «та же самая»"
    )
    assert [lap.repeated_categories for lap in circle.laps] == [(), ()]

    assert await name_the_circle(db, task_id) is True
    said = (await _circle_notices(db, task_id))[0]
    assert "ОТДЕЛЬНО" not in said and "Повтор категории" not in said, (
        "круг назван, а повтор категории — нет: это разные признаки"
    )


async def test_the_repeat_reaches_the_reviewer_brief_text(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Повтор категории доходит до СТРОКИ брифа, а не только до поля.

    Ревьюер читает бриф текстом (hub_get_review_brief склеивает его в
    строки), и признак, оставшийся в структуре, до него не доходит. Тест
    ловит мутацию, снимающую повтор из строки: число заходов в ней при
    этом остаётся, и по нему одному подмену не заметить.
    """
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-brief"}, "run": {"id": "r-brief"}}
    )
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-brief")
    same = "test-adequacy"
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("мутация выжила в А", same, 10)], []),
            ([_confirmed("мутация выжила в Б", same, 20)], []),
            ([_confirmed("мутация выжила в В", same, 30)], []),
        ],
    )

    from hub.mcp_server import _review_circle_line
    from hub.services.review_brief import build_review_brief

    brief = await build_review_brief(db, task_id)
    assert brief.review_circle.repeated_categories == [same]
    rendered = _review_circle_line(brief.model_dump())
    assert "Круг ревью: заходов 2" in rendered
    assert f"Повтор категории: {same}" in rendered, (
        "признак, не дошедший до строки брифа, ревьюер не прочитает"
    )
    assert "важнее числа заходов" in rendered


async def test_a_zero_threshold_switches_the_signal_off(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Порог 0 выключает сигнал, а не зовёт человека на каждую задачу.

    Ноль — это то, чем такую вещь выключают в конфиге, и без явной защиты
    сравнение «заходов не меньше порога» стало бы истинным при НУЛЕ
    заходов: карточка каждой задачи получила бы алерт о круге, которого
    нет. Порог выбран по трём наблюдениям одного дня, так что выключатель
    ему понадобится раньше, чем следующее уточнение.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-off"}, "run": {"id": "r-off"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 0)

    from hub.services.review_dispatch import name_the_circle, review_circle

    quiet = await _submitted(client, db, "spike-1235-off-quiet")
    circle = await review_circle(db, quiet)
    assert circle.count == 0 and circle.threshold == 0
    assert not circle.named, "ноль заходов при пороге 0 — это не круг"
    assert await name_the_circle(db, quiet) is False
    assert await _circle_notices(db, quiet) == []

    # И на задаче, которая по кругу действительно идёт, ноль тоже молчит.
    spinning = await _submitted(client, db, "spike-1235-off-spinning")
    await _walk_the_circle(
        db,
        spinning,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )
    spun = await review_circle(db, spinning)
    assert spun.count == 2, "заходы считаются по-прежнему"
    assert not spun.named, "но при пороге 0 человека не зовут"
    assert await name_the_circle(db, spinning) is False


async def test_a_deeper_lap_is_named_again_on_the_next_generation(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Следующий заход называется СНОВА: дедуп живёт в пределах поколения.

    МУТАЦИЯ, которую тест обязан ловить: убрать номер поколения из метки
    дедупа. Одна запись за всю жизнь задачи прошла бы тест на дедуп в
    пределах одной сдачи целиком, а на проде означала бы, что про третий,
    четвёртый и пятый заходы человеку не скажут ничего — как раз тогда,
    когда круг стал дороже всего.
    """
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-again"}, "run": {"id": "r-again"}}
    )
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-again")
    layers = [
        ([_confirmed("гонка", "concurrency", 10)], []),
        ([_confirmed("код возврата", "error-handling", 20)], []),
        ([_confirmed("утечка", "resource-leak", 30)], []),
    ]
    await _walk_the_circle(db, task_id, layers)

    from hub.services.review_dispatch import name_the_circle, review_circle

    assert await name_the_circle(db, task_id) is True
    assert len(await _circle_notices(db, task_id)) == 1

    # Автор закрыл третий слой, пересдал, и четвёртый отчёт принёс новое.
    await _author_closed_them(
        db,
        task_id,
        await _last_review_id(db, task_id),
        3,
        confirmed=layers[2][0],
        unresolved=layers[2][1],
    )
    await _generation_with_findings(
        db,
        task_id,
        4,
        confirmed=[_confirmed("дескриптор не закрыт", "resource-leak", 40)],
    )

    assert (await review_circle(db, task_id)).count == 3, "заходов стало три"
    assert await name_the_circle(db, task_id) is True, (
        "новый заход — новая сдача — новая запись: молчание про углубившийся "
        "круг было бы худшим из возможных исходов"
    )
    notices = await _circle_notices(db, task_id)
    assert len(notices) == 2
    assert "3-й раз" in notices[1], "и во второй раз названо новое число заходов"


async def test_two_reports_on_one_generation_do_not_double_count_a_finding(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Разбивка «пришло новых N» считает НАХОДКИ, а не строки отчётов.

    Лестница добора (#879) кладёт на одно поколение два отчёта, и второй
    часто повторяет находки первого. Находки поколения сливаются в один
    список, поэтому длина списка — не число находок: тот же дефект,
    названный дважды, дал бы двойку там, где находка одна.

    Число дорогое: человек по нему решает, стоит ли продолжать круг. «Слой
    из двух находок» вместо одной — ровно то завышение, из-за которого он
    решит иначе.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-dbl"}, "run": {"id": "r-dbl"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-double")
    first = [_confirmed("гонка на записи", "concurrency", 10)]
    review_id = await _generation_with_findings(db, task_id, 1, confirmed=first)

    # Поколение 2: ДВА отчёта. Второй повторяет находку первого и приносит
    # свою. Строк в слитом списке четыре, РАЗНЫХ находок — три.
    leak = _confirmed("утечка дескриптора", "resource-leak", 40)
    hang = _confirmed("необработанный код возврата", "error-handling", 50)
    starve = _confirmed("тест не убивает мутацию", "test-adequacy", 60)
    await _generation_with_findings(db, task_id, 2, confirmed=[leak, hang])
    await _generation_with_findings(db, task_id, 2, confirmed=[leak, starve])
    await _author_closed_them(db, task_id, review_id, 1, confirmed=first, unresolved=[])

    from hub.services.review_dispatch import review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 1, (
        "заход один: два отчёта на поколении — всё ещё одно поколение"
    )
    assert circle.laps[0].arrived == 3, (
        "находок три, а строк в слитом списке четыре: считать надо "
        "уникальные finding_uid. Число не константа — оно обязано ЗАВИСЕТЬ "
        "от размера слоя, иначе человеку показывают не то, по чему он решает"
    )
    assert "пришло новых 3" in circle.breakdown()[0]


async def test_the_lap_count_stands_on_the_card_before_the_threshold(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Счёт заходов виден в КАРТОЧКЕ, а не только в алерте после порога.

    Постановка требует, чтобы число было видно в карточке и в брифе, и
    комментарий к REVIEW_CIRCLE_THRESHOLD обещает то же при пороге 0:
    «заходы считаются по-прежнему и видны в карточке и в брифе, но
    человека не зовут». Пока число доходит до карточки одним алертом на
    пороге, до порога карточка выглядит так, будто круга нет вовсе, —
    а молчание читается как «чисто» (#516, #549).
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-card"}, "run": {"id": "r-card"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-card")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    assert await _circle_notices(db, task_id) == [], "порог не достигнут: не зовём"

    card = (await client.get(f"/api/tasks/{task_id}")).json()
    circle = card.get("review_circle") or {}
    assert circle.get("laps") == 2, (
        "два захода из трёх обязаны стоять в карточке ДО порога: иначе "
        "человек видит круг только тогда, когда круг уже стал дорогим"
    )
    assert circle.get("named") is False, "видно — не значит позвали"
    assert len(circle.get("breakdown") or []) == 2, (
        "разбивка «закрыто / пришло новых» едет вместе с числом"
    )


async def test_a_switched_off_threshold_still_counts_on_the_card(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Порог 0 гасит ЗОВ, а не счёт: ровно то, что обещает конфиг.

    Выключатель, прячущий заодно и число, отнял бы у человека
    единственный способ увидеть, что выключил он не то.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-off"}, "run": {"id": "r-off"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 0)

    task_id = await _submitted(client, db, "spike-1235-off")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    card = (await client.get(f"/api/tasks/{task_id}")).json()
    circle = card.get("review_circle") or {}
    assert circle.get("laps") == 2 and circle.get("named") is False
    assert await _circle_notices(db, task_id) == []


async def test_an_incomplete_empty_report_does_not_wipe_the_laps(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Неполный отчёт без находок — «неизвестно», а не «чисто».

    Пустой ПОЛНЫЙ отчёт круг кончает: харнесс дочитал и не нашёл ничего.
    Неполный отчёт говорит о себе сам, что дочитал не всё, и лестница
    добора (#879) существует ровно затем, чтобы добрать непрочитанное. Тот
    же файл уже исключает incomplete из «код прочитан» по этой самой
    причине, а докстринг review_circle обещает, что поколение без сведений
    цепь не рвёт: «неизвестно» не равно «чисто».

    Пока цепь рвалась, пустая строка оставалась в machine_reviews
    навсегда, и КАЖДАЯ следующая сдача снова упиралась в ту же пару и
    снова сбрасывала хвост: круг переставал быть видимым насовсем.

    Наблюдено на этой же задаче 10.09.2026: отчёт #366 по поколению 2
    пришёл с incomplete=true и нулём находок, после чего хаб сам вызвал
    глубокий добор.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-inc"}, "run": {"id": "r-inc"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-incomplete")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    from hub.services.review_dispatch import review_circle

    assert (await review_circle(db, task_id)).count == 2, "предпосылка: два захода"

    # Поколение 4: НЕПОЛНЫЙ отчёт, находок ноль.
    await db.execute("UPDATE tasks SET submission_generation=4 WHERE id=?", (task_id,))
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=4,
        harness_skill="lite-diff-review",
        model="grok-4.6",
        raw_count=0,
        incomplete=True,
        findings_confirmed="[]",
        unresolved="[]",
        submitted_by="cursor-cloud-reviewer",
    )
    await db.commit()

    assert (await review_circle(db, task_id)).count == 2, (
        "неполный отчёт не рвёт цепь: он сам говорит, что дочитал не всё, "
        "и обнулять по нему счёт значит объявить «чисто» там, где сказано "
        "«неизвестно»"
    )

    # КОНТРОЛЬ: полный пустой отчёт круг по-прежнему кончает.
    await db.execute("UPDATE tasks SET submission_generation=5 WHERE id=?", (task_id,))
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=5,
        harness_skill="deep-review",
        model="grok-4.6",
        raw_count=0,
        incomplete=False,
        findings_confirmed="[]",
        unresolved="[]",
        submitted_by="cursor-cloud-reviewer",
    )
    await db.commit()

    assert (await review_circle(db, task_id)).count == 0, (
        "а вот ПОЛНЫЙ пустой отчёт круг кончает: харнесс дочитал и не нашёл "
        "ничего, и звать человека к вышедшей задаче незачем"
    )


async def test_the_brief_text_carries_the_count_before_the_circle_is_named(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Строка брифа несёт число и ДО того, как круг назван.

    Все прочие проверки _review_circle_line идут при named=true, поэтому
    мутация «молчать, пока не названо» осталась бы зелёной: ревьюер
    очередной сдачи читал бы отчёт как первый ровно в том случае, ради
    которого строка и заведена.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-txt"}, "run": {"id": "r-txt"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-brieftext")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    from hub.mcp_server import _review_circle_line
    from hub.services.review_brief import build_review_brief

    brief = await build_review_brief(db, task_id)
    assert brief.review_circle.laps == 2 and not brief.review_circle.named
    rendered = _review_circle_line(brief.model_dump())
    assert "Круг ревью: заходов 2" in rendered, (
        "число обязано стоять в ТЕКСТЕ брифа и до порога: ревьюер, не "
        "знающий, что предыдущий слой был разобран, читает отчёт как первый"
    )
    assert "заход 1" in rendered and "заход 2" in rendered


async def test_an_incomplete_report_that_found_things_still_counts(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Неполнота отменяет ЧИСТОТУ отчёта, а не его находки.

    Пропускается только отчёт, не принёсший НИЧЕГО: там сведений ноль.
    Неполный отчёт, который что-то нашёл, — обычный слой находок, и
    выбросить его значило бы спрятать заход, который человек оплатил.

    Наблюдено на этой же задаче 10.09.2026: глубокий отчёт #369 пришёл с
    incomplete=true И с находками — то есть это не редкий угол, а обычный
    исход лестницы добора (#879).
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-inc2"}, "run": {"id": "r-inc2"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-incomplete-found")
    first = [_confirmed("гонка на записи", "concurrency", 10)]
    review_id = await _generation_with_findings(db, task_id, 1, confirmed=first)

    await db.execute("UPDATE tasks SET submission_generation=2 WHERE id=?", (task_id,))
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=2,
        harness_skill="deep-review",
        model="grok-4.6",
        raw_count=1,
        incomplete=True,
        findings_confirmed=json.dumps(
            [_confirmed("утечка дескриптора", "resource-leak", 40)],
            ensure_ascii=False,
        ),
        unresolved="[]",
        submitted_by="cursor-cloud-reviewer",
    )
    await db.commit()
    await _author_closed_them(db, task_id, review_id, 1, confirmed=first, unresolved=[])

    from hub.services.review_dispatch import review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 1, (
        "заход есть: отчёт неполон, но находку он принёс, и заход этой "
        "находкой и состоялся"
    )
    assert circle.laps[0].arrived == 1
