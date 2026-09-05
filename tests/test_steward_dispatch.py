"""Побудка стюарда: заказ размещает хаб, и ровно один раз (#1073).

Проверяется не «умеет ли диспетчер заказать», а четыре предохранителя, ради
которых он вообще выделен в отдельный контракт: выключатель, идемпотентность
заказа, суточный потолок и дедлайн слота. Каждый из них при срабатывании
оставляет сегодняшний человеческий маршрут работать — отказ здесь никогда не
означает «проверено и чисто».
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services.steward_dispatch import (
    EVENT_ORDERED,
    EVENT_REFUSED,
    KIND_DOR,
    REFUSED_ALREADY_ORDERED,
    REFUSED_DAILY_CAP,
    REFUSED_MODE_OFF,
    REFUSED_NO_GENERATION,
    REFUSED_NO_NEW_INFORMATION,
    RUN_OPEN,
    RUN_REFUSED,
    RUN_SUPERSEDED,
    RUN_TIMEOUT,
    close_finished_runs,
    open_run,
    order_due_dor_runs,
    order_due_runs,
    order_run,
)


@pytest.fixture(autouse=True)
def shadow_mode(monkeypatch):
    """Контур включён в тень: заказы размещаются, ничего не решают."""
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 20)
    monkeypatch.setattr(config, "STEWARD_RUN_DEADLINE_MIN", 30)


async def _project(db: aiosqlite.Connection, slug: str, *, steward: bool) -> int:
    project_id = await repo.create_project(
        db, slug=slug, name=slug, workspace_path="", status="active"
    )
    policy = {"verdict": "steward"} if steward else {}
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE id=?",
        (json.dumps(policy), project_id),
    )
    await db.commit()
    return project_id


async def _submitted_task(
    db: aiosqlite.Connection, project_id: int, *, generation: int = 1
) -> int:
    task_id = await repo.create_task(
        db,
        title="сдача на суд",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="review",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        task_id,
        project_id=project_id,
        submission_generation=generation,
        submission_sha="a" * 40,
    )
    await db.commit()
    return task_id


async def _events(db: aiosqlite.Connection, kind: str) -> list[dict]:
    rows = await fetchall(db, "SELECT * FROM events WHERE kind=?", (kind,))
    return [dict(r) for r in rows]


async def test_review_entry_orders_one_run(db: aiosqlite.Connection):
    """#1073 AC-1: тик поллера заказывает ровно один прогон и говорит об этом.

    Заказ — единственный способ, которым прогон вообще начинается: у самого
    стюарда такой операции нет (#1021).
    """
    project_id = await _project(db, "steward-one", steward=True)
    task_id = await _submitted_task(db, project_id)

    ordered = await order_due_runs(db)

    assert ordered == 1
    run = await open_run(db, task_id, 1)
    assert run is not None
    assert run["status"] == RUN_OPEN
    assert run["model"] == config.STEWARD_MODEL
    events = await _events(db, EVENT_ORDERED)
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["generation"] == 1
    assert payload["kind"] == "verdict"
    assert payload["model"] == config.STEWARD_MODEL


async def test_order_is_at_most_once_per_generation(db: aiosqlite.Connection):
    """#1073 AC-2: второй заказ на ту же генерацию не создаётся.

    Два тика, идущих подряд по одной сдаче, — обычный случай, а не редкий:
    поллер тикает каждые тридцать секунд, пока задача стоит в review. Дубль
    стоил бы второго оплаченного прогона и второго суждения на один код.
    """
    project_id = await _project(db, "steward-once", steward=True)
    task_id = await _submitted_task(db, project_id)

    first = await order_run(db, task_id, 1)
    second = await order_run(db, task_id, 1)
    await order_due_runs(db)

    assert first is not None
    assert second is None
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE task_id=?", (task_id,))
    assert len(rows) == 1
    refusals = await _events(db, EVENT_REFUSED)
    assert any(
        json.loads(e["payload"])["reason"] == REFUSED_ALREADY_ORDERED for e in refusals
    )


async def test_kill_switch_closes_dispatcher(db: aiosqlite.Connection, monkeypatch):
    """#1073 AC-3: off и нераспознанное значение одинаково закрывают контур.

    Опечатка в drop-in не должна ВКЛЮЧАТЬ проверку, которой никто не
    заказывал, — это правило #835 про потолок класса, здесь оно же.
    """
    project_id = await _project(db, "steward-off", steward=True)
    task_id = await _submitted_task(db, project_id)

    for mode in ("off", "shadwo", ""):
        monkeypatch.setattr(config, "STEWARD_MODE", mode)
        assert await order_due_runs(db) == 0
        assert await order_run(db, task_id, 1) is None

    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE task_id=?", (task_id,))
    assert rows == []
    refusals = await _events(db, EVENT_REFUSED)
    assert refusals, "закрытый контур обязан сказать об этом в фиде"
    assert all(json.loads(e["payload"])["reason"] == REFUSED_MODE_OFF for e in refusals)


async def test_daily_cap_falls_back_to_human(db: aiosqlite.Connection, monkeypatch):
    """#1073 AC-4: исчерпанный потолок — человеческий маршрут, а не тишина.

    «Упёрлись в потолок» и «проверено, чисто» обязаны быть различимы: второе
    прочтение первого — это ровно то, как пустой отчёт однажды прошёл за
    чистый (#750).
    """
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 2)
    project_id = await _project(db, "steward-cap", steward=True)
    first = await _submitted_task(db, project_id)
    second = await _submitted_task(db, project_id)
    third = await _submitted_task(db, project_id)

    assert await order_run(db, first, 1) is not None
    assert await order_run(db, second, 1) is not None
    over_cap = await order_run(db, third, 1)

    assert over_cap is None
    assert await open_run(db, third, 1) is None
    refusals = await _events(db, EVENT_REFUSED)
    assert any(
        json.loads(e["payload"])["reason"] == REFUSED_DAILY_CAP for e in refusals
    )


async def test_hung_run_closes_on_timeout(db: aiosqlite.Connection):
    """#1073 AC-5: просроченный слот закрывается, статус задачи не трогается.

    review:client — человеческий слот без дедлайна, поэтому зависший прогон
    иначе не эскалирует никогда: он просто стоит и выглядит заказанным.
    """
    project_id = await _project(db, "steward-timeout", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await db.execute(
        "UPDATE steward_runs SET deadline_at = datetime('now', '-1 minute') WHERE id=?",
        (run["id"],),
    )
    await db.commit()

    closed = await close_finished_runs(db)

    assert closed == 1
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],))
    assert dict(rows[0])["status"] == RUN_TIMEOUT
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "review", "диспетчер не двигает задачу — это F4"


async def test_human_verdict_closes_slot(db: aiosqlite.Connection):
    """#1073 AC-6: человеческий вердикт на эту генерацию закрывает слот.

    Человек всегда старше: суждение, пришедшее после него, получает 409 по
    контракту F1, а слот, ради которого его ждали, больше ничего не ждёт.
    """
    project_id = await _project(db, "steward-human", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await repo.update_task(
        db,
        task_id,
        review_verdict="approved",
        review_verdict_generation=1,
    )
    await db.commit()

    closed = await close_finished_runs(db)

    assert closed == 1
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],))
    assert dict(rows[0])["status"] == RUN_SUPERSEDED
    assert await open_run(db, task_id, 1) is None


async def test_project_without_the_policy_is_left_alone(db: aiosqlite.Connection):
    """Проект, не просивший стюарда, не получает заказов.

    Не отдельный AC, а граница всех шести: политику ставит человек (#743), и
    диспетчер не расширяет её молча на соседние проекты.
    """
    plain = await _project(db, "steward-none", steward=False)
    task_id = await _submitted_task(db, plain)

    assert await order_due_runs(db) == 0
    assert await open_run(db, task_id, 1) is None


# ---------------------------------------------------------------------------
# #1150 — пересдача без изменений не оплачивается
# ---------------------------------------------------------------------------


async def _reported(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    confirmed: list[dict],
) -> None:
    """Отчёт машинного ревью с подтверждёнными находками на эту генерацию."""
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill="multi-agent-review",
        harness_version=1,
        agent_count=11,
        tokens_spent=None,
        duration_ms=1000,
        orchestrator="cursor",
        model="grok-4.6",
        raw_count=7,
        findings_confirmed=json.dumps(confirmed),
        findings_rejected=json.dumps([]),
        unresolved=json.dumps([]),
        lost_dimensions=json.dumps([]),
        incomplete=False,
        submitted_by="cursor-cloud-reviewer",
        self_reviewed=False,
    )
    await db.commit()


def _touch(monkeypatch, outcome: str) -> None:
    """Что хаб узнал про места находок — подменяется на уровне вычисления.

    Настоящий ответ считает git по клону, которого в тестах нет: без
    подмены все три случая слились бы в один — «неизвестно». Подменяется
    ИМЕННО вычисление, а не решение диспетчера: правило остаётся под
    проверкой, подделан только факт, на котором оно работает.
    """

    async def _fake(db, task_id, findings, *, generation, head=""):
        assert head, (
            "решение о сдаче обязано считаться до ЗАКРЕПЛЁННОГО sha, "
            "а не до вершины ветки: имя ветки — движущаяся цель (#572)"
        )

        from hub.services.finding_identity import finding_uids

        return {uid: {"outcome": outcome} for uid in finding_uids(findings)}

    monkeypatch.setattr(
        "hub.services.finding_evidence.evidence_for_report", _fake, raising=True
    )


_FINDING = {
    "title": "страж не читает пин",
    "severity": "high",
    "file": "hub/services/steward_apply.py",
    "locator": "lines",
    "start_line": 10,
    "end_line": 20,
}


async def test_resubmit_without_changes_refused_before_run(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1150): прогона нет ВООБЩЕ, а не «есть, но бесполезный».

    Отказ до старта стоит ноль, отказ после — полтора-два миллиона токенов
    провайдера за воспроизведение известного ответа. Поэтому проверяется
    отсутствие СТРОКИ в steward_runs, а не отсутствие суждения: заказ,
    который потом закроют, уже оплачен.
    """
    project_id = await _project(db, "steward-stale", steward=True)
    task_id = await _submitted_task(db, project_id, generation=1)
    await _reported(db, task_id, 1, [_FINDING])
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()
    _touch(monkeypatch, "untouched")

    ordered = await order_due_runs(db)

    assert ordered == 0
    rows = [
        dict(r)
        for r in await fetchall(
            db, "SELECT status FROM steward_runs WHERE task_id=?", (task_id,)
        )
    ]
    assert [r["status"] for r in rows] == [RUN_REFUSED], (
        "ЗАКАЗА не должно появиться вовсе — он и есть оплата. Строка есть, но "
        "это не заказ, а его невозможность: генерация закрыта отказом (отчёт "
        "212), и открытого слота, который кто-то мог бы исполнить, нет"
    )
    assert await open_run(db, task_id, 2) is None
    refusals = await _events(db, EVENT_REFUSED)
    assert refusals, "молчаливый отказ неотличим от бага"
    payload = json.loads(refusals[-1]["payload"])
    assert payload["reason"] == "no_new_information"
    assert "не тронула места находок" in payload["detail"]


async def test_real_change_still_gets_its_run(db: aiosqlite.Connection, monkeypatch):
    """AC-2 (#1150): отказ, умеющий только отказывать, — это выключатель.

    Три случая обязаны пропускать, и каждый по своей причине: правка мест
    находок, отсутствие подтверждённых находок у прошлой сдачи, и —
    отдельно — неизвестность. «Хаб не смог посмотреть» не равно «ничего не
    изменилось» (#762), и цена ошибки здесь несимметрична: лишний прогон
    стоит денег, пропущенная правка — суждения о коде, которого никто не
    судил.
    """
    project_id = await _project(db, "steward-fresh", steward=True)

    touched = await _submitted_task(db, project_id, generation=1)
    await _reported(db, touched, 1, [_FINDING])
    await repo.update_task(db, touched, submission_generation=2)
    await db.commit()
    _touch(monkeypatch, "touched")
    assert await order_due_runs(db) == 1, "правка мест находок обязана купить прогон"

    # Прошлая сдача без подтверждённых находок: отказывать не за что —
    # возвращали работу не по ним.
    clean = await _submitted_task(db, project_id, generation=1)
    await _reported(db, clean, 1, [])
    await repo.update_task(db, clean, submission_generation=2)
    await db.commit()
    _touch(monkeypatch, "untouched")
    assert await order_due_runs(db) == 1, "без находок прошлой сдачи отказ беспредметен"

    # Неизвестность покупает прогон, а не отказ.
    unknown = await _submitted_task(db, project_id, generation=1)
    await _reported(db, unknown, 1, [_FINDING])
    await repo.update_task(db, unknown, submission_generation=2)
    await db.commit()
    _touch(monkeypatch, "unknown")
    assert await order_due_runs(db) == 1, (
        "«не удалось посмотреть» — не «ничего не изменилось»: неизвестность "
        "стоит прогона, а не отказа"
    )


async def test_the_first_submission_is_never_stale(
    db: aiosqlite.Connection, monkeypatch
):
    """Первой сдаче сравнивать не с чем, и она проходит без вопросов.

    Отдельным тестом, потому что арифметика поколений — обычное место
    ошибки на единицу: generation-1 у первой сдачи равен нулю, и «нет
    отчёта нулевой генерации» не должно читаться как «ничего не менялось».
    """
    project_id = await _project(db, "steward-first", steward=True)
    task_id = await _submitted_task(db, project_id, generation=1)
    _touch(monkeypatch, "untouched")

    assert await order_due_runs(db) == 1
    assert await open_run(db, task_id, 1) is not None


async def test_a_second_tick_does_not_repeat_the_refusal(
    db: aiosqlite.Connection, monkeypatch
):
    """Отказ пишется один раз на генерацию, а не на каждый тик поллера.

    Поллер тикает раз в тридцать секунд, а задача стоит в review часами.
    Отказ на каждом проходе за ночь превращает фид в сотню одинаковых
    строк, среди которых больше нечего прочитать — а фид тут единственное
    место, где человек вообще узнаёт, что прогона не будет.
    """
    project_id = await _project(db, "steward-quiet-refusal", steward=True)
    task_id = await _submitted_task(db, project_id, generation=1)
    await _reported(db, task_id, 1, [_FINDING])
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()
    _touch(monkeypatch, "untouched")

    for _ in range(5):
        assert await order_due_runs(db) == 0

    # Считаются ВСЕ отказы по задаче, не только no_new_information. Индекс
    # и без короткого пути не даст второго заказа — но тогда каждый тик
    # упирался бы в него и писал already_ordered: тишина в фиде держится не
    # индексом, а тем, что до вычисления тик не доходит вовсе.
    refusals = [e for e in await _events(db, EVENT_REFUSED) if e["task_id"] == task_id]
    assert len(refusals) == 1, (
        f"ожидался один отказ, получено {len(refusals)}: "
        f"{[json.loads(e['payload']).get('reason') for e in refusals]}"
    )
    # И причина тишины — не память о событии, а закрытая строка: генерация
    # решена, и до вычисления следующий тик не доходит (отчёт 212).
    rows = await fetchall(
        db,
        "SELECT status FROM steward_runs WHERE task_id=? AND generation=2",
        (task_id,),
    )
    assert [dict(r)["status"] for r in rows] == [RUN_REFUSED]

    # Новая генерация — новое состояние, и о ней сказать надо.
    await repo.update_task(db, task_id, submission_generation=3)
    await _reported(db, task_id, 2, [_FINDING])
    await db.commit()
    assert await order_due_runs(db) == 0

    refusals = [
        e
        for e in await _events(db, EVENT_REFUSED)
        if json.loads(e["payload"]).get("reason") == "no_new_information"
    ]
    assert len(refusals) == 2, "следующая сдача — отдельный отказ, а не повтор"


async def test_a_refused_generation_stays_refused_when_the_facts_flip(
    db: aiosqlite.Connection, monkeypatch
):
    """Отчёт 212: отказ обязан ЗАПЕРЕТЬ генерацию, а не только сказать «нет».

    Событие в фиде ничего не запирало. Стоило вычислению на следующем тике
    ответить иначе — git не прочитал дифф, «неизвестно» честно покупает
    прогон, — и тот же заказ размещался на основании, которое минуту назад
    отвергли. Здесь факт меняется с «не тронуто» на «неизвестно» между
    тиками, и прогон всё равно не появляется: решение принято один раз.
    """
    project_id = await _project(db, "steward-refused-locked", steward=True)
    task_id = await _submitted_task(db, project_id, generation=1)
    await _reported(db, task_id, 1, [_FINDING])
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()

    _touch(monkeypatch, "untouched")
    assert await order_due_runs(db) == 0

    _touch(monkeypatch, "unknown")
    assert await order_due_runs(db) == 0, (
        "«неизвестно» купило бы прогон на свежей генерации — но эта уже решена"
    )
    _touch(monkeypatch, "touched")
    assert await order_due_runs(db) == 0
    assert await open_run(db, task_id, 2) is None
    # И ни одного ЛИШНЕГО слова: без короткого пути тик доходил бы до
    # order_run, упирался в индекс и писал already_ordered на каждом
    # проходе — корректно по исходу, шумно по фиду.
    refusals = [e for e in await _events(db, EVENT_REFUSED) if e["task_id"] == task_id]
    assert [json.loads(e["payload"])["reason"] for e in refusals] == [
        "no_new_information"
    ], "решённая генерация не обсуждается повторно ни под каким кодом"
    rows = await fetchall(
        db,
        "SELECT status FROM steward_runs WHERE task_id=? AND generation=2",
        (task_id,),
    )
    assert [dict(r)["status"] for r in rows] == [RUN_REFUSED], (
        "ровно одна строка, и она refused: второй заказ невозможен по индексу"
    )


async def test_a_refusal_does_not_spend_the_daily_cap(
    db: aiosqlite.Connection, monkeypatch
):
    """Отказ — не заказ: он ничего не купил и не тратит потолок покупок.

    Строка refused стоит в той же таблице, что и заказы, и потолок считает
    по ней. Без исключения десять отказов за утро оставили бы проект без
    прогонов до полуночи — при том, что ни один прогон не состоялся.
    """
    from hub.services.steward_dispatch import runs_today

    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 1)
    project_id = await _project(db, "steward-cap-refused", steward=True)

    refused = await _submitted_task(db, project_id, generation=1)
    await _reported(db, refused, 1, [_FINDING])
    await repo.update_task(db, refused, submission_generation=2)
    await db.commit()
    _touch(monkeypatch, "untouched")
    assert await order_due_runs(db) == 0
    assert await runs_today(db, project_id) == 0, "отказ не считается заказом"

    fresh = await _submitted_task(db, project_id, generation=1)
    assert await order_due_runs(db) == 1, (
        "потолок 1 ещё не потрачен — отказ его не съел, и свежая сдача получает прогон"
    )
    assert await open_run(db, fresh, 1) is not None


# ---------------------------------------------------------------------------
# #1160 — второй вид работы того же диспетчера: чтение постановки драфта
# ---------------------------------------------------------------------------


async def _project_with_policy(
    db: aiosqlite.Connection, slug: str, policy: dict
) -> int:
    """Проект с ПРОИЗВОЛЬНОЙ политикой гейтов.

    Отдельно от ``_project`` выше, который умеет один ключ: смешанный день
    (AC-3) требует проекта, отдавшего стюарду ОБА гейта, а граница —
    проекта, отдавшего только вердикт.
    """
    project_id = await repo.create_project(
        db, slug=slug, name=slug, workspace_path="", status="active"
    )
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE id=?",
        (json.dumps(policy), project_id),
    )
    await db.commit()
    return project_id


_DOR_FIELDS: dict[str, object] = {
    "work_type": "feature",
    "user_story": "как владелец, я хочу X, чтобы Y",
    "problem_statement": "что именно сломано",
    "business_value": "зачем это надо",
    "size": "S",
    "wip_tag": "feature_work",
    "scope_in": json.dumps(["hub/services/steward_dispatch.py"]),
    "affected_areas": json.dumps(["hub/services/steward_dispatch.py"]),
    "validation_commands": json.dumps(["uv run pytest -q"]),
}


async def _ready_draft(
    db: aiosqlite.Connection, project_id: int, *, title: str = "драфт на прочтение"
) -> int:
    """Драфт, дошедший до dor_passed БЕЗ ЕДИНОГО refine.

    Ровно тот случай, что воспроизведён в постановке на develop 6c22332:
    задача создана целиком (у ``create_task_full`` тот же эффект — один
    INSERT), поля постановки уже в строке, а базис не снят, потому что
    снимают его пути ПРАВКИ. Готовность дописывается ленивой починкой при
    чтении карточки (#1166) — она пишет dor_passed и не трогает отпечаток.

    Поэтому здесь колонки пишутся напрямую, а не через ``refine_task``:
    refine снял бы базис и сделал бы предусловие AC-5 недостижимым.
    """
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, project_id=project_id, **_DOR_FIELDS)
    await db.execute(
        "INSERT INTO acceptance_criteria "
        "(task_id, ac_id, given, when_clause, then_clause, verifiable_by, "
        "test_ref, expectation_source, position) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            "AC-1",
            "дано",
            "когда",
            "тогда",
            "test",
            "tests/x.py::y",
            "requirement",
            0,
        ),
    )
    await db.commit()

    from hub.services.refinement import get_readiness

    await get_readiness(db, task_id)

    row = dict(await repo.get_task(db, task_id))
    assert row["dor_passed"], "предусловие: драфт прошёл DoR"
    assert row["statement_generation"] == 0, "предусловие: правок не было"
    assert not row["statement_fingerprint"], "предусловие: базис не снят"
    return task_id


