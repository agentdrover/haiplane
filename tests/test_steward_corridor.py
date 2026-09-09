"""Коридор эскалаций как процесс, не только точка входа в act (#1145).

#1107 проверяет коридор ОДИН РАЗ — в момент, когда act запрашивают. Но
включение это событие одного дня, а вырождение — процесс: судья, прошедший
пороги в понедельник, может съехать в штамп или в бесполезную эскалацию всего
подряд к пятнице, и если метрику никто не пойдёт и не прочитает, «я её не
читал» и «всё в порядке» неотличимы. Здесь проверяется повторяемое
наблюдение над той же долей эскалаций, а не замена входному контролю.
"""

from __future__ import annotations

import json

import aiosqlite

from hub.db import fetchall
from hub.services.steward_shadow import (
    ACT_ESCALATION_CEILING,
    ACT_ESCALATION_FLOOR,
    CORRIDOR_INSIDE,
    CORRIDOR_MIN_FOR_CEILING,
    CORRIDOR_MIN_FOR_FLOOR,
    CORRIDOR_NO_SAMPLE,
    EVENT_CORRIDOR_ALERT,
    REASON_OVER_ESCALATING,
    REASON_STAMPING,
    check_escalation_corridor,
    weekly_sample,
)
from tests.test_steward_shadow import _pair, _project


async def _alerts(db: aiosqlite.Connection) -> list[dict]:
    rows = await fetchall(
        db, "SELECT * FROM events WHERE kind=? ORDER BY id ASC", (EVENT_CORRIDOR_ALERT,)
    )
    return [dict(r) for r in rows]


async def test_escalation_corridor_alerts_both_ends(db: aiosqlite.Connection):
    """AC-1: обе границы алертят, и текстами, а не одной общей строкой.

    Выше потолка стюард бесполезен: человек и так разбирает всё сам. Ниже
    пола он штампует: согласие ничего не стоит, потому что несогласие он не
    пробовал ни разу. Это разные болезни, значит и текст у них разный.
    """
    project_id = await _project(db, "corridor-both-ends")

    # Верхняя граница: судья эскалирует почти всё подряд. Ровно
    # CORRIDOR_MIN_FOR_FLOOR — с этого размера доля вообще измерима.
    for _ in range(CORRIDOR_MIN_FOR_FLOOR):
        await _pair(db, project_id, steward="escalate", human=None)

    boundary = await check_escalation_corridor(db)
    assert boundary == REASON_OVER_ESCALATING

    alerts = await _alerts(db)
    assert len(alerts) == 1
    over = json.loads(alerts[0]["payload"])
    assert over["state"] == REASON_OVER_ESCALATING
    assert f"{ACT_ESCALATION_CEILING:.0%}" in over["detail"]

    # Нижняя граница: разбавляем выборку до доли ниже пола. Прежние двадцать
    # эскалаций остаются в окне — 21 из 421 (5.0%, строго ниже) это уже штамповка, а не
    # избыточная эскалация.
    for _ in range(400):
        await _pair(db, project_id, steward="changes_requested", human=None)

    share = (await weekly_sample(db)).share
    assert share is not None and share < ACT_ESCALATION_FLOOR

    boundary = await check_escalation_corridor(db)
    assert boundary == REASON_STAMPING

    alerts = await _alerts(db)
    assert len(alerts) == 2, "смена границы обязана поднять НОВЫЙ алерт"
    under = json.loads(alerts[-1]["payload"])
    assert under["state"] == REASON_STAMPING
    assert f"{ACT_ESCALATION_FLOOR:.0%}" in under["detail"]

    # Формулировки различаются буквально — не два экземпляра одной строки.
    assert over["detail"] != under["detail"]


async def test_empty_window_raises_nothing(db: aiosqlite.Connection):
    """AC-2: за неделю не было ни одного суждения — алерта нет вовсе.

    Пустая выборка — не ноль процентов и не обвинение в штамповке: «нет
    данных» и «ноль эскалаций» это разные состояния (#762).
    """
    assert (await weekly_sample(db)).share is None

    boundary = await check_escalation_corridor(db)

    assert boundary is None
    assert await _alerts(db) == []


