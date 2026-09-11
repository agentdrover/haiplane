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


async def test_the_digest_reads_the_report_of_the_verdicts_own_generation(db):
    """Ступень берётся у отчёта ТОГО поколения, а не у самого свежего.

    Дайджест собирается ночью, а вердикт выносится днём. Между ними ложится
    ещё один отчёт — добор лестницы (#879) кладёт второй в то же поколение,
    пересдача кладёт первый в следующее, — и «самый свежий отчёт задачи»
    перестаёт быть тем, под которым вердикт вынесен. Здесь именно этот
    случай: под автовердиктом стоял отчёт с неразрешёнными находками, а
    после него лёг чистый. Пока отчёт брался по свежести, дайджест показывал
    на месте неразрешённых «находок нет» — то самое, против чего вся задача.
    """
    import json as _json

    from hub import repository as repo_module
    from hub.services.digest import generate_due_digests
    from tests.test_autopilot_digest import _autopilot_project, _node, _tomorrow

    _pid, feature = await _autopilot_project(db, "spike-1234-gen")
    task_id = await _node(db, title="ступень", task_type="task", parent_id=feature)
    await repo_module.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        model="grok-4.6",
        raw_count=4,
        findings_confirmed="[]",
        unresolved=_json.dumps([{"title": "никто не рассудил"}], ensure_ascii=False),
    )
    await repo_module.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="policy",
        payload={"verdict": "approved", "submission_generation": 1},
    )
    # Пересдача: отчёт о ДРУГОМ диффе ложится ПОСЛЕ вердикта и раньше дайджеста.
    await repo_module.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=2,
        model="sonnet-4.5",
        raw_count=3,
        findings_confirmed="[]",
        unresolved="[]",
    )
    await db.commit()

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    payload = _json.loads((await repo_module.list_digests(db))[0]["payload"])
    entry = payload["auto_verdicts"][0]

    assert entry["machine_review"]["outcome"] == OUTCOME_UNRESOLVED, (
        "под вердиктом стоял отчёт с неразрешёнными — его ступень и показывают"
    )
    assert entry["machine_review"]["names_clean"] is False
    assert CLEAN_WORDS not in entry["machine_review"]["label"]
    # Ревьюер — тоже свой: пара «кто писал / кто ревьюил» сравнивается
    # правилом монокультуры (#758), и чужой ревьюер в ней ничего не значит.
    assert entry["models"]["reviewer"] == "grok-4.6"


async def test_a_verdict_without_a_report_of_its_generation_says_so(db):
    """Отчёта своего поколения нет — дайджест говорит «отчёта нет».

    Подстановка по свежести здесь была бы тем же дефектом, только тише:
    чужой отчёт на месте своего читается как свой, а пустота видна.
    """
    import json as _json

    from hub import repository as repo_module
    from hub.services.digest import generate_due_digests
    from tests.test_autopilot_digest import _autopilot_project, _node, _tomorrow

    _pid, feature = await _autopilot_project(db, "spike-1234-absent")
    task_id = await _node(db, title="без отчёта", task_type="task", parent_id=feature)
    await repo_module.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="policy",
        payload={"verdict": "approved", "submission_generation": 2},
    )
    await repo_module.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        model="grok-4.6",
        raw_count=3,
        findings_confirmed="[]",
        unresolved="[]",
    )
    await db.commit()

    # И вердикт, у которого поколения в событии нет вовсе (запись старше
    # поля): подстановка «самого свежего» здесь была бы тем же дефектом, что
    # и в тесте выше, только тише — чужой отчёт на месте своего.
    await repo_module.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="policy",
        payload={"verdict": "approved"},
    )
    await db.commit()

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    payload = _json.loads((await repo_module.list_digests(db))[0]["payload"])
    assert len(payload["auto_verdicts"]) == 2
    for entry in payload["auto_verdicts"]:
        assert entry["machine_review"]["state"] == "absent"
        assert entry["models"]["reviewer"] == ""


