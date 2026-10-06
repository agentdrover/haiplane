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
    EVENT_DEFERRED,
    EVENT_ORDERED,
    EVENT_REFUSED,
    KIND_DOR,
    REFUSED_ALREADY_ORDERED,
    REFUSED_DAILY_CAP,
    REFUSED_MODE_OFF,
    REFUSED_NO_GENERATION,
    REFUSED_NO_NEW_INFORMATION,
    REFUSED_REVIEW_IN_FLIGHT,
    RUN_JUDGED,
    RUN_OPEN,
    RUN_REFUSED,
    RUN_SUPERSEDED,
    PENDING_PREFIX,
    RUN_NEVER_STARTED,
    RUN_TIMEOUT,
    close_finished_runs,
    open_run,
    order_due_dor_runs,
    order_due_runs,
    order_run,
    runs_today,
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


async def _started(db: aiosqlite.Connection, run_id: int, agent: str = "bc-1") -> None:
    """Отметить прогон начатым так же, как это делает захват слота.

    Стоял ниже, у тестов #1181; поднят сюда, когда тот же признак понадобился
    #1201 — помощник, употребляемый за тысячу строк до своего определения,
    работает и читается неверно.
    """
    await db.execute(
        "UPDATE steward_runs SET agent_id=?, run_id=? WHERE id=?",
        (agent, "run-1", run_id),
    )
    await db.commit()


async def _verdict(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    generation: int = 1,
    actor: str = "policy",
    verdict: str = "approved",
) -> None:
    """Вердикт так, как он ложится в жизни: строка задачи И запись в ленте.

    Автора помнит только лента (#1201): в строке задачи его нет вовсе.
    """
    await repo.update_task(
        db,
        task_id,
        review_verdict=verdict,
        review_verdict_generation=generation,
    )
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor=actor,
        payload={"verdict": verdict, "submission_generation": generation},
    )
    await db.commit()


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

    Заказ здесь НЕ начинался, поэтому с #1181 он закрывается как
    never_started, а не timeout. Прежнее ожидание закрепляло неточность:
    таймаут судьи писался тому, кто не работал ни секунды.
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
    assert dict(rows[0])["status"] == RUN_NEVER_STARTED
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "review", "диспетчер не двигает задачу — это F4"


