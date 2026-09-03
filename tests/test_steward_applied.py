"""Применение суждения: стюард впервые двигает чужую задачу (#1149).

До этого он советует, и ошибка стоит строки в карточке. Здесь он меняет
исход, и проверяется не «умеет ли применить», а три правила, каждое из
которых существует против своей ошибки: клиентский путь без облачного
исполнителя, общий бюджет циклов и старшинство человека.
"""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub.models import ReviewVerdict, TaskReviewVerdict
from hub.services.steward_applied import (
    APPLIED,
    ESCALATED_TO_HUMAN,
    RETURNED,
    apply_judgement,
)
from tests.test_steward_shadow import _project, _task


async def _judge(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    verdict: str,
    generation: int = 1,
) -> None:
    """Суждение приходит контрактом #1022 — тем же путём, что у живого прогона."""
    from hub.config import TokenIdentity
    from hub.models import StewardJudgementSubmit
    from hub.services.steward_judgement import record_steward_judgement

    await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=generation,
            kind="verdict",
            verdict=verdict,
            confidence="high",
            escalate_reason="precondition_failed" if verdict == "escalate" else None,
            model="gpt-5.3-codex",
        ),
        TokenIdentity("steward-bot", "steward", principal_id=42),
    )


async def _client_task(db: aiosqlite.Connection, project_id: int, **fields) -> int:
    """Задача клиентского пути: в review и БЕЗ review_job_id."""
    task_id = await _task(db, project_id)
    await repo.update_task(db, task_id, status="review", review_job_id="", **fields)
    await db.commit()
    return task_id