# ---------------------------------------------------------------------------
# БОЕВОЙ ПУТЬ ШАГА.
#
# В первой сдаче #1234 шаг был написан, покрыт тремя тестами приёмки — и не
# вызывался ниоткуда, кроме них. Это поймано не чтением: анализатор вызовов
# самого хаба (hub/services/call_sites.py) на диффе сдачи ответил
# ``only_tests -> mechanical_step``, а полный набор с исключением, вставленным
# первой строкой шага, дал РОВНО три падения — те самые три теста из 3487.
# Мутационная серия такого не ловит и поймать не может: и она, и тесты зовут
# функцию напрямую.
#
# Поэтому тесты ниже ведут не шаг, а ПРИЁМ ОТЧЁТА — ту дверь, которой отчёты
# приезжают на самом деле.
# ---------------------------------------------------------------------------

from hub.services.mechanical_pass import (  # noqa: E402
    MECHANICAL_STEP_RECORDED,
    parse_suite_output,
    run_mechanical_pass,
)
from hub.services.steward_corridor import STEP_NO_RUN  # noqa: E402


async def _step_events(db, task_id: int) -> list[dict]:
    import json as _json

    from hub import repository as repo_module

    rows = await repo_module.list_events(
        db, since=0, kinds=[MECHANICAL_STEP_RECORDED], limit=100
    )
    return [
        _json.loads(dict(r)["payload"] or "{}")
        for r in rows
        if dict(r)["task_id"] == task_id
    ]


async def test_the_mechanical_step_runs_when_a_real_report_arrives(client, db):
    """Шаг делается на ПРИЁМЕ отчёта, а не только в тестах шага.

    Мутация «снять вызов механического прохода из record_machine_review»
    роняет именно этот тест: отчёт приедет, задача останется ждать человека,
    и ни у одной неразрешённой находки не будет ни исхода, ни имени — ровно
    то состояние, из-за которого 09.09.2026 три задачи разбирали руками.
    """
    from tests.test_web import _web_task_in_review_with_test_ac

    task_id = await _web_task_in_review_with_test_ac(client, db)
    await _report_with_unresolved(client, task_id)

    recorded = await _step_events(db, task_id)
    assert len(recorded) == 1, "исход пишется на КАЖДУЮ неразрешённую находку"
    step = recorded[0]
    # Живого прогона в умолчаниях нет — и это НЕ «набор зелёный».
    assert step["outcome"] == STEP_NO_RUN
    assert step["confirms"] is False
    assert step["needs_human"] is True
    # Человек не начинает с нуля: мутация НАЗВАНА местом, а не пересказом.
    assert step["mutation"] == "hub/web.py:1871"

    # И то же самое — словами, в ленте задачи, которую человек читает.
    from hub import repository as repo_module

    updates = [dict(u) for u in await repo_module.get_task_updates(db, task_id)]
    said = [u for u in updates if "Механический шаг" in (u["content"] or "")]
    assert said, "проход обязан сказать человеку, что он проверил"
    assert "hub/web.py:1871" in said[-1]["content"]
    assert CLEAN_WORDS not in said[-1]["content"]


async def test_a_report_without_unresolved_findings_is_not_touched(client, db):
    """Отчёт без неразрешённых находок шагу не предмет — и не платит за него.

    Без этого правила проход писал бы событие и сводку на КАЖДЫЙ отчёт, и
    «шаг сделан» перестало бы что-либо значить.
    """
    from tests.test_web import _web_task_in_review_with_test_ac

    task_id = await _web_task_in_review_with_test_ac(client, db)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "raw_count": 3,
            "findings_confirmed": [],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "agent": "cursor-cloud-reviewer",
        },
    )
    assert resp.status_code == 200, resp.text
    assert await _step_events(db, task_id) == []

    # И ни слова в ленте: «механический шаг сделан» на отчёте, которому он не
    # предмет, — это строка, после которой «шаг сделан» перестаёт что-либо
    # значить. Проверяется отдельно от событий: проход без находок пишет ноль
    # событий и в мутированном виде тоже, а сводку — уже нет.
    from hub import repository as repo_module

    updates = [dict(u) for u in await repo_module.get_task_updates(db, task_id)]
    assert not [u for u in updates if "Механический шаг" in (u["content"] or "")]