async def test_human_verdict_closes_slot(db: aiosqlite.Connection):
    """#1073 AC-6: вердикт на эту генерацию закрывает НЕНАЧАТЫЙ слот.

    Заказ, за который ещё не платили, снимать не жалко: ждали его ради
    решения, а решение состоялось.

    Начатый прогон с #1201 живёт дальше, и суждение после вердикта не
    отвергается — ни здесь, ни на приёме: раньше эта строка обещала ему 409,
    которого в коде нет. Отдельные тесты обоих случаев — ниже.
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


async def test_a_started_run_outlives_the_verdict(db: aiosqlite.Connection):
    """#1201 AC-1: начатый прогон не снимается вердиктом — он доживает до суждения.

    Измерено на #1183: агент bc-060ae97b стартовал в 07:44:43, автовердикт
    закрыл чистую сдачу в 07:47:45, слот сняли в 07:48:11. Прогон прожил три
    минуты двадцать восемь секунд, за агента заплачено, суждение выброшено.

    Выброшена при этом не только цена. В теневой фазе суждение на вердикт не
    влияет вовсе — оно нужно надзору F7 как пара «суждение против исхода», и
    снималось оно ровно на ЧИСТЫХ сдачах: на грязной автовердикт не
    срабатывает и слот никто не трогает. Выборка надзора оставалась без
    чистых сдач по устройству.

    Проверяется СТАТУС строки, а не текст причины: причина — слова, статус —
    то, доживёт ли прогон до своего суждения.
    """
    project_id = await _project(db, "steward-started", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await _started(db, run["id"])
    await _verdict(db, task_id, actor="policy")

    closed = await close_finished_runs(db)

    assert closed == 0
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],))
    assert dict(rows[0])["status"] == RUN_OPEN
    # Дверь пакета (#1075) спрашивает именно открытый слот: закрытый слот
    # означал бы, что суждение не только не нужно, но и невозможно.
    assert await open_run(db, task_id, 1) is not None


async def test_a_started_run_still_dies_at_the_deadline(db: aiosqlite.Connection):
    """#1201: потолок остался потолком — вечно открытых прогонов не появилось.

    Граница предыдущего теста, и без неё он опасен: «не снимать вердиктом»
    ровно на шаг отстоит от «не закрывать никогда». Закрывает дедлайн, и
    закрывает он как таймаут — этот судья работал.
    """
    project_id = await _project(db, "steward-started-deadline", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await _started(db, run["id"])
    await _verdict(db, task_id, actor="policy")
    await db.execute(
        "UPDATE steward_runs SET deadline_at = datetime('now', '-1 minute') WHERE id=?",
        (run["id"],),
    )
    await db.commit()

    closed = await close_finished_runs(db)

    assert closed == 1
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],))
    assert dict(rows[0])["status"] == RUN_TIMEOUT


async def test_an_unstarted_order_is_still_superseded(db: aiosqlite.Connection):
    """#1201 AC-2: заказ, который не начинался, вердикт снимает как и раньше.

    Платить за суждение по решённому вопросу незачем: агента ещё нет, и
    сохранять здесь нечего — ни наблюдения, ни денег. Метка захвата
    ``pending:`` к начатым не относится: это обещание заплатить, а не агент.
    """
    project_id = await _project(db, "steward-unstarted", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await _verdict(db, task_id, actor="policy")

    closed = await close_finished_runs(db)

    assert closed == 1
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],))
    assert dict(rows[0])["status"] == RUN_SUPERSEDED
    assert await open_run(db, task_id, 1) is None


async def test_a_claimed_but_unstarted_order_is_superseded_too(
    db: aiosqlite.Connection,
):
    """#1201 AC-2, вторая половина: метка захвата — не начатый прогон.

    Отдельным тестом, потому что именно здесь признак «начат» ломается тише
    всего: непустой agent_id выглядит как работающий агент, а означает
    захваченный слот, за который ещё не платили.
    """
    project_id = await _project(db, "steward-claimed", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await _started(db, run["id"], agent=f"{PENDING_PREFIX}{run['id']}")
    await _verdict(db, task_id, actor="policy")

    closed = await close_finished_runs(db)

    assert closed == 1
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],))
    assert dict(rows[0])["status"] == RUN_SUPERSEDED


async def test_a_late_judgement_is_recorded_but_changes_nothing(
    db: aiosqlite.Connection,
):
    """#1201 AC-3: суждение после вердикта записывается и ничего не решает.

    Ради этого прогон и оставлен жить: наблюдение доезжает до надзора. И
    ровно поэтому же теневая фаза обязана остаться теневой — суждение,
    пришедшее после вердикта, не повод пересмотреть решённое.

    Заодно снимается допущение постановки, проверенное чтением ДО кода:
    приём суждения не отказывает из-за уже вынесенного вердикта. Единственная
    проверка поколения на этом пути (``pinned_generation``) отказывает только
    на ПЕРЕСДАЧЕ, а вердикт генерацию не двигает.
    """
    from hub.config import TokenIdentity
    from hub.models import StewardJudgementSubmit
    from hub.services.steward_judgement import record_steward_judgement

    project_id = await _project(db, "steward-late", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await _started(db, run["id"])
    await _verdict(db, task_id, actor="policy", verdict="approved")
    await close_finished_runs(db)
    before = dict(await repo.get_task(db, task_id))

    await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=1,
            kind="verdict",
            verdict="changes_requested",
            confidence="high",
            grounds=[{"source": "ci_pinned_sha"}],
            model="gpt-5.3-codex",
        ),
        TokenIdentity("steward-bot", "steward", principal_id=42),
    )

    judgements = await fetchall(
        db,
        "SELECT * FROM steward_judgements WHERE task_id=? AND generation=?",
        (task_id, 1),
    )
    assert len(judgements) == 1, "наблюдение доехало до надзора"
    assert dict(judgements[0])["verdict"] == "changes_requested"
    after = dict(await repo.get_task(db, task_id))
    assert after["review_verdict"] == before["review_verdict"] == "approved"
    assert after["review_verdict_generation"] == 1
    assert after["status"] == before["status"], "судьба сдачи не изменилась"
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],))
    assert dict(rows[0])["status"] == RUN_JUDGED, "слот закрыт своим суждением"


async def test_the_closing_reason_names_who_decided(db: aiosqlite.Connection):
    """#1201 AC-4: причина снятия называет автора вердикта верно.

    Правило писалось под человека, который думает часами, и говорило
    «человеческий вердикт» всегда. На делегированном проекте решает политика
    через минуты после сдачи — и запись приписывала решение тому, кого там
    не было, ровно как «отказ API» в #1199.
    """
    project_id = await _project(db, "steward-author", steward=True)

    by_policy = await _submitted_task(db, project_id)
    policy_run = await order_run(db, by_policy, 1)
    assert policy_run is not None
    await _verdict(db, by_policy, actor="policy")

    by_human = await _submitted_task(db, project_id)
    human_run = await order_run(db, by_human, 1)
    assert human_run is not None
    await _verdict(db, by_human, actor="mrPDA")

    await close_finished_runs(db)

    rows = await fetchall(
        db, "SELECT * FROM steward_runs WHERE id=?", (policy_run["id"],)
    )
    policy_reason = dict(rows[0])["closed_reason"]
    assert "policy" in policy_reason
    # Корень, а не слово: старая формулировка звучала «человеческий вердикт»,
    # и проверка на «человек» пропустила бы ровно её — то самое враньё.
    assert "человеч" not in policy_reason, "политика не выдаётся за человека"

    rows = await fetchall(
        db, "SELECT * FROM steward_runs WHERE id=?", (human_run["id"],)
    )
    human_reason = dict(rows[0])["closed_reason"]
    assert "человек" in human_reason
    assert "mrPDA" in human_reason


async def test_an_unnamed_verdict_invents_no_author(db: aiosqlite.Connection):
    """#1201 AC-4, граница: автора нечем назвать — значит его не называют.

    Вердикт без записи в ленте бывает: строку задачи мог поставить путь,
    который событие не пишет. Догадка «раз не политика, значит человек» и
    была бы тем самым враньём, только с другой стороны.
    """
    project_id = await _project(db, "steward-unnamed", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await repo.update_task(
        db, task_id, review_verdict="approved", review_verdict_generation=1
    )
    await db.commit()

    await close_finished_runs(db)

    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],))
    reason = dict(rows[0])["closed_reason"]
    assert dict(rows[0])["status"] == RUN_SUPERSEDED
    assert "человеч" not in reason
    assert "автоматика" not in reason


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


# ---------------------------------------------------------------------------
# Находки машинного ревью #238 (сдача #2)
# ---------------------------------------------------------------------------


async def test_a_draft_approved_in_the_window_buys_no_run(
    db: aiosqlite.Connection, monkeypatch
):
    """Находка 52bd3401: статус перечитывается ПЕРЕД оплатой, а не только в выборке.

    Выборка снимает id запросом status='draft' и идёт по списку. Человеческий
    апрув с другого соединения законно приходит между выборкой и заказом, и
    слот ложился на задачу, которая уже не драфт: строка занимает общую
    суточную квоту, а отменить её нечем — заказ и есть оплата.

    Окно воспроизводится там, где оно и есть: подмена автопилота переводит
    статус ровно в тот момент, когда диспетчер спрашивает его перед платой,
    и возвращает False — то есть ведёт себя как автопилот на проекте
    dor=steward, пока апрув прилетает со стороны.
    """
    project_id = await _project_with_policy(db, "dor-window", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)

    async def _approved_meanwhile(conn, tid):
        assert conn.in_transaction, (
            "решение по драфту обязано идти ОДНОЙ транзакцией: перечитанный "
            "статус устаревает к следующей строке кода, и только write-лок "
            "делает апрув с другого соединения либо видимым, либо ждущим"
        )
        await repo.update_task(conn, tid, status="open")
        return False

    monkeypatch.setattr(
        "hub.services.auto_approve.maybe_auto_approve",
        _approved_meanwhile,
        raising=True,
    )

    assert await order_due_dor_runs(db) == 0
    assert await _runs(db, task_id) == [], (
        "заказ на задачу, которая уже не драфт, — оплаченное чтение "
        "постановки, которую решили без стюарда"
    )


async def test_the_baseline_returns_the_revision_it_left_on_the_row(
    db: aiosqlite.Connection, monkeypatch
):
    """Находка 798d6fee: вернуть прочитанное ДО записи значит вернуть ложь.

    SELECT транзакции не открывает, и между чтением счётчика и записью
    отпечатка законно вклинивается refine: он видит пустой отпечаток,
    считает его изменением (#1156) и двигает счётчик. Функция дописывала
    отпечаток и возвращала прочитанный ноль — слот лёг бы на ревизию 0,
    следующий проход закрыл бы его устаревшим, а новый тик купил бы ревизию
    1. Две строки квоты за нетронутый текст.

    Окно воспроизводится в самой его точке: подмена отпечатка двигает
    счётчик как раз между чтением и записью. Проверяется ИНВАРИАНТ —
    возвращённое значение описывает строку, которая лежит в базе после
    записи, — потому что он держит и тогда, когда окно кто-нибудь откроет
    заново.
    """
    from hub.services import statement_generation as sg

    project_id = await _project_with_policy(db, "dor-baseline-race", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)
    real = sg.statement_fingerprint

    async def _bumps_meanwhile(conn, tid):
        fresh = await real(conn, tid)
        await repo.update_task(conn, tid, statement_generation=1)
        return fresh

    monkeypatch.setattr(sg, "statement_fingerprint", _bumps_meanwhile, raising=True)

    generation = await sg.baseline_if_absent(db, task_id)
    await db.commit()

    row = dict(await repo.get_task(db, task_id))
    assert generation == row["statement_generation"] == 1, (
        "возвращённая ревизия обязана описывать строку, которая лежит в базе "
        "ПОСЛЕ записи, а не ту, что читали до неё"
    )


async def test_an_approved_draft_releases_its_open_slot(db: aiosqlite.Connection):
    """Незакрытая находка ревью #238: слот, который больше некому исполнять.

    DoR-апрув человеком меняет СТАТУС, а не вердикт, поэтому вердиктное
    правило закрытия его не видит: слот стоял открытым до дедлайна и ждал
    исполнителя для постановки, которую уже одобрили. Квоту закрытие не
    возвращает — заказ оплачен, — но прогон, который никому не нужен, не
    должен выглядеть заказанным.
    """
    project_id = await _project_with_policy(db, "dor-approved", {"dor": "steward"})
    task_id = await _ready_draft(db, project_id)
    assert await order_due_dor_runs(db) == 1

    await repo.update_task(db, task_id, status="open")
    await db.commit()

    assert await close_finished_runs(db) == 1
    runs = await _runs(db, task_id)
    assert [r["status"] for r in runs] == [RUN_SUPERSEDED]
    assert "больше не драфт" in runs[0]["closed_reason"]


# --- #1181: окно судьи отмеряется от РАБОТЫ, а не от заказа ---------------
#
# Первый прогон стюарда: заказан 06:20:32, стартовал 06:38:20, закрыт
# 06:50:50 с причиной «не вернул суждение». Семнадцать минут съели повторные
# попытки старта, судье досталось двенадцать минут из тридцати — и вина
# досталась ему же.


async def test_never_started_closes_as_never_started(
    db: aiosqlite.Connection, monkeypatch
):
    """#1181 AC-2: не начавшийся заказ закрывается СВОИМ исходом.

    Различие обязано быть в записи, а не в словах: надзор F7 считает по
    статусу, и «не смог стартовать» в графе «судья не справился» — это
    испорченная статистика, а не мелочь формулировки.
    """
    monkeypatch.setattr(config, "STEWARD_START_DEADLINE_MIN", 30)
    project_id = await _project(db, "steward-never", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now','-1 minute') WHERE id=?",
        (run["id"],),
    )
    await db.commit()

    assert await close_finished_runs(db) == 1
    row = dict(
        (await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],)))[0]
    )
    assert row["status"] == RUN_NEVER_STARTED
    assert row["status"] != RUN_TIMEOUT, "таймаут судьи — обвинение того, кто работал"
    assert "не удалось начать" in row["closed_reason"]


async def test_a_started_run_still_times_out(db: aiosqlite.Connection, monkeypatch):
    """#1181 AC-3: сдвиг точки отсчёта не отменяет самого дедлайна.

    Иначе правка тихо превращается в «никогда не закрывать», и слот живёт
    вечно — цена, которой окно судьи не стоит.
    """
    monkeypatch.setattr(config, "STEWARD_RUN_DEADLINE_MIN", 30)
    project_id = await _project(db, "steward-still-times-out", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await _started(db, run["id"], agent="bc-worked")
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now','-1 minute') WHERE id=?",
        (run["id"],),
    )
    await db.commit()

    assert await close_finished_runs(db) == 1
    row = dict(
        (await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],)))[0]
    )
    assert row["status"] == RUN_TIMEOUT, "работавший и не ответивший — это таймаут"
    assert "не вернул суждение" in row["closed_reason"]


async def test_a_pending_claim_is_not_a_started_run(
    db: aiosqlite.Connection, monkeypatch
):
    """#1181: метка захвата — намерение, а не работа.

    Между меткой и подтверждённым стартом лежит вызов провайдера, который
    может не состояться вовсе (#1195). Считать метку началом работы значило
    бы записать таймаут судьи тому, кого так и не создали.
    """
    monkeypatch.setattr(config, "STEWARD_START_DEADLINE_MIN", 30)
    project_id = await _project(db, "steward-pending", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await db.execute(
        "UPDATE steward_runs SET agent_id=?, deadline_at=datetime('now','-1 minute') "
        "WHERE id=?",
        (f"{PENDING_PREFIX}{run['id']}", run["id"]),
    )
    await db.commit()

    assert await close_finished_runs(db) == 1
    row = dict(
        (await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],)))[0]
    )
    assert row["status"] == RUN_NEVER_STARTED


# ---------------------------------------------------------------------------
# #1268 — теневое участие: стюард судит, вердикт остаётся за человеком
# ---------------------------------------------------------------------------

#: Политика default, какой она стоит на проде, плюс признак тени.
_SHADOW_POLICY: dict[str, object] = {
    "dor": "human",
    "verdict": "human",
    "review": "dispatch",
    "release": "auto",
    "steward_shadow": True,
}


async def test_a_shadow_participant_with_a_human_verdict_gets_a_run(
    db: aiosqlite.Connection,
):
    """#1268 AC-1: verdict=human и признак тени — прогон заказан, как для steward.

    Сдача несёт отчёт ревью: на default review=dispatch, и именно такая сдача
    идёт по потоку. Заказ тот же, что у делегированного проекта, — модель,
    событие, генерация; различие только в том, кто потом выносит вердикт.
    """
    project_id = await _project_with_policy(db, "shadow-default", _SHADOW_POLICY)
    task_id = await _submitted_task(db, project_id)
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="multi-agent-review",
        harness_version=1,
        agent_count=11,
        tokens_spent=None,
        duration_ms=1000,
        orchestrator="cursor",
        model="grok-4.6",
        raw_count=0,
        findings_confirmed="[]",
        findings_rejected="[]",
        unresolved="[]",
        lost_dimensions="[]",
        incomplete=False,
        submitted_by="cursor-cloud-reviewer",
        self_reviewed=False,
    )
    await db.commit()

    assert await order_due_runs(db) == 1
    run = await open_run(db, task_id, 1)
    assert run is not None, "теневой участник обязан получить прогон вердикта"
    assert run["status"] == RUN_OPEN
    assert run["model"] == config.STEWARD_MODEL
    payload = json.loads((await _events(db, EVENT_ORDERED))[0]["payload"])
    assert payload["kind"] == "verdict"

    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "заказ прогона не трогает статус сдачи"
    assert row["review_verdict"] is None, "и не выносит вердикта"


async def test_shadow_participation_does_not_open_the_dor_gate(
    db: aiosqlite.Connection,
):
    """#1268, граница scope_out: тень — только вердикт, DoR-стюарда она не заказывает."""
    project_id = await _project_with_policy(db, "shadow-no-dor", _SHADOW_POLICY)
    task_id = await _ready_draft(db, project_id)

    assert await order_due_dor_runs(db) == 0
    assert await _runs(db, task_id) == []


async def test_a_malformed_shadow_flag_reads_as_not_participating(
    db: aiosqlite.Connection,
):
    """#1268 AC-4: признак неверного типа или незнакомый — проект не участвует (#835).

    Перебором: строка «true», единица, «yes», пустой объект, null. Ровно
    ``true`` JSON — и только оно — есть участие; всё прочее читается как
    отсутствие признака, потому что нераспознанная политика не есть просьба.
    """
    for number, value in enumerate(("true", "True", 1, "yes", {}, None, [True])):
        policy = {"verdict": "human", "steward_shadow": value}
        project_id = await _project_with_policy(db, f"shadow-bad-{number}", policy)
        task_id = await _submitted_task(db, project_id)

        assert await order_due_runs(db) == 0, (
            f"steward_shadow={value!r} не участие, а прогон заказан"
        )
        assert await open_run(db, task_id, 1) is None


async def _dispatch(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    generation: int = 1,
    status: str = "active",
    channel: str = "cloud",
    model: str = "gpt-5.2",
) -> int:
    """Строка заказа кросс-модельного ревью — тот же факт, что бриф зовёт
    review_in_flight."""
    cur = await db.execute(
        "INSERT INTO review_dispatches "
        "(task_id, submission_generation, agent_id, run_id, model, status, channel) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, generation, "rev-agent", "run-7", model, status, channel),
    )
    await db.commit()
    return int(cur.lastrowid or 0)


async def _report(
    db: aiosqlite.Connection, task_id: int, *, generation: int = 1
) -> None:
    """Отчёт ревью этой генерации лёг."""
    await db.execute(
        "INSERT INTO machine_reviews "
        "(task_id, submission_generation, model, submitted_by) "
        "VALUES (?, ?, ?, ?)",
        (task_id, generation, "gpt-5.2", "rev-agent"),
    )
    await db.commit()


async def test_a_review_in_flight_defers_the_steward_run(db: aiosqlite.Connection):
    """#1289 AC-1: пока ревью этой сдачи идёт, прогон не покупается.

    Наблюдено 22.09.2026 на #1283 и #1286: заказ размещался через минуту
    после сдачи, отчёта ещё не было, и gate_grounds эскалировал по
    no_current_report — исход был предрешён до начала прогона. Отказ здесь
    стоит ноль, прогон стоил бы денег и суточной квоты.
    """
    project_id = await _project(db, "steward-inflight", steward=True)
    task_id = await _submitted_task(db, project_id)
    await _dispatch(db, task_id)

    # Два тика подряд — обычный случай: поллер тикает каждые тридцать
    # секунд, а ревью идёт минутами. Отсрочка повторяется, слово о ней — нет.
    assert await order_due_runs(db) == 0
    assert await order_due_runs(db) == 0

    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE task_id=?", (task_id,))
    assert list(rows) == [], "отложенный заказ не смеет оставлять строку прогона"
    assert await runs_today(db, project_id) == 0, "квота на отложенный заказ потрачена"
    deferrals = await _events(db, EVENT_DEFERRED)
    assert len(deferrals) == 1
    payload = json.loads(deferrals[0]["payload"])
    assert payload["reason"] == REFUSED_REVIEW_IN_FLIGHT
    assert payload["generation"] == 1
    assert "ревью" in payload["detail"]


async def test_a_deferred_order_comes_back_when_the_report_lands(
    db: aiosqlite.Connection,
):
    """#1289 AC-2: отложенный заказ не теряется — следующий проход вернётся.

    Откладывание держится на отсутствии строки в steward_runs: генерация
    остаётся нерешённой, и поллер обязан прийти к ней снова. Строка
    (хоть refused) заперла бы её навсегда.
    """
    project_id = await _project(db, "steward-comes-back", steward=True)
    task_id = await _submitted_task(db, project_id)
    await _dispatch(db, task_id)

    assert await order_due_runs(db) == 0
    assert await open_run(db, task_id, 1) is None

    await _report(db, task_id)

    assert await order_due_runs(db) == 1
    run = await open_run(db, task_id, 1)
    assert run is not None
    assert run["status"] == RUN_OPEN


async def test_no_review_at_all_still_buys_a_run(db: aiosqlite.Connection):
    """#1289 AC-3, переписан #1600 AC-1: ревью НЕ ПОЛОЖЕНО — прогон заказан.

    Раньше тест звал проект с verdict=steward и без строки ревью «ревью нет
    вовсе». Но при verdict=steward ревью положено (review_dispatch_enabled),
    и отсутствие строки — это ожидание, а не отсутствие ревью: это и была
    дыра #1600. Здесь «нет вовсе» — проект с вердиктом человека и теневым
    участием: ревью не просит, ждать нечего, эскалация по такому прогону
    законна. Мутация «откладывать всегда» роняет именно этот тест.
    """
    project_id = await _project_with_policy(
        db, "steward-no-review", {"verdict": "human", "steward_shadow": True}
    )
    task_id = await _submitted_task(db, project_id)
    await _on_a_branch(db, task_id)

    assert await order_due_runs(db) == 1

    run = await open_run(db, task_id, 1)
    assert run is not None
    assert run["status"] == RUN_OPEN
    assert await _events(db, EVENT_DEFERRED) == []
    payload = json.loads((await _events(db, EVENT_ORDERED))[0]["payload"])
    assert "review_not_requested" in payload["why"]


async def test_a_finished_dispatch_without_a_report_does_not_defer(
    db: aiosqlite.Connection,
):
    """#1289 AC-3, тот же случай с закрытым заказом ревью.

    Свип закрывает зависший заказ как failed. После этого ждать снова нечего:
    ревью было вызвано и не сдало отчёта — ровно #1241.
    """
    project_id = await _project(db, "steward-dispatch-failed", steward=True)
    task_id = await _submitted_task(db, project_id)
    # Не облачный заказ: переспрос #1242 касается только облака, и «упал без
    # переспроса» — терминальный исход (#1600). Облачный упавший заказ с
    # назначенным переспросом — ожидание, его проверяет AC-3.
    await _dispatch(db, task_id, status="failed", channel="local")

    assert await order_due_runs(db) == 1
    assert await open_run(db, task_id, 1) is not None


async def test_a_second_door_debt_without_a_report_defers_too(
    db: aiosqlite.Connection,
):
    """#1289, находка bec6db75314abd83: долг второй двери — тоже «ещё идёт».

    ``second_door`` не состояние прогона, а ДОЛГ: облачный прогон кончился
    без отчёта, строка нарочно оставлена открытой, чтобы свип пришёл и
    открыл вторую дверь. Пока долг не отдан, отчёта этой сдачи нет — и
    купленный здесь прогон стюарда прочитал бы ровно то же отсутствие и
    эскалировал бы по no_current_report, не начав судить. Постановка так и
    определяет активный заказ: ``active`` ИЛИ ``second_door``.

    Вечной отсрочки это не создаёт: долг закрывает ``_settle_second_door``
    — либо второй дверью (новый заказ, ``active``), либо ``failed``, а
    ``failed`` прогон покупает (тест выше).
    """
    project_id = await _project(db, "steward-second-door", steward=True)
    task_id = await _submitted_task(db, project_id)
    await _dispatch(db, task_id, status="second_door")

    assert await order_due_runs(db) == 0
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE task_id=?", (task_id,))
    assert list(rows) == [], "долг второй двери не смеет оставлять строку прогона"
    assert await runs_today(db, project_id) == 0, "квота на отложенный заказ потрачена"
    deferrals = await _events(db, EVENT_DEFERRED)
    assert len(deferrals) == 1
    payload = json.loads(deferrals[0]["payload"])
    assert payload["reason"] == REFUSED_REVIEW_IN_FLIGHT
    assert "вторая дверь" in payload["detail"], (
        "причина обязана назвать долг второй двери, а не выдавать его за идущий прогон"
    )


async def test_a_second_door_debt_still_comes_back_when_the_report_lands(
    db: aiosqlite.Connection,
):
    """#1289 AC-2 для долга второй двери: отсрочка кончается заказом.

    Отчёт спрашивается вторым и решает в пользу прогона — и на долге тоже:
    поздний отчёт облачного прогона может лечь раньше, чем свип закроет
    строку.
    """
    project_id = await _project(db, "steward-second-door-back", steward=True)
    task_id = await _submitted_task(db, project_id)
    await _dispatch(db, task_id, status="second_door")

    assert await order_due_runs(db) == 0
    await _report(db, task_id)

    assert await order_due_runs(db) == 1
    assert await open_run(db, task_id, 1) is not None


async def test_the_brief_does_not_call_a_second_door_debt_a_flying_review(
    db: aiosqlite.Connection,
):
    """Расширен ОДИН читатель, и ровно на одном месте применения (#1289).

    Долг второй двери — не летящий прогон: облачный уже кончился, а
    локальный ещё не заказан. Карточка и бриф (``review_in_flight``) не
    смеют показывать его как идущее ревью, иначе человек у гейта прочитает
    «подожди, платный прогон в воздухе» там, где ждать нечего. Широкий
    ответ берёт только тот, кто спросил широко — диспетчер стюарда.
    """
    from hub.services.review_evidence import inflight_view

    project_id = await _project(db, "steward-second-door-brief", steward=True)
    task_id = await _submitted_task(db, project_id)
    await _dispatch(db, task_id, status="second_door")
    task = dict(await repo.get_task(db, task_id))

    assert await inflight_view(db, task) is None, "бриф показал долг как полёт"
    wide = await inflight_view(db, task, include_owed=True)
    assert wide is not None
    assert "вторая дверь" in wide.headline


# ---------------------------------------------------------------------------
# #1330: отказ заказа пишется один раз, а не на каждом тике поллера
# ---------------------------------------------------------------------------


async def _refusals_of(
    db: aiosqlite.Connection, task_id: int, reason: str
) -> list[dict]:
    rows = await fetchall(
        db,
        "SELECT * FROM events WHERE kind=? AND task_id=? "
        "AND json_extract(payload, '$.reason')=? ORDER BY id",
        (EVENT_REFUSED, task_id, reason),
    )
    return [dict(r) for r in rows]


async def _cap_spent_by_a_neighbour(
    db: aiosqlite.Connection, monkeypatch, slug: str
) -> int:
    """Потолок в один прогон, и его уже потратила соседняя сдача."""
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 1)
    project_id = await _project(db, slug, steward=True)
    neighbour = await _submitted_task(db, project_id)
    assert await order_run(db, neighbour, 1) is not None
    return project_id


async def test_a_daily_cap_refusal_is_said_once_per_generation(
    db: aiosqlite.Connection, monkeypatch
):
    """#1330 AC-1: исчерпанный потолок — одно событие, а не одно на тик.

    Наблюдено 22.09.2026: 216 одинаковых отказов daily_cap за два часа, до 74
    на одну задачу; 23.09 к пяти утра — 1520. Первая запись обязана лечь
    (#1150: молчаливый отказ неотличим от бага), повторы — нет. Новое
    поколение сдачи — новый факт, и оно снова получает свою запись.
    """
    project_id = await _cap_spent_by_a_neighbour(db, monkeypatch, "cap-once")
    task_id = await _submitted_task(db, project_id)

    assert await order_due_runs(db) == 0
    assert await order_due_runs(db) == 0

    events = await _refusals_of(db, task_id, REFUSED_DAILY_CAP)
    assert len(events) == 1, f"два прохода дали {len(events)} отказов"
    payload = json.loads(events[0]["payload"])
    assert payload["generation"] == 1
    assert payload["kind"] == "verdict"

    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()
    assert await order_due_runs(db) == 0
    assert await order_due_runs(db) == 0

    events = await _refusals_of(db, task_id, REFUSED_DAILY_CAP)
    assert [json.loads(e["payload"])["generation"] for e in events] == [1, 2]


async def test_a_new_utc_day_says_the_cap_refusal_again(
    db: aiosqlite.Connection, monkeypatch
):
    """#1330 AC-2: вчерашний отказ не глушит сегодняшний.

    Потолок считается за UTC-сутки: исчерпанный сегодня — это новый факт, а
    не повтор вчерашнего, даже на той же задаче и том же поколении.
    """
    project_id = await _cap_spent_by_a_neighbour(db, monkeypatch, "cap-new-day")
    task_id = await _submitted_task(db, project_id)
    assert await order_due_runs(db) == 0
    await db.execute(
        "UPDATE events SET created_at=datetime('now', '-1 day') "
        "WHERE kind=? AND task_id=?",
        (EVENT_REFUSED, task_id),
    )
    await db.commit()

    assert await order_due_runs(db) == 0
    assert await order_due_runs(db) == 0

    events = await _refusals_of(db, task_id, REFUSED_DAILY_CAP)
    assert len(events) == 2, f"новые сутки дали {len(events) - 1} новых отказов"


async def test_every_order_refusal_is_said_once(db: aiosqlite.Connection, monkeypatch):
    """#1330: каждое место вызова _refuse в order_run проверено поимённо.

    daily_cap — не единственный отказ, который поллер повторяет: DoR-путь
    зовёт order_run на каждом тике так же, а mode_off, no_generation и
    already_ordered стоят в той же функции и пишут тем же _refuse.
    """
    project_id = await _project(db, "every-refusal", steward=True)

    ordered = await _submitted_task(db, project_id)
    assert await order_run(db, ordered, 1) is not None
    unsubmitted = await _submitted_task(db, project_id)
    switched_off = await _submitted_task(db, project_id)

    for _ in range(3):
        assert await order_run(db, ordered, 1) is None
        assert await order_run(db, unsubmitted, 0) is None
    monkeypatch.setattr(config, "STEWARD_MODE", "off")
    for _ in range(3):
        assert await order_run(db, switched_off, 1) is None

    assert len(await _refusals_of(db, ordered, REFUSED_ALREADY_ORDERED)) == 1
    assert len(await _refusals_of(db, unsubmitted, REFUSED_NO_GENERATION)) == 1
    assert len(await _refusals_of(db, switched_off, REFUSED_MODE_OFF)) == 1


async def test_a_waiting_refusal_is_said_once_and_still_read(
    db: aiosqlite.Connection, monkeypatch
):
    """#1330 AC-3: регрессионная защита дедупа #1290 рядом с его расширением.

    Отказ undeclared_model приходит из steward_shadow, а дедуп заказных
    отказов — тот же читатель прошлого события. Эта правка его расширяет, и
    тест падает, если расширение сломало исходный приём: ожидание ревьюера
    пишется один раз, а waiting_refusal_code по-прежнему его читает.
    """
    from unittest.mock import AsyncMock, patch

    from hub.services import steward_shadow as sh

    async def _delivery(_db, _task_id, _generation, _base_url):
        return "код доступа: ABC-123"

    monkeypatch.setattr(sh, "identity_delivery", _delivery)
    monkeypatch.setattr(config, "STEWARD_MODEL", "gpt-5.3-codex")
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", "steward-token")
    monkeypatch.setattr(config, "CURSOR_API_KEY", "cursor-key")
    # Тест про внутренний дедуп #1290, а не про страж #1600: потолок ожидания
    # отчёта вышел, и страж пропускает.
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 0)
    project_id = await _project(db, "waiting-once", steward=True)
    await db.execute(
        "UPDATE projects SET repo=? WHERE id=?", ("agentdrover/haiplane", project_id)
    )
    task_id = await _submitted_task(db, project_id)
    await repo.update_task(
        db, task_id, submission_model="claude-opus-5", branch=f"task-{task_id}/w"
    )
    await db.commit()
    run = await order_run(db, task_id, 1)
    assert run is not None

    created = {"agent": {"id": "agent-1"}, "run": {"id": "run-1"}}
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(created, None)),
    ) as started:
        for _ in range(4):
            assert await sh.start_due_runs(db) == 0
    assert started.await_count == 0

    waiting = await _refusals_of(db, task_id, sh.REFUSED_UNDECLARED_MODEL)
    assert len(waiting) == 1, f"четыре прохода дали {len(waiting)} записей"
    assert json.loads(waiting[0]["payload"])["retryable"] is True
    assert (
        await sh.waiting_refusal_code(db, task_id, run["id"])
        == sh.REFUSED_UNDECLARED_MODEL
    )

    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now','-1 minute') WHERE id=?",
        (run["id"],),
    )
    await db.commit()
    assert await close_finished_runs(db) == 1
    closed = dict(
        (await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],)))[0]
    )
    assert closed["status"] == RUN_NEVER_STARTED
    assert "ревьюер" in closed["closed_reason"], closed["closed_reason"]


