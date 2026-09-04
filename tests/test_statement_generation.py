"""Поколение постановки: единица счёта для суждений о драфте (#1156).

Задача существует против одного конкретного ограничения: суждения и
прогоны стюарда стоят на ``(task_id, generation, kind)``, а у драфта
поколение сдачи навсегда ноль. Поэтому тесты ниже проверяют не «работает
ли счётчик», а три разных способа сделать его бесполезным — растущий
всегда, не растущий никогда, и растущий так, что at-most-once из #1022
перестаёт держать.
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import repository as repo
from hub.models import AcceptanceCriterion, TaskRefine
from hub.services.refinement import refine_task, upsert_acceptance_criterion
from hub.services.statement_generation import STATEMENT_FIELDS


async def _draft(db: aiosqlite.Connection, title: str = "драфт") -> int:
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
    await db.commit()
    return task_id


async def _generation(db: aiosqlite.Connection, task_id: int) -> int:
    row = await repo.get_task(db, task_id)
    return int(dict(row)["statement_generation"])


async def _submission_generation(db: aiosqlite.Connection, task_id: int) -> int:
    row = await repo.get_task(db, task_id)
    return int(dict(row)["submission_generation"] or 0)


async def test_a_real_edit_moves_the_counter(db: aiosqlite.Connection):
    """AC-1: правка постановки двигает ЕЁ поколение и не трогает поколение сдачи.

    Вторая половина утверждения не менее важна первой. Два поколения лежат
    рядом на одной строке, и слияние их в одно означало бы, что правка
    текста обесценивает вердикт по коду, — поэтому проверяется, что
    поколение сдачи осталось на месте.
    """
    task_id = await _draft(db)
    before = await _generation(db, task_id)
    before_submission = await _submission_generation(db, task_id)

    await refine_task(db, task_id, TaskRefine(problem_statement="что именно сломано"))

    assert await _generation(db, task_id) == before + 1
    assert await _submission_generation(db, task_id) == before_submission

    # Вторая правка — второе поколение: счётчик считает ревизии, а не
    # факт «постановку когда-то трогали».
    await refine_task(db, task_id, TaskRefine(business_value="зачем это надо"))
    assert await _generation(db, task_id) == before + 2


async def test_an_edit_to_the_criteria_counts_too(db: aiosqlite.Connection):
    """Критерии приёмки — часть постановки, хотя лежат в своей таблице.

    Правка AC меняет постановку ровно так же, как правка scope: именно по
    AC стюард и судит проверяемость. Счётчик, слепой к их таблице, пропустил
    бы самый частый вид правки по замечаниям (#1161).
    """
    task_id = await _draft(db)
    await refine_task(db, task_id, TaskRefine(user_story="как владелец, я хочу"))
    before = await _generation(db, task_id)

    await upsert_acceptance_criterion(
        db,
        task_id,
        AcceptanceCriterion(
            id="AC-1",
            given="драфт",
            when="его читают",
            then="видно, что проверять",
            verifiable_by="test",
            test_ref="tests/test_x.py::test_y",
        ),
    )

    assert await _generation(db, task_id) == before + 1


async def test_a_rewrite_without_changes_does_not(db: aiosqlite.Connection):
    """AC-2: пересохранение теми же значениями поколение НЕ двигает.

    Здесь ломается наивная реализация «расти на каждом вызове»: она
    проходит AC-1 и падает только тут. Ставка на этом высокая — на признаке
    «постановку правили» стоит потолок против хождения драфта по кругу
    (#1161), и счётчик, растущий сам по себе, покупал бы стюарду новый
    прогон за каждый пересчёт готовности.
    """
    task_id = await _draft(db)
    await refine_task(db, task_id, TaskRefine(problem_statement="что именно сломано"))
    settled = await _generation(db, task_id)

    # Ровно те же значения, тем же путём, дважды.
    await refine_task(db, task_id, TaskRefine(problem_statement="что именно сломано"))
    await refine_task(db, task_id, TaskRefine(problem_statement="что именно сломано"))

    assert await _generation(db, task_id) == settled, (
        "пересохранение без изменений — не ревизия"
    )


async def test_lifecycle_movement_is_not_a_statement_edit(db: aiosqlite.Connection):
    """Движение по жизненному циклу постановкой не является.

    Статус, ветка, поколение сдачи и счёт готовности лежат на той же
    строке, что и текст задачи. Счётчик, считающий любое изменение строки,
    объявлял бы новую ревизию на каждом переходе — и стюард платил бы за
    чтение текста, который никто не трогал.
    """
    task_id = await _draft(db)
    await refine_task(db, task_id, TaskRefine(technical_hints="где смотреть"))
    settled = await _generation(db, task_id)

    await repo.update_task(
        db,
        task_id,
        status="open",
        branch="task-1/x",
        submission_generation=3,
        readiness_score=99,
    )
    await db.commit()
    # Постановку не трогали — значит и ревизии не было. Пересчёт готовности
    # здесь и есть та точка, где счётчик мог бы сработать ошибочно.
    await refine_task(db, task_id, TaskRefine(technical_hints="где смотреть"))

    assert await _generation(db, task_id) == settled


async def test_at_most_once_now_counts_revisions(db: aiosqlite.Connection):
    """AC-3: at-most-once из #1022 не ослаблен, а получил верную единицу счёта.

    Два суждения kind='dor' с ревизией между ними записываются оба —
    иначе цикл F6 обрывается на первом же возврате. Повтор на
    НЕИЗМЕНИВШЕЙСЯ ревизии по-прежнему отвергается индексом — иначе
    правило «нет новой информации — нет нового мнения» (#1150) потеряло бы
    опору на этом контуре.

    Суждения пишутся настоящим контрактным путём, а не вставкой в таблицу:
    подделка записи проверяла бы согласие теста с самим собой.
    """
    from fastapi import HTTPException

    from hub.config import TokenIdentity
    from hub.models import StewardJudgementSubmit
    from hub.services.steward_judgement import record_steward_judgement

    async def judge(generation: int) -> None:
        await record_steward_judgement(
            db,
            task_id,
            StewardJudgementSubmit(
                generation=generation,
                kind="dor",
                verdict="changes_requested",
                confidence="high",
                model="gpt-5.3-codex",
            ),
            TokenIdentity("steward-bot", "steward", principal_id=42),
        )

    task_id = await _draft(db)
    await refine_task(db, task_id, TaskRefine(problem_statement="первая редакция"))
    first = await _generation(db, task_id)
    await judge(first)

    # Повтор на той же ревизии — отказ: нового мнения не появилось.
    with pytest.raises(HTTPException) as exc:
        await judge(first)
    assert exc.value.status_code == 409

    # Автор поправил постановку — появилась новая ревизия и новое место.
    await refine_task(db, task_id, TaskRefine(problem_statement="вторая редакция"))
    second = await _generation(db, task_id)
    assert second == first + 1
    await judge(second)

    rows = await repo.fetchall(
        db,
        "SELECT generation FROM steward_judgements WHERE task_id = ? AND kind = ?",
        (task_id, "dor"),
    )
    assert sorted(int(dict(r)["generation"]) for r in rows) == [first, second]


def test_the_statement_fields_match_what_refine_can_write():
    """Набор полей отпечатка сверяется с refine ПЕРЕЧИСЛЕНИЕМ, а не примером.

    Поле, добавленное в TaskRefine и забытое в отпечатке, стало бы правкой
    постановки, которой счётчик не заметит: стюард получил бы старое
    суждение на новый текст и не смог бы записать новое. Такую ошибку
    примером не поймать — только сверкой множеств.
    """
    writable = set(TaskRefine.model_fields)
    # Не часть постановки, и каждое исключение названо своей причиной.
    not_the_statement = {
        # привязка к проекту — размещение задачи, а не её содержание
        "project",
        # флаг операции, а не поле: «сними причинную связь»
        "clear_caused_by",
        # отметка времени последней шлифовки; растёт от самой правки и
        # включение её в отпечаток сделало бы каждую правку двойной
        "prepared_at",
        # критерии приёмки лежат в своей таблице и входят в отпечаток
        # отдельным чтением, а не колонкой задачи
        "acceptance_criteria",
        # кто отвечает и кто принимает — назначение ответственных, а не
        # содержание постановки. Смена владельца не меняет ни одного слова,
        # которое читает стюард, и объявлять из-за неё новую ревизию значило
        # бы покупать прогон за перестановку людей
        "human_owner",
        "human_reviewer",
    }

    assert writable - not_the_statement == set(STATEMENT_FIELDS), (
        "refine умеет писать поле, которого нет в отпечатке постановки — "
        "правка по нему пройдёт мимо счётчика ревизий"
    )