async def test_a_failing_suite_confirms_the_finding_on_the_production_path(
    client, db, monkeypatch
):
    """AC-2 на боевом пути: упавший тест подтверждает находку без человека.

    Тот же исход, что и в тесте шага, но добытый ЧЕРЕЗ ПРИЁМ ОТЧЁТА: пока
    прогон был инъекцией только в тесте, подтверждение не могло случиться ни
    с одной настоящей находкой.
    """
    from tests.test_web import _web_task_in_review_with_test_ac
    from hub.services import mechanical_pass as mp

    async def _probe(mutation):
        return SuiteResult(failed=("tests/test_web.py::test_card",), passed=3486)

    async def _configured(db_, task_id_):
        return _probe

    monkeypatch.setattr(mp, "configured_probe", _configured)

    task_id = await _web_task_in_review_with_test_ac(client, db)
    await _report_with_unresolved(client, task_id)

    step = (await _step_events(db, task_id))[0]
    assert step["outcome"] == STEP_CONFIRMED
    assert step["confirms"] is True
    assert step["needs_human"] is False
    assert "tests/test_web.py::test_card" in step["observation"]


async def test_the_pass_is_not_repeated_for_one_submission(client, db):
    """Второй отчёт по той же сдаче не гоняет набор заново.

    Лестница добора (#879) кладёт в одно поколение два отчёта. Проход,
    повторённый на каждом, стоил бы вторых минут прогона и написал бы
    человеку то же самое дважды.
    """
    from tests.test_web import _web_task_in_review_with_test_ac

    task_id = await _web_task_in_review_with_test_ac(client, db)
    await _report_with_unresolved(client, task_id)
    await _report_with_unresolved(client, task_id)

    assert len(await _step_events(db, task_id)) == 1


async def test_a_run_that_named_no_failed_test_is_not_an_answer(db):
    """Ненулевой код возврата без единого названного теста — НЕ наблюдение.

    Ошибка сбора набора и упавший тест дают один и тот же ненулевой код.
    Подтвердить находку ошибкой сбора значило бы подтвердить её тем, что
    мутация синтаксически задела файл, а не тем, что поведение изменилось.
    """
    assert parse_suite_output(2, "ERRORS\ninternal error during collection") is None
    assert parse_suite_output(0, "3475 passed, 1 skipped in 265s") == SuiteResult(
        failed=(), passed=3475
    )
    killed = parse_suite_output(
        1, "FAILED tests/test_x.py::test_y - AssertionError\n1 failed, 3474 passed"
    )
    assert killed is not None and killed.failed == ("tests/test_x.py::test_y",)


async def test_a_probe_that_could_not_run_is_not_a_green_suite(client, db, monkeypatch):
    """Зонд, не сумевший прогнать набор, не выдаёт себя за переживший.

    Мутация «считать отказ прогона зелёным набором» роняет именно этот тест:
    находка получила бы исход «непокрытый случай» — то есть ОТВЕТ — там, где
    никто ничего не наблюдал.
    """
    from tests.test_web import _web_task_in_review_with_test_ac
    from hub.services import mechanical_pass as mp

    async def _configured(db_, task_id_):
        async def _probe(mutation):
            return None

        return _probe

    monkeypatch.setattr(mp, "configured_probe", _configured)

    task_id = await _web_task_in_review_with_test_ac(client, db)
    await _report_with_unresolved(client, task_id)

    step = (await _step_events(db, task_id))[0]
    assert step["outcome"] == STEP_NO_RUN
    assert step["outcome"] != STEP_UNCOVERED
    assert step["needs_human"] is True