# ---------------------------------------------------------------------------
# #1600: судья заказывается и стартует только когда отчёт ревью уже не ждать
# ---------------------------------------------------------------------------


async def _on_a_branch(db: aiosqlite.Connection, task_id: int) -> None:
    """Сдача, у которой есть ветка: диспетчер ревью её заказать МОЖЕТ.

    Без ветки диспетчер молча не заказывает ничего (terminal
    not_dispatchable), и сценарии про «ещё не заказано» теряли бы смысл.
    """
    await repo.update_task(db, task_id, branch=f"task-{task_id}/work")
    await db.commit()


async def _submitted_minutes_ago(
    db: aiosqlite.Connection, task_id: int, minutes: int, generation: int = 1
) -> None:
    await db.execute(
        "INSERT INTO submissions (task_id, generation, sha, submitted_at) "
        "VALUES (?, ?, ?, datetime('now', ?))",
        (task_id, generation, "a" * 40, f"-{minutes} minutes"),
    )
    await db.commit()


async def _due_task(db: aiosqlite.Connection, slug: str) -> tuple[int, int]:
    """Проект с verdict=steward (ревью положено) и сдача с веткой."""
    project_id = await _project(db, slug, steward=True)
    task_id = await _submitted_task(db, project_id)
    await _on_a_branch(db, task_id)
    return project_id, task_id