async def test_alert_is_written_once_per_state_change(db: aiosqlite.Connection):
    """AC-3: доля держится вне коридора несколько тиков подряд — один алерт.

    Поллер тикает раз в тридцать секунд; строка на тик — это фид, в котором
    больше нечего прочитать.
    """
    project_id = await _project(db, "corridor-quiet")
    for _ in range(CORRIDOR_MIN_FOR_FLOOR):
        await _pair(db, project_id, steward="escalate", human=None)

    for _ in range(5):
        boundary = await check_escalation_corridor(db)
        assert boundary == REASON_OVER_ESCALATING

    alerts = await _alerts(db)
    assert len(alerts) == 1, f"ожидался один алерт, получено {len(alerts)}"


async def test_old_judgements_fall_out_of_the_week_window(db: aiosqlite.Connection):
    """Суждение старше недели не считается — иначе окно не скользит вовсе.

    Не входит в перечисленные AC, но без этой проверки реализация могла бы
    молча считать долю за всё время (как ShadowTable) и всё равно проходить
    три названных теста.
    """
    project_id = await _project(db, "corridor-stale")
    task_id = await _pair(db, project_id, steward="escalate", human=None)
    await db.execute(
        "UPDATE steward_judgements SET created_at = datetime('now', '-30 days') "
        "WHERE task_id=?",
        (task_id,),
    )
    await db.commit()

    assert (await weekly_sample(db)).share is None
    assert await check_escalation_corridor(db) is None
    assert await _alerts(db) == []


async def test_a_second_breach_after_recovery_alerts_again(db: aiosqlite.Connection):
    """Выход, возврат, повторный выход — второй раз алерт обязан прозвучать.

    Дефект, ради которого этот тест написан: пока помнились только две
    границы, возврат ВНУТРЬ коридора следа не оставлял, и повторный выход за
    ту же границу читался как «состояние не менялось». Алерт срабатывал один
    раз за всю жизнь хаба на каждую сторону — то есть молчал ровно в том
    сценарии, ради которого наблюдение и заводили: судья съехал, выправился,
    съехал снова.

    Три названных AC этот случай не ловили: AC-1 меняет границу с верхней на
    нижнюю (это смена состояния), AC-3 держит одно и то же состояние. Между
    ними и лежала дыра.
    """
    project_id = await _project(db, "corridor-again")

    for _ in range(CORRIDOR_MIN_FOR_FLOOR):
        await _pair(db, project_id, steward="escalate", human=None)
    assert await check_escalation_corridor(db) == REASON_OVER_ESCALATING
    assert len(await _alerts(db)) == 1

    # Возврат в коридор: доля 21/81 = 26%, внутри 5–50%.
    for _ in range(60):
        await _pair(db, project_id, steward="approve", human=None)
    assert await check_escalation_corridor(db) is None
    restored = await _alerts(db)
    assert len(restored) == 2, "возврат в коридор — тоже смена состояния"
    assert json.loads(restored[-1]["payload"])["state"] == CORRIDOR_INSIDE

    # Повторный выход за ТУ ЖЕ границу: 121/181 ≈ 67%.
    for _ in range(100):
        await _pair(db, project_id, steward="escalate", human=None)
    assert await check_escalation_corridor(db) == REASON_OVER_ESCALATING

    alerts = await _alerts(db)
    assert len(alerts) == 3, (
        "повторный выход за границу после возврата обязан алертить снова — "
        f"получено записей: {len(alerts)}"
    )
    assert json.loads(alerts[-1]["payload"])["state"] == REASON_OVER_ESCALATING