async def _runs(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = await fetchall(
        db, "SELECT * FROM steward_runs WHERE task_id=? ORDER BY id", (task_id,)
    )
    return [dict(r) for r in rows]


async def test_a_draft_gets_one_dor_slot_per_revision(db: aiosqlite.Connection):
    """AC-1: один слот kind=dor на текущую ревизию, и второй тик его не удваивает.

    At-most-once здесь тот же, что у вердикта, но ключ другой: ревизия
    ПОСТАНОВКИ (#1156). Дубль стоил бы второго оплаченного прогона за один и
    тот же текст.
    """
    project_id = await _project_with_policy(db, "dor-one", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)

    ordered = await order_due_dor_runs(db)

    assert ordered == 1
    run = await open_run(db, task_id, 0, KIND_DOR)
    assert run is not None
    assert run["status"] == RUN_OPEN
    assert run["kind"] == KIND_DOR
    events = [json.loads(e["payload"]) for e in await _events(db, EVENT_ORDERED)]
    assert [e["kind"] for e in events] == [KIND_DOR]

    # Второй тик — по той же ревизии, и покупать ему нечего.
    assert await order_due_dor_runs(db) == 0
    assert len(await _runs(db, task_id)) == 1


async def test_the_autopilot_is_asked_before_paying(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-2: драфт, который снимает правило, платного прогона не получает.

    Проверяются два разных утверждения, и второе — про ПОРЯДОК. Мало не
    заказать прогон для снятого драфта: автопилот обязан быть спрошен ДО
    того, как в таблице появилась хоть одна строка. Поэтому подмена сама
    смотрит в steward_runs в момент вызова — иначе тест прошёл бы и на
    реализации «сначала заказать, потом сообразить».

    Подменяется именно автопилот, а не политика: композиция dor=steward с
    автопилотом — предмет соседней задачи (#1157), а предмет ЭТОЙ — что
    диспетчер спрашивает его вызовом и подчиняется ответу.
    """
    project_id = await _project_with_policy(db, "dor-autopilot", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)
    asked: list[int] = []

    async def _approves(conn, tid):
        rows = await fetchall(conn, "SELECT 1 FROM steward_runs", ())
        assert not rows, (
            "автопилот обязан быть спрошен ДО заказа: платить за работу, "
            "которую снимает правило, нечем"
        )
        asked.append(tid)
        await repo.update_task(conn, tid, status="open")
        return True

    monkeypatch.setattr(
        "hub.services.auto_approve.maybe_auto_approve", _approves, raising=True
    )

    assert await order_due_dor_runs(db) == 0

    assert asked == [task_id], "автопилот обязан быть спрошен, а не угадан"
    assert await _runs(db, task_id) == []
    assert dict(await repo.get_task(db, task_id))["status"] == "open"


async def test_the_daily_cap_is_shared(db: aiosqlite.Connection, monkeypatch):
    """AC-3: потолок один на два вида прогонов — проверено СМЕШАННЫМ днём.

    Два отдельных дня ничего не доказали бы: своя квота у каждого вида
    прошла бы такую проверку целиком. Здесь вердиктные прогоны выбирают
    потолок, и DoR-прогон обязан упереться в него — в чужой, с его точки
    зрения, счёт.
    """
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 2)
    project_id = await _project_with_policy(
        db, "dor-cap", {"verdict": "steward", "dor": "steward"}
    )
    await _submitted_task(db, project_id)
    await _submitted_task(db, project_id)
    draft = await _ready_draft(db, project_id)

    assert await order_due_runs(db) == 2, "вердиктные прогоны выбрали потолок"

    assert await order_due_dor_runs(db) == 0
    assert await _runs(db, draft) == [], "заказа нет вовсе — потолок общий"
    refusals = [
        json.loads(e["payload"])
        for e in await _events(db, EVENT_REFUSED)
        if e["task_id"] == draft
    ]
    assert [r["reason"] for r in refusals] == [REFUSED_DAILY_CAP]


async def test_an_unchanged_draft_buys_no_run(db: aiosqlite.Connection):
    """AC-4: суждение на этой ревизии уже есть — отказ ДО старта, и без квоты.

    Урок #1150 дословно, только про постановку: прогон без новой информации
    вернул бы то же мнение, посчитанное второй раз. Отказ до заказа стоит
    ноль, отказ после — полный прогон, поэтому проверяется отсутствие
    ЗАКАЗА, а не отсутствие суждения.
    """
    from hub.services.steward_dispatch import runs_today

    project_id = await _project_with_policy(db, "dor-unchanged", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)
    # Базис снимает диспетчер, поэтому ревизия у суждения — та же, на
    # которую он придёт: 0.
    from hub.services.statement_generation import baseline_if_absent

    generation = await baseline_if_absent(db, task_id)
    await db.commit()
    assert await repo.insert_steward_judgement(
        db,
        task_id=task_id,
        generation=generation,
        kind=KIND_DOR,
        submitted_verdict="approved",
        verdict="approved",
    )
    await db.commit()

    assert await order_due_dor_runs(db) == 0

    assert [r["status"] for r in await _runs(db, task_id)] == [RUN_REFUSED], (
        "строка есть, но это не заказ, а его невозможность: ревизия закрыта"
    )
    assert await open_run(db, task_id, generation, KIND_DOR) is None
    assert await runs_today(db, project_id) == 0, "отказ не занимает квоту"
    refusals = [
        json.loads(e["payload"])
        for e in await _events(db, EVENT_REFUSED)
        if e["task_id"] == task_id
    ]
    assert [r["reason"] for r in refusals] == [REFUSED_NO_NEW_INFORMATION]
    assert refusals[0]["kind"] == KIND_DOR


async def test_a_draft_that_never_was_refined_buys_one_run(db: aiosqlite.Connection):
    """AC-5: пустая правка после заказа второго прогона НЕ покупает.

    Воспроизведено на develop 6c22332: драфт, приехавший готовым, лежит с
    поколением 0 и ПУСТЫМ отпечатком, а чтение карточки его не снимает
    (#1166). Без снятия базиса диспетчером первый же refine — в том числе
    ничего не меняющий — сдвинул бы счётчик в 1, потому что пустая строка
    не равна sha256 ни от чего, и купил бы второй платный прогон за
    нетронутый текст.
    """
    project_id = await _project_with_policy(db, "dor-never-refined", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)

    assert await order_due_dor_runs(db) == 1
    assert [r["generation"] for r in await _runs(db, task_id)] == [0]
    row = dict(await repo.get_task(db, task_id))
    assert row["statement_generation"] == 0, (
        "снятие базиса НЕ двигает счётчик: иначе сам заказ прогона стал бы "
        "ревизией постановки и купил бы себе следующий"
    )
    assert row["statement_fingerprint"], "базис снят — ревизия 0 стала настоящей"

    # Ничего не меняющий refine: те же значения, что уже в строке.
    from hub.models import TaskRefine
    from hub.services.refinement import refine_task

    await refine_task(db, task_id, TaskRefine(title=row["title"]))

    assert dict(await repo.get_task(db, task_id))["statement_generation"] == 0
    assert await order_due_dor_runs(db) == 0
    assert len(await _runs(db, task_id)) == 1, (
        "пустая правка нового мнения не покупает — второй платный прогон за "
        "текст, которого никто не трогал"
    )


async def test_a_real_edit_after_the_baseline_still_buys_a_run(
    db: aiosqlite.Connection,
):
    """AC-6: снятие базиса гасит ЛОЖНУЮ ревизию, а не настоящую.

    Обратная сторона AC-5, и без неё та проверка описывала бы выключатель:
    механизм, который научился не покупать, обязан по-прежнему покупать
    там, где текст действительно изменился.
    """
    project_id = await _project_with_policy(db, "dor-real-edit", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)
    assert await order_due_dor_runs(db) == 1

    from hub.models import TaskRefine
    from hub.services.refinement import refine_task

    await refine_task(
        db, task_id, TaskRefine(problem_statement="постановку переписали")
    )

    assert dict(await repo.get_task(db, task_id))["statement_generation"] == 1
    assert await order_due_dor_runs(db) == 1
    assert [r["generation"] for r in await _runs(db, task_id)] == [0, 1]


async def test_a_project_without_the_dor_policy_is_left_alone(
    db: aiosqlite.Connection,
):
    """Граница: гейт вердикта, отданный стюарду, гейта постановки не отдаёт.

    Два ключа политики — два разных решения владельца, и молчаливое
    распространение одного на другой означало бы, что проект получил
    делегирование, которого не просил (#743).
    """
    verdict_only = await _project_with_policy(db, "dor-none", {"verdict": "steward"})
    task_id = await _ready_draft(db, verdict_only)

    assert await order_due_dor_runs(db) == 0
    assert await _runs(db, task_id) == []


async def test_a_verdict_run_does_not_settle_a_draft_revision(
    db: aiosqlite.Connection,
):
    """Числа двух счётчиков совпадают случайно — слот различает их видом.

    Регрессия на способ ошибиться, который живёт в самом устройстве слота:
    ``(task_id, generation, kind)``. Проверка решённости, забывшая про kind,
    прочитала бы вердиктный прогон на поколении 1 как «ревизия 1 постановки
    уже решена» — и драфт молча остался бы непрочитанным.
    """
    project_id = await _project_with_policy(
        db, "dor-kind-leak", {"verdict": "steward", "dor": "steward"}
    )
    task_id = await _ready_draft(db, project_id)
    # Тот же драфт правят: ревизия становится 1 — тем же числом, что и
    # поколение сдачи ниже.
    from hub.models import TaskRefine
    from hub.services.refinement import refine_task

    await refine_task(db, task_id, TaskRefine(problem_statement="правка"))
    await repo.update_task(db, task_id, dor_passed=1)
    await db.commit()
    # Вердиктный прогон на поколении 1 той же задачи, закрытый суждением.
    await db.execute(
        "INSERT INTO steward_runs (task_id, generation, kind, status, model, "
        "project_id, deadline_at) VALUES (?, ?, ?, ?, '', ?, datetime('now'))",
        (task_id, 1, "verdict", "judged", project_id),
    )
    await db.commit()

    assert await order_due_dor_runs(db) == 1, (
        "решённое поколение СДАЧИ ничего не говорит о ревизии ПОСТАНОВКИ"
    )
    assert await open_run(db, task_id, 1, KIND_DOR) is not None


async def test_a_dor_order_does_not_open_the_verdict_evidence_door(
    db: aiosqlite.Connection,
):
    """Заказ чтения драфта не отпирает пакет СДАЧИ.

    Дверь к пакету открывает открытый прогон на генерацию (#1074), и до
    второго вида заказов вопрос имел один ответ. Теперь у совпавших чисел
    два смысла, и заказ, открывший чужую дверь, расширил бы вход судьи
    ровно на этот путь — а вход судьи и есть граница безопасности.
    """
    from hub.services.steward_evidence import open_run_exists

    project_id = await _project_with_policy(db, "dor-door", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)
    assert await order_due_dor_runs(db) == 1

    assert await open_run_exists(db, task_id, 0) is False


async def test_a_dor_order_is_not_started_as_a_verdict_run(
    db: aiosqlite.Connection, monkeypatch
):
    """Исполнитель вердиктов DoR-заказ не берёт.

    ``start_run`` собирает пакет СДАЧИ — ветку, закреплённый sha, отчёт
    ревью, — и на драфте ничего этого нет. Прогон был бы оплачен и прочитал
    бы не то, о чём его спрашивали. Исполнитель для драфта приезжает своей
    задачей; до тех пор заказ ждёт и закрывается по дедлайну слота.
    """
    from hub.services import steward_shadow

    started: list[dict] = []

    async def _start(conn, order):
        started.append(order)
        return True

    monkeypatch.setattr(steward_shadow, "start_run", _start, raising=True)
    project_id = await _project_with_policy(db, "dor-start", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)
    assert await order_due_dor_runs(db) == 1

    assert await steward_shadow.start_due_runs(db) == 0
    assert started == []
    assert await open_run(db, task_id, 0, KIND_DOR) is not None, (
        "заказ остаётся открытым: неисполненный стоит ноль, исполненный не "
        "по тому пакету — полный прогон"
    )


async def test_an_unbaselined_revision_is_never_paid_for(db: aiosqlite.Connection):
    """Прогон не заказывается на ревизии, которой нет, — даже мимо диспетчера.

    Проверка стоит в самой оплачиваемой операции, а не только в отборе:
    пустой отпечаток означает «базис не снят», и первая же пустая правка
    объявила бы эту ревизию другой.
    """
    project_id = await _project_with_policy(db, "dor-unbaselined", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)

    assert await order_run(db, task_id, 0, KIND_DOR) is None
    assert await _runs(db, task_id) == []
    refusals = [
        json.loads(e["payload"])
        for e in await _events(db, EVENT_REFUSED)
        if e["task_id"] == task_id
    ]
    assert [r["reason"] for r in refusals] == [REFUSED_NO_GENERATION]


async def test_a_revised_draft_releases_its_old_slot(db: aiosqlite.Connection):
    """Открытый слот на старой ревизии закрывается, а не держит квоту.

    Тот же довод, что у пересдачи (#1120): прогон читал текст, которого
    больше нет. Счётчик при этом ДРУГОЙ — правка постановки, а не сдача:
    спросить у драфта поколение сдачи значило бы сравнивать его ревизию с
    вечным нулём, и слот не закрылся бы никогда.
    """
    project_id = await _project_with_policy(db, "dor-superseded", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)
    assert await order_due_dor_runs(db) == 1

    from hub.models import TaskRefine
    from hub.services.refinement import refine_task

    await refine_task(db, task_id, TaskRefine(problem_statement="другой текст"))

    assert await close_finished_runs(db) == 1
    runs = await _runs(db, task_id)
    assert [r["status"] for r in runs] == [RUN_SUPERSEDED]
    assert "постановку правили" in runs[0]["closed_reason"]