async def test_steward_waits_when_review_is_due_but_not_yet_dispatched(
    db: aiosqlite.Connection,
):
    """#1600 AC-1: ревью положено, строки заказа ещё нет — судью не покупают.

    Живой случай #1557 (01.10): заказ судьи размещён в ту же минуту, когда
    диспетчер ревью ещё только готовил заказ; отчёт пришёл через час, а
    прогон уже кончился эскалацией no_current_report. Страж #1289 видел
    только идущий заказ и это окно пропускал.
    """
    project_id, task_id = await _due_task(db, "steward-due-not-dispatched")

    assert await order_due_runs(db) == 0
    assert await order_due_runs(db) == 0

    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE task_id=?", (task_id,))
    assert list(rows) == [], "отсрочка не смеет оставлять строку прогона"
    assert await runs_today(db, project_id) == 0
    assert await _events(db, EVENT_ORDERED) == []
    deferrals = await _events(db, EVENT_DEFERRED)
    assert len(deferrals) == 1, "отсрочка пишется один раз на поколение"
    payload = json.loads(deferrals[0]["payload"])
    assert payload["generation"] == 1
    assert payload["reason"] == REFUSED_REVIEW_IN_FLIGHT
    assert "не заказано" in payload["detail"]

    # Другая сдача (поколение 2) — своя запись: дедуп в пределах поколения.
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()
    assert await order_due_runs(db) == 0
    assert len(await _events(db, EVENT_DEFERRED)) == 2