async def test_a_silent_week_does_not_swallow_the_next_breach(
    db: aiosqlite.Connection,
):
    """Неделя тишины после нарушения записывается — иначе она глушит следующее.

    Второй вход в ту же дыру и потому отдельный тест: если «нет данных» не
    считать состоянием, то после нарушения молчание не меняет памяти, и
    возобновившееся нарушение той же стороны снова окажется «без изменений».
    Заодно это честная запись сама по себе: неделя без единого суждения в
    теневой фазе — то, о чём владельцу стоит знать.
    """
    project_id = await _project(db, "corridor-silence")

    task_ids = [
        await _pair(db, project_id, steward="escalate", human=None)
        for _ in range(CORRIDOR_MIN_FOR_FLOOR)
    ]
    assert await check_escalation_corridor(db) == REASON_OVER_ESCALATING
    assert len(await _alerts(db)) == 1

    # Все суждения выпадают из недельного окна — судья замолчал.
    await db.execute(
        "UPDATE steward_judgements SET created_at = datetime('now', '-30 days')"
    )
    await db.commit()
    assert await check_escalation_corridor(db) is None
    silent = await _alerts(db)
    assert len(silent) == 2, "молчание после нарушения — смена состояния"
    assert json.loads(silent[-1]["payload"])["state"] == CORRIDOR_NO_SAMPLE
    assert json.loads(silent[-1]["payload"])["share"] is None, (
        "у «нет данных» нет доли: ноль здесь был бы обвинением в штамповке"
    )

    # Судья возвращается и снова эскалирует всё подряд.
    assert task_ids
    for _ in range(CORRIDOR_MIN_FOR_FLOOR):
        await _pair(db, project_id, steward="escalate", human=None)
    assert await check_escalation_corridor(db) == REASON_OVER_ESCALATING
    assert len(await _alerts(db)) == 3, "после тишины нарушение обязано прозвучать"


async def test_a_short_week_is_not_measured(db: aiosqlite.Connection):
    """Отчёт 203: первое суждение недели не имеет права поднимать алерт.

    При одном суждении доля 0% или 100% — обе вне коридора. Проверяется по
    краю ПОЛА: на единицу меньше минимума молчит, ровно минимум измеряет —
    и 0 из 21 это уже штамповка, потому что при 21 одна эскалация была бы
    строго ниже 5%, а её нет.
    """
    project_id = await _project(db, "corridor-short")

    await _pair(db, project_id, steward="approve", human=None)
    assert await check_escalation_corridor(db) is None
    assert await _alerts(db) == [], "одно суждение — не штамповка, а одно суждение"

    for _ in range(CORRIDOR_MIN_FOR_FLOOR - 2):
        await _pair(db, project_id, steward="approve", human=None)
    assert await check_escalation_corridor(db) is None
    assert await _alerts(db) == [], "на единицу меньше минимума пола — не измерено"

    await _pair(db, project_id, steward="approve", human=None)
    assert await check_escalation_corridor(db) == REASON_STAMPING, (
        "ровно минимум пола — доля измерима, и 0 из 21 это уже штамповка"
    )


async def test_the_ceiling_is_not_silenced_by_the_floor_minimum(
    db: aiosqlite.Connection,
):
    """Отчёт 214: минимум, выведенный из пола, глушил потолок.

    Судья, эскалировавший пятнадцать из пятнадцати, читался как «не
    измерен», хотя это «бесполезен» — у потолка свой минимум, и он три:
    с трёх суждений одна НЕ-эскалация уже строго выше 50%. Проверяется по
    краю: два — молчит, три — алерт.
    """
    project_id = await _project(db, "corridor-ceiling")

    for _ in range(CORRIDOR_MIN_FOR_CEILING - 1):
        await _pair(db, project_id, steward="escalate", human=None)
    assert await check_escalation_corridor(db) is None
    assert await _alerts(db) == [], "две эскалации из двух — ещё не измерено"

    await _pair(db, project_id, steward="escalate", human=None)
    assert await check_escalation_corridor(db) == REASON_OVER_ESCALATING, (
        "три из трёх — потолок значим, и это «бесполезен», а не «не измерено»"
    )
    assert CORRIDOR_MIN_FOR_CEILING < CORRIDOR_MIN_FOR_FLOOR, (
        "потолок значим раньше пола — иначе пол снова глушил бы его"
    )


