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
    CORRIDOR_MIN_JUDGEMENTS,
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
    # CORRIDOR_MIN_JUDGEMENTS — с этого размера доля вообще измерима.
    for _ in range(CORRIDOR_MIN_JUDGEMENTS):
        await _pair(db, project_id, steward="escalate", human=None)

    boundary = await check_escalation_corridor(db)
    assert boundary == REASON_OVER_ESCALATING

    alerts = await _alerts(db)
    assert len(alerts) == 1
    over = json.loads(alerts[0]["payload"])
    assert over["state"] == REASON_OVER_ESCALATING
    assert f"{ACT_ESCALATION_CEILING:.0%}" in over["detail"]

    # Нижняя граница: разбавляем выборку до доли ниже пола. Прежние двадцать
    # эскалаций остаются в окне — 20 из 420 (4.8%) это уже штамповка, а не
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
    for _ in range(CORRIDOR_MIN_JUDGEMENTS):
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

    for _ in range(CORRIDOR_MIN_JUDGEMENTS):
        await _pair(db, project_id, steward="escalate", human=None)
    assert await check_escalation_corridor(db) == REASON_OVER_ESCALATING
    assert len(await _alerts(db)) == 1

    # Возврат в коридор: доля 20/80 = 25%, внутри 5–50%.
    for _ in range(60):
        await _pair(db, project_id, steward="approve", human=None)
    assert await check_escalation_corridor(db) is None
    restored = await _alerts(db)
    assert len(restored) == 2, "возврат в коридор — тоже смена состояния"
    assert json.loads(restored[-1]["payload"])["state"] == CORRIDOR_INSIDE

    # Повторный выход за ТУ ЖЕ границу: 120/180 ≈ 67%.
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
        for _ in range(CORRIDOR_MIN_JUDGEMENTS)
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
    for _ in range(CORRIDOR_MIN_JUDGEMENTS):
        await _pair(db, project_id, steward="escalate", human=None)
    assert await check_escalation_corridor(db) == REASON_OVER_ESCALATING
    assert len(await _alerts(db)) == 3, "после тишины нарушение обязано прозвучать"


async def test_a_short_week_is_not_measured(db: aiosqlite.Connection):
    """Отчёт 203: первое суждение недели не имеет права поднимать алерт.

    При одном суждении доля равна 0% или 100% — обе вне коридора, и первое
    же суждение теневой фазы писало бы алерт: approve — «штампует»,
    escalate — «бесполезен». Пустую выборку от нуля отличили (AC-2),
    недостаточную — нет. Проверяется по краю: на единицу меньше минимума
    молчит, ровно минимум — измеряет.
    """
    project_id = await _project(db, "corridor-short")

    await _pair(db, project_id, steward="approve", human=None)
    assert await check_escalation_corridor(db) is None
    assert await _alerts(db) == [], "одно суждение — не штамповка, а одно суждение"

    for _ in range(CORRIDOR_MIN_JUDGEMENTS - 2):
        await _pair(db, project_id, steward="approve", human=None)
    assert await check_escalation_corridor(db) is None
    assert await _alerts(db) == [], "на единицу меньше минимума — всё ещё не измерено"

    await _pair(db, project_id, steward="approve", human=None)
    assert await check_escalation_corridor(db) == REASON_STAMPING, (
        "ровно минимум — доля измерима, и 0 из 20 это уже штамповка"
    )


def test_minimum_sample_derives_from_the_floor():
    """Минимум не выбран, а выведен из пола: сдвинется пол — сдвинется он.

    Второй константы рядом с ACT_ESCALATION_FLOOR не заводится: 5%
    достижимы ненулевым счётчиком только от двадцати суждений.
    """
    import math

    assert CORRIDOR_MIN_JUDGEMENTS == math.ceil(1 / ACT_ESCALATION_FLOOR)
    assert 1 / CORRIDOR_MIN_JUDGEMENTS <= ACT_ESCALATION_FLOOR, (
        "при минимальном размере одна эскалация обязана достигать пола"
    )


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