async def test_a_deferred_order_is_bought_once_the_report_lands(
    db: aiosqlite.Connection,
):
    """#1600 AC-1, продолжение: отсрочка кончается отчётом, а не потолком."""
    _, task_id = await _due_task(db, "steward-due-then-report")
    assert await order_due_runs(db) == 0

    await _report(db, task_id)

    assert await order_due_runs(db) == 1
    assert await open_run(db, task_id, 1) is not None


def _start_env(monkeypatch, *, configured: bool = True) -> None:
    """Всё, что нужно прогону, чтобы дойти до провайдера."""
    from hub.services import steward_shadow as sh

    async def _delivery(_db, _task_id, _generation, _base_url):
        return "код доступа: ABC-123"

    monkeypatch.setattr(sh, "identity_delivery", _delivery)
    monkeypatch.setattr(config, "STEWARD_MODEL", "gpt-5.3-codex")
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", "steward-token")
    monkeypatch.setattr(config, "CURSOR_API_KEY", "cursor-key" if configured else "")


async def _start_ready_order(
    db: aiosqlite.Connection, slug: str, *, reviewer: str = "grok-4.6"
) -> tuple[int, dict]:
    """Заказ судьи, размещённый, пока отчёт ещё не был нужен (минуя страж)."""
    project_id, task_id = await _due_task(db, slug)
    await db.execute(
        "UPDATE projects SET repo=? WHERE id=?", ("agentdrover/haiplane", project_id)
    )
    await repo.update_task(db, task_id, submission_model="claude-opus-5")
    if reviewer:
        await _dispatch(db, task_id, status="done", channel="local", model=reviewer)
    await db.commit()
    run = await order_run(db, task_id, 1)
    assert run is not None
    return task_id, run


async def test_every_steward_start_attempt_rechecks_review_state(
    db: aiosqlite.Connection, monkeypatch
):
    """#1600 AC-2: каждая попытка старта заново спрашивает, ждать ли отчёта.

    Три причины повтора из постановки: undeclared_model, отказ провайдера и
    отсутствие конфигурации. После каждой ревью переходит в pending (ушёл
    переспрос или встал новый заказ) — и повтор не смеет ни занять слот, ни
    позвать провайдера. После отчёта тот же заказ стартует.
    """
    from unittest.mock import AsyncMock, patch

    from hub.integrations import cursor_cloud
    from hub.services import steward_shadow as sh

    _start_env(monkeypatch)
    created = {"agent": {"id": "agent-1"}, "run": {"id": "run-1"}}
    transport = cursor_cloud.Refusal(detail="соединение оборвалось")

    # 1) undeclared_model: ревьюера ещё не названо, ревью terminal — первая
    #    попытка отказывает временно; затем ревью снова pending.
    undeclared, undeclared_run = await _start_ready_order(
        db, "recheck-undeclared", reviewer=""
    )
    # 2) отказ провайдера: ревьюер назван, ревью terminal (done без отчёта).
    provider, provider_run = await _start_ready_order(db, "recheck-provider")
    # 3) нет конфигурации: тоже временный отказ.
    config_missing, config_run = await _start_ready_order(db, "recheck-config")

    # Первые попытки идут, когда ожидать отчёта уже незачем (потолок вышел или
    # ревью terminal) — иначе они не дошли бы до своих отказов.
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 0)
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(None, transport)),
    ) as first:
        monkeypatch.setattr(config, "CURSOR_API_KEY", "")
        await sh.start_run(db, config_run)
        monkeypatch.setattr(config, "CURSOR_API_KEY", "cursor-key")
        await sh.start_run(db, provider_run)
        await sh.start_run(db, undeclared_run)
    assert first.await_count == 1, "до повтора провайдера звала только вторая попытка"
    refused_before = len(await _events(db, sh.EVENT_RUN_REFUSED))
    assert refused_before == 3, "три временных отказа — исходные условия повтора"

    # Ревью каждой сдачи вновь в ожидании: идёт новый заказ ревью, а потолок
    # ожидания далеко.
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 120)
    for task_id in (undeclared, provider, config_missing):
        await _dispatch(db, task_id, status="active", channel="cloud", model="grok-4.6")

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(created, None)),
    ) as retry:
        for _ in range(2):
            assert await sh.start_due_runs(db) == 0
    assert retry.await_count == 0, "провайдер позван, пока отчёт ещё ждут"
    for run_id in (undeclared_run["id"], provider_run["id"], config_run["id"]):
        row = dict(
            (await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run_id,)))[0]
        )
        assert row["status"] == RUN_OPEN
        assert row["agent_id"] == "", "слот захвачен, пока отчёт ещё ждут"
    assert len(await _events(db, sh.EVENT_RUN_REFUSED)) == refused_before, (
        "повтор при ожидании не смеет плодить отказы — он откладывается"
    )
    assert len(await _events(db, EVENT_DEFERRED)) == 3, "одна отсрочка на поколение"

    # Отчёты легли — те же заказы стартуют.
    for task_id in (undeclared, provider, config_missing):
        await _report(db, task_id)
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(created, None)),
    ) as after_report:
        assert await sh.start_due_runs(db) == 3
    assert after_report.await_count == 3