def test_minimums_derive_from_their_own_boundaries():
    """Каждый минимум выведен из СВОЕЙ границы, и оба строгие.

    Первый заход брал ceil(1/f) = 20, но при 20 одна эскалация даёт ровно
    5% — на полу, а не ниже, и «ниже» всё ещё означало «ноль». Наименьшее
    целое строго больше 1/x — это floor(1/x) + 1, и это проверяется здесь
    не числом, а свойством: одна эскалация обязана быть СТРОГО ниже пола,
    одна не-эскалация — СТРОГО выше потолка.
    """
    import math

    assert CORRIDOR_MIN_FOR_FLOOR == math.floor(1 / ACT_ESCALATION_FLOOR) + 1
    assert 1 / CORRIDOR_MIN_FOR_FLOOR < ACT_ESCALATION_FLOOR
    assert 1 / (CORRIDOR_MIN_FOR_FLOOR - 1) >= ACT_ESCALATION_FLOOR, (
        "на единицу меньше — и одна эскалация уже не ниже пола: минимум точный"
    )

    assert CORRIDOR_MIN_FOR_CEILING == math.floor(1 / (1 - ACT_ESCALATION_CEILING)) + 1
    n = CORRIDOR_MIN_FOR_CEILING
    assert (n - 1) / n > ACT_ESCALATION_CEILING
    assert (n - 2) / (n - 1) <= ACT_ESCALATION_CEILING, (
        "на единицу меньше — и одна не-эскалация уже не выше потолка"
    )


async def test_stamping_text_counts_the_escalations_it_saw(db: aiosqlite.Connection):
    """Отчёт 214: «не пробовал не согласиться» — неправда, если эскалация была.

    1 из 21 — штамповка по доле, но судья не согласился один раз, и текст
    обязан это признать. 0 из 21 — «ни разу», и это уже правда.
    """
    project_id = await _project(db, "corridor-text-once")
    await _pair(db, project_id, steward="escalate", human=None)
    for _ in range(CORRIDOR_MIN_FOR_FLOOR - 1):
        await _pair(db, project_id, steward="approve", human=None)
    assert await check_escalation_corridor(db) == REASON_STAMPING
    once = json.loads((await _alerts(db))[-1]["payload"])["detail"]
    assert "ни разу" not in once, "эскалация была — «ни разу» неправда"
    assert "редкость" in once

    project_never = await _project(db, "corridor-text-never")
    # Сдвигаем прежние суждения из окна, чтобы считались только новые, и
    # даём коридору ЗАМЕТИТЬ пустую неделю: событие пишется на смену
    # состояния, и штамповка после штамповки без промежуточного «пусто»
    # новой записи не дала бы — тест читал бы старый текст.
    await db.execute(
        "UPDATE steward_judgements SET created_at = datetime('now', '-30 days')"
    )
    await db.commit()
    assert await check_escalation_corridor(db) is None
    for _ in range(CORRIDOR_MIN_FOR_FLOOR):
        await _pair(db, project_never, steward="approve", human=None)
    assert await check_escalation_corridor(db) == REASON_STAMPING
    never = json.loads((await _alerts(db))[-1]["payload"])["detail"]
    assert "ни разу" in never


async def test_unmeasured_state_carries_no_share(db: aiosqlite.Connection):
    """Отчёт 214: no_sample с долей в payload — пустота, выданная за число.

    Состояние «не измерено» пишется в фид при переходе в него, и доля рядом
    читалась бы как результат. Счётчики остаются — они факты; доли нет —
    она вывод, которого не делали (#762).
    """
    project_id = await _project(db, "corridor-payload")
    ids = [
        await _pair(db, project_id, steward="escalate", human=None)
        for _ in range(CORRIDOR_MIN_FOR_FLOOR)
    ]
    assert await check_escalation_corridor(db) == REASON_OVER_ESCALATING

    # Окно усыхает до двух суждений: потолок больше не значим.
    await db.execute(
        "UPDATE steward_judgements SET created_at = datetime('now', '-30 days') "
        "WHERE task_id IN (%s)" % ",".join("?" * (len(ids) - 2)),
        tuple(ids[:-2]),
    )
    await db.commit()
    assert await check_escalation_corridor(db) is None
    payload = json.loads((await _alerts(db))[-1]["payload"])
    assert payload["state"] == CORRIDOR_NO_SAMPLE
    assert payload["judged"] == 2 and payload["escalated"] == 2, "счётчики — факты"
    assert payload["share"] is None, "доля в no_sample — вывод, которого не делали"