async def test_client_path_changes_requested(db: aiosqlite.Connection, monkeypatch):
    """AC-1: возврат в ту же ветку, цикл +1, и НИЧЕГО больше.

    У клиентской задачи нет облачного исполнителя, которому можно поручить
    правку. Серверный маршрут породил бы либо висящий job, либо вторую
    задачу на ту же работу — поэтому проверяется не только куда задача
    ушла, но и что рядом ничего не появилось.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "applied-client")
    task_id = await _client_task(db, project_id)
    before = dict(await repo.get_task(db, task_id))
    await _judge(db, task_id, verdict="changes_requested")

    outcome, detail = await apply_judgement(db, task_id, 1)

    assert outcome == RETURNED, detail
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", "работа возвращается автору, а не висит"
    assert task["branch"] == before["branch"], "та же ветка — правка идёт туда же"
    assert task["review_cycle"] == (before["review_cycle"] or 0) + 1
    assert not (task.get("review_job_id") or ""), (
        "review_job_id на клиентском пути не появляется: поручать правку некому"
    )
    from hub.db import fetchall

    children = await fetchall(db, "SELECT id FROM tasks WHERE parent_id=?", (task_id,))
    assert list(children) == [], "параллельных fix-задач не создаётся"


async def test_budget_exhausted_client_path_needs_decision(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-2: исчерпанный бюджет ведёт к человеку существующим переходом.

    Арбитра на клиентском пути нет, и отдельной квоты для стюарда тоже:
    счётчик, который никто не сверяет с общим, разъезжается. Проверяется
    по КРАЮ — на единицу меньше потолка работа ещё возвращается автору, на
    потолке уходит к человеку.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "applied-budget")

    almost = await _client_task(db, project_id, review_cycle=2)
    await _judge(db, almost, verdict="changes_requested")
    outcome, _ = await apply_judgement(db, almost, 1)
    assert outcome == RETURNED, "на единицу меньше потолка бюджет ещё есть"

    spent = await _client_task(db, project_id, review_cycle=3)
    await _judge(db, spent, verdict="changes_requested")

    outcome, detail = await apply_judgement(db, spent, 1)

    assert outcome == ESCALATED_TO_HUMAN, detail
    task = dict(await repo.get_task(db, spent))
    assert task["status"] == "needs_decision"
    assert task["review_cycle"] == 3, "исчерпанный бюджет не тратится дальше"
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, spent)]
    assert any("Бюджет циклов ревью исчерпан" in c for c in updates), (
        "молчаливая эскалация неотличима от зависшей задачи"
    )


async def test_the_budget_question_has_one_owner(db: aiosqlite.Connection, monkeypatch):
    """Потолок читается из общей функции, а не сравнивается на месте.

    #423 прямо запрещает любому потоку сравнивать review_cycle с
    MAX_REVIEW_CYCLES самостоятельно. Проверяется сдвигом потолка: при
    MAX=1 та же задача с одним циклом уже уходит к человеку, при MAX=9 —
    ещё возвращается автору. Своё сравнение на это не отреагировало бы.
    """
    project_id = await _project(db, "applied-owner")

    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 9)
    generous = await _client_task(db, project_id, review_cycle=1)
    await _judge(db, generous, verdict="changes_requested")
    assert (await apply_judgement(db, generous, 1))[0] == RETURNED

    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 1)
    strict = await _client_task(db, project_id, review_cycle=1)
    await _judge(db, strict, verdict="changes_requested")
    assert (await apply_judgement(db, strict, 1))[0] == ESCALATED_TO_HUMAN


async def test_human_verdict_wins_race(db: aiosqlite.Connection, monkeypatch):
    """AC-3: вердикт на эту генерацию уже стоит — суждение опоздало.

    Проверяется ОБОИМИ порядками прихода, потому что «человек старше» —
    правило о старшинстве, а не о скорости: и когда человек успел раньше,
    и когда стюард уже применил, второй записи не будет.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "applied-race")

    # Человек успел раньше.
    from hub.services.lifecycle import record_review_verdict

    first = await _client_task(db, project_id)
    await _judge(db, first, verdict="approve")
    await record_review_verdict(
        db,
        first,
        TaskReviewVerdict(agent="Denis", verdict=ReviewVerdict.approved),
    )
    with pytest.raises(HTTPException) as refused:
        await apply_judgement(db, first, 1)
    assert refused.value.status_code == 409
    assert "человек старше" in str(refused.value.detail)

    # Стюард уже применил: второе применение — тот же отказ, то же поле.
    second = await _client_task(db, project_id)
    await _judge(db, second, verdict="approve")
    assert (await apply_judgement(db, second, 1))[0] == APPLIED
    with pytest.raises(HTTPException) as twice:
        await apply_judgement(db, second, 1)
    assert twice.value.status_code == 409


async def test_an_approve_is_recorded_as_a_verdict_by_the_steward(
    db: aiosqlite.Connection, monkeypatch
):
    """Применение видно там же, где человеческое решение, и с актором steward.

    Событие steward_applied из #1023 пишется в момент ЗАПИСИ суждения и
    означает «вердикт не эскалация», а не «применено» — следом применения
    служит обычная запись вердикта. Она же кладёт суждение в те же
    метрики, где считаются человеческие решения.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "applied-trail")
    task_id = await _client_task(db, project_id)
    await _judge(db, task_id, verdict="approve")

    assert (await apply_judgement(db, task_id, 1))[0] == APPLIED

    task = dict(await repo.get_task(db, task_id))
    assert task["review_verdict"] == "approved"
    assert task["review_verdict_generation"] == 1
    events = await repo.list_events(
        db, since=0, kinds=["review_verdict_recorded"], limit=20
    )
    mine = [dict(e) for e in events if dict(e)["task_id"] == task_id]
    assert mine and mine[-1]["actor"] == "steward", (
        "актор обязан называть, кто решил: иначе суждение стюарда неотличимо "
        "от человеческого в тех же метриках"
    )


async def test_an_escalation_is_not_applied(db: aiosqlite.Connection, monkeypatch):
    """Эскалация — отказ судить, применять в ней нечего.

    Отдельный тест, потому что молчаливый пропуск незнакомого вердикта
    выглядел бы как применение: задача осталась бы в review без записи, и
    отличить это от «применили и ничего не изменилось» было бы нечем.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "applied-escalate")
    task_id = await _client_task(db, project_id)
    await _judge(db, task_id, verdict="escalate")

    with pytest.raises(HTTPException) as refused:
        await apply_judgement(db, task_id, 1)

    assert refused.value.status_code == 409
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "review", "задача не двинулась"
    assert not (task.get("review_verdict") or ""), "и вердикта не появилось"