async def _ci_red(db: aiosqlite.Connection, task_id: int) -> None:
    await db.execute(
        "INSERT INTO ci_run_reports (task_id, head_sha, checks, validation_status) "
        "VALUES (?, ?, ?, ?)",
        (task_id, "a" * 40, json.dumps({"lint": "fail"}), ""),
    )
    await db.commit()


async def test_steward_ordered_on_terminal_review_or_wait_ceiling(
    db: aiosqlite.Connection, monkeypatch
):
    """#1600 AC-3: три терминальных исхода и потолок ожидания — прогон заказан.

    Красный CI без строки заказа, failed без переспроса, pending дольше
    STEWARD_REVIEW_WAIT_MAX. Причина названа в записи заказа; а ровно под
    потолком и при назначенном переспросе — по-прежнему отсрочка.
    """
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 120)
    _, red = await _due_task(db, "ceiling-red-ci")
    await _ci_red(db, red)
    _, failed = await _due_task(db, "ceiling-failed")
    await _dispatch(db, failed, status="failed", channel="local")
    _, ceiling = await _due_task(db, "ceiling-over")
    await _submitted_minutes_ago(db, ceiling, 121)
    _, under = await _due_task(db, "ceiling-under")
    await _submitted_minutes_ago(db, under, 119)
    _, retry = await _due_task(db, "ceiling-ask-again")
    await _dispatch(db, retry, status="failed", channel="cloud")

    assert await order_due_runs(db) == 3

    why: dict[int, str] = {}
    for event in await _events(db, EVENT_ORDERED):
        payload = json.loads(event["payload"])
        row = (
            await fetchall(
                db, "SELECT task_id FROM steward_runs WHERE id=?", (payload["run_id"],)
            )
        )[0]
        why[dict(row)["task_id"]] = payload["why"]
    assert set(why) == {red, failed, ceiling}
    assert "red_ci" in why[red]
    assert "failed_without_ask_again" in why[failed]
    assert "ждали отчёт 121 мин" in why[ceiling]
    deferred = {
        json.loads(e["payload"])["generation"]: e["task_id"]
        for e in await _events(db, EVENT_DEFERRED)
    }
    assert {e["task_id"] for e in await _events(db, EVENT_DEFERRED)} == {under, retry}
    assert deferred  # отсрочки названы, вечной нет: потолок выше ещё сработает


async def test_each_other_terminal_review_outcome_orders_a_run(
    db: aiosqlite.Connection, monkeypatch
):
    """#1600: остальные терминальные исходы — те же предикаты диспетчера.

    Код уже прочитан и находки перестали сходиться читает диспетчер ревью;
    здесь проверяется проводка читателя к ним, сами предикаты — в тестах
    диспетчера. Без ветки диспетчер ревью не закажет ничего — тоже конец.
    """
    from hub.services import review_dispatch as dispatcher

    _, read = await _due_task(db, "terminal-already-read")
    _, converge = await _due_task(db, "terminal-converging")
    project_id = await _project(db, "terminal-no-branch", steward=True)
    no_branch = await _submitted_task(db, project_id)

    def _only(target):
        # Читатель ожидания спрашивает read-only версии тех же предикатов.
        async def _read(_db, task):
            return task["id"] == target

        return _read

    def _converging(target):
        async def _stopped(_db, task):
            return task["id"] == target, None

        return _stopped

    monkeypatch.setattr(dispatcher, "_report_already_covers_this_sha", _only(read))
    monkeypatch.setattr(dispatcher, "_convergence_stopped", _converging(converge))

    assert await order_due_runs(db) == 3
    reasons = {}
    for event in await _events(db, EVENT_ORDERED):
        payload = json.loads(event["payload"])
        row = (
            await fetchall(
                db, "SELECT task_id FROM steward_runs WHERE id=?", (payload["run_id"],)
            )
        )[0]
        reasons[dict(row)["task_id"]] = payload["why"]
    assert "code_already_read" in reasons[read]
    assert "findings_not_converging" in reasons[converge]
    assert "not_dispatchable" in reasons[no_branch]


async def test_the_wait_ceiling_counts_from_the_first_deferral_without_a_submission_row(
    db: aiosqlite.Connection,
):
    """#1600: нет строки сдачи в учёте — потолок мерится от первой отсрочки.

    Без запасной отметки времени такая сдача ждала бы вечно: ``pending``
    без начала отсчёта потолка не достигает никогда.
    """
    _, task_id = await _due_task(db, "ceiling-no-submission-row")
    assert await order_due_runs(db) == 0
    await db.execute(
        "UPDATE events SET created_at=datetime('now', '-130 minutes') WHERE kind=?",
        (EVENT_DEFERRED,),
    )
    await db.commit()

    assert await order_due_runs(db) == 1

    payload = json.loads((await _events(db, EVENT_ORDERED))[0]["payload"])
    assert "ждали отчёт 130 мин" in payload["why"]


async def test_a_start_deferred_for_the_review_keeps_its_slot_alive(
    db: aiosqlite.Connection, monkeypatch
):
    """#1600 круг 2, п. 2: просроченный слот переживает НАСТОЯЩИЙ проход поллера.

    sweep_steward_runs сначала закрывает просроченные слоты и только потом
    стартует. Поллер, простоявший дольше окна ожидания старта (30 минут),
    закрыл бы заказ never_started раньше, чем страж старта его увидел, и
    после отчёта заказывать было бы уже нечего — поколение заперто. Тест
    идёт через sweep_steward_runs в его порядке вызовов, а не зовёт старт
    сам перед закрытием.
    """
    from unittest.mock import AsyncMock, patch

    from hub.services.steward_dispatch import sweep_steward_runs

    _start_env(monkeypatch)
    task_id, run = await _start_ready_order(db, "start-deferral-deadline")
    await db.execute("DELETE FROM review_dispatches WHERE task_id=?", (task_id,))
    await _dispatch(db, task_id, status="active", channel="cloud", model="grok-4.6")
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now', '-45 minutes') WHERE id=?",
        (run["id"],),
    )
    await db.commit()
    created = {"agent": {"id": "agent-1"}, "run": {"id": "run-1"}}

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(created, None)),
    ) as provider:
        await sweep_steward_runs(db)
        row = dict(
            (await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],)))[
                0
            ]
        )
        assert row["status"] == RUN_OPEN, row["closed_reason"]
        assert row["agent_id"] == ""
        assert provider.await_count == 0

        await _report(db, task_id)
        await sweep_steward_runs(db)

    assert provider.await_count == 1, "после отчёта тот же заказ обязан стартовать"
    row = dict(
        (await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],)))[0]
    )
    assert row["agent_id"] == "agent-1"


async def _overdue_waiting_slot(
    db: aiosqlite.Connection, monkeypatch, slug: str, *, deferred: bool = True
) -> tuple[int, dict]:
    """Слот, просроченный на 45 минут, пока ревью его сдачи ещё не кончено."""
    from hub.services.steward_dispatch import _defer

    _start_env(monkeypatch)
    task_id, run = await _start_ready_order(db, slug)
    await db.execute("DELETE FROM review_dispatches WHERE task_id=?", (task_id,))
    await _dispatch(db, task_id, status="active", channel="cloud", model="grok-4.6")
    if deferred:
        await _defer(db, task_id, 1, "ревью ещё не кончено")
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now', '-45 minutes') WHERE id=?",
        (run["id"],),
    )
    await db.commit()
    return task_id, run


async def _run_row(db: aiosqlite.Connection, run_id: int) -> dict:
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run_id,))
    return dict(rows[0])


_CREATED_AGENT = {"agent": {"id": "agent-1"}, "run": {"id": "run-1"}}