async def test_alert_text_does_not_round_across_the_boundary(
    db: aiosqlite.Connection,
):
    """Отчёт 203: «5% ниже 5%» — противоречие, а не факт.

    1 из 21 это 4.76%; округление до целого печатало 5% и утверждало, что
    5% ниже 5%. Человеку, за которым решение (F7), уходила полуправда,
    которая читается как «проверено». Текст обязан нести счётчик и долю с
    десятыми — то, из чего человек сам увидит и границу, и расстояние до
    неё.
    """
    project_id = await _project(db, "corridor-rounding")
    await _pair(db, project_id, steward="escalate", human=None)
    for _ in range(20):
        await _pair(db, project_id, steward="approve", human=None)

    assert await check_escalation_corridor(db) == REASON_STAMPING
    detail = json.loads((await _alerts(db))[-1]["payload"])["detail"]

    assert "1 из 21" in detail, "счётчик — то, что не округляется"
    assert "4.8%" in detail, "доля с десятыми, а не с округлением к границе"
    assert "5% — ниже 5%" not in detail and "5% ниже 5%" not in detail


# ---------------------------------------------------------------------------
# #1234: неразрешённая находка — это незаданный вопрос, а не чистота.
#
# ЗАМЕР. Ранее по пяти отчётам: тринадцать ПОДТВЕРЖДЁННЫХ находок — ни одной
# настоящей, шесть НЕРАЗРЕШЁННЫХ — настоящими все шесть. 09.09.2026 замер
# повторён на новых данных: из семи неразрешённых настоящими шесть. При этом
# отчёт с нулём подтверждённых и непустым unresolved читался всеми
# поверхностями как «находок нет».
#
# ГРАНИЦА, КОТОРУЮ ЭТИ ТЕСТЫ ДЕРЖАТ. Неразрешённая находка не приравнивается
# к подтверждённой: подтверждённой становится только та, под которой УПАЛ
# ТЕСТ. Здесь проверяется видимость и счёт, а не объявление разногласия
# дефектом.
# ---------------------------------------------------------------------------

from hub.services.steward_corridor import (  # noqa: E402
    CLEAN_OUTCOMES,
    OUTCOME_CLEAN,
    OUTCOME_CONFIRMED,
    OUTCOME_INCOMPLETE,
    OUTCOME_NO_DATA,
    OUTCOME_UNRESOLVED,
    REPORT_OUTCOMES,
    STEP_CONFIRMED,
    STEP_NO_MUTATION,
    STEP_UNCOVERED,
    SuiteResult,
    derive_mutation,
    mechanical_step,
    names_clean,
    outcome_label,
    report_outcome,
)

#: Ровно та подпись, которую поверхность печатает, объявляя отчёт чистым.
#: Берётся из источника, а не переписывается словами: искать слово «чисто»
#: вообще нельзя — подписи ступеней unresolved и no_data сами его цитируют,
#: чтобы ОПРОВЕРГНУТЬ, и совпадение по такому поиску ничего бы не значило.
CLEAN_WORDS = outcome_label(OUTCOME_CLEAN)


