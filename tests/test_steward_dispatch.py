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
    REFUSED_ALREADY_ORDERED,
    REFUSED_DAILY_CAP,
    REFUSED_MODE_OFF,
    RUN_OPEN,
    RUN_SUPERSEDED,
    RUN_TIMEOUT,
    close_finished_runs,
    open_run,
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
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE task_id=?", (task_id,))
    assert rows == [], "заказа не должно появиться вовсе — он и есть оплата"
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

    refusals = [
        e
        for e in await _events(db, EVENT_REFUSED)
        if json.loads(e["payload"]).get("reason") == "no_new_information"
    ]
    assert len(refusals) == 1, f"ожидался один отказ, получено {len(refusals)}"

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