async def test_an_overdue_slot_whose_report_arrived_first_still_starts(
    db: aiosqlite.Connection, monkeypatch
):
    """#1600 круг 3, п. A1: отчёт пришёл ДО первого восстановленного прохода.

    Поллер простоял дольше окна старта, отчёт лёг, пока он стоял. Проверка
    видит отчёт, ждать нечего — и прежний код закрывал слот never_started, а
    UNIQUE запирал поколение: старт не случался никогда. Переход «ждал
    ревью -> готов» даёт слоту новое окно, и тот же проход его запускает.
    """
    from unittest.mock import AsyncMock, patch

    from hub.services.steward_dispatch import sweep_steward_runs

    task_id, run = await _overdue_waiting_slot(db, monkeypatch, "report-first")
    await _report(db, task_id)

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED_AGENT, None)),
    ) as provider:
        await sweep_steward_runs(db)

    assert provider.await_count == 1
    row = await _run_row(db, run["id"])
    assert row["status"] == RUN_OPEN, row["closed_reason"]
    assert row["agent_id"] == "agent-1"
    renewed = await _events(db, "steward_run_window_renewed")
    assert len(renewed) == 1
    assert "отчёт" in json.loads(renewed[0]["payload"])["because"]


async def test_an_overdue_slot_at_the_ceiling_gets_a_start_attempt(
    db: aiosqlite.Connection, monkeypatch
):
    """#1600 круг 3, п. A2: потолок ожидания разрешает ПОПЫТКУ старта.

    Прежний тест закреплял never_started на достигнутом потолке — а это
    запирало поколение ровно тогда, когда прогон наконец разрешён. Слот
    получает новое окно с названной причиной и стартует; закрыться
    never_started он может только если не стартовал и в новом окне.
    """
    from unittest.mock import AsyncMock, patch

    from hub.services.steward_dispatch import sweep_steward_runs

    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 0)
    task_id, run = await _overdue_waiting_slot(db, monkeypatch, "ceiling-window")

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(None, None)),
    ) as provider:
        await sweep_steward_runs(db)
        assert provider.await_count == 1, "потолок вышел — попытка старта разрешена"
        row = await _run_row(db, run["id"])
        assert row["status"] == RUN_OPEN, "слот закрыт на достигнутом потолке"
        renewed = await _events(db, "steward_run_window_renewed")
        assert len(renewed) == 1
        assert "ждали отчёт" in json.loads(renewed[0]["payload"])["because"]

        # Новое окно кончилось, а старт так и не случился: теперь — never_started,
        # и второго продления нет.
        await db.execute(
            "UPDATE steward_runs SET deadline_at=datetime('now', '-1 minutes') "
            "WHERE id=?",
            (run["id"],),
        )
        await db.commit()
        await close_finished_runs(db)

    row = await _run_row(db, run["id"])
    assert row["status"] == RUN_NEVER_STARTED
    assert len(await _events(db, "steward_run_window_renewed")) == 1


async def test_an_overdue_slot_that_never_waited_for_the_review_is_still_closed(
    db: aiosqlite.Connection, monkeypatch
):
    """#1600 круг 3: новое окно — только слоту, который ревью действительно ждал."""
    task_id, run = await _overdue_waiting_slot(
        db, monkeypatch, "never-waited", deferred=False
    )
    await _report(db, task_id)

    await close_finished_runs(db)

    assert (await _run_row(db, run["id"]))["status"] == RUN_NEVER_STARTED
    assert await _events(db, "steward_run_window_renewed") == []


async def test_a_cloud_review_whose_ask_again_is_spent_is_terminal(
    db: aiosqlite.Connection,
):
    """#1600: переспрос #1242 исчерпан — ждать нечего; назначен — ещё ждём.

    Решение «переспрос ещё будет» берётся из того же правила, что у самого
    переспроса (ask_again_exhausted): потолок выбран, либо прошлая попытка не
    оставила заказа. Облачный упавший заказ с неизрасходованным переспросом —
    ожидание (проверяется в AC-3), здесь — две формы исчерпания.
    """
    from hub.services.review_dispatch import (
        ASK_AGAIN_EXHAUSTED_MARK,
        ASK_AGAIN_MARK,
        REVIEW_ASK_AGAIN_MAX,
    )

    _, spent = await _due_task(db, "ask-again-spent")
    first = await _dispatch(db, spent, status="failed", channel="cloud")
    for _ in range(REVIEW_ASK_AGAIN_MAX):
        await db.execute(
            "INSERT INTO review_dispatches (task_id, submission_generation, "
            "agent_id, model, status, channel, replaces_dispatch_id) "
            "VALUES (?, 1, 'rev-agent', 'gpt-5.2', 'failed', 'cloud', ?)",
            (spent, first),
        )
        await repo.add_task_update(
            db, spent, "hub", "alert", ASK_AGAIN_MARK.format(generation=1) + " повтор"
        )
    # Вторая форма: попытка записана, заказа за ней нет (слепой исход #1199),
    # и следующий проход назвал переспрос исчерпанным. Пока не назвал —
    # попытка может ещё готовиться, и это ожидание (отдельный тест).
    _, blind = await _due_task(db, "ask-again-blind")
    await _dispatch(db, blind, status="failed", channel="cloud")
    await repo.add_task_update(
        db, blind, "hub", "alert", ASK_AGAIN_MARK.format(generation=1) + " повтор"
    )
    await repo.add_task_update(
        db, blind, "hub", "alert", ASK_AGAIN_EXHAUSTED_MARK.format(generation=1)
    )
    await db.commit()

    assert await order_due_runs(db) == 2

    for task_id in (spent, blind):
        assert await open_run(db, task_id, 1) is not None
    assert await _events(db, EVENT_DEFERRED) == []


async def test_a_review_the_project_does_not_ask_for_is_not_waited_for(
    db: aiosqlite.Connection,
):
    """#1600: «ревью не положено» решает и при живой строке заказа.

    Политика могла смениться, пока заказ шёл: ждать отчёта, который проект
    больше не просит, — значит держать суждение ни за что.
    """
    project_id = await _project_with_policy(
        db, "not-asked-live-row", {"verdict": "human", "steward_shadow": True}
    )
    task_id = await _submitted_task(db, project_id)
    await _on_a_branch(db, task_id)
    await _dispatch(db, task_id, status="active")

    assert await order_due_runs(db) == 1
    assert await _events(db, EVENT_DEFERRED) == []


_SNAPSHOT_TABLES = (
    "events",
    "task_updates",
    "machine_reviews",
    "review_dispatches",
    "steward_runs",
    "tasks",
    "submissions",
    "ci_run_reports",
)


async def _snapshot(db: aiosqlite.Connection) -> dict[str, list[dict]]:
    """Содержимое строк, а не только их число: UPDATE существующей строки тоже запись."""
    out = {}
    for table in _SNAPSHOT_TABLES:
        rows = await fetchall(db, f"SELECT * FROM {table} ORDER BY id")  # nosec B608
        out[table] = [dict(r) for r in rows]
    return out


async def _carriable_source(
    db: aiosqlite.Connection, task_id: int, monkeypatch, *, same_edit: bool = True
) -> None:
    """Прошлые три сдачи по две открытые находки; текущая — только слияние базы.

    Диспетчер перенёс бы отчёт последней из них на эту пересдачу (#1361).
    """
    from hub.services import orchestration

    async def _kept(_db, _task, _pinned, _tip):
        return same_edit, "правка та же"

    monkeypatch.setattr(orchestration, "base_merge_kept_the_verdict", _kept)
    await repo.update_task(db, task_id, submission_generation=4)
    for generation in (1, 2, 3):
        await db.execute(
            "INSERT INTO submissions (task_id, generation, sha, submitted_at) "
            "VALUES (?, ?, ?, datetime('now', '-300 minutes'))",
            (task_id, generation, str(generation) * 40),
        )
        findings = [
            {"finding_uid": f"uid-{generation}-{n}", "title": f"находка {n}"}
            for n in (1, 2)
        ]
        await db.execute(
            "INSERT INTO machine_reviews (task_id, submission_generation, model, "
            "submitted_by, raw_count, findings_confirmed, incomplete) "
            "VALUES (?, ?, 'gpt-5.2', 'rev-agent', 2, ?, 0)",
            (task_id, generation, json.dumps(findings)),
        )
    await db.commit()