async def _report_with_unresolved(client, task_id: int) -> dict:
    """Ровно тот отчёт, из-за которого заведена задача: 0 подтверждённых при
    непустом unresolved. Возвращает СОХРАНЁННУЮ запись, как её отдаёт API."""
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "agent_count": 3,
            "tokens_spent": 12000,
            "model": "grok-4.6",
            "raw_count": 4,
            "findings_confirmed": [],
            "findings_rejected": [
                {"title": "style nit", "category": "style", "reason": "не дефект"}
            ],
            "incomplete": False,
            "unresolved": [
                {
                    "title": "путь записи полей из карточки может выдать автоодобрение",
                    "why": "адъюдикаторы не сошлись: hub/web.py:1871 читает отчёт",
                }
            ],
            "lost_dimensions": [],
            "agent": "cursor-cloud-reviewer",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_unresolved_findings_are_never_shown_as_clean(client, db):
    """AC-1: три поверхности, одно правило, и ни одна не говорит «чисто».

    Карточка, дайджест и квитанция ревью раньше выводили чистоту каждая
    сама — по длине ``findings_confirmed``, — и все три выводили одинаково и
    неверно. Теперь право назвать отчёт чистым принадлежит ступени лестницы,
    и мутация, снимающая проверку ``unresolved`` в :func:`report_outcome`,
    роняет этот тест: ступень станет ``clean``, и все три поверхности
    послушно напечатают чистоту.
    """
    from tests.test_web import _web_task_in_review_with_test_ac

    task_id = await _web_task_in_review_with_test_ac(client, db)
    stored = await _report_with_unresolved(client, task_id)

    # Сама ступень: unresolved — свой исход, а не отсутствие находок.
    assert stored["outcome"] == OUTCOME_UNRESOLVED
    assert stored["outcome_names_clean"] is False
    # ...и он НЕ приравнен к подтверждённому: автор не отвечает за то, в чём
    # ревьюер сам не сошёлся.
    assert stored["outcome"] != OUTCOME_CONFIRMED
    assert stored["findings_confirmed"] == []

    # ПОВЕРХНОСТЬ 1 — карточка.
    page = (await client.get(f"/tasks/{task_id}")).text
    strip = page[page.index("task-review-strip") : page.index("task-evidence")]
    tile_at = strip.index("Машинное ревью")
    # Якорь — ПРОБЕЛ после имени класса: внутренний ``task-review-tile-label``
    # тоже начинается с «task-review-tile», и поиск без пробела находит его,
    # а не саму плитку. Проверяется открывающий тег: цвет живёт в нём.
    tile_open = strip.rfind('<div class="task-review-tile ', 0, tile_at)
    tile = strip[tile_open : strip.index(">", tile_open)]
    # Не просто «не зелёная»: непустой unresolved — САМОСТОЯТЕЛЬНЫЙ исход, а
    # значит громкий. Пока плитка красилась по длине findings_confirmed, эти
    # отчёты попадали в жёлтое вместе с «полнота не заявлена» — и жёлтое от
    # «есть находки, которых никто не рассудил» неотличимо.
    assert "task-review-tile--bad" in tile, (
        "0 подтверждённых при непустом unresolved — не зелёная и не жёлтая плитка"
    )
    assert "task-review-tile--ok" not in tile
    assert outcome_label(OUTCOME_UNRESOLVED) in strip, (
        "плитка обязана назвать ступень словами, а не только числом"
    )
    assert CLEAN_WORDS not in strip

    # ПОВЕРХНОСТЬ 2 — отчёт в карточке, та строка, которую цитируют в гейт.
    note = page[page.index("Machine review:") :][:1200]
    assert outcome_label(OUTCOME_UNRESOLVED) in note
    assert CLEAN_WORDS not in note

    # ПОВЕРХНОСТЬ 3 — дайджест: под автовердиктом видно, ЧТО стояло в отчёте.
    from hub import repository as repo_module
    from hub.services.digest import _report_outcome_of

    row = await repo_module.get_latest_machine_review(db, task_id)
    digest_view = _report_outcome_of(row)
    assert digest_view["outcome"] == OUTCOME_UNRESOLVED
    assert digest_view["names_clean"] is False
    assert CLEAN_WORDS not in digest_view["label"]

    # ПОВЕРХНОСТЬ 4 — квитанция MCP: агент цитирует её в задачу дословно.
    from unittest.mock import AsyncMock, patch

    from hub.mcp_server import hub_submit_machine_review

    with patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post:
        post.return_value = stored
        receipt = await hub_submit_machine_review(
            task_id, raw_count=4, incomplete=False
        )
    text = receipt.content[0].text
    assert "0 confirmed" in text, "счёт остаётся — меняется то, чем он назван"
    assert outcome_label(OUTCOME_UNRESOLVED) in text
    assert CLEAN_WORDS not in text


async def test_the_outcome_ladder_lets_exactly_one_step_say_clean(client, db):
    """Полнота лестницы — перечислением, а не внимательностью читателя.

    Ступень, добавленная без подписи, печаталась бы пустым местом рядом с
    числом находок, а пустое место читается как «всё в порядке» — ровно та
    подмена, против которой заведена задача.
    """
    assert set(CLEAN_OUTCOMES) == {OUTCOME_CLEAN}
    for outcome in REPORT_OUTCOMES:
        label = outcome_label(outcome)
        assert label and "неизвестный исход" not in label
        assert (label == CLEAN_WORDS) == (outcome == OUTCOME_CLEAN)
        assert names_clean(outcome) == (outcome == OUTCOME_CLEAN)

    # Порядок ступеней: первым называется то, что дороже всего пропустить.
    assert (
        report_outcome(confirmed=[], unresolved=[{"title": "x"}], incomplete=True)
        == OUTCOME_INCOMPLETE
    )
    assert (
        report_outcome(confirmed=[{"title": "c"}], unresolved=[{"title": "u"}])
        == OUTCOME_CONFIRMED
    )
    assert report_outcome(confirmed=[], unresolved=[], raw_count=0) == OUTCOME_NO_DATA
    assert report_outcome(confirmed=[], unresolved=[], raw_count=3) == OUTCOME_CLEAN