async def test_the_pass_runs_the_step_on_every_finding_but_bounds_the_runs(db, client):
    """Исход получают ВСЕ находки, прогон — только первые: шаг дешёвый.

    Отчёт с шестью неразрешёнными находками (#1158 такой был) при прогоне на
    каждую стоил бы часа. Находки сверх бюджета едут человеку с названной
    мутацией — это хуже наблюдения и лучше тишины.
    """
    from tests.test_web import _web_task_in_review_with_test_ac
    from hub.services import mechanical_pass as mp

    import json as _json

    from hub import repository as repo_module

    task_id = await _web_task_in_review_with_test_ac(client, db)
    row = dict(await repo_module.get_task(db, task_id))
    # Отчёт кладётся прямо в хранилище, а не через приём: приём сам делает
    # проход, и тогда этот тест мерил бы идемпотентность вместо бюджета.
    await repo_module.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=int(row["submission_generation"]),
        raw_count=9,
        findings_confirmed="[]",
        unresolved=_json.dumps(
            [
                {"title": f"находка {n} — hub/app.py:{100 + n}", "why": "не сошлись"}
                for n in range(mp.MAX_RUNS_PER_REPORT + 2)
            ],
            ensure_ascii=False,
        ),
    )
    await db.commit()

    ran: list[str] = []

    async def _probe(mutation):
        ran.append(mutation.name)
        return SuiteResult(failed=(), passed=10)

    result = await run_mechanical_pass(db, task_id, probe=_probe)
    assert len(result.outcomes) == mp.MAX_RUNS_PER_REPORT + 2, (
        "исход получает каждая находка"
    )
    assert len(ran) == mp.MAX_RUNS_PER_REPORT, "прогонов не больше бюджета"
    assert result.answered == mp.MAX_RUNS_PER_REPORT
    assert [o["outcome"] for o in result.outcomes[mp.MAX_RUNS_PER_REPORT :]] == [
        STEP_NO_RUN,
        STEP_NO_RUN,
    ]


async def test_the_live_probe_kills_a_mutation_in_a_sandbox_and_leaves_no_trace(
    tmp_path, monkeypatch
):
    """Живой прогон — НАСТОЯЩИЙ: мутация, pytest, имя упавшего теста, уборка.

    Всё остальное в этом файле проверяет решения над инъектированным
    прогоном, и правильно: исход обязан проверяться без минут ожидания. Но
    инъекция ничего не говорит о том, умеет ли хаб применить мутацию и
    прогнать набор — а первая сдача #1234 показала, чего стоит механизм, в
    который никто ни разу не сходил по-настоящему. Здесь заводится крошечный
    репозиторий с одним тестом, и прогон идёт как в бою.

    Дерево проекта при этом не трогают вовсе: мутация живёт в ОДНОРАЗОВОМ
    рабочем дереве на коммите сдачи. Требование «дерево после шага чистое»
    выполняется не аккуратностью отката, а тем, что откатывать нечего.
    """
    import subprocess
    import sys

    from hub import config as config_module
    from hub.services.mechanical_pass import live_probe
    from hub.services.steward_corridor import Mutation

    project = tmp_path / "project"
    project.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(project), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "probe@example.com")
    git("config", "user.name", "probe")
    (project / "calc.py").write_text(
        "def double(n):\n    result = n * 2\n    return result\n", encoding="utf-8"
    )
    (project / "test_calc.py").write_text(
        "from calc import double\n\n\ndef test_double():\n    assert double(2) == 4\n",
        encoding="utf-8",
    )
    git("add", "-A")
    git("commit", "-m", "base")
    sha = git("rev-parse", "HEAD")

    monkeypatch.setattr(
        config_module,
        "MUTATION_PROBE_CMD",
        f"{sys.executable} -m pytest -q -rf -p no:randomly",
    )
    monkeypatch.setattr(config_module, "MUTATION_PROBE_SCRATCH_DIR", str(tmp_path))

    suite = await live_probe(
        Mutation(
            file="calc.py",
            start_line=2,
            end_line=2,
            source_field="title",
            quote="calc.py:2",
        ),
        repo_path=str(project),
        sha=sha,
    )

    assert suite is not None, "прогон обязан состояться — команда набора задана"
    assert suite.failed == ("test_calc.py::test_double",), (
        "наблюдение называет ИМЯ упавшего теста, а не их число"
    )
    assert suite.green is False

    # Уборка: ни рабочего дерева, ни правки в самой копии проекта.
    assert git("status", "--porcelain") == ""
    assert "haiplane-mutation-" not in git("worktree", "list")
    assert not list(tmp_path.glob("haiplane-mutation-*"))
    assert (project / "calc.py").read_text(encoding="utf-8").startswith("def double")