async def test_a_judgement_about_an_older_submission_is_refused(
    db: aiosqlite.Connection, monkeypatch
):
    """Суждение о прошлой сдаче не становится вердиктом нынешней.

    Найдено кросс-модельным ревью на первой сдаче #1149 и воспроизведено
    здесь тем же сценарием: человек одобрил живую сдачу, а суждение о
    ПРЕДЫДУЩЕЙ приходит следом. Без пина оно записывалось вердиктом на
    текущую генерацию — потому что запись вердикта привязывает его к
    текущей сдаче, а не к той, о которой судили.

    Проверка «вердикт на эту генерацию уже стоит» этот случай пропускает
    по устройству: она сравнивает поле с ЗАПРОШЕННОЙ генерацией, и чужая
    проходит мимо неё именно потому, что чужая. Поэтому тест смотрит на
    вердикт задачи ПОСЛЕ отказа — что он остался человеческим.
    """
    project_id = await _project(db, "applied-stale-gen")
    task_id = await _client_task(db, project_id, submission_generation=2)
    await _judge(db, task_id, verdict="changes_requested", generation=1)
    await repo.update_task(
        db,
        task_id,
        review_verdict="approved",
        review_verdict_generation=2,
    )
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await apply_judgement(db, task_id, 1)
    assert exc.value.status_code == 409
    assert "живая сдача" in str(exc.value.detail)

    row = dict(await repo.get_task(db, task_id))
    assert row["review_verdict"] == "approved"
    assert row["review_verdict_generation"] == 2


async def test_the_exhausted_budget_escalates_once(
    db: aiosqlite.Connection, monkeypatch
):
    """Второе применение на исчерпанном бюджете отказывает, а не алертит снова.

    Найдено ревью как unresolved и подтверждено: бюджетный путь вердикта
    не пишет, поэтому замок «вердикт уже стоит» его не держит. Раньше
    повтор молча проваливал переход и всё равно клал в карточку второй
    алерт — два одинаковых события там, где произошло одно.

    Заодно проверяется само событие: канонический путь эскалации пишет
    needs_decision с причиной review_cycle_limit, и стюард обязан писать
    ТО ЖЕ, иначе счётчик исчерпанных бюджетов разойдётся по тому, кто
    вернул работу.
    """
    project_id = await _project(db, "applied-budget-once")
    task_id = await _client_task(
        db,
        project_id,
        submission_generation=1,
        review_cycle=config.MAX_REVIEW_CYCLES,
    )
    await _judge(db, task_id, verdict="changes_requested", generation=1)

    outcome, _ = await apply_judgement(db, task_id, 1)
    assert outcome == ESCALATED_TO_HUMAN

    events = await repo.fetchall(
        db,
        "SELECT kind, payload FROM events WHERE task_id = ? AND kind = ?",
        (task_id, ESCALATED_TO_HUMAN),
    )
    assert len(events) == 1
    assert "review_cycle_limit" in str(dict(events[0])["payload"])

    alerts_before = await _budget_alerts(db, task_id)
    with pytest.raises(HTTPException) as exc:
        await apply_judgement(db, task_id, 1)
    assert exc.value.status_code == 409
    assert await _budget_alerts(db, task_id) == alerts_before


async def _budget_alerts(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await repo.fetchall(
        db,
        "SELECT content FROM task_updates WHERE task_id = ? AND kind = 'alert'",
        (task_id,),
    )
    return sum(1 for r in rows if "Бюджет циклов ревью исчерпан" in dict(r)["content"])