def test_a_failing_mutation_turns_an_unresolved_finding_into_a_confirmed_one():
    """AC-2: подтверждение — доказательством, и человека не зовут.

    ЕДИНСТВЕННЫЙ путь из unresolved в confirmed. Голосование адъюдикаторов
    им не является ни при каком числе голосов: наблюдение называет ИМЯ
    упавшего теста, которое человек может перезапустить сам.
    """
    finding = {
        "title": "проверка unresolved снимается без падения — hub/models.py:2599",
        "why": "адъюдикаторы разошлись",
    }
    runs: list[str] = []

    def run_suite(mutation):
        runs.append(mutation.name)
        return SuiteResult(
            failed=("tests/test_steward_corridor.py::test_x",), passed=3474
        )

    result = mechanical_step(finding, run_suite, tree_is_clean=lambda: True)

    assert result.outcome == STEP_CONFIRMED
    assert result.confirms is True
    assert result.needs_human is False, "у находки есть ответ — звать некого"
    assert "tests/test_steward_corridor.py::test_x" in result.observation
    assert result.mutation is not None
    assert result.mutation.file == "hub/models.py"
    assert result.mutation.start_line == 2599
    assert runs == ["hub/models.py:2599"], "один прогон на находку — шаг дешёвый"
    assert result.tree_clean is True


def test_a_surviving_mutation_is_an_answer_not_silence():
    """AC-3: зелёный набор — тоже ответ, и это НЕ «находка ложная».

    Образец 09.09.2026: находка про автоодобрение опровергалась чтением и
    подтвердилась опытом — мутация пережила 3475 тестов. Поведение может
    быть верным, а покрытия под ним нет, и число прошедших — то, что человек
    увидит вместо тишины.
    """
    finding = {
        "title": "путь записи полей из карточки, hub/web.py строки 1871-1904",
        "why": "адъюдикаторы разошлись",
    }

    result = mechanical_step(
        finding,
        lambda mutation: SuiteResult(failed=(), passed=3475),
        tree_is_clean=lambda: True,
    )

    assert result.outcome == STEP_UNCOVERED
    assert result.outcome != STEP_CONFIRMED, "зелёный набор не подтверждает находку"
    assert result.confirms is False
    assert result.needs_human is False, "«непокрытый случай» — ответ, а не тишина"
    assert "3475" in result.observation
    assert result.mutation is not None
    assert result.mutation.name == "hub/web.py:1871-1904"


def test_a_finding_without_a_mutation_goes_to_a_human_with_context():
    """AC-4: честный отказ, и человек не начинает с нуля.

    Неверно выведенная мутация даёт ложный ответ ОБОИХ видов, поэтому
    догадка здесь дороже отказа. Отказ уносит с собой формулировку находки и
    список того, что уже проверено.
    """
    finding = {
        "title": "гейт может одобрить не глядя",
        "why": "адъюдикаторы разошлись, места никто не назвал",
    }
    calls: list[str] = []

    result = mechanical_step(
        finding,
        lambda mutation: calls.append(mutation.name) or SuiteResult(),
        already_checked=("диспозиции сверены", "sha сдачи совпадает"),
    )

    assert derive_mutation(finding) is None
    assert calls == [], "набор не гоняют, когда мутировать нечего"
    assert result.outcome == STEP_NO_MUTATION
    assert result.needs_human is True
    assert result.confirms is False, "отказ шага не подтверждает находку"
    assert "гейт может одобрить не глядя" in result.observation
    assert result.checked == ("диспозиции сверены", "sha сдачи совпадает")