async def test_the_wait_reader_does_not_write(db: aiosqlite.Connection, monkeypatch):
    """#1600 круг 2, п. 3: review_wait_view только читает.

    Диспетчер ревью пишет события и алерты (отчёт CI не пришёл, красный CI,
    несходимость находок) и переносит отчёты. Читатель, зовущий те же
    предикаты, не вправе делать это раньше диспетчера: перенос из читателя
    рождал две копии одного отчёта в поколении. Круг 3: сравнивается
    СОДЕРЖИМОЕ строк всех затронутых таблиц, чтобы поймать и UPDATE, и
    состояние на переносимом отчёте.
    """
    from hub.services import review_dispatch as dispatcher
    from hub.services.review_evidence import review_wait_view

    async def _never(*_a, **_k):
        raise AssertionError("читатель позвал записывающий путь диспетчера")

    monkeypatch.setattr(dispatcher, "_carry_the_report_over", _never)
    monkeypatch.setattr(dispatcher, "_this_code_was_already_read", _never)
    monkeypatch.setattr(dispatcher, "findings_stopped_converging", _never)
    monkeypatch.setattr(dispatcher.review_ci_gate, "review_may_be_bought", _never)

    _, no_ci = await _due_task(db, "reader-no-ci")  # отчёта CI нет
    _, red = await _due_task(db, "reader-red")
    await _ci_red(db, red)
    _, converge = await _due_task(db, "reader-converge")
    _, carry = await _due_task(db, "reader-carry")
    await _carriable_source(db, carry, monkeypatch)
    monkeypatch.setattr(dispatcher, "_fires_at_last", lambda *_a, **_k: True)

    before = await _snapshot(db)
    states = {}
    for name, task_id in (
        ("no_ci", no_ci),
        ("red", red),
        ("converge", converge),
        ("carry", carry),
    ):
        task = dict(await repo.get_task(db, task_id))
        states[name] = await review_wait_view(db, task)
    assert await _snapshot(db) == before, "читатель ожидания изменил базу"
    assert states["red"].reason == "red_ci"
    assert states["converge"].reason == "findings_not_converging"
    assert states["carry"].pending, "переносимый отчёт — не отказ, а скорый отчёт"


async def test_a_carriable_report_beats_the_convergence_stop(
    db: aiosqlite.Connection, monkeypatch
):
    """#1600 круг 3, п. B: перенос отчёта идёт РАНЬШЕ проверки несходимости.

    Диспетчер сначала переносит отчёт прошлой сдачи (правка та же, слита база)
    и лишь потом спрашивает, сходятся ли находки. Читатель, смотревший их в
    обратном порядке, объявлял terminal и заказывал судью без отчёта, который
    диспетчер вот-вот положил бы.
    """
    from hub.services import review_dispatch as dispatcher
    from hub.services.review_evidence import review_wait_view

    _, task_id = await _due_task(db, "carry-and-converge")
    await _carriable_source(db, task_id, monkeypatch, same_edit=False)
    task = dict(await repo.get_task(db, task_id))
    # Без переноса те же три поколения по две находки — настоящая несходимость.
    assert (await review_wait_view(db, task)).reason == "findings_not_converging"

    await _carriable_source_flip(monkeypatch, True)
    assert (await review_wait_view(db, task)).pending
    assert await order_due_runs(db) == 0
    assert len(await _events(db, EVENT_DEFERRED)) == 1

    # Диспетчер переносит — и у поколения появляется отчёт, судья заказывается.
    assert await dispatcher._carry_the_report_over(db, task) is True
    assert (await review_wait_view(db, task)).reason == "report_ready"
    assert await order_due_runs(db) == 1


async def _carriable_source_flip(monkeypatch, same_edit: bool) -> None:
    from hub.services import orchestration

    async def _kept(_db, _task, _pinned, _tip):
        return same_edit, "правка та же"

    monkeypatch.setattr(orchestration, "base_merge_kept_the_verdict", _kept)


async def test_an_unreadable_deferral_stamp_counts_as_the_ceiling(
    db: aiosqlite.Connection,
):
    """#1600 круг 3, п. C: нечитаемая метка отсрочки — «неизвестно», не «сейчас».

    Нет строки сдачи в учёте, метка первой отсрочки нечитаема. Отсчёт «от
    сейчас» начинался бы заново на каждом тике — вечная отсрочка. Потолок
    считается достигнутым, причина названа.
    """
    _, task_id = await _due_task(db, "unreadable-deferral")
    assert await order_due_runs(db) == 0
    await db.execute(
        "UPDATE events SET created_at='не дата' WHERE kind=?", (EVENT_DEFERRED,)
    )
    await db.commit()

    assert await order_due_runs(db) == 1

    payload = json.loads((await _events(db, EVENT_ORDERED))[0]["payload"])
    assert "метка отсрочки нечитаема" in payload["why"]


async def test_a_cloud_review_being_asked_again_is_pending_not_terminal(
    db: aiosqlite.Connection, monkeypatch
):
    """#1600 круг 2, п. 1: переспрос, который готовится, ещё не отказан.

    _ask_again коммитит метку попытки и только потом готовит заказ; в этом
    окне попыток записано больше, чем заказов. Читатель обязан видеть
    «готовится» (ожидание, ограниченное потолком), а не «переспроса не будет».
    Окончательный отказ называет следующий проход меткой исчерпания — после
    неё заказ идёт.
    """
    from hub.services import review_dispatch as dispatcher

    _, task_id = await _due_task(db, "ask-again-preparing")
    await _dispatch(db, task_id, status="failed", channel="cloud")
    seen: dict[str, object] = {}

    async def _preparing(conn, tid, **_k):
        # Здесь метка попытки уже закоммичена, заказа ещё нет.
        seen["ordered"] = await order_due_runs(conn)
        seen["deferred"] = len(await _events(conn, EVENT_DEFERRED))
        return True

    monkeypatch.setattr(dispatcher, "maybe_dispatch_review", _preparing)
    failed = dict(
        (
            await fetchall(
                db, "SELECT * FROM review_dispatches WHERE task_id=?", (task_id,)
            )
        )[0]
    )

    await dispatcher._ask_again(db, failed)

    assert seen == {"ordered": 0, "deferred": 1}, "судья стартовал посреди переспроса"
    assert await open_run(db, task_id, 1) is None

    # Попытка не дала заказа: следующий проход называет переспрос исчерпанным.
    await dispatcher._ask_again(db, failed)
    assert await order_due_runs(db) == 1
    payload = json.loads((await _events(db, EVENT_ORDERED))[0]["payload"])
    assert "failed_without_ask_again" in payload["why"]


async def test_a_failed_cloud_review_with_budget_left_but_a_red_ci_is_terminal(
    db: aiosqlite.Connection,
):
    """#1600 круг 2, п. 4: назначенный переспрос не скрывает отказ диспетчера.

    Переспрос идёт тем же путём, что первый заказ, и красный CI его остановит:
    ждать нечего, судья заказывается с названной причиной.
    """
    _, task_id = await _due_task(db, "failed-budget-red-ci")
    await _dispatch(db, task_id, status="failed", channel="cloud")
    await _ci_red(db, task_id)

    assert await order_due_runs(db) == 1

    payload = json.loads((await _events(db, EVENT_ORDERED))[0]["payload"])
    assert "red_ci" in payload["why"]
    assert await _events(db, EVENT_DEFERRED) == []


async def test_an_unreadable_submission_time_does_not_restart_the_wait(
    db: aiosqlite.Connection,
):
    """#1600 круг 2, п. 5: пустая или нечитаемая отметка сдачи — не «сейчас».

    Иначе начало ожидания сдвигалось бы на каждый тик, и потолок был
    недостижим — вечная отсрочка. Работает запасной отсчёт от первой отсрочки.
    """
    for number, stamp in enumerate(("", "не дата")):
        _, task_id = await _due_task(db, f"unreadable-stamp-{number}")
        await db.execute(
            "INSERT INTO submissions (task_id, generation, sha, submitted_at) "
            "VALUES (?, 1, ?, ?)",
            (task_id, "a" * 40, stamp),
        )
        await db.commit()
    assert await order_due_runs(db) == 0
    await db.execute(
        "UPDATE events SET created_at=datetime('now', '-130 minutes') WHERE kind=?",
        (EVENT_DEFERRED,),
    )
    await db.commit()

    assert await order_due_runs(db) == 2

    for event in await _events(db, EVENT_ORDERED):
        assert "ждали отчёт" in json.loads(event["payload"])["why"]


async def test_the_last_ask_again_being_prepared_is_pending_too(
    db: aiosqlite.Connection,
):
    """#1600 круг 2, п. 1: и ПОСЛЕДНИЙ переспрос в подготовке — ещё ожидание.

    Метка попытки лежит на потолке (две из двух), а заказ второй попытки ещё
    не создан: попыток записано больше, чем заказов. Потолок попыток не
    означает, что переспрос окончательно отказан.
    """
    from hub.services.review_dispatch import ASK_AGAIN_MARK

    _, task_id = await _due_task(db, "last-ask-again-preparing")
    first = await _dispatch(db, task_id, status="failed", channel="cloud")
    await db.execute(
        "INSERT INTO review_dispatches (task_id, submission_generation, "
        "agent_id, model, status, channel, replaces_dispatch_id) "
        "VALUES (?, 1, 'rev-agent', 'gpt-5.2', 'failed', 'cloud', ?)",
        (task_id, first),
    )
    for _ in range(2):
        await repo.add_task_update(
            db, task_id, "hub", "alert", ASK_AGAIN_MARK.format(generation=1) + " ещё"
        )
    await db.commit()

    assert await order_due_runs(db) == 0
    assert len(await _events(db, EVENT_DEFERRED)) == 1