async def test_the_live_probe_refuses_a_mutation_that_breaks_the_parse(
    tmp_path, monkeypatch
):
    """Мутация, ломающая РАЗБОР файла, — отказ прогона, а не подтверждение.

    Закомментированное тело функции роняет не тест, а сбор набора, и «упало
    всё» выдало бы себя за доказательство находки. Отказ здесь дешевле
    ложного подтверждения: неверно выведенная мутация даёт ложный ответ
    обоих видов.
    """
    import subprocess
    import sys

    from hub import config as config_module
    from hub.services.mechanical_pass import live_probe
    from hub.services.steward_corridor import Mutation

    project = tmp_path / "broken"
    project.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(project), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "probe@example.com")
    git("config", "user.name", "probe")
    (project / "calc.py").write_text("def double(n):\n    return n * 2\n", "utf-8")
    # Тест в песочнице обязателен: без него прогон «ничего не собрал» и был бы
    # отказом в любом случае — включая тот, где проверку разбора сняли. Тест,
    # проходящий и без правила, ничего о правиле не говорит.
    (project / "test_calc.py").write_text(
        "from calc import double\n\n\ndef test_double():\n    assert double(2) == 4\n",
        "utf-8",
    )
    git("add", "-A")
    git("commit", "-m", "base")
    sha = git("rev-parse", "HEAD")

    monkeypatch.setattr(
        config_module, "MUTATION_PROBE_CMD", f"{sys.executable} -m pytest -q"
    )
    monkeypatch.setattr(config_module, "MUTATION_PROBE_SCRATCH_DIR", str(tmp_path))

    assert (
        await live_probe(
            Mutation(
                file="calc.py",
                start_line=2,
                end_line=2,
                source_field="title",
                quote="calc.py:2",
            ),
            repo_path=str(project),
            sha=sha,
        )
        is None
    )
    assert git("status", "--porcelain") == ""


async def test_no_configured_suite_command_means_no_probe_at_all(
    client, db, monkeypatch, tmp_path
):
    """Пустая команда набора — «живого прогона нет», а не «запускай как есть».

    То же правило, которым выключен локальный ревьюер (#1180): набор проекта
    — чужой код, и запуск его на хосте, где лежат ключи, не бывает
    умолчанием. Мутация «считать пустую команду разрешением» роняет именно
    этот тест: зонд вернулся бы вместо честного отказа.
    """
    from hub import config as config_module
    from hub.services import mechanical_pass as mp
    from tests.test_web import _web_task_in_review_with_test_ac

    from hub import repository as repo_module
    from hub.services import orchestration

    task_id = await _web_task_in_review_with_test_ac(client, db)

    # Копия проекта и коммит сдачи ЕСТЬ: иначе зонда не было бы и без правила,
    # и тест мерил бы отсутствие рабочей копии вместо отсутствия команды.
    async def _ctx(db_, task_id_):
        return {"repo": str(tmp_path)}

    monkeypatch.setattr(orchestration, "project_git_context", _ctx)
    await repo_module.update_task(db, task_id, submission_sha="0" * 40)
    await db.commit()

    monkeypatch.setattr(config_module, "MUTATION_PROBE_CMD", "")
    assert await mp.configured_probe(db, task_id) is None, (
        "пустая команда набора — «живого прогона нет», а не «запускай как есть»"
    )

    # С командой зонд появляется — значит отказ выше дал именно её отсутствие.
    monkeypatch.setattr(config_module, "MUTATION_PROBE_CMD", "pytest -q")
    assert await mp.configured_probe(db, task_id) is not None

    # А без коммита сдачи — снова «нет»: гонять не на чём.
    await repo_module.update_task(db, task_id, submission_sha="")
    await db.commit()
    assert await mp.configured_probe(db, task_id) is None
